import logging
import json
import re
import asyncio
import httpx
from collections import defaultdict
from enum import Enum, auto
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters, JobQueue)

# --- Configuration and Mocking (for local testing) ---
try:
    from cloudflare_api import *
    from config import BOT_TOKEN, ADMIN_ID
except ImportError:
    # This block is for running the bot without actual Cloudflare credentials.
    BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
    ADMIN_ID = 123456789
    MOCKED_ZONES = {
        "zone1": {"id": "zone1", "name": "example.com", "status": "active"},
        "zone2": {"id": "zone2", "name": "mysite.org", "status": "active"},
    }
    MOCKED_RECORDS = {
        "zone1": [
            {"id": "rec1", "type": "A", "name": "test.example.com", "content": "1.1.1.1", "proxied": False, "ttl": 1},
            {"id": "rec2", "type": "A", "name": "www.example.com", "content": "2.2.2.2", "proxied": True, "ttl": 120},
        ],
        "zone2": [
            {"id": "rec4", "type": "A", "name": "app.mysite.org", "content": "3.3.3.3", "proxied": False, "ttl": 1}
        ],
    }
    def get_zones(): return list(MOCKED_ZONES.values())
    def get_dns_records(zone_id): return MOCKED_RECORDS.get(zone_id, [])
    def get_record_details(zone_id, record_id):
        for rec in MOCKED_RECORDS.get(zone_id, []):
            if rec["id"] == record_id:
                return rec
        return None
    def get_zone_info_by_id(zone_id): return MOCKED_ZONES.get(zone_id)
    def create_dns_record(zone_id, type, name, content, ttl, proxied): return True
    def update_dns_record(zone_id, record_id, name, type, content, ttl, proxied):
        for rec in MOCKED_RECORDS.get(zone_id, []):
            if rec["id"] == record_id:
                rec.update({"content": content, "ttl": ttl, "proxied": proxied})
                return True
        return False
    def delete_dns_record(zone_id, record_id): return True
    def toggle_proxied_status(zone_id, record_id):
        for rec in MOCKED_RECORDS.get(zone_id, []):
            if rec["id"] == record_id:
                rec["proxied"] = not rec["proxied"]
                return True
        return False
    def delete_zone(zone_id):
        if zone_id in MOCKED_ZONES:
            del MOCKED_ZONES[zone_id]
            return True
        return False

# --- Basic Setup ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- File Paths ---
USER_FILE = "users.json"
LOG_FILE = "bot_audit.log"
BLOCKED_USER_FILE = "blocked_users.json"
REQUEST_FILE = "access_requests.json"
IP_LIST_FILE = "smart_connect_ips.json"
SMART_SETTINGS_FILE = "smart_connect_settings.json"

# --- Global Variables ---
CLEAN_IP_SOURCE = ["8.8.8.8", "8.8.4.4", "1.1.1.1", "1.0.0.1"]
user_state = defaultdict(dict)
class State(Enum):
    NONE, ADDING_USER, ADDING_RECORD_NAME, ADDING_RECORD_CONTENT, EDITING_IP, EDITING_TTL, CLONING_NEW_IP, ADDING_RESERVE_IP = auto(), auto(), auto(), auto(), auto(), auto(), auto(), auto()

# --- Data Persistence Functions ---
def load_data(filename, default_data):
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default_data

def save_data(filename, data):
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4)

def load_ip_lists(): return load_data(IP_LIST_FILE, {"reserve": CLEAN_IP_SOURCE, "deprecated": []})
def save_ip_lists(ip_lists): save_data(IP_LIST_FILE, ip_lists)
def load_smart_settings(): return load_data(SMART_SETTINGS_FILE, {"auto_check_records": []})
def save_smart_settings(settings): save_data(SMART_SETTINGS_FILE, settings)

