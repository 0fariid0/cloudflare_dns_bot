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
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import CLOUDFLARE_API_KEY, CLOUDFLARE_EMAIL

logger = logging.getLogger(__name__)

BASE_URL = "https://api.cloudflare.com/client/v4"
_DEFAULT_TIMEOUT: Tuple[int, int] = (8, 25)
_CACHE_TTL_SECONDS = 20

# Stores the last Cloudflare error message (used by the bot UI to show a helpful message)
_LAST_ERROR: Optional[str] = None
_ZONES_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}
_RECORDS_CACHE: Dict[str, Dict[str, Any]] = {}

# Cloudflare Global API Key is 37 hex characters.
_GLOBAL_KEY_RE = re.compile(r"^[a-f0-9]{37}$", re.IGNORECASE)

_SESSION = requests.Session()
_RETRY = Retry(
    total=2,
    connect=2,
    read=2,
    status=2,
    backoff_factor=0.35,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=frozenset(["GET", "POST", "PUT", "PATCH", "DELETE"]),
    raise_on_status=False,
)
_ADAPTER = HTTPAdapter(max_retries=_RETRY, pool_connections=10, pool_maxsize=20)
_SESSION.mount("https://", _ADAPTER)
_SESSION.mount("http://", _ADAPTER)


class CloudflareAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: Optional[int] = None, errors: Optional[List[Dict[str, Any]]] = None):
        super().__init__(message)
        self.status_code = status_code
        self.errors = errors or []


def get_last_error() -> Optional[str]:
    """Return last Cloudflare API error message (if any)."""
    return _LAST_ERROR


def _set_last_error(err: Optional[str]) -> None:
    global _LAST_ERROR
    _LAST_ERROR = err


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
        resp = _SESSION.request(method, url, headers=headers, params=params, json=json, timeout=_DEFAULT_TIMEOUT)
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
        return list(cached)

    try:
        zones = _paginate("/zones")
        _cache_set(_ZONES_CACHE, list(zones))
        return zones
    except CloudflareAPIError:
        return []


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
            return list(cached)

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
            "ttl": ttl,
            "proxied": proxied,
        }
        _request("POST", f"/zones/{zone_id}/dns_records", json=payload)
        _invalidate_records_cache(zone_id)
        return True
    except CloudflareAPIError:
        return False


def update_dns_record(zone_id: str, record_id: str, name: str, type_: str, content: str, ttl: int = 120, proxied: bool = False) -> bool:
    try:
        payload = {
            "type": type_,
            "name": name,
            "content": content,
            "ttl": ttl,
            "proxied": proxied,
        }
        _request("PUT", f"/zones/{zone_id}/dns_records/{record_id}", json=payload)
        _invalidate_records_cache(zone_id)
        return True
    except CloudflareAPIError:
        return False


def toggle_proxied_status(zone_id: str, record_id: str) -> bool:
    record = get_record_details(zone_id, record_id)
    if not record:
        return False
    new_status = not record.get("proxied", False)
    return update_dns_record(
        zone_id,
        record_id,
        record.get("name", ""),
        record.get("type", ""),
        record.get("content", ""),
        int(record.get("ttl", 120)),
        new_status,
    )
