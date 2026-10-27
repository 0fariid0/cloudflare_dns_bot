"""Cloudflare API wrapper used by the Telegram bot.

Supports both authentication modes:
- Recommended API Token: Authorization: Bearer <token>
- Legacy Global API Key: X-Auth-Email + X-Auth-Key

The public function names are kept compatible with bot.py.
"""

from __future__ import annotations

import logging
import re
import time
import threading
import copy
from bot_runtime import api_error_scope
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import CLOUDFLARE_API_KEY, CLOUDFLARE_EMAIL

logger = logging.getLogger(__name__)

BASE_URL = "https://api.cloudflare.com/client/v4"
_DEFAULT_TIMEOUT: Tuple[int, int] = (5, 15)
_CACHE_TTL_SECONDS = 20

# Stores the last Cloudflare error message (used by the bot UI to show a helpful message)
WRITE_OBSERVER = None
_ZONES_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
_RECORDS_CACHE: Dict[str, Dict[str, Any]] = {}

# Cloudflare Global API Key is 37 hex characters.
_GLOBAL_KEY_RE = re.compile(r"^[a-f0-9]{37}$", re.IGNORECASE)

class BoundedRetry(Retry):
    def get_retry_after(self, response):
        value = super().get_retry_after(response)
        return min(value, 5) if value is not None else None


_LOCAL = threading.local()


def _session():
    # requests.Session is not shared between worker threads.
    if not hasattr(_LOCAL, 'session'):
        session = requests.Session()
        retry = BoundedRetry(total=1, connect=1, read=1, status=1,
            backoff_factor=0.35, status_forcelist=(429,500,502,503,504),
            allowed_methods=frozenset(['GET']), raise_on_status=False)
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4,pool_maxsize=4)
        session.mount('https://',adapter)
        _LOCAL.session=session
    return _LOCAL.session


class CloudflareAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: Optional[int] = None, errors: Optional[List[Dict[str, Any]]] = None):
        super().__init__(message)
        self.status_code = status_code
        self.errors = errors or []


def get_last_error() -> Optional[str]:
    """Return last Cloudflare API error message (if any)."""
    scope = api_error_scope.get()
    return scope.get("error") if scope is not None else getattr(_LOCAL,"error",None)


def _set_last_error(err: Optional[str]) -> None:
    scope = api_error_scope.get()
    if scope is not None:scope["error"]=err
    _LOCAL.error=err


def _cache_get(cache: Dict[str, Any], key: str = "data"):
    if cache.get(key) is None:
        return None
    if time.monotonic() - float(cache.get("ts", 0)) > _CACHE_TTL_SECONDS:
        return None
    return cache.get(key)


def _cache_set(cache: Dict[str, Any], data, key: str = "data") -> None:
    cache["ts"] = time.monotonic()
    cache[key] = data


def _invalidate_zones_cache() -> None:
    _ZONES_CACHE["ts"] = 0.0
    _ZONES_CACHE["data"] = None


def _invalidate_records_cache(zone_id: Optional[str] = None) -> None:
    if zone_id:
        _RECORDS_CACHE.pop(zone_id, None)
    else:
        _RECORDS_CACHE.clear()


