import logging
import json
import re
import time
import asyncio
import copy
import os
import tempfile
import httpx
from collections import defaultdict
from enum import Enum, auto
from datetime import datetime, timedelta
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters, JobQueue)

# --- Configuration & Cloudflare API imports ---
#
# نکته مهم:
# قبلاً اگر هرکدام از ایمپورت‌ها خطا می‌داد (مثلاً نبودن پکیج requests)،
# پروژه می‌رفت روی حالت Mock و BOT_TOKEN هم تبدیل می‌شد به YOUR_BOT_TOKEN_HERE.
# نتیجه‌اش این بود که تلگرام توکن را رد می‌کرد و عیب‌یابی سخت می‌شد.
#
# اینجا عمداً Fail-Fast می‌کنیم تا خطای واقعی دقیقاً در لاگ مشخص باشد.
try:
    from config import BOT_TOKEN, ADMIN_ID
except Exception as e:
    raise RuntimeError(
        "config.py پیدا نشد یا نامعتبر است. فایل config.py.template را به config.py کپی کنید و مقادیر را کامل کنید."
    ) from e

# اعتبارسنجی مقادیر تنظیمات (کمک می‌کند خطاها زودتر و واضح‌تر دیده شوند)
try:
    ADMIN_ID = int(ADMIN_ID)
except Exception as e:
    raise RuntimeError("ADMIN_ID باید یک عدد (Telegram Numeric ID) باشد.") from e

if not isinstance(BOT_TOKEN, str) or not BOT_TOKEN.strip() or BOT_TOKEN.strip() == "YOUR_BOT_TOKEN_HERE":
    raise RuntimeError(
        "BOT_TOKEN تنظیم نشده یا نامعتبر است. لطفاً در فایل config.py توکن صحیح BotFather را قرار دهید."
    )

try:
    from cloudflare_api import *  # noqa: F401,F403
except Exception as e:
    raise RuntimeError(
        "ایمپورت cloudflare_api ناموفق بود. لطفاً مطمئن شوید پکیج‌ها نصب هستند: pip install -r requirements.txt (خصوصاً requests)."
    ) from e

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

USER_FILE = "users.json"
LOG_FILE = "bot_audit.log"
BLOCKED_USER_FILE = "blocked_users.json"
REQUEST_FILE = "access_requests.json"
IP_LIST_FILE = "smart_connect_ips.json"
SMART_SETTINGS_FILE = "smart_connect_settings.json"

CLEAN_IP_SOURCE = ["8.8.8.8", "8.8.4.4", "185.235.195.1", "185.235.195.2", "45.87.65.1", "45.87.65.2"]

_DATA_CACHE = {}
user_state = defaultdict(dict)

class State(Enum):
    NONE, ADDING_USER, EDITING_USER_PROFILE, ADDING_RECORD_NAME, ADDING_RECORD_CONTENT, EDITING_IP, EDITING_TTL, CLONING_NEW_IP, ADDING_RESERVE_IP = auto(), auto(), auto(), auto(), auto(), auto(), auto(), auto(), auto()

def _clone_data(data):
    return copy.deepcopy(data)

def load_data(filename, default_data):
    """Load JSON safely with a tiny mtime cache to reduce repeated disk I/O."""
    path = os.path.abspath(filename)
    try:
        stat = os.stat(path)
    except FileNotFoundError:
        return _clone_data(default_data)

    cached = _DATA_CACHE.get(path)
    if cached and cached.get("mtime_ns") == stat.st_mtime_ns and cached.get("size") == stat.st_size:
        return _clone_data(cached["data"])

    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except json.JSONDecodeError as e:
        logger.error("Invalid JSON in %s: %s", filename, e)
        return _clone_data(default_data)

    _DATA_CACHE[path] = {"mtime_ns": stat.st_mtime_ns, "size": stat.st_size, "data": _clone_data(data)}
    return _clone_data(data)