# --- IP Ping Check Function ---
async def check_ip_ping(ip: str, location: str):
    params = {'host': ip, 'node': location, 'max_nodes': 10}
    headers = {'Accept': 'application/json'}
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get("https://check-host.net/check-ping", params=params, headers=headers, timeout=10)
            response.raise_for_status()
            initial_data = response.json()
            request_id = initial_data.get("request_id")
            if not request_id: return False, "پاسخ اولیه از API نامعتبر است."

            await asyncio.sleep(10)
            
            result_url = f"https://check-host.net/check-result/{request_id}"
            result_response = await client.get(result_url, headers=headers, timeout=20)
            result_response.raise_for_status()
            results = result_response.json()
            
            report = []
            successful_nodes_count = 0
            total_nodes = 0

            for node_key in results:
                total_nodes += 1
                ping_results = results.get(node_key)
                
                if not ping_results or not isinstance(ping_results, list) or not ping_results[0] or not isinstance(ping_results[0], list):
                    continue

                successful_pings_count = sum(1 for single_ping in ping_results[0] if isinstance(single_ping, list) and len(single_ping) > 0 and single_ping[0] == "OK")
                
                if successful_pings_count > 0:
                    successful_nodes_count += 1
            
            if total_nodes == 0:
                return False, "هیچ نودی برای تست یافت نشد."

            # For Iran (ir), all nodes must be successful. For others, at least one is enough.
            is_overall_successful = (successful_nodes_count == total_nodes) if location.lower() == "ir" else (successful_nodes_count > 0)
            
            return is_overall_successful, f"تعداد نودهای موفق: {successful_nodes_count} از {total_nodes}"

    except Exception as e:
        logger.error(f"Error in check_ip_ping for {ip} from {location}: {e}")
        return False, f"❌ خطا در ارتباط با API: {e}"

# --- User & Access Management ---
def log_action(user_id: int, action: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] User: {user_id} | Action: {action}\n"
    with open(LOG_FILE, "a", encoding="utf-8") as f: f.write(log_entry)

# ... (All other user management functions: load_users, save_users, is_user_authorized, etc. remain the same) ...
def load_users():
    data = load_data(USER_FILE, {"users": {}})
    if isinstance(data, dict) and "authorized_ids" in data:
        migrated_users = {str(uid): {"access": []} for uid in data["authorized_ids"] if uid != ADMIN_ID}
        migrated_users[str(ADMIN_ID)] = {"access": "all"}
        data = {"users": migrated_users}
        save_data(USER_FILE, data)
    admin_id_str = str(ADMIN_ID)
    if admin_id_str not in data.get("users", {}):
        data.setdefault("users", {})[admin_id_str] = {"access": "all"}
        save_data(USER_FILE, data)
    return data["users"]

def save_users(users_dict):
    save_data(USER_FILE, {"users": users_dict})

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
    accessible_zone_ids = user_data.get("access", [])
    return [zone for zone in all_zones if zone["id"] in accessible_zone_ids]

def add_user(user_id):
    users = load_users()
    user_id_str = str(user_id)
    if user_id_str not in users:
        users[user_id_str] = {"access": []}
        save_users(users)
        unblock_user(user_id)
        return True
    return False

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
    return load_data(BLOCKED_USER_FILE, {"blocked_ids": []})["blocked_ids"]

def save_blocked_users(blocked_list):
    save_data(BLOCKED_USER_FILE, {"blocked_ids": sorted(list(set(blocked_list)))})

def is_user_blocked(user_id):
    return user_id in load_blocked_users()

def block_user(user_id):
    if user_id == ADMIN_ID: return False
    blocked = load_blocked_users()
    if user_id not in blocked:
        blocked.append(user_id)
        save_blocked_users(blocked)
        remove_user(user_id)
        return True
    return False

def unblock_user(user_id):
    blocked = load_blocked_users()
    if user_id in blocked:
        blocked.remove(user_id)
        save_blocked_users(blocked)
        return True
    return False

def load_requests():
    return load_data(REQUEST_FILE, {"requests": []})["requests"]

def save_requests(request_list):
    save_data(REQUEST_FILE, {"requests": request_list})

def add_request(user: dict):
    requests = load_requests()
    user_ids = [r['id'] for r in requests]
    if user['id'] not in user_ids and not is_user_authorized(user['id']):
        requests.append(user)
        save_requests(requests)
        return True
    return False