def _auth_headers() -> Dict[str, str]:
    key = (CLOUDFLARE_API_KEY or "").strip()
    email = (CLOUDFLARE_EMAIL or "").strip()

    if not key:
        _set_last_error("CLOUDFLARE_API_KEY در config.py خالی است.")
        raise CloudflareAPIError("CLOUDFLARE_API_KEY در config.py خالی است.")

    # If the key looks like a Global API Key, require email and use X-Auth headers.
    if _GLOBAL_KEY_RE.match(key):
        if not email:
            msg = (
                "CLOUDFLARE_API_KEY شبیه Global API Key است، ولی CLOUDFLARE_EMAIL خالی است. "
                "اگر از API Token استفاده می‌کنید، یک API Token بسازید و همان را در CLOUDFLARE_API_KEY قرار دهید."
            )
            _set_last_error(msg)
            raise CloudflareAPIError(msg)
        return {
            "X-Auth-Email": email,
            "X-Auth-Key": key,
            "Content-Type": "application/json",
        }

    # Otherwise treat as API Token
    return {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


def _request(method: str, path: str, *, params: Optional[Dict[str, Any]] = None, json: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    url = f"{BASE_URL}{path}"
    headers = _auth_headers()

    try:
        resp = _session().request(method, url, headers=headers, params=params, json=json, timeout=_DEFAULT_TIMEOUT)
    except requests.RequestException as e:
        _set_last_error(f"خطا در ارتباط با Cloudflare: {e}")
        raise CloudflareAPIError(f"خطا در ارتباط با Cloudflare: {e}") from e

    # Cloudflare returns JSON for most errors; try to parse.
    try:
        data = resp.json()
    except ValueError:
        _set_last_error(f"پاسخ نامعتبر از Cloudflare (status={resp.status_code}).")
        raise CloudflareAPIError(
            f"پاسخ نامعتبر از Cloudflare (status={resp.status_code}).",
            status_code=resp.status_code,
        )

    if not isinstance(data, dict):
        _set_last_error("ساختار پاسخ Cloudflare نامعتبر است.")
        raise CloudflareAPIError(get_last_error(),status_code=resp.status_code)

    if resp.status_code >= 400 or not data.get("success", False):
        errors = data.get("errors") or []
        msg = errors[0].get("message") if errors and isinstance(errors[0], dict) else None
        msg = msg or f"Cloudflare API error (status={resp.status_code})."
        logger.error(
            "Cloudflare API error: method=%s path=%s status=%s errors=%s",
            method,
            path,
            resp.status_code,
            errors,
        )
        _set_last_error(msg)
        raise CloudflareAPIError(msg, status_code=resp.status_code, errors=errors)

    _set_last_error(None)
    if WRITE_OBSERVER and method in ('PATCH','PUT','DELETE'):
        match=re.fullmatch(r'/zones/([^/]+)/dns_records/([^/]+)',path)
        if match:
            try:
                if method=='DELETE':WRITE_OBSERVER(match[1],match[2],None)
                elif isinstance(data.get('result'),dict):WRITE_OBSERVER(match[1],match[2],data['result'])
                else:logger.warning('Successful DNS write did not include a record snapshot')
            except Exception:logger.exception('Cannot synchronize local clone tracking')
    return data


def _paginate(path: str, *, params: Optional[Dict[str, Any]] = None, per_page: int = 100) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    page = 1
    while True:
        merged_params = dict(params or {})
        merged_params.setdefault("per_page", per_page)
        merged_params["page"] = page

        data = _request("GET", path, params=merged_params)
        items = data.get("result") or []
        if not isinstance(items,list):raise CloudflareAPIError("فهرست Cloudflare نامعتبر است.")
        out.extend(items)

        info = data.get("result_info") or {}
        total_pages = info.get("total_pages")
        if total_pages is None:
            break
        if page >= int(total_pages):
            break
        page += 1

    return out


# ---------------------------------------------------------------------------
# Public API (kept compatible with the original project)
# ---------------------------------------------------------------------------

def get_zones() -> List[Dict[str, Any]]:
    """Return all zones accessible by the configured credentials."""
    cached = _cache_get(_ZONES_CACHE)
    if cached is not None:
        _set_last_error(None)
        return copy.deepcopy(cached)

    try:
        zones = _paginate("/zones", per_page=50)
        _cache_set(_ZONES_CACHE, list(zones))
        return zones
    except CloudflareAPIError:
        return []


def get_zones_strict():
    cached=_cache_get(_ZONES_CACHE)
    if cached is not None:
        _set_last_error(None)
        return copy.deepcopy(cached)
    zones=_paginate('/zones',per_page=50)
    _cache_set(_ZONES_CACHE,copy.deepcopy(zones))
    return zones


def get_zone_info(domain_name: str) -> Optional[Dict[str, Any]]:
    try:
        for zone in get_zones():
            if zone.get("name") == domain_name:
                return zone
        return None
    except Exception:
        return None


def get_zone_info_by_id(zone_id: str) -> Optional[Dict[str, Any]]:
    try:
        for zone in get_zones():
            if zone.get("id") == zone_id:
                return zone
        data = _request("GET", f"/zones/{zone_id}")
        return data.get("result")
    except CloudflareAPIError:
        return None


def delete_zone(zone_id: str) -> bool:
    try:
        _request("DELETE", f"/zones/{zone_id}")
        _invalidate_zones_cache()
        _invalidate_records_cache(zone_id)
        return True
    except CloudflareAPIError:
        return False


def add_domain_to_cloudflare(domain_name: str) -> bool:
    try:
        payload = {"name": domain_name, "jump_start": True}
        _request("POST", "/zones", json=payload)
        _invalidate_zones_cache()
        return True
    except CloudflareAPIError:
        return False


def get_dns_records(zone_id: str) -> List[Dict[str, Any]]:
    zone_id = str(zone_id)
    cached_bucket = _RECORDS_CACHE.get(zone_id)
    if cached_bucket:
        cached = _cache_get(cached_bucket)
        if cached is not None:
            _set_last_error(None)
            return copy.deepcopy(cached)

    try:
        records = _paginate(f"/zones/{zone_id}/dns_records")
        _RECORDS_CACHE[zone_id] = {"ts": time.monotonic(), "data": list(records)}
        return records
    except CloudflareAPIError:
        return []


def get_record_details(zone_id: str, record_id: str) -> Dict[str, Any]:
    try:
        data = _request("GET", f"/zones/{zone_id}/dns_records/{record_id}")
        return data.get("result") or {}
    except CloudflareAPIError:
        return {}


def delete_dns_record(zone_id: str, record_id: str) -> bool:
    try:
        _request("DELETE", f"/zones/{zone_id}/dns_records/{record_id}")
        _invalidate_records_cache(zone_id)
        return True
    except CloudflareAPIError:
        return False


def create_dns_record(zone_id: str, type_: str, name: str, content: str, ttl: int = 120, proxied: bool = False) -> bool:
    try:
        payload = {
            "type": type_,
            "name": name,
            "content": content,
            "ttl": 1 if proxied else ttl,
            "proxied": proxied,
        }
        _request("POST", f"/zones/{zone_id}/dns_records", json=payload)
        _invalidate_records_cache(zone_id)
        return True
    except CloudflareAPIError:
        return False


def update_dns_record(zone_id: str, record_id: str, name: str, type_: str, content: str, ttl: int = 120, proxied: bool = False) -> bool:
    """Keep original signature but preserve Cloudflare comments/tags/settings.

    Existing callers pass the unchanged name/type. Use PATCH for changed fields
    rather than full PUT, which would unintentionally replace record metadata.
    """
    try:
        old = get_record_details_strict(zone_id, record_id)
        if old.get("name") != name or old.get("type") != type_:
            # Legacy compatibility for name/type edits (not used by current UI).
            payload = {"type": type_, "name": name, "content": content,
                       "ttl": 1 if proxied else ttl, "proxied": proxied}
            for extra in ("comment", "tags", "settings"):
                if extra in old:
                    payload[extra] = old[extra]
            _request("PUT", f"/zones/{zone_id}/dns_records/{record_id}", json=payload)
        else:
            changes = {}
            for field, value in (("content", content), ("ttl", int(ttl)), ("proxied", bool(proxied))):
                if old.get(field) != value:
                    changes[field] = value
            if proxied:changes["ttl"]=1
            if changes:
                _request("PATCH", f"/zones/{zone_id}/dns_records/{record_id}", json=changes)
        _invalidate_records_cache(zone_id)
        return True
    except CloudflareAPIError:
        return False


def toggle_proxied_status(zone_id: str, record_id: str) -> bool:
    record = get_record_details(zone_id, record_id)
    if not record:
        return False
    if not record.get("proxiable", record.get("type") in ("A", "AAAA", "CNAME")):
        return False
    new_status = not record.get("proxied", False)
    return update_dns_record(
        zone_id,
        record_id,
        record.get("name", ""),
        record.get("type", ""),
        record.get("content", ""),
        1 if new_status else int(record.get("ttl", 120)),
        new_status,
    )


def patch_dns_fields(zone_id: str, record_id: str, fields: Dict[str, Any]) -> Dict[str, Any]:
    """Change only requested DNS fields, preserving Cloudflare record metadata.

    Unlike the legacy full PUT helper this is safe for comments, tags and other
    record properties not surfaced by the old Telegram UI. Raises on failures.
    """
    allowed = {"content", "ttl", "proxied", "comment"}
    if not fields or any(key not in allowed for key in fields):
        raise ValueError("Unsupported DNS field")
    data = _request("PATCH", f"/zones/{zone_id}/dns_records/{record_id}", json=fields)
    _invalidate_records_cache(str(zone_id))
    return data.get("result") or {}


def get_dns_records_fresh(zone_id: str) -> List[Dict[str, Any]]:
    """Fresh, complete listing. Errors propagate (never confuse outage with empty list)."""
    return _paginate(f"/zones/{zone_id}/dns_records")


def get_record_details_strict(zone_id: str, record_id: str) -> Dict[str, Any]:
    """Fresh record or raised API error; no cached/ambiguous empty result."""
    data = _request("GET", f"/zones/{zone_id}/dns_records/{record_id}")
    result = data.get("result")
    if not isinstance(result, dict) or not result:
        raise CloudflareAPIError("رکورد انتخابی پیدا نشد.")
    return result