def save_data(filename, data):
    """Write JSON atomically so runtime files do not get corrupted on interruption."""
    path = os.path.abspath(filename)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)

    fd, tmp_path = tempfile.mkstemp(prefix=f".{os.path.basename(filename)}.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
            f.write("\n")
        os.replace(tmp_path, path)
        stat = os.stat(path)
        _DATA_CACHE[path] = {"mtime_ns": stat.st_mtime_ns, "size": stat.st_size, "data": _clone_data(data)}
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

def load_ip_lists():
    return load_data(IP_LIST_FILE, {"reserve": CLEAN_IP_SOURCE, "deprecated": []})

def save_ip_lists(ip_lists):
    save_data(IP_LIST_FILE, ip_lists)

def load_smart_settings():
    return load_data(SMART_SETTINGS_FILE, {"auto_check_records": []})

def save_smart_settings(settings):
    save_data(SMART_SETTINGS_FILE, settings)

def interval_to_text(seconds):
    if seconds == 1800: return "۳۰ دقیقه"
    if seconds == 3600: return "۱ ساعت"
    if seconds == 7200: return "۲ ساعت"
    if seconds == 21600: return "۶ ساعت"
    if seconds == 43200: return "۱۲ ساعت"
    if seconds == 86400: return "۱ روز"
    if seconds == 172800: return "۲ روز"
    return f"{seconds} ثانیه"

def smart_job_name(zone_id: str, record_id: str) -> str:
    return f"smart_check_{zone_id}_{record_id}"

def sync_smart_job(job_queue, zone_id: str, record_id: str, record_config):
    """Create/update/remove the runtime job immediately after smart settings change."""
    if not job_queue:
        return

    name = smart_job_name(zone_id, record_id)
    try:
        for job in job_queue.get_jobs_by_name(name):
            job.schedule_removal()
    except Exception as e:
        logger.warning("Could not remove old smart jobs for %s: %s", name, e)

    if not record_config:
        return

    interval = int(record_config.get("interval", 1800) or 1800)
    context_data = {"zone_id": zone_id, "record_id": record_id}
    job_queue.run_repeating(automated_check_job, interval=interval, first=10, name=name, data=context_data)
    logger.info("Scheduled smart job %s every %s seconds.", name, interval)

async def check_ip_ping(ip: str, location: str):
    params = {'host': ip, 'node': location, 'max_nodes': 10}
    headers = {'Accept': 'application/json'}
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get("https://check-host.net/check-ping", params=params, headers=headers, timeout=10)
            response.raise_for_status()
            initial_data = response.json()
            request_id = initial_data.get("request_id")
            nodes_info = initial_data.get("nodes")
            
            if not request_id or not nodes_info:
                return False, "پاسخ اولیه از API نامعتبر است یا اطلاعات نودها یافت نشد."

            await asyncio.sleep(10)
            
            result_url = f"https://check-host.net/check-result/{request_id}"
            result_response = await client.get(result_url, headers=headers, timeout=20)
            result_response.raise_for_status()
            results = result_response.json()
            
            report = []
            is_overall_successful = False
            active_nodes_count = 0
            successful_nodes_count = 0

            for node_key in nodes_info:
                node_country_code = nodes_info[node_key][0]
                node_city = nodes_info[node_key][2]
                
                if location.lower() != node_country_code.lower():
                    continue

                active_nodes_count += 1
                ping_results = results.get(node_key)
                
                if not ping_results or not isinstance(ping_results, list) or not ping_results[0] or not isinstance(ping_results[0], list):
                    report.append(f"❌ {node_city}: تست ناموفق (پاسخ نامعتبر)")
                    continue

                successful_pings_count = 0
                avg_ping_time = 0.0

                for single_ping in ping_results[0]:
                    if isinstance(single_ping, list) and len(single_ping) > 0 and single_ping[0] == "OK":
                        successful_pings_count += 1
                        avg_ping_time += single_ping[1]
                
                if successful_pings_count > 0:
                    successful_nodes_count += 1
                    avg_ping_ms = (avg_ping_time / successful_pings_count) * 1000
                    report.append(f"✅ {node_city}: پینگ موفق ({successful_pings_count} بار) | میانگین: {avg_ping_ms:.1f} ms")
                else:
                    first_failure_reason = "نامشخص"
                    if ping_results[0] and isinstance(ping_results[0][0], list) and len(ping_results[0][0]) > 0:
                        first_failure_reason = ping_results[0][0][0]
                    report.append(f"❌ {node_city}: پینگ ناموفق ({first_failure_reason})")

            if not report:
                report.append("🚫 هیچ نتیجه‌ای از نودهای مربوطه دریافت نشد.")
            
            if location.lower() == "ir":
                if successful_nodes_count == active_nodes_count and active_nodes_count > 0:
                    is_overall_successful = True
            else:
                if successful_nodes_count > 0:
                    is_overall_successful = True

            return is_overall_successful, "\n".join(report)

    except Exception as e:
        logger.error(f"Error in check_ip_ping for {ip} from {location}: {e}")
        return False, f"❌ خطا در ارتباط با API: {e}"

def log_action(user_id: int, action: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] User: {user_id} | Action: {action}\n"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f: f.write(log_entry)
    except Exception as e:
        logger.error(f"Failed to write to log file: {e}")

def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def normalize_username(username):
    if not username:
        return ""
    return str(username).strip().lstrip("@")

def normalize_user_record(user_id, record=None):
    user_id = int(user_id)
    record = record or {}

    if isinstance(record, list):
        access = record
        record = {}
    elif record == "all":
        access = "all"
        record = {}
    elif isinstance(record, dict):
        access = record.get("access", [] if user_id != ADMIN_ID else "all")
    else:
        access = []
        record = {}

    if user_id == ADMIN_ID:
        access = "all"
    elif access != "all":
        if not isinstance(access, list):
            access = []
        access = [str(zone_id) for zone_id in access if zone_id]

    return {
        "access": access,
        "first_name": str(record.get("first_name") or record.get("name") or "").strip(),
        "last_name": str(record.get("last_name") or "").strip(),
        "username": normalize_username(record.get("username")),
        "added_at": record.get("added_at") or now_text(),
        "updated_at": record.get("updated_at") or "",
    }

def merge_user_profile(record, profile=None):
    record = normalize_user_record(0, record) if not isinstance(record, dict) else dict(record)
    profile = profile or {}

    first_name = (profile.get("first_name") or profile.get("name") or "").strip() if isinstance(profile.get("first_name") or profile.get("name") or "", str) else ""
    last_name = (profile.get("last_name") or "").strip() if isinstance(profile.get("last_name") or "", str) else ""
    username = normalize_username(profile.get("username"))

    changed = False
    if first_name and record.get("first_name") != first_name:
        record["first_name"] = first_name
        changed = True
    if last_name and record.get("last_name") != last_name:
        record["last_name"] = last_name
        changed = True
    if username and record.get("username") != username:
        record["username"] = username
        changed = True
    if changed:
        record["updated_at"] = now_text()
    return record, changed

def profile_from_telegram_user(tg_user):
    return {
        "first_name": tg_user.first_name or "",
        "last_name": tg_user.last_name or "",
        "username": tg_user.username or "",
    }

def display_name_for_user(user_id, user_data):
    user_data = normalize_user_record(user_id, user_data)
    full_name = " ".join(part for part in [user_data.get("first_name"), user_data.get("last_name")] if part).strip()
    username = user_data.get("username")
    if full_name and username:
        return f"{full_name} (@{username})"
    if full_name:
        return full_name
    if username:
        return f"@{username}"
    return "نام ثبت نشده"

def short_button_name(user_id, user_data, index=None):
    prefix = f"{index}) " if index is not None else ""
    role = "👑" if int(user_id) == ADMIN_ID else "👤"
    name = display_name_for_user(user_id, user_data)
    if len(name) > 24:
        name = name[:21] + "..."
    return f"{prefix}{role} {name}"

def access_text(user_data):
    access = user_data.get("access", [])
    if access == "all":
        return "همه دامنه‌ها"
    if not access:
        return "بدون دامنه"
    return f"{len(access)} دامنه"

def is_user_profile_missing(user_id, user_data):
    user_data = normalize_user_record(user_id, user_data)
    return not any([user_data.get("first_name"), user_data.get("last_name"), user_data.get("username")])

def compact_user_button_label(user_id, user_data):
    user_id = int(user_id)
    user_data = normalize_user_record(user_id, user_data)
    role = "👑" if user_id == ADMIN_ID else "👤"
    name = display_name_for_user(user_id, user_data)
    if name == "نام ثبت نشده":
        name = "بدون نام"
    if len(name) > 20:
        name = name[:17] + "..."
    return f"{role} {name} | ID: {user_id} | {access_text(user_data)}"

def user_profile_lines(user_id, user_data):
    user_id = int(user_id)
    user_data = normalize_user_record(user_id, user_data)
    full_name = " ".join(part for part in [user_data.get("first_name"), user_data.get("last_name")] if part).strip()
    username = user_data.get("username")
    role = "مدیر اصلی" if user_id == ADMIN_ID else "کاربر مجاز"
    return [
        f"نقش: {role}",
        f"نام: {full_name or 'ثبت نشده'}",
        f"یوزرنیم: @{username}" if username else "یوزرنیم: ثبت نشده",
        f"ID: {user_id}",
        f"دسترسی: {access_text(user_data)}",
        f"افزوده شده: {user_data.get('added_at') or '-'}",
        f"آخرین بروزرسانی: {user_data.get('updated_at') or '-'}",
    ]

def zone_access_details(user_data, all_zones):
    access = user_data.get("access", [])
    if access == "all":
        return "همه دامنه‌ها"
    if not access:
        return "هیچ دامنه‌ای فعال نیست"
    zone_map = {zone.get("id"): zone.get("name", zone.get("id")) for zone in all_zones or []}
    names = [zone_map.get(zone_id, zone_id) for zone_id in access]
    if len(names) <= 8:
        return "، ".join(names)
    return "، ".join(names[:8]) + f" و {len(names) - 8} دامنه دیگر"

async def refresh_known_user_profiles(context: ContextTypes.DEFAULT_TYPE, users):
    """Try to fill missing display names from Telegram without blocking the bot too long."""
    missing_ids = [int(uid) for uid, data in users.items() if is_user_profile_missing(uid, data)]
    if not missing_ids:
        return users

    semaphore = asyncio.Semaphore(5)
    changed = False

    async def fetch_one(user_id: int):
        async with semaphore:
            try:
                chat = await context.bot.get_chat(user_id)
            except Exception as e:
                logger.debug("Could not fetch Telegram profile for %s: %s", user_id, e)
                return None
            return user_id, {
                "first_name": getattr(chat, "first_name", "") or getattr(chat, "title", "") or "",
                "last_name": getattr(chat, "last_name", "") or "",
                "username": getattr(chat, "username", "") or "",
            }

    results = await asyncio.gather(*(fetch_one(uid) for uid in missing_ids), return_exceptions=True)
    for result in results:
        if not result or isinstance(result, Exception):
            continue
        user_id, profile = result
        uid_str = str(user_id)
        if uid_str not in users:
            continue
        merged, did_change = merge_user_profile(users[uid_str], profile)
        if did_change:
            users[uid_str] = normalize_user_record(user_id, merged)
            changed = True

    if changed:
        save_users(users)
    return users

def parse_profile_edit_input(raw_text: str):
    raw_text = raw_text.strip()
    if raw_text in {"-", "clear", "پاک"}:
        return {"first_name": "", "last_name": "", "username": ""}

    parts = raw_text.split()
    profile = {"first_name": "", "last_name": "", "username": ""}
    name_parts = []
    for part in parts:
        if part.startswith("@") and not profile["username"]:
            profile["username"] = normalize_username(part)
        else:
            name_parts.append(part)
    profile["first_name"] = " ".join(name_parts).strip()
    if not profile["first_name"] and not profile["username"]:
        raise ValueError("empty profile")
    return profile

def set_user_profile(user_id: int, profile: dict):
    users = load_users()
    uid_str = str(int(user_id))
    if uid_str not in users:
        return False
    current = normalize_user_record(user_id, users[uid_str])
    current["first_name"] = str(profile.get("first_name") or "").strip()
    current["last_name"] = str(profile.get("last_name") or "").strip()
    current["username"] = normalize_username(profile.get("username"))
    current["updated_at"] = now_text()
    users[uid_str] = current
    save_users(users)
    return True

def set_user_access(user_id: int, access):
    user_id = int(user_id)
    users = load_users()
    uid_str = str(user_id)
    if uid_str not in users or user_id == ADMIN_ID:
        return False
    users[uid_str]["access"] = access
    users[uid_str]["updated_at"] = now_text()
    save_users(users)
    return True

def parse_user_add_input(raw_text: str):
    parts = raw_text.strip().split()
    if not parts or not parts[0].isdigit():
        raise ValueError("شناسه عددی نامعتبر است")

    user_id = int(parts[0])
    profile = {}
    name_parts = []
    for part in parts[1:]:
        if part.startswith("@") and not profile.get("username"):
            profile["username"] = part.lstrip("@")
        else:
            name_parts.append(part)

    if name_parts:
        profile["first_name"] = " ".join(name_parts)
    return user_id, profile

def update_known_user_profile(tg_user):
    users = load_users()
    user_id_str = str(tg_user.id)
    if user_id_str not in users:
        return
    merged, changed = merge_user_profile(users[user_id_str], profile_from_telegram_user(tg_user))
    if changed:
        users[user_id_str] = normalize_user_record(tg_user.id, merged)
        save_users(users)

def load_users():
    data = load_data(USER_FILE, {"users": {}})
    changed = False

    if not isinstance(data, dict):
        data = {"users": {}}
        changed = True

    if "authorized_ids" in data:
        migrated_users = {}
        for uid in data.get("authorized_ids", []):
            try:
                uid_int = int(uid)
            except (TypeError, ValueError):
                continue
            if uid_int != ADMIN_ID:
                migrated_users[str(uid_int)] = {"access": []}
        migrated_users[str(ADMIN_ID)] = {"access": "all"}
        data = {"users": migrated_users}
        changed = True

    raw_users = data.setdefault("users", {})
    normalized_users = {}
    for uid_str, record in raw_users.items():
        try:
            uid = int(uid_str)
        except (TypeError, ValueError):
            changed = True
            continue
        normalized = normalize_user_record(uid, record)
        normalized_users[str(uid)] = normalized
        if normalized != record:
            changed = True

    admin_id_str = str(ADMIN_ID)
    if admin_id_str not in normalized_users:
        normalized_users[admin_id_str] = normalize_user_record(ADMIN_ID, {"access": "all", "first_name": "Admin"})
        changed = True

    if changed:
        save_data(USER_FILE, {"users": normalized_users})

    return normalized_users

def save_users(users_dict):
    normalized = {}
    for uid_str, record in users_dict.items():
        try:
            uid = int(uid_str)
        except (TypeError, ValueError):
            continue
        normalized[str(uid)] = normalize_user_record(uid, record)
    save_data(USER_FILE, {"users": normalized})

def is_user_authorized(user_id):
    users = load_users()
    return str(user_id) in users

def get_user_accessible_zones(user_id):
    users = load_users()
    user_id_str = str(user_id)
    user_data = users.get(user_id_str)
    if not user_data: return []
    all_zones = get_zones()
    if user_data.get("access") == "all": return all_zones
    accessible_zone_ids = set(user_data.get("access", []))
    return [zone for zone in all_zones if zone["id"] in accessible_zone_ids]

def add_user(user_id, profile=None):
    users = load_users()
    user_id = int(user_id)
    user_id_str = str(user_id)
    is_new = user_id_str not in users

    if is_new:
        users[user_id_str] = normalize_user_record(user_id, {"access": []})

    if profile:
        users[user_id_str], _ = merge_user_profile(users[user_id_str], profile)

    users[user_id_str] = normalize_user_record(user_id, users[user_id_str])
    save_users(users)
    unblock_user(user_id)
    return is_new

def remove_user(user_id):
    if user_id == ADMIN_ID: return False
    users = load_users()
    user_id_str = str(user_id)
    if user_id_str in users:
        del users[user_id_str]
        save_users(users)
        return True
    return False

def load_blocked_users():
    data = load_data(BLOCKED_USER_FILE, {"blocked_ids": []})
    blocked = data.get("blocked_ids", []) if isinstance(data, dict) else []
    normalized = []
    for uid in blocked:
        try:
            normalized.append(int(uid))
        except (TypeError, ValueError):
            continue
    return sorted(set(normalized))

def save_blocked_users(blocked_list):
    normalized = []
    for uid in blocked_list:
        try:
            normalized.append(int(uid))
        except (TypeError, ValueError):
            continue
    save_data(BLOCKED_USER_FILE, {"blocked_ids": sorted(set(normalized))})

def is_user_blocked(user_id):
    return int(user_id) in load_blocked_users()

def block_user(user_id):
    user_id = int(user_id)
    if user_id == ADMIN_ID: return False
    blocked = load_blocked_users()
    if user_id not in blocked:
        blocked.append(user_id)
        save_blocked_users(blocked)
        remove_user(user_id)
        return True
    return False

def unblock_user(user_id):
    user_id = int(user_id)
    blocked = load_blocked_users()
    if user_id in blocked:
        blocked.remove(user_id)
        save_blocked_users(blocked)
        return True
    return False

def load_requests():
    data = load_data(REQUEST_FILE, {"requests": []})
    requests = data.get("requests", []) if isinstance(data, dict) else []
    cleaned = []
    seen = set()
    for req in requests:
        if not isinstance(req, dict):
            continue
        try:
            req_id = int(req.get("id"))
        except (TypeError, ValueError):
            continue
        if req_id in seen:
            continue
        seen.add(req_id)
        cleaned.append({
            "id": req_id,
            "first_name": str(req.get("first_name") or "").strip(),
            "last_name": str(req.get("last_name") or "").strip(),
            "username": normalize_username(req.get("username")),
            "requested_at": req.get("requested_at") or now_text(),
        })
    return cleaned

def save_requests(request_list):
    save_data(REQUEST_FILE, {"requests": request_list})

def add_request(user: dict):
    requests = load_requests()
    user_id = int(user["id"])
    user_ids = [int(r['id']) for r in requests]
    if user_id not in user_ids and not is_user_authorized(user_id):
        requests.append({
            "id": user_id,
            "first_name": str(user.get("first_name") or "").strip(),
            "last_name": str(user.get("last_name") or "").strip(),
            "username": normalize_username(user.get("username")),
            "requested_at": now_text(),
        })
        save_requests(requests)
        return True
    return False

def get_request_profile(user_id: int):
    user_id = int(user_id)
    for req in load_requests():
        if int(req.get("id")) == user_id:
            return req
    return {}

def remove_request(user_id: int):
    user_id = int(user_id)
    requests = load_requests()
    original_len = len(requests)
    requests = [r for r in requests if int(r['id']) != user_id]
    if len(requests) < original_len:
        save_requests(requests)
        return True
    return False

def reset_user_state(uid, keep_zone=False):
    current_state = user_state.get(uid, {})
    if keep_zone:
        zone_id = current_state.get("zone_id")
        zone_name = current_state.get("zone_name")
        record_id = current_state.get("record_id")
        user_state[uid] = {"zone_id": zone_id, "zone_name": zone_name, "record_id": record_id}
    else:
        user_state.pop(uid, None)

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    reset_user_state(user_id)
    try:
        zones = get_user_accessible_zones(user_id)
    except Exception as e:
        logger.error(f"Could not fetch zones for user {user_id}: {e}")
        await update.effective_message.reply_text("❌ خطا در ارتباط با Cloudflare.")
        return
    keyboard = []
    if not zones:
        # اگر لیست دامنه‌ها خالی است، ممکن است واقعاً دامنه‌ای نداشته باشید یا
        # ممکن است مشکل دسترسی/توکن Cloudflare باشد.
        cf_err = None
        try:
            cf_err = get_last_error()
        except Exception:
            cf_err = None
        if cf_err:
            welcome_text = (
                "❌ خطا در دریافت دامنه‌ها از Cloudflare:\n\n"
                f"{cf_err}\n\n"
                "✅ اگر از API Token استفاده می‌کنید، مطمئن شوید دسترسی‌های زیر را داده‌اید:\n"
                "- Zone → Zone → Read\n"
                "- Zone → DNS → Edit"
            )
        else:
            welcome_text = "شما به هیچ دامنه‌ای دسترسی ندارید."
    else:
        welcome_text = "👋 به ربات مدیریت DNS خوش آمدید!\n\n🌐 برای مدیریت رکوردها، دامنه خود را انتخاب کنید:"
        for zone in zones:
            status_icon = "✅" if zone["status"] == "active" else "⏳"
            keyboard.append([InlineKeyboardButton(f"{zone['name']} {status_icon}", callback_data=f"zone_{zone['id']}")])
    action_buttons = [InlineKeyboardButton("🔄 رفرش", callback_data="refresh_domains")]
    if user_id == ADMIN_ID:
        action_buttons.append(InlineKeyboardButton("🗑️ حذف دامنه", callback_data="delete_domain_menu"))
        action_buttons.append(InlineKeyboardButton("👥 مدیریت کاربران", callback_data="manage_users"))
    action_buttons.extend([
        InlineKeyboardButton("📜 نمایش لاگ‌ها", callback_data="show_logs"),
        InlineKeyboardButton("ℹ️ راهنما", callback_data="show_help")
    ])
    for i in range(0, len(action_buttons), 2):
        keyboard.append(action_buttons[i:i + 2])
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        await update.effective_message.edit_text(welcome_text, reply_markup=reply_markup)
    else:
        await update.effective_message.reply_text(welcome_text, reply_markup=reply_markup)

async def manage_users_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("👤 کاربران مجاز", callback_data="manage_whitelist")],
        [InlineKeyboardButton("🚫 کاربران مسدود", callback_data="manage_blacklist")],
        [InlineKeyboardButton("📨 درخواست‌های در انتظار", callback_data="manage_requests")],
        [InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="back_to_main")]
    ]
    await update.effective_message.edit_text("لطفا بخش مورد نظر برای مدیریت کاربران را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))

async def manage_whitelist_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = load_users()
    users = await refresh_known_user_profiles(context, users)
    ordered_users = sorted(
        users.items(),
        key=lambda item: (0 if int(item[0]) == ADMIN_ID else 1, display_name_for_user(item[0], item[1]).lower(), int(item[0]))
    )

    lines = [
        "👥 لیست کاربران مجاز",
        "",
        f"تعداد کل: {len(ordered_users)} نفر",
        "برای مدیریت، روی خود کاربر بزنید؛ حذف و تغییر دسترسی داخل صفحه همان کاربر است.",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    keyboard = []
    for index, (uid_str, u_data) in enumerate(ordered_users, start=1):
        uid = int(uid_str)
        name = display_name_for_user(uid, u_data)
        role = "مدیر" if uid == ADMIN_ID else "کاربر"
        username = normalize_user_record(uid, u_data).get("username")
        username_text = f"@{username}" if username else "بدون یوزرنیم"
        lines.append(f"{index}) {role}: {name} | ID: {uid} | {access_text(u_data)} | {username_text}")
        keyboard.append([InlineKeyboardButton(compact_user_button_label(uid, u_data), callback_data=f"user_card_{uid}")])

    keyboard.append([InlineKeyboardButton("➕ افزودن کاربر جدید", callback_data="add_user_prompt")])
    keyboard.append([InlineKeyboardButton("🔙 بازگشت", callback_data="manage_users")])

    text = "\n".join(lines).strip()
    if len(text) > 3900:
        text = text[:3850] + "\n\n… لیست طولانی است؛ برای مدیریت هر کاربر از دکمه‌های زیر استفاده کنید."

    await update.effective_message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard) )