def remove_request(user_id: int):
    requests = load_requests()
    original_len = len(requests)
    requests = [r for r in requests if r['id'] != user_id]
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
# --- Smart Connection Logic ---
async def run_smart_check_logic(context: ContextTypes.DEFAULT_TYPE, zone_id: str, record_id: str, user_id: int):
    record_details = get_record_details(zone_id, record_id)
    if not record_details: 
        logger.warning(f"Smart check failed: Record {record_id} not found in zone {zone_id}.")
        return

    current_ip = record_details['content']
    settings = load_smart_settings()
    record_config = next((item for item in settings.get("auto_check_records", []) if item["record_id"] == record_id and item["zone_id"] == zone_id), None)
    
    check_location = record_config.get("location", "ir") if record_config else "ir"

    is_pinging, report_text = await check_ip_ping(current_ip, check_location)
    
    # If it's a manual check, send the report to the user and stop if the IP is fine.
    if user_id != 0:
        await context.bot.send_message(chat_id=user_id, text=f"📊 **نتیجه بررسی دستی IP** `{current_ip}`:\n`{report_text}`", parse_mode="Markdown")
        if is_pinging:
            return

    if not is_pinging:
        ip_lists = load_ip_lists()
        
        if current_ip in ip_lists["reserve"]: ip_lists["reserve"].remove(current_ip)
        if current_ip not in ip_lists["deprecated"]: ip_lists["deprecated"].append(current_ip)

        notification_text = f"🚨 *گزارش اتصال هوشمند برای `{record_details['name']}`*\n\n"
        notification_text += f"- آی‌پی فعلی `{current_ip}` از کار افتاد و به لیست منسوخ منتقل شد.\n"
        
        new_ip_found = False
        while ip_lists["reserve"]:
            next_ip = ip_lists["reserve"].pop(0) # Get and remove the first IP
            
            if update_dns_record(zone_id, record_id, record_details["name"], record_details["type"], next_ip, record_details["ttl"], record_details.get("proxied", False)):
                notification_text += f"- آی‌پی جدید `{next_ip}` از لیست رزرو جایگزین شد. در حال تست...\n"
                
                is_next_pinging, _ = await check_ip_ping(next_ip, check_location)
                
                if is_next_pinging:
                    notification_text += f"✅ تست موفق! آی‌پی `{next_ip}` اکنون فعال است."
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
        
        # Send notification to the manual user or admin for automated checks
        target_chat_id = user_id if user_id != 0 else ADMIN_ID
        await context.bot.send_message(chat_id=target_chat_id, text=notification_text, parse_mode="Markdown")
        log_action(user_id or "Auto", f"Smart check for {record_details['name']} completed. IP changed.")

async def run_smart_check_with_semaphore(context, semaphore, zone_id, record_id, user_id):
    async with semaphore:
        await run_smart_check_logic(context, zone_id, record_id, user_id)

async def scheduled_job_for_record(context: ContextTypes.DEFAULT_TYPE):
    job_data = context.job.data
    zone_id = job_data.get("zone_id")
    record_id = job_data.get("record_id")
    semaphore = context.bot_data.get("semaphore")
    
    logger.info(f"Running scheduled job for record {record_id} in zone {zone_id}")
    await run_smart_check_with_semaphore(context, semaphore, zone_id, record_id, user_id=0)

# --- Menu and UI Functions ---
# ... (show_main_menu, manage_users_main_menu, manage_whitelist_menu, etc. remain the same) ...
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
    
    # Arrange action buttons in rows of two
    for i in range(0, len(action_buttons), 2):
        keyboard.append(action_buttons[i:i + 2])
        
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        await update.effective_message.edit_text(welcome_text, reply_markup=reply_markup)
    else:
        await update.effective_message.reply_text(welcome_text, reply_markup=reply_markup)

