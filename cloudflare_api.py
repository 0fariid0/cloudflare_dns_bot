import requests
from config import CLOUDFLARE_API_KEY

# Optional: if you still use Global API Key auth, set CLOUDFLARE_EMAIL in config.py
try:
    from config import CLOUDFLARE_EMAIL  # type: ignore
except Exception:
    CLOUDFLARE_EMAIL = None  # noqa

BASE_URL = "https://api.cloudflare.com/client/v4"

def _headers_bearer():
    return {
        "Authorization": f"Bearer {CLOUDFLARE_API_KEY}",
        "Content-Type": "application/json",
    }

def _headers_global_key():
    if not CLOUDFLARE_EMAIL:
        return None
    return {
        "X-Auth-Email": CLOUDFLARE_EMAIL,
        "X-Auth-Key": CLOUDFLARE_API_KEY,
        "Content-Type": "application/json",
    }

def _request(method: str, path: str, *, json=None, params=None):
    """Cloudflare request helper.

    Tries API Token (Bearer) first (recommended). If that fails with auth error and
    CLOUDFLARE_EMAIL is configured, retries using Global API Key headers.
    """
    url = f"{BASE_URL}{path}"
    resp = requests.request(method, url, headers=_headers_bearer(), json=json, params=params, timeout=30)

    # If token auth fails and email is present, retry as Global API Key.
    if resp.status_code in (401, 403) and CLOUDFLARE_EMAIL:
        hk = _headers_global_key()
        if hk:
            resp2 = requests.request(method, url, headers=hk, json=json, params=params, timeout=30)
            return resp2
    return resp

def _ok_result(resp):
    try:
        data = resp.json()
    except Exception:
        return False, None, {"message": f"Non-JSON response (HTTP {resp.status_code})"}
    if resp.status_code != 200 or not data.get("success", False):
        return False, None, data.get("errors") or data
    return True, data.get("result"), None

def get_zones():
    resp = _request("GET", "/zones")
    ok, result, _err = _ok_result(resp)
    return result if ok and isinstance(result, list) else []

def get_zone_info(domain_name):
    zones = get_zones()
    for zone in zones:
        if zone.get("name") == domain_name:
            return zone
    return None

def get_zone_info_by_id(zone_id):
    resp = _request("GET", f"/zones/{zone_id}")
    ok, result, _err = _ok_result(resp)
    return result if ok else None

def delete_zone(zone_id):
    resp = _request("DELETE", f"/zones/{zone_id}")
    ok, _result, _err = _ok_result(resp)
    return ok

def add_domain_to_cloudflare(domain_name):
    data = {"name": domain_name, "jump_start": True}
    resp = _request("POST", "/zones", json=data)
    ok, _result, _err = _ok_result(resp)
    return ok

def get_dns_records(zone_id):
    resp = _request("GET", f"/zones/{zone_id}/dns_records")
    ok, result, _err = _ok_result(resp)
    return result if ok and isinstance(result, list) else []

def create_dns_record(zone_id, record_data):
    resp = _request("POST", f"/zones/{zone_id}/dns_records", json=record_data)
    ok, result, _err = _ok_result(resp)
    return result if ok else None

def update_dns_record(zone_id, record_id, record_data):
    resp = _request("PUT", f"/zones/{zone_id}/dns_records/{record_id}", json=record_data)
    ok, result, _err = _ok_result(resp)
    return result if ok else None

def delete_dns_record(zone_id, record_id):
    resp = _request("DELETE", f"/zones/{zone_id}/dns_records/{record_id}")
    ok, _result, _err = _ok_result(resp)
    return ok