async def show_user_card_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, target_user_id=None):
    query = update.callback_query
    if target_user_id is None:
        target_user_id = int(query.data.split("_")[2])

    users = load_users()
    if str(target_user_id) in users and is_user_profile_missing(target_user_id, users[str(target_user_id)]):
        users = await refresh_known_user_profiles(context, users)

    user_data = users.get(str(target_user_id))
    if not user_data:
        keyboard = [[InlineKeyboardButton("🔙 بازگشت به لیست کاربران", callback_data="manage_whitelist")]]
        text = "❌ این کاربر در لیست مجاز پیدا نشد."
        if query:
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        else:
            await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
        return

    all_zones = []
    cf_error = None
    try:
        all_zones = get_zones()
    except Exception as e:
        logger.warning("Could not load zones for user card: %s", e)
        try:
            cf_error = get_last_error()
        except Exception:
            cf_error = str(e)

    details = "\n".join(user_profile_lines(target_user_id, user_data))
    text = (
        "👤 تنظیمات کاربر\n\n"
        f"{details}\n\n"
        "🌐 دامنه‌های قابل دسترسی:\n"
        f"{zone_access_details(user_data, all_zones)}"
    )
    if cf_error:
        text += f"\n\n⚠️ خطا در دریافت لیست دامنه‌ها از Cloudflare:\n{cf_error}"

    keyboard = []
    if int(target_user_id) == ADMIN_ID:
        keyboard.append([InlineKeyboardButton("🔑 مشاهده دسترسی‌ها", callback_data=f"manage_access_{target_user_id}")])
    else:
        keyboard.extend([
            [InlineKeyboardButton("🔑 تغییر دسترسی دامنه‌ها", callback_data=f"manage_access_{target_user_id}")],
            [
                InlineKeyboardButton("✅ دسترسی به همه دامنه‌ها", callback_data=f"set_all_access_{target_user_id}"),
                InlineKeyboardButton("🧹 حذف همه دسترسی‌ها", callback_data=f"clear_access_{target_user_id}"),
            ],
            [InlineKeyboardButton("✏️ ویرایش نام نمایشی", callback_data=f"edit_user_profile_{target_user_id}")],
            [
                InlineKeyboardButton("🗑 حذف از مجازها", callback_data=f"confirm_delete_user_{target_user_id}"),
                InlineKeyboardButton("🚫 حذف و مسدودسازی", callback_data=f"confirm_block_user_{target_user_id}"),
            ],
        ])
    keyboard.append([InlineKeyboardButton("🔙 بازگشت به لیست کاربران", callback_data="manage_whitelist")])

    if query:
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard) )
    else:
        await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard) )