async def show_smart_connection_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, record_id: str):
    uid = update.effective_user.id
    state = user_state[uid]
    zone_id = state['zone_id']
    settings = load_smart_settings()
    record_config = next((item for item in settings.get("auto_check_records", []) if item["record_id"] == record_id and item["zone_id"] == zone_id), None)
    
    check_location = record_config.get("location", "ir") if record_config else "ir"
    interval_seconds = record_config.get("interval") if record_config else 0
    
    location_text = "ایران 🇮🇷" if check_location == "ir" else "آلمان 🇩🇪"
    
    if interval_seconds:
        interval_minutes = interval_seconds / 60
        auto_check_text = f"✅ فعال (هر {int(interval_minutes)} دقیقه)"
    else:
        auto_check_text = "❌ غیرفعال"

    record_details = get_record_details(zone_id, record_id)
    text = f"🤖 *منوی اتصال هوشمند برای رکورد: `{record_details.get('name', '')}`*\n\nاین بخش به شما امکان مدیریت و بررسی خودکار IPها را می‌دهد."
    
    keyboard = [
        [InlineKeyboardButton(f"مکان پینگ: {location_text}", callback_data=f"smart_toggle_loc_{record_id}")],
        [InlineKeyboardButton(f"بررسی خودکار: {auto_check_text}", callback_data=f"smart_schedule_menu_{record_id}")],
        [InlineKeyboardButton("➕ افزودن IP رزرو", callback_data=f"smart_add_ip_{record_id}")],
        [InlineKeyboardButton("📋 مشاهده IPهای رزرو", callback_data=f"smart_view_reserve_{record_id}")],
        [InlineKeyboardButton("🗑 مشاهده IPهای منسوخ", callback_data=f"smart_view_deprecated_{record_id}")],
        [InlineKeyboardButton("▶️ اجرای بررسی دستی", callback_data=f"smart_run_manual_{record_id}")],
        [InlineKeyboardButton("🔙 بازگشت به تنظیمات رکورد", callback_data=f"record_settings_{record_id}")]
    ]
    await update.effective_message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def show_smart_schedule_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, record_id: str):
    text = "⏱ زمان‌بندی بررسی خودکار را انتخاب کنید:"
    keyboard = [
        [InlineKeyboardButton("هر ۳۰ دقیقه", callback_data=f"set_schedule_{record_id}_1800")],
        [InlineKeyboardButton("هر ۱ ساعت", callback_data=f"set_schedule_{record_id}_3600")],
        [InlineKeyboardButton("هر ۲ ساعت", callback_data=f"set_schedule_{record_id}_7200")],
        [InlineKeyboardButton("هر ۶ ساعت", callback_data=f"set_schedule_{record_id}_21600")],
        [InlineKeyboardButton("هر ۱۲ ساعت", callback_data=f"set_schedule_{record_id}_43200")],
        [InlineKeyboardButton("هر ۲۴ ساعت", callback_data=f"set_schedule_{record_id}_86400")],
        [InlineKeyboardButton("هر ۴۸ ساعت", callback_data=f"set_schedule_{record_id}_172800")],
        [InlineKeyboardButton("❌ غیرفعال کردن", callback_data=f"set_schedule_{record_id}_0")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data=f"smart_menu_{record_id}")]
    ]
    await update.effective_message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
# ... (Other UI functions can remain largely the same) ...
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
    keyboard = []
    text = "👤 *لیست کاربران مجاز:*\n\n"
    for uid_str, u_data in users.items():
        uid = int(uid_str)
        user_text = f"`{uid}`"
        if uid == ADMIN_ID: user_text += " (ادمین)"
        buttons = []
        if uid != ADMIN_ID:
            buttons.extend([
                InlineKeyboardButton("🔑", callback_data=f"manage_access_{uid}"),
                InlineKeyboardButton("🗑", callback_data=f"delete_user_{uid}"),
                InlineKeyboardButton("🚫", callback_data=f"block_user_{uid}")
            ])
        keyboard.append([InlineKeyboardButton(user_text, callback_data="noop")] + buttons)
    keyboard.append([InlineKeyboardButton("➕ افزودن کاربر جدید", callback_data="add_user_prompt")])
    keyboard.append([InlineKeyboardButton("🔙 بازگشت", callback_data="manage_users")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        await update.effective_message.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await update.effective_message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

async def manage_user_access_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    target_user_id = int(query.data.split('_')[2])
    all_zones = get_zones()
    users = load_users()
    user_access = users.get(str(target_user_id), {}).get("access", [])
    text = f"🔑 *مدیریت دسترسی برای کاربر `{target_user_id}`*\n\n"
    keyboard = []
    for zone in all_zones:
        has_access = zone['id'] in user_access
        status_icon = "✅" if has_access else "❌"
        button_text = f"{status_icon} {zone['name']}"
        callback_data = f"toggle_access_{target_user_id}_{zone['id']}"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=callback_data)])
    keyboard.append([InlineKeyboardButton("🔙 بازگشت به لیست کاربران", callback_data="manage_whitelist")])
    await query.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