async def confirm_user_action_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, action: str, target_user_id: int):
    users = load_users()
    user_data = users.get(str(target_user_id))
    if not user_data:
        await update.effective_message.edit_text(
            "❌ این کاربر پیدا نشد.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="manage_whitelist")]]),
        )
        return

    if int(target_user_id) == ADMIN_ID:
        await update.callback_query.answer("مدیر اصلی قابل حذف یا مسدودسازی نیست.", show_alert=True)
        await show_user_card_menu(update, context, target_user_id)
        return

    title = "حذف کاربر" if action == "delete" else "حذف و مسدودسازی کاربر"
    explain = "کاربر فقط از لیست مجاز حذف می‌شود." if action == "delete" else "کاربر از لیست مجاز حذف و وارد لیست مسدود می‌شود."
    yes_callback = f"delete_user_{target_user_id}" if action == "delete" else f"block_user_{target_user_id}"
    text = (
        f"⚠️ {title}\n\n"
        f"کاربر: {display_name_for_user(target_user_id, user_data)}\n"
        f"ID: {target_user_id}\n\n"
        f"{explain}\n"
        "آیا مطمئن هستید؟"
    )
    keyboard = [
        [InlineKeyboardButton("✅ بله، انجام بده", callback_data=yes_callback)],
        [InlineKeyboardButton("❌ لغو", callback_data=f"user_card_{target_user_id}")],
    ]
    await update.effective_message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard) )

async def manage_user_access_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    target_user_id = int(query.data.split("_")[2])
    users = load_users()
    user_data = users.get(str(target_user_id))
    if not user_data:
        await query.message.edit_text(
            "❌ این کاربر در لیست مجاز پیدا نشد.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="manage_whitelist")]]),
        )
        return

    try:
        all_zones = get_zones()
    except Exception as e:
        logger.error("Could not fetch zones for access menu: %s", e)
        cf_err = None
        try:
            cf_err = get_last_error()
        except Exception:
            cf_err = str(e)
        await query.message.edit_text(
            f"❌ خطا در دریافت دامنه‌ها از Cloudflare\n\n{cf_err}",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data=f"user_card_{target_user_id}")]]),
        )
        return

    current_access = user_data.get("access", [])
    user_access = {zone["id"] for zone in all_zones} if current_access == "all" else set(current_access or [])
    text = (
        "🔑 تغییر دسترسی دامنه‌ها\n\n"
        f"کاربر: {display_name_for_user(target_user_id, user_data)}\n"
        f"ID: {target_user_id}\n"
        f"وضعیت فعلی: {access_text(user_data)}\n\n"
        "روی هر دامنه بزنید تا فعال/غیرفعال شود."
    )

    keyboard = []
    if int(target_user_id) != ADMIN_ID:
        keyboard.append([
            InlineKeyboardButton("✅ همه دامنه‌ها", callback_data=f"set_all_access_{target_user_id}"),
            InlineKeyboardButton("🧹 بدون دسترسی", callback_data=f"clear_access_{target_user_id}"),
        ])

    for zone in all_zones:
        has_access = zone['id'] in user_access
        status_icon = "✅" if has_access else "❌"
        callback_data = f"toggle_access_{target_user_id}_{zone['id']}"
        keyboard.append([InlineKeyboardButton(f"{status_icon} {zone['name']}", callback_data=callback_data)])

    keyboard.append([InlineKeyboardButton("🔙 بازگشت به تنظیمات کاربر", callback_data=f"user_card_{target_user_id}")])
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard) )

async def manage_blacklist_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    blocked_users = load_blocked_users()
    text = "🚫 کاربران مسدود\n\n"
    keyboard = []
    if not blocked_users:
        text += "لیست کاربران مسدود خالی است."
    else:
        text += f"تعداد: {len(blocked_users)} نفر\n\n"
        for index, uid in enumerate(blocked_users, start=1):
            text += f"{index}) ID: {uid}\n"
            keyboard.append([InlineKeyboardButton(f"{index}) ID: {uid}", callback_data="noop"), InlineKeyboardButton("✅ رفع انسداد", callback_data=f"unblock_user_{uid}")])
    keyboard.append([InlineKeyboardButton("🔙 بازگشت", callback_data="manage_users")])
    await update.effective_message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def manage_requests_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    requests = load_requests()
    text = "📨 درخواست‌های در انتظار\n\n"
    keyboard = []
    if not requests:
        text += "هیچ درخواست جدیدی وجود ندارد."
    else:
        text += f"تعداد: {len(requests)} درخواست\n━━━━━━━━━━━━━━━━━━━━\n"
        for index, req in enumerate(requests, start=1):
            name = display_name_for_user(req["id"], req)
            text += f"{index}) {name}\nID: {req['id']}\nزمان درخواست: {req.get('requested_at', '-')}\n\n"
            buttons = [
                InlineKeyboardButton("✅ تایید", callback_data=f"access_approve_{req['id']}"),
                InlineKeyboardButton("❌ رد", callback_data=f"access_reject_{req['id']}"),
                InlineKeyboardButton("🚫 مسدود", callback_data=f"access_block_{req['id']}")
            ]
            keyboard.append([InlineKeyboardButton(f"{index}) {name[:28]}", callback_data="noop")])
            keyboard.append(buttons)
    keyboard.append([InlineKeyboardButton("🔄 رفرش", callback_data="manage_requests")])
    keyboard.append([InlineKeyboardButton("🔙 بازگشت", callback_data="manage_users")])
    await update.effective_message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_delete_domain_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    zones = get_zones()
    if not zones:
        cf_err = None
        try:
            cf_err = get_last_error()
        except Exception:
            cf_err = None
        if cf_err:
            await update.effective_message.edit_text(
                f"❌ خطا در دریافت دامنه‌ها از Cloudflare\n\n{cf_err}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_main")]]),
            )
        else:
            await update.effective_message.edit_text(
                "هیچ دامنه‌ای برای حذف یافت نشد.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_main")]]),
            )
        return
    keyboard = [[InlineKeyboardButton(f"🗑️ {z['name']}", callback_data=f"confirm_delete_zone_{z['id']}")] for z in zones]
    keyboard.append([InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="back_to_main")])
    text = "لطفا دامنه‌ای که قصد حذف آن را دارید انتخاب کنید.\n\n**توجه:** این عمل غیرقابل بازگشت است!"
    await update.effective_message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard) )

async def show_records_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid, state = update.effective_user.id, user_state.get(update.effective_user.id, {})
    zone_id, zone_name = state.get("zone_id"), state.get("zone_name", "")
    if not zone_id:
        await update.effective_message.edit_text("خطا: دامنه انتخاب نشده است.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("بازگشت", callback_data="back_to_main")]]))
        return
    records = get_dns_records(zone_id)
    if not records:
        cf_err = None
        try:
            cf_err = get_last_error()
        except Exception:
            cf_err = None
        if cf_err:
            err_text = f"❌ خطا در دریافت رکوردها از Cloudflare\n\n{cf_err}"
            err_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت به دامنه‌ها", callback_data="back_to_main")]])
            if update.callback_query:
                await update.effective_message.edit_text(err_text, reply_markup=err_kb)
            else:
                await context.bot.send_message(chat_id=uid, text=err_text, reply_markup=err_kb)
            return
    text = f"📋 رکوردهای DNS دامنه: `{zone_name}`\n\n"
    keyboard = []
    supported_types = ["A", "AAAA", "CNAME"]
    for rec in records:
        if rec["type"] in supported_types:
            name = rec["name"].replace(f".{zone_name}", "").replace(zone_name, "@")
            keyboard.append([InlineKeyboardButton(f"{rec['type']} | {name}", callback_data="noop"), InlineKeyboardButton(f"{rec['content']} | ⚙️", callback_data=f"record_settings_{rec['id']}")])
    keyboard.extend([
        [InlineKeyboardButton("➕ افزودن رکورد", callback_data="add_record")],
        [InlineKeyboardButton("🔄 رفرش", callback_data="refresh_records")],
        [InlineKeyboardButton("🔙 بازگشت به دامنه‌ها", callback_data="back_to_main")]
    ])
    if update.callback_query:
        await update.effective_message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await context.bot.send_message(chat_id=uid, text=text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def show_record_settings(message, uid, zone_id, record_id):
    record = get_record_details(zone_id, record_id)
    if not record:
        cf_err = None
        try:
            cf_err = get_last_error()
        except Exception:
            cf_err = None
        if cf_err:
            await message.edit_text(
                f"❌ خطا در دریافت اطلاعات رکورد از Cloudflare\n\n{cf_err}",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("بازگشت", callback_data="back_to_records")]]),
            )
        else:
            await message.edit_text(
                "❌ رکورد یافت نشد.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("بازگشت", callback_data="back_to_records")]]),
            )
        return
    user_state[uid]["record_id"] = record_id
    proxied_status = '✅ فعال' if record.get('proxied') else '❌ غیرفعال'
    text = f"⚙️ تنظیمات رکورد: `{record['name']}`\n\n**Type:** `{record['type']}`\n**Content:** `{record['content']}`\n**TTL:** `{record['ttl']}`\n**Proxied:** {proxied_status}"
    keyboard = [[InlineKeyboardButton("🖊 تغییر IP/Content", callback_data=f"editip_{record_id}"), InlineKeyboardButton("🕒 تغییر TTL", callback_data=f"edittll_{record_id}")],
                 [InlineKeyboardButton("🔁 پروکسی", callback_data=f"toggle_proxy_{record_id}")]]
    action_row = []
    if record['type'] == 'A' and record.get('proxied') == False:
        action_row.append(InlineKeyboardButton("🤖 اتصال هوشمند", callback_data=f"smart_menu_{record_id}"))
    if record['type'] == 'A': action_row.append(InlineKeyboardButton("🐑 کلون", callback_data=f"clone_record_{record_id}"))
    action_row.append(InlineKeyboardButton("🗑️ حذف", callback_data=f"confirm_delete_record_{record_id}"))
    if action_row: keyboard.append(action_row)
    keyboard.append([InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_records")])
    await message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def show_smart_connection_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, record_id: str):
    uid = update.effective_user.id
    state = user_state[uid]
    zone_id = state['zone_id']
    settings = load_smart_settings()
    record_config = next((item for item in settings.get("auto_check_records", []) if item["record_id"] == record_id and item["zone_id"] == zone_id), None)
    
    is_auto_check_enabled = record_config is not None
    check_location = record_config.get("location", "ir") if record_config else "ir"
    interval_seconds = record_config.get("interval", 1800) if record_config else 1800
    location_text = "ایران 🇮🇷" if check_location == "ir" else "آلمان 🇩🇪"
    auto_check_text = "✅ فعال" if is_auto_check_enabled else "❌ غیرفعال"
    
    record_details = get_record_details(zone_id, record_id)
    text = f"🤖 *منوی اتصال هوشمند برای رکورد: `{record_details.get('name', '')}`*\n\nاین بخش به شما امکان مدیریت و بررسی خودکار IPها را می‌دهد."
    
    keyboard = [
        [InlineKeyboardButton(f"مکان پینگ: {location_text}", callback_data=f"smart_toggle_loc_{record_id}")],
        [InlineKeyboardButton(f"بررسی خودکار: {auto_check_text}", callback_data=f"smart_toggle_auto_{record_id}")],
        [InlineKeyboardButton(f"زمان‌بندی: هر {interval_to_text(interval_seconds)}", callback_data=f"smart_interval_menu_{record_id}")],
        [InlineKeyboardButton("➕ افزودن IP رزرو", callback_data=f"smart_add_ip_{record_id}")],
        [InlineKeyboardButton("📋 مشاهده IPهای رزرو", callback_data=f"smart_view_reserve_{record_id}")],
        [InlineKeyboardButton("🗑 مشاهده IPهای منسوخ", callback_data=f"smart_view_deprecated_{record_id}")],
        [InlineKeyboardButton("▶️ اجرای بررسی دستی", callback_data=f"smart_run_manual_{record_id}")],
        [InlineKeyboardButton("🔙 بازگشت به تنظیمات رکورد", callback_data=f"record_settings_{record_id}")]
    ]
    # اگر این منو از طریق کلیک روی دکمه‌ها باز شده باشد، پیام قبلی را ادیت می‌کنیم.
    # اما اگر از طریق ارسال متن (MessageHandler) فراخوانی شود، باید پیام جدید ارسال کنیم
    # چون بات اجازه ادیت پیام کاربر را ندارد.
    if update.callback_query:
        await update.effective_message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard) )
    else:
        await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard) )