async def manage_blacklist_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    blocked_users = load_blocked_users()
    text = "🚫 *لیست کاربران مسدود:*\n\n"
    keyboard = []
    if not blocked_users: text += "لیست کاربران مسدود خالی است."
    else:
        for uid in blocked_users:
            keyboard.append([InlineKeyboardButton(f"`{uid}`", callback_data="noop"), InlineKeyboardButton("✅ رفع انسداد", callback_data=f"unblock_user_{uid}")])
    keyboard.append([InlineKeyboardButton("🔙 بازگشت", callback_data="manage_users")])
    await update.effective_message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def manage_requests_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    requests = load_requests()
    text = "📨 *لیست درخواست‌های در انتظار:*\n\n"
    keyboard = []
    if not requests: text += "هیچ درخواست جدیدی وجود ندارد."
    else:
        for req in requests:
            user_info = f"{req.get('first_name', 'کاربر')} (`{req['id']}`)"
            buttons = [
                InlineKeyboardButton("✅", callback_data=f"access_approve_{req['id']}"),
                InlineKeyboardButton("❌", callback_data=f"access_reject_{req['id']}"),
                InlineKeyboardButton("🚫", callback_data=f"access_block_{req['id']}")
            ]
            keyboard.append([InlineKeyboardButton(user_info, callback_data="noop")] + buttons)
    keyboard.append([InlineKeyboardButton("🔄", callback_data="manage_requests")])
    keyboard.append([InlineKeyboardButton("🔙 بازگشت", callback_data="manage_users")])
    await update.effective_message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def show_delete_domain_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    zones = get_zones()
    if not zones:
        await update.effective_message.edit_text("هیچ دامنه‌ای برای حذف یافت نشد.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_main")]]))
        return
    keyboard = [[InlineKeyboardButton(f"🗑️ {z['name']}", callback_data=f"confirm_delete_zone_{z['id']}")] for z in zones]
    keyboard.append([InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="back_to_main")])
    text = "لطفا دامنه‌ای که قصد حذف آن را دارید انتخاب کنید.\n\n**توجه:** این عمل غیرقابل بازگشت است!"
    await update.effective_message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def show_records_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid, state = update.effective_user.id, user_state.get(update.effective_user.id, {})
    zone_id, zone_name = state.get("zone_id"), state.get("zone_name", "")
    if not zone_id:
        await update.effective_message.edit_text("خطا: دامنه انتخاب نشده است.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("بازگشت", callback_data="back_to_main")]]))
        return
    records = get_dns_records(zone_id)
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
        await message.edit_text("❌ رکورد یافت نشد.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("بازگشت", callback_data="back_to_records")]]))
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

# --- Command and Message Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_user_blocked(user_id): return
    if not is_user_authorized(user_id):
        # ... logic for unauthorized access
        return
    await show_main_menu(update, context)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_user_blocked(uid) or not is_user_authorized(uid):
        return

    state = user_state.get(uid, {})
    mode = state.get("mode")
    text = update.message.text.strip()
    
    # Clean up user's message
    await update.message.delete()
    
    if not mode or mode == State.NONE:
        return

    # Find the bot's prompt message and edit it to show it's processing
    prompt_message_id = state.get("prompt_message_id")
    if prompt_message_id:
        try:
            await context.bot.edit_message_text("⏳ در حال پردازش...", chat_id=uid, message_id=prompt_message_id)
        except Exception as e:
            logger.warning(f"Could not edit prompt message: {e}")

    # ... (rest of the handle_message logic)
    # Important: After each action, call the appropriate menu function to guide the user back.
    # For example, after adding a user:
    if mode == State.ADDING_USER and uid == ADMIN_ID:
        try:
            new_user_id = int(text)
            if add_user(new_user_id):
                log_action(uid, f"Added user {new_user_id}")
            # Do not reply here, let the menu function handle the message
        except ValueError:
            pass # Error will be shown in the menu
        finally:
            reset_user_state(uid)
            await manage_whitelist_menu(update, context) # This sends a new, clean menu
            if prompt_message_id: await context.bot.delete_message(chat_id=uid, message_id=prompt_message_id) # delete the old prompt

# The full handle_message and handle_callback would be too long to repeat,
# but the key is to apply the logic above and the new callback handlers below.

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    data = query.data

    if is_user_blocked(uid) or not is_user_authorized(uid):
        # ... handle unauthorized ...
        return

    state = user_state.get(uid, {})
    zone_id = state.get("zone_id")

    # --- New Schedule Handlers ---
    if data.startswith("smart_schedule_menu_"):
        record_id = data.split("_")[-1]
        await show_smart_schedule_menu(update, context, record_id)

    elif data.startswith("set_schedule_"):
        parts = data.split("_")
        record_id = parts[2]
        interval = int(parts[3])
        
        settings = load_smart_settings()
        record_list = settings.setdefault("auto_check_records", [])
        record_config = next((item for item in record_list if item["record_id"] == record_id and item.get("zone_id") == zone_id), None)
        
        job_name = f"smart_check_{zone_id}_{record_id}"
        
        # Remove existing job for this record
        current_jobs = context.job_queue.get_jobs_by_name(job_name)
        for job in current_jobs:
            job.schedule_removal()
            logger.info(f"Removed existing job: {job_name}")

        if interval > 0:
            # Add new job
            context.job_queue.run_repeating(
                scheduled_job_for_record,
                interval=interval,
                first=10,
                name=job_name,
                data={"zone_id": zone_id, "record_id": record_id}
            )
            if not record_config:
                record_list.append({"zone_id": zone_id, "record_id": record_id, "interval": interval, "location": "ir"})
            else:
                record_config["interval"] = interval
            
            await query.answer(f"✅ زمان‌بندی به هر {interval/60:.0f} دقیقه تغییر یافت.")
            log_action(uid, f"Set schedule for record {record_id} to {interval}s.")
        else:
            # If interval is 0, just remove the job and the config
            if record_config:
                record_list.remove(record_config)
            await query.answer("❌ بررسی خودکار غیرفعال شد.")
            log_action(uid, f"Disabled schedule for record {record_id}.")

        save_smart_settings(settings)
        await show_smart_connection_menu(update, context, record_id)

    # --- Existing Callback Handlers (abbreviated) ---
    elif data.startswith("smart_run_manual_"):
        record_id = data.split("_")[-1]
        await query.message.edit_text("⏳ بررسی دستی شروع شد. لطفاً منتظر بمانید...")
        semaphore = context.bot_data.get("semaphore")
        await run_smart_check_with_semaphore(context, semaphore, zone_id, record_id, uid)
        await show_smart_connection_menu(update, context, record_id) # Return to menu

    elif data.startswith("add_user_prompt"):
        user_state[uid]['mode'] = State.ADDING_USER
        msg = await query.message.edit_text(
            "لطفاً شناسه عددی (ID) کاربر را ارسال کنید...",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="manage_whitelist")]])
        )
        user_state[uid]['prompt_message_id'] = msg.message_id # Save message_id to edit it later
    
    # ... (rest of the handle_callback logic remains the same) ...
    elif data == "noop": return
    elif data in ["back_to_main", "refresh_domains"]: await show_main_menu(update, context)
    elif data == "delete_domain_menu": await show_delete_domain_menu(update, context)
    elif data == "back_to_records" or data == "refresh_records": await show_records_list(update, context)
    # ... other handlers
    else:
        # Fallback for other unhandled callbacks
        logger.warning(f"Unhandled callback data: {data}")

# --- Main Bot Function ---
def main():
    logger.info("Starting bot...")
    
    app_builder = Application.builder().token(BOT_TOKEN)
    job_queue = JobQueue()
    app_builder.job_queue(job_queue)
    app = app_builder.build()

    # Create and store the semaphore for rate limiting
    app.bot_data["semaphore"] = asyncio.Semaphore(5)

    # Load all saved scheduled jobs on startup
    settings = load_smart_settings()
    for record_config in settings.get("auto_check_records", []):
        interval = record_config.get("interval")
        if interval and interval > 0:
            zone_id = record_config["zone_id"]
            record_id = record_config["record_id"]
            job_name = f"smart_check_{zone_id}_{record_id}"
            job_queue.run_repeating(
                scheduled_job_for_record,
                interval=interval,
                first=10,
                name=job_name,
                data={"zone_id": zone_id, "record_id": record_id}
            )
            logger.info(f"Loaded scheduled job '{job_name}' with interval {interval}s.")

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    logger.info("Bot is running.")
    app.run_polling()

if __name__ == "__main__":
    main()