async def show_interval_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, record_id: str):
    text = "⏱️ لطفا بازه زمانی برای بررسی خودکار را انتخاب کنید:"
    keyboard = [
        [
            InlineKeyboardButton("۳۰ دقیقه", callback_data=f"smart_set_interval_{record_id}_1800"),
            InlineKeyboardButton("۱ ساعت", callback_data=f"smart_set_interval_{record_id}_3600")
        ],
        [
            InlineKeyboardButton("۲ ساعت", callback_data=f"smart_set_interval_{record_id}_7200"),
            InlineKeyboardButton("۶ ساعت", callback_data=f"smart_set_interval_{record_id}_21600")
        ],
        [
            InlineKeyboardButton("۱۲ ساعت", callback_data=f"smart_set_interval_{record_id}_43200"),
            InlineKeyboardButton("۱ روز", callback_data=f"smart_set_interval_{record_id}_86400")
        ],
        [
            InlineKeyboardButton("۲ روز", callback_data=f"smart_set_interval_{record_id}_172800")
        ],
        [InlineKeyboardButton("🔙 بازگشت", callback_data=f"smart_menu_{record_id}")]
    ]
    if update.callback_query:
        await update.effective_message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = "این ربات برای مدیریت رکوردهای DNS در Cloudflare طراحی شده است."
    await update.effective_message.edit_text(help_text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_main")]]))

async def show_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.effective_message.reply_text("❌ شما اجازه دسترسی به این بخش را ندارید.")
        return
    try:
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            last_lines = f.readlines()[-20:]
    except FileNotFoundError:
        await update.effective_message.reply_text("فایل لاگ یافت نشد.")
        return
    if not last_lines:
        await update.effective_message.reply_text("هنوز هیچ فعالیتی ثبت نشده است.")
        return
    formatted_log = "📜 **۲۰ فعالیت آخر ربات:**\n" + "-"*20
    for line in reversed(last_lines):
        match = re.search(r'\[(.*?)\] User: (\d+) \| Action: (.*)', line)
        if not match: continue
        timestamp, log_user_id, action = match.groups()
        dt_obj = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
        formatted_time = dt_obj.strftime("%H:%M | %Y/%m/%d")
        formatted_log += f"\n\n- `{action}`\n  (توسط کاربر `{log_user_id}` در {formatted_time})"
    reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_main")]])
    if update.callback_query:
        await update.effective_message.edit_text(formatted_log, parse_mode="Markdown", reply_markup=reply_markup)
    else:
        await update.effective_message.reply_text(formatted_log, parse_mode="Markdown", reply_markup=reply_markup)

async def show_request_access_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("✉️ ارسال درخواست دسترسی", callback_data="request_access")]]
    text = "❌ شما به این ربات دسترسی ندارید."
    if update.callback_query:
        await update.effective_message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_unauthorized_access_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    user_data = {"id": user.id, **profile_from_telegram_user(user)}
    if add_request(user_data):
        log_action(user.id, "Submitted an access request.")
        admin_text = (
            "📨 درخواست دسترسی جدید\n\n"
            f"نام: {display_name_for_user(user.id, user_data)}\n"
            f"ID: {user.id}"
        )
        try:
            await context.bot.send_message(chat_id=ADMIN_ID, text=admin_text)
        except Exception as e:
            logger.error(f"Failed to send access request notification to admin: {e}")
        await query.edit_message_text("✅ درخواست شما ثبت شد.")
    else:
        await query.answer("⚠️ شما قبلاً یک درخواست ارسال کرده‌اید.", show_alert=True)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_user_blocked(user_id): return
    if not is_user_authorized(user_id):
        await show_request_access_menu(update, context)
    else:
        update_known_user_profile(update.effective_user)
        await show_main_menu(update, context)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_user_blocked(uid): return
    if not is_user_authorized(uid):
        await show_request_access_menu(update, context)
        return
    update_known_user_profile(update.effective_user)

    state = user_state.get(uid, {})
    mode = state.get("mode")
    text = update.message.text.strip()
    if not mode or mode == State.NONE: return

    if mode == State.EDITING_USER_PROFILE and uid == ADMIN_ID:
        target_user_id = state.get("target_user_id")
        try:
            if not target_user_id:
                raise ValueError("missing target")
            profile = parse_profile_edit_input(text)
            if set_user_profile(int(target_user_id), profile):
                shown_name = display_name_for_user(int(target_user_id), normalize_user_record(int(target_user_id), profile))
                await update.message.reply_text(f"✅ اطلاعات نمایشی کاربر ذخیره شد.\nنام جدید: {shown_name}\nID: {target_user_id}")
                log_action(uid, f"Updated display profile for user {target_user_id}")
            else:
                await update.message.reply_text("❌ کاربر پیدا نشد.")
        except ValueError:
            await update.message.reply_text("❌ فرمت درست: نام و در صورت نیاز یوزرنیم. مثال: Ali @username\nبرای پاک کردن اطلاعات نمایشی فقط `-` را ارسال کنید." )
        finally:
            reset_user_state(uid)
            if target_user_id:
                await show_user_card_menu(update, context, int(target_user_id))
            else:
                await manage_whitelist_menu(update, context)
        return

    if mode == State.ADDING_RESERVE_IP:
        record_id = state.get("record_id")
        new_ips = [ip.strip() for ip in re.split(r'[,\s\n]+', text) if ip.strip()]
        if not new_ips:
            await update.message.reply_text("❌ ورودی نامعتبر است.")
            return
        ip_lists = load_ip_lists()
        added_count = 0
        for ip in new_ips:
            if ip not in ip_lists["reserve"] and ip not in ip_lists["deprecated"]:
                ip_lists["reserve"].append(ip)
                added_count += 1
        save_ip_lists(ip_lists)
        await update.message.reply_text(f"✅ تعداد {added_count} آی‌پی جدید به لیست رزرو اضافه شد.")
        log_action(uid, f"Added {added_count} new IPs to reserve list.")
        # خروج از حالت دریافت IP و بازگشت خودکار به منوی قبلی
        reset_user_state(uid, keep_zone=True)
        await show_smart_connection_menu(update, context, record_id)
        return

    if mode == State.ADDING_USER and uid == ADMIN_ID:
        try:
            new_user_id, profile = parse_user_add_input(text)
            is_new = add_user(new_user_id, profile)
            shown_name = display_name_for_user(new_user_id, normalize_user_record(new_user_id, profile))
            if is_new:
                await update.message.reply_text(f"✅ کاربر اضافه شد.\nنام: {shown_name}\nID: {new_user_id}")
                log_action(uid, f"Added user {new_user_id}")
            else:
                await update.message.reply_text(f"⚠️ این کاربر از قبل وجود داشت؛ اطلاعات نمایشی به‌روزرسانی شد.\nنام: {shown_name}\nID: {new_user_id}")
        except ValueError:
            await update.message.reply_text("❌ فرمت درست: ID عددی، یا ID + نام/یوزرنیم. مثال: 123456789 Ali @ali")
        finally:
            reset_user_state(uid)
            await manage_whitelist_menu(update, context)
        return

    elif mode == State.CLONING_NEW_IP:
        new_ip = text; clone_data = user_state[uid].get("clone_data", {}); zone_id = state.get("zone_id"); full_name = clone_data.get("name")
        if not all([new_ip, clone_data, zone_id, full_name]):
            await update.message.reply_text("❌ خطای داخلی."); reset_user_state(uid, keep_zone=True); return
        await update.message.reply_text(f"⏳ در حال افزودن IP `{new_ip}`..." )
        try:
            if create_dns_record(zone_id, clone_data["type"], full_name, new_ip, clone_data["ttl"], clone_data["proxied"]):
                log_action(uid, f"CREATE (Clone) record '{full_name}' with IP '{new_ip}'")
                await update.message.reply_text("✅ رکورد جدید با موفقیت اضافه شد.")
            else: await update.message.reply_text("❌ عملیات ناموفق بود.")
        except Exception as e: logger.error(f"Error creating cloned record: {e}"); await update.message.reply_text("❌ خطا در ارتباط با API.")
        finally:
            # بازگشت خودکار به منوی قبلی (تنظیمات همان رکورد)
            original_record_id = state.get("record_id")
            reset_user_state(uid, keep_zone=True)
            if original_record_id and zone_id:
                new_msg = await update.message.reply_text("↩️ بازگشت به منوی رکورد...")
                await show_record_settings(new_msg, uid, zone_id, original_record_id)
            else:
                await show_records_list(update, context)
        return
            

    elif mode == State.EDITING_IP:
        new_content = text; record_id = state.get("record_id"); zone_id = state.get("zone_id")
        await update.message.reply_text(f"⏳ در حال به‌روزرسانی محتوا..." )
        try:
            record = get_record_details(zone_id, record_id)
            if record:
                if update_dns_record(zone_id, record_id, record["name"], record["type"], new_content, record["ttl"], record.get("proxied", False)):
                    log_action(uid, f"UPDATE Content for '{record['name']}' to '{new_content}'")
                    await update.message.reply_text("✅ محتوای رکورد با موفقیت به‌روز شد.")
                    new_msg = await update.message.reply_text("...در حال بارگذاری تنظیمات جدید")
                    reset_user_state(uid, keep_zone=True)
                    await show_record_settings(new_msg, uid, zone_id, record_id)
                else: 
                    await update.message.reply_text("❌ به‌روزرسانی ناموفق بود.")
                    reset_user_state(uid, keep_zone=True); await show_records_list(update, context)
            else: 
                await update.message.reply_text("❌ رکورد مورد نظر یافت نشد.")
                reset_user_state(uid, keep_zone=True); await show_records_list(update, context)
        except Exception: 
            await update.message.reply_text("❌ خطا در ارتباط با API.")
            reset_user_state(uid, keep_zone=True); await show_records_list(update, context)

    elif mode == State.ADDING_RECORD_NAME:
        user_state[uid]["record_data"]["name"] = text
        user_state[uid]["mode"] = State.ADDING_RECORD_CONTENT
        await update.message.reply_text("📌 مرحله ۳ از ۵: مقدار رکورد را وارد کنید:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]]))
    
    elif mode == State.ADDING_RECORD_CONTENT:
        user_state[uid]["record_data"]["content"] = text
        user_state[uid].pop("mode", None)
        keyboard = [
            [InlineKeyboardButton("۱ دقیقه", callback_data=f"select_ttl_1"), InlineKeyboardButton("۲ دقیقه", callback_data=f"select_ttl_120")],
            [InlineKeyboardButton("۵ دقیقه", callback_data=f"select_ttl_300"), InlineKeyboardButton("۱۰ دقیقه", callback_data=f"select_ttl_600")],
            [InlineKeyboardButton("۱ ساعت", callback_data=f"update_ttl_3600"), InlineKeyboardButton("۱ روز", callback_data=f"update_ttl_86400")],
            [InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]
        ]
        await update.message.reply_text("📌 مرحله ۴ از ۵: مقدار TTL را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))

async def run_smart_check_logic(context: ContextTypes.DEFAULT_TYPE, zone_id: str, record_id: str, user_id: int):
    record_details = get_record_details(zone_id, record_id)
    if not record_details: return
    
    current_ip = record_details['content']
    settings = load_smart_settings()
    record_config = next((item for item in settings.get("auto_check_records", []) if item["record_id"] == record_id and item["zone_id"] == zone_id), None)
    
    check_location = "ir"
    if user_id != 0: 
        manual_record_config = next((item for item in settings.get("auto_check_records", []) if item["record_id"] == record_id and item["zone_id"] == zone_id), None)
        if manual_record_config:
            check_location = manual_record_config.get("location", "ir")
    elif record_config:
        check_location = record_config.get("location", "ir")

    is_pinging, report_text = await check_ip_ping(current_ip, check_location)
    
    if user_id != 0: 
        await context.bot.send_message(chat_id=user_id, text=f"📊 **نتیجه بررسی IP** `{current_ip}`:\n{report_text}" )
        if is_pinging: return

    if not is_pinging:
        ip_lists = load_ip_lists()
        
        if current_ip in ip_lists["reserve"]: ip_lists["reserve"].remove(current_ip)
        if current_ip not in ip_lists["deprecated"]: ip_lists["deprecated"].append(current_ip)

        notification_text = f"🚨 *گزارش اتصال هوشمند برای `{record_details['name']}`*\n\n"
        notification_text += f"- آی‌پی فعلی `{current_ip}` از کار افتاد و به لیست منسوخ منتقل شد.\n"
        
        new_ip_found = False
        while ip_lists["reserve"]:
            next_ip = ip_lists["reserve"].pop(0)
            
            if update_dns_record(zone_id, record_id, record_details["name"], record_details["type"], next_ip, record_details["ttl"], record_details.get("proxied", False)):
                notification_text += f"- آی‌پی جدید `{next_ip}` از لیست رزرو جایگزین شد. در حال تست...\n"
                
                is_next_pinging, new_ip_report = await check_ip_ping(next_ip, check_location)
                
                if is_next_pinging:
                    notification_text += f"✅ تست موفق! آی‌پی `{next_ip}` اکنون فعال است."
                    notification_text += f"\n\n📊 *نتیجه تست آی‌پی جدید:*\n{new_ip_report}"
                    new_ip_found = True
                    break
                else:
                    notification_text += f"❌ تست ناموفق! آی‌پی `{next_ip}` نیز از کار افتاده و به لیست منسوخ منتقل شد.\n"
                    if next_ip not in ip_lists["deprecated"]: ip_lists["deprecated"].append(next_ip)
            else:
                notification_text += f"- خطا در جایگزینی آی‌پی `{next_ip}`.\n"

        if not new_ip_found:
            notification_text += "\n🚫 *هشدار:* هیچ آی‌پی سالمی در لیست رزرو باقی نمانده است! لطفاً IP جدید اضافه کنید."

        save_ip_lists(ip_lists)
        
        target_chat_id = user_id if user_id != 0 else ADMIN_ID
        await context.bot.send_message(chat_id=target_chat_id, text=notification_text )
        log_action(user_id or "Auto", f"Smart check for {record_details['name']} completed.")

async def automated_check_job(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    zone_id = job.data["zone_id"]
    record_id = job.data["record_id"]
    logger.info(f"Running job for record {record_id}...")
    await run_smart_check_logic(context, zone_id, record_id, user_id=0)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    uid = query.from_user.id; data = query.data

    if is_user_blocked(uid): return

    if data == "request_access":
        await handle_unauthorized_access_request(update, context); return

    if not is_user_authorized(uid):
        await show_request_access_menu(update, context); return
    update_known_user_profile(query.from_user)
        
    if data.startswith((
        'manage_', 'user_card_', 'delete_user_', 'block_user_', 'unblock_user_', 'access_',
        'add_user_prompt', 'toggle_access_', 'set_all_access_', 'clear_access_',
        'edit_user_profile_', 'confirm_delete_user_', 'confirm_block_user_'
    )):
        if uid != ADMIN_ID:
            await query.answer("شما اجازه دسترسی به این بخش را ندارید.", show_alert=True); return

        if not data.startswith("edit_user_profile_") and user_state.get(uid, {}).get("mode") == State.EDITING_USER_PROFILE:
            reset_user_state(uid)

        if data == "manage_users":
            await manage_users_main_menu(update, context)

        elif data == "manage_whitelist":
            await manage_whitelist_menu(update, context)

        elif data == "manage_blacklist":
            await manage_blacklist_menu(update, context)

        elif data == "manage_requests":
            await manage_requests_menu(update, context)

        elif data.startswith("user_card_"):
            await show_user_card_menu(update, context)

        elif data.startswith("manage_access_"):
            await manage_user_access_menu(update, context)

        elif data.startswith("toggle_access_"):
            parts = data.split('_')
            target_user_id_str, zone_id_to_toggle = parts[2], parts[3]
            target_user_id = int(target_user_id_str)
            users = load_users()
            user_data = users.get(target_user_id_str)
            if not user_data or target_user_id == ADMIN_ID:
                await query.answer("امکان تغییر دسترسی این کاربر وجود ندارد.", show_alert=True)
                return

            try:
                all_zones = get_zones()
            except Exception as e:
                logger.error("Could not fetch zones while toggling access: %s", e)
                await query.answer("خطا در دریافت دامنه‌ها.", show_alert=True)
                return

            all_zone_ids = [zone["id"] for zone in all_zones]
            if user_data.get("access") == "all":
                access_list = [zone_id for zone_id in all_zone_ids if zone_id != zone_id_to_toggle]
                action_text = "دسترسی این دامنه غیرفعال شد."
                log_action(uid, f"Changed all-access user {target_user_id_str} to custom access and revoked zone {zone_id_to_toggle}")
            else:
                access_list = list(user_data.get("access", []))
                if zone_id_to_toggle in access_list:
                    access_list.remove(zone_id_to_toggle)
                    action_text = "دسترسی دامنه غیرفعال شد."
                    log_action(uid, f"Revoked access to zone {zone_id_to_toggle} for user {target_user_id_str}")
                else:
                    access_list.append(zone_id_to_toggle)
                    action_text = "دسترسی دامنه فعال شد."
                    log_action(uid, f"Granted access to zone {zone_id_to_toggle} for user {target_user_id_str}")

            users[target_user_id_str]["access"] = access_list
            users[target_user_id_str]["updated_at"] = now_text()
            save_users(users)
            await query.answer(action_text)
            await manage_user_access_menu(update, context)

        elif data.startswith("set_all_access_"):
            target_user_id = int(data.split("_")[3])
            if set_user_access(target_user_id, "all"):
                log_action(uid, f"Granted all zones to user {target_user_id}")
                await query.answer("دسترسی همه دامنه‌ها فعال شد.")
            else:
                await query.answer("عملیات ناموفق بود.", show_alert=True)
            await show_user_card_menu(update, context, target_user_id)

        elif data.startswith("clear_access_"):
            target_user_id = int(data.split("_")[2])
            if set_user_access(target_user_id, []):
                log_action(uid, f"Cleared all zone access for user {target_user_id}")
                await query.answer("همه دسترسی‌ها حذف شد.")
            else:
                await query.answer("عملیات ناموفق بود.", show_alert=True)
            await show_user_card_menu(update, context, target_user_id)

        elif data.startswith("edit_user_profile_"):
            target_user_id = int(data.split("_")[3])
            if target_user_id == ADMIN_ID:
                await query.answer("اطلاعات مدیر اصلی از تلگرام خوانده می‌شود.", show_alert=True)
                await show_user_card_menu(update, context, target_user_id)
                return
            user_state[uid] = {"mode": State.EDITING_USER_PROFILE, "target_user_id": target_user_id}
            await query.message.edit_text(
                "✏️ نام نمایشی کاربر را ارسال کنید.\n\n"
                "فرمت پیشنهادی:\n"
                "`Ali @username`\n\n"
                "برای پاک کردن نام و یوزرنیم ذخیره‌شده، فقط `-` را ارسال کنید.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data=f"user_card_{target_user_id}")]]),
                parse_mode="Markdown"
            )

        elif data.startswith("confirm_delete_user_"):
            target_user_id = int(data.split("_")[3])
            await confirm_user_action_menu(update, context, "delete", target_user_id)

        elif data.startswith("confirm_block_user_"):
            target_user_id = int(data.split("_")[3])
            await confirm_user_action_menu(update, context, "block", target_user_id)

        elif data.startswith("delete_user_"):
            user_to_manage = int(data.split("_")[2])
            if remove_user(user_to_manage):
                log_action(uid, f"Removed user {user_to_manage}.")
                await query.answer("کاربر حذف شد.")
            else:
                await query.answer("عملیات ناموفق بود.", show_alert=True)
            await manage_whitelist_menu(update, context)

        elif data.startswith("block_user_"):
            user_to_manage = int(data.split("_")[2])
            if block_user(user_to_manage):
                log_action(uid, f"Blocked user {user_to_manage}.")
                await query.answer("کاربر مسدود شد.")
            else:
                await query.answer("عملیات ناموفق بود.", show_alert=True)
            await manage_whitelist_menu(update, context)

        elif data.startswith("unblock_user_"):
            user_to_manage = int(data.split("_")[2])
            if unblock_user(user_to_manage):
                log_action(uid, f"Unblocked user {user_to_manage}.")
                await query.answer("کاربر رفع انسداد شد.")
            else:
                await query.answer("عملیات ناموفق بود.", show_alert=True)
            await manage_blacklist_menu(update, context)

        elif data.startswith("access_"):
            action, target_user_id = data.split("_")[1], int(data.split("_")[2])
            req_profile = get_request_profile(target_user_id)
            if action == "approve":
                add_user(target_user_id, req_profile); log_action(uid, f"Approved access for {target_user_id}.")
                await context.bot.send_message(chat_id=target_user_id, text="✅ درخواست شما تایید شد. /start")
                await query.answer("دسترسی تایید شد.")
            elif action == "reject":
                log_action(uid, f"Rejected access for {target_user_id}.")
                await context.bot.send_message(chat_id=target_user_id, text="❌ درخواست شما رد شد.")
                await query.answer("درخواست رد شد.")
            elif action == "block":
                block_user(target_user_id); log_action(uid, f"Blocked user {target_user_id}.")
                await query.answer("کاربر مسدود شد.")
            remove_request(target_user_id)
            await manage_requests_menu(update, context)

        elif data == "add_user_prompt":
            user_state[uid]['mode'] = State.ADDING_USER
            await query.message.edit_text(
                "شناسه عددی کاربر را ارسال کنید.\n\n"
                "فرمت بهتر برای ثبت نام در لیست:\n"
                "`123456789 Ali @username`\n\n"
                "اگر فقط ID را بفرستید، نام بعد از اولین /start کاربر ذخیره می‌شود.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="manage_whitelist")]]),
                parse_mode="Markdown"
            )
        return

    state = user_state.get(uid, {}); zone_id = state.get("zone_id")
    if data == "noop": return
    if data in ["back_to_main", "refresh_domains"]: await show_main_menu(update, context)
    elif data == "delete_domain_menu": await show_delete_domain_menu(update, context)
    elif data == "back_to_records" or data == "refresh_records": await show_records_list(update, context)
    elif data == "show_help": await show_help(update, context)
    elif data == "show_logs": await show_logs(update, context)
    elif data == "cancel_action":
        # بازگشت خودکار به لیست رکوردها
        reset_user_state(uid, keep_zone=True)
        await query.message.edit_text("❌ عملیات لغو شد.")
        await show_records_list(update, context)
    elif data.startswith("zone_"):
        selected_zone_id = data.split("_")[1]; zone_info = get_zone_info_by_id(selected_zone_id)
        if zone_info:
            user_state[uid].update({"zone_id": selected_zone_id, "zone_name": zone_info["name"]}); await show_records_list(update, context)
    elif data.startswith("record_settings_"):
        await show_record_settings(query.message, uid, zone_id, data.split("_")[-1])
    
    elif data.startswith("smart_"):
        parts = data.split("_"); action = parts[1]; record_id = parts[-1]
        user_state[uid]['record_id'] = record_id
        if action == "menu":
            await show_smart_connection_menu(update, context, record_id)
        elif action == "toggle":
            sub_action = parts[2]
            settings = load_smart_settings()
            record_list = settings.setdefault("auto_check_records", [])
            record_config = next((item for item in record_list if item["record_id"] == record_id and item["zone_id"] == zone_id), None)
            if sub_action == "loc":
                if not record_config:
                    record_config = {"zone_id": zone_id, "record_id": record_id, "location": "de"}
                    record_list.append(record_config)
                else: record_config["location"] = "de" if record_config.get("location", "ir") == "ir" else "ir"
            elif sub_action == "auto":
                if record_config:
                    record_list.remove(record_config)
                    record_config = None
                else:
                    record_config = {"zone_id": zone_id, "record_id": record_id, "location": "ir", "interval": 1800}
                    record_list.append(record_config)
            save_smart_settings(settings)
            active_config = next((item for item in record_list if item["record_id"] == record_id and item["zone_id"] == zone_id), None)
            sync_smart_job(context.job_queue, zone_id, record_id, active_config)
            await show_smart_connection_menu(update, context, record_id)
        elif action == "add":
            user_state[uid]["mode"] = State.ADDING_RESERVE_IP
            await query.message.edit_text("➕ لطفاً IP یا IPهای جدید را وارد کنید. می‌توانید چندین IP را با فاصله، کاما یا در خطوط جدید ارسال نمایید:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data=f"smart_menu_{record_id}")]]))
        elif action == "view":
            list_type = parts[2]
            ip_lists = load_ip_lists()
            ip_list = ip_lists.get(list_type, [])
            title = "IPهای رزرو" if list_type == "reserve" else "IPهای منسوخ"
            text = f"*{title}:*\n\n"
            keyboard = [[InlineKeyboardButton("🔙 بازگشت", callback_data=f"smart_menu_{record_id}")]]
            if list_type == "deprecated" and ip_list:
                keyboard.insert(0, [InlineKeyboardButton("🗑️ خالی کردن لیست", callback_data=f"smart_clear_deprecated_{record_id}")])
            text += "\n".join(f"`{ip}`" for ip in ip_list) if ip_list else "این لیست خالی است."
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard) )
        elif action == "clear":
            if parts[2] == "deprecated":
                ip_lists = load_ip_lists()
                ip_lists["deprecated"] = []
                save_ip_lists(ip_lists)
                await query.answer("✅ لیست IPهای منسوخ خالی شد.")
                log_action(uid, "Cleared deprecated IP list.")
                await show_smart_connection_menu(update, context, record_id)
        elif action == "run":
            await query.message.edit_text(f"⏳ بررسی دستی پینگ شروع شد. لطفاً منتظر بمانید...")
            await run_smart_check_logic(context, zone_id, record_id, uid)
            await show_smart_connection_menu(update, context, record_id)
        elif action == "quick":
            await query.message.edit_text(f"⏳ در حال اجرای تست سریع پینگ برای IP `{record_id}`...")
            record_details = get_record_details(zone_id, record_id)
            if not record_details: return
            ip_to_test = record_details['content']
            
            settings = load_smart_settings()
            record_config = next((item for item in settings.get("auto_check_records", []) if item["record_id"] == record_id and item["zone_id"] == zone_id), None)
            check_location = record_config.get("location", "ir") if record_config else "ir"
            
            is_pinging, report_text = await check_ip_ping(ip_to_test, check_location)
            
            await query.message.edit_text(f"📊 **نتیجه بررسی IP** `{ip_to_test}`:\n\n{report_text}", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data=f"smart_menu_{record_id}")]]) )
        elif action == "interval" and parts[2] == "menu":
            await show_interval_menu(update, context, record_id)
        elif action == "set" and parts[2] == "interval":
            interval_seconds = int(parts[-1])
            settings = load_smart_settings()
            record_list = settings.setdefault("auto_check_records", [])
            record_config = next((item for item in record_list if item["record_id"] == record_id and item["zone_id"] == zone_id), None)
            
            if record_config:
                record_config["interval"] = interval_seconds
            else:
                record_config = {"zone_id": zone_id, "record_id": record_id, "location": "ir", "interval": interval_seconds}
                record_list.append(record_config)
            
            save_smart_settings(settings)
            sync_smart_job(context.job_queue, zone_id, record_id, record_config)
            await query.answer(f"✅ زمان‌بندی به هر {interval_to_text(interval_seconds)} تغییر کرد.")
            await show_smart_connection_menu(update, context, record_id)
            
    elif data.startswith("clone_record_"):
        record_id = data.split("_")[-1]; original_record = get_record_details(zone_id, record_id)
        if not original_record: await query.answer("❌ رکورد اصلی یافت نشد.", show_alert=True); return
        user_state[uid]["clone_data"] = { "name": original_record["name"], "type": original_record["type"], "ttl": original_record["ttl"], "proxied": original_record.get("proxied", False) }
        user_state[uid]["mode"] = State.CLONING_NEW_IP
        await query.message.edit_text(f"🐑 **کلون کردن رکورد**\n`{original_record['name']}`\n\nلطفاً **IP جدید** را وارد کنید:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]]))
    elif data.startswith("toggle_proxy_"):
        record_id = data.split("_")[-1]; record_details = get_record_details(zone_id, record_id)
        if toggle_proxied_status(zone_id, record_id):
            log_action(uid, f"Toggled proxy for '{record_details.get('name', record_id)}'"); await show_record_settings(query.message, uid, zone_id, record_id)
        else: await query.answer("❌ عملیات ناموفق بود.", show_alert=True)
    elif data.startswith("editip_"):
        record_id = data.split("_")[-1]
        user_state[uid].update({"mode": State.EDITING_IP, "record_id": record_id})
        await query.message.edit_text("📝 لطفاً IP/Content جدید را وارد کنید:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]]))
    elif data.startswith("edittll_"):
        record_id = data.split("_")[-1]
        keyboard = [
            [InlineKeyboardButton("۱ دقیقه", callback_data=f"update_ttl_{record_id}_1"), InlineKeyboardButton("۲ دقیقه", callback_data=f"update_ttl_{record_id}_120")],
            [InlineKeyboardButton("۵ دقیقه", callback_data=f"update_ttl_{record_id}_300"), InlineKeyboardButton("۱۰ دقیقه", callback_data=f"update_ttl_{record_id}_600")],
            [InlineKeyboardButton("۱ ساعت", callback_data=f"update_ttl_{record_id}_3600"), InlineKeyboardButton("۱ روز", callback_data=f"update_ttl_{record_id}_86400")],
            [InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]
        ]
        await query.message.edit_text("⏱ مقدار جدید TTL را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif data.startswith("update_ttl_"):
        parts, record_id, ttl = data.split("_"), data.split("_")[2], int(data.split("_")[3])
        record = get_record_details(zone_id, record_id)
        if record and update_dns_record(zone_id, record_id, record["name"], record["type"], record["content"], ttl, record.get("proxied", False)):
            log_action(uid, f"Updated TTL for '{record['name']}' to {ttl}"); await query.answer("✅ TTL تغییر یافت."); await show_record_settings(query.message, uid, zone_id, record_id)
        else: await query.answer("❌ عملیات ناموفق بود.")
    elif data == "add_record":
        user_state[uid]["record_data"] = {}
        keyboard = [
            [InlineKeyboardButton("A", callback_data="select_type_A"), InlineKeyboardButton("AAAA", callback_data="select_type_AAAA")],
            [InlineKeyboardButton("CNAME", callback_data="select_type_CNAME")],
            [InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]
        ]
        await query.message.edit_text("📌 مرحله ۱ از ۵: نوع رکورد را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif data.startswith("select_type_"):
        user_state[uid]["record_data"]["type"] = data.split("_")[2]; user_state[uid]["mode"] = State.ADDING_RECORD_NAME
        await query.message.edit_text("📌 مرحله ۲ از ۵: نام رکورد را وارد کنید (مثال: sub یا @):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]]))
    elif data.startswith("select_ttl_"):
        user_state[uid]["record_data"]["ttl"] = int(data.split("_")[2]); keyboard = [[InlineKeyboardButton("✅ بله", callback_data="select_proxied_true"), InlineKeyboardButton("❌ خیر", callback_data="select_proxied_false")], [InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]]
        await query.message.edit_text("📌 مرحله ۵ از ۵: آیا پروکسی فعال باشد؟", reply_markup=InlineKeyboardMarkup(keyboard))
    elif data.startswith("select_proxied_"):
        user_state[uid]["record_data"]["proxied"] = data.endswith("true")
        r_data, zone_name = user_state[uid]["record_data"], state["zone_name"]
        full_name = f"{r_data['name']}.{zone_name}" if r_data['name'] != "@" else zone_name
        await query.message.edit_text("⏳ در حال ایجاد رکورد...")
        if create_dns_record(zone_id, r_data["type"], full_name, r_data["content"], r_data["ttl"], r_data["proxied"]):
            log_action(uid, f"CREATE record '{full_name}' with content '{r_data['content']}'")
            await query.message.edit_text("✅ رکورد با موفقیت اضافه شد.")
        else: await query.message.edit_text("❌ افزودن رکورد ناموفق بود.")
        reset_user_state(uid, keep_zone=True); await show_records_list(update, context)
    elif data.startswith("confirm_delete_"):
        parts, item_type, item_id = data.split('_'), data.split('_')[2], data.split('_')[-1]
        back_action = "delete_domain_menu" if item_type == "zone" else f"record_settings_{item_id}"
        text = f"❗ آیا از حذف این {'دامنه' if item_type == 'zone' else 'رکورد'} مطمئن هستید؟"
        keyboard = [[InlineKeyboardButton("✅ بله، حذف شود", callback_data=f"delete_{item_type}_{item_id}")], [InlineKeyboardButton("❌ خیر، لغو", callback_data=back_action)]]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    elif data.startswith("delete_zone_"):
        zone_to_delete_id = data.split("_")[-1]; zone_info = get_zone_info_by_id(zone_to_delete_id); zone_name = zone_info.get("name", "N/A") if zone_info else "N/A"
        await query.message.edit_text(f"⏳ در حال حذف دامنه {zone_name}...")
        if delete_zone(zone_to_delete_id):
            log_action(uid, f"DELETED ZONE: '{zone_name}'"); await query.message.edit_text("✅ دامنه با موفقیت حذف شد.")
        else: await query.message.edit_text("❌ حذف دامنه ناموفق بود.")
        await show_main_menu(update, context)
    elif data.startswith("delete_record_"):
        record_id = data.split("_")[-1]; 
        record_details = get_record_details(zone_id, record_id)
        await query.message.edit_text("⏳ در حال حذف رکورد...")
        if delete_dns_record(zone_id, record_id):
            if record_details: log_action(uid, f"DELETE record '{record_details.get('name', 'N/A')}'")
            else: log_action(uid, f"DELETE record with ID '{record_id}' (details not found).")
            await query.message.edit_text("✅ رکورد حذف شد.")
        else: await query.message.edit_text("❌ حذف رکورد ناموفق بود.")
        await show_records_list(update, context)

def main():
    load_users(); load_blocked_users(); load_requests(); load_ip_lists(); load_smart_settings()
    logger.info("Starting bot...")
    
    app_builder = Application.builder().token(BOT_TOKEN)
    job_queue = JobQueue()
    app_builder.job_queue(job_queue)
    app = app_builder.build()
    
    # Schedule jobs for all auto-check records at startup
    settings = load_smart_settings()
    auto_check_list = settings.get("auto_check_records", [])
    for record_config in auto_check_list:
        zone_id = record_config.get("zone_id")
        record_id = record_config.get("record_id")
        if zone_id and record_id:
            sync_smart_job(job_queue, zone_id, record_id, record_config)
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("logs", show_logs))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == "__main__":
    main()
