import logging
import json
import re
from collections import defaultdict
from enum import Enum, auto
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters)

try:
    from cloudflare_api import *
    from config import BOT_TOKEN, ADMIN_ID
except ImportError:
    BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
    ADMIN_ID = 123456789
    MOCKED_ZONES = {
        "zone1": {"id": "zone1", "name": "example.com", "status": "active"},
        "zone2": {"id": "zone2", "name": "mysite.org", "status": "active"},
        "zone3": {"id": "zone3", "name": "anothersite.net", "status": "pending"}
    }
    MOCKED_RECORDS = {
        "zone1": [
            {"id": "rec1", "type": "A", "name": "test.example.com", "content": "1.1.1.1"},
            {"id": "rec2", "type": "CNAME", "name": "www.example.com", "content": "example.com"},
        ],
        "zone2": [
            {"id": "rec4", "type": "AAAA", "name": "ipv6.mysite.org", "content": "2001:db8::1"}
        ],
         "zone3": []
    }
    def get_zones(): return list(MOCKED_ZONES.values())
    def get_dns_records(zone_id): return MOCKED_RECORDS.get(zone_id, [])
    def get_record_details(zone_id, record_id):
        for rec in MOCKED_RECORDS.get(zone_id, []):
            if rec["id"] == record_id:
                return {**rec, "ttl": 1, "proxied": True}
        return None
    def get_zone_info_by_id(zone_id): return MOCKED_ZONES.get(zone_id)
    def create_dns_record(zone_id, type, name, content, ttl, proxied): return True
    def update_dns_record(zone_id, record_id, name, type, content, ttl, proxied): return True
    def delete_dns_record(zone_id, record_id): return True
    def toggle_proxied_status(zone_id, record_id): return True
    def delete_zone(zone_id):
        if zone_id in MOCKED_ZONES:
            del MOCKED_ZONES[zone_id]
            return True
        return False

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

USER_FILE = "users.json"
LOG_FILE = "bot_audit.log"
BLOCKED_USER_FILE = "blocked_users.json"
REQUEST_FILE = "access_requests.json"

user_state = defaultdict(dict)

class State(Enum):
    NONE, ADDING_USER, ADDING_RECORD_NAME, ADDING_RECORD_CONTENT, EDITING_IP, EDITING_TTL, CLONING_NEW_IP = auto(), auto(), auto(), auto(), auto(), auto(), auto()

def log_action(user_id: int, action: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] User: {user_id} | Action: {action}\n"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f: f.write(log_entry)
    except Exception as e:
        logger.error(f"Failed to write to log file: {e}")

def load_data(filename, default_data):
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default_data

def save_data(filename, data):
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=4)

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

    if not user_data:
        return []

    all_zones = get_zones()
    if user_data.get("access") == "all":
        return all_zones
    
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
        user_state[uid] = {"zone_id": zone_id, "zone_name": zone_name}
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
        welcome_text = "شما به هیچ دامنه‌ای دسترسی ندارید. لطفاً با مدیر تماس بگیرید."
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
        [InlineKeyboardButton("👤 کاربران مجاز (Whitelist)", callback_data="manage_whitelist")],
        [InlineKeyboardButton("🚫 کاربران مسدود (Blacklist)", callback_data="manage_blacklist")],
        [InlineKeyboardButton("📨 درخواست‌های در انتظار", callback_data="manage_requests")],
        [InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="back_to_main")]
    ]
    await update.effective_message.edit_text("لطفا بخش مورد نظر برای مدیریت کاربران را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))

async def manage_whitelist_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = load_users()
    keyboard = []
    text = "👤 *لیست کاربران مجاز (Whitelist):*\n\n"
    for uid_str, u_data in users.items():
        uid = int(uid_str)
        user_text = f"`{uid}`"
        if uid == ADMIN_ID:
            user_text += " (ادمین اصلی)"
        
        buttons = []
        if uid != ADMIN_ID:
            buttons.extend([
                InlineKeyboardButton("🔑 دسترسی‌ها", callback_data=f"manage_access_{uid}"),
                InlineKeyboardButton("🗑 حذف", callback_data=f"delete_user_{uid}"),
                InlineKeyboardButton("🚫 بلاک", callback_data=f"block_user_{uid}")
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
    text = "🚫 *لیست کاربران مسدود (Blacklist):*\n\n"
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
                InlineKeyboardButton("✅ تایید", callback_data=f"access_approve_{req['id']}"),
                InlineKeyboardButton("❌ رد", callback_data=f"access_reject_{req['id']}"),
                InlineKeyboardButton("🚫 بلاک", callback_data=f"access_block_{req['id']}")
            ]
            keyboard.append([InlineKeyboardButton(user_info, callback_data="noop")] + buttons)
    keyboard.append([InlineKeyboardButton("🔄 رفرش", callback_data="manage_requests")])
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
    if record['type'] == 'A': action_row.append(InlineKeyboardButton("🐑 کلون", callback_data=f"clone_record_{record_id}"))
    action_row.append(InlineKeyboardButton("🗑️ حذف", callback_data=f"confirm_delete_record_{record_id}"))
    if action_row: keyboard.append(action_row)
    keyboard.append([InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_records")])
    await message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = "این ربات برای مدیریت رکوردهای DNS در Cloudflare طراحی شده است. می‌توانید دامنه‌های خود را مشاهده کرده، رکوردهای آن‌ها را مدیریت (افزودن، ویرایش، حذف) کنید. ادمین اصلی قابلیت مدیریت کاربران و دسترسی آن‌ها به دامنه‌های مختلف را دارد."
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
        await update.effective_message.reply_text("فایل لاگ یافت نشد. هنوز فعالیتی ثبت نشده است.")
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
    text = "❌ شما به این ربات دسترسی ندارید. برای ارسال درخواست به مدیر، دکمه زیر را فشار دهید."
    if update.callback_query:
        await update.effective_message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_unauthorized_access_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    user_data = {"id": user.id, "first_name": user.first_name or " ", "username": user.username}
    
    if add_request(user_data):
        log_action(user.id, "Submitted an access request.")
        admin_text = f"📨 یک درخواست دسترسی جدید از طرف کاربر {user.first_name} (`{user.id}`) ثبت شد."
        try:
            await context.bot.send_message(chat_id=ADMIN_ID, text=admin_text, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Failed to send access request notification to admin: {e}")
        await query.edit_message_text("✅ درخواست شما با موفقیت ثبت شد. مدیر به زودی آن را بررسی خواهد کرد.")
    else:
        await query.answer("⚠️ شما قبلاً یک درخواست ارسال کرده‌اید. لطفاً منتظر بمانید.", show_alert=True)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_user_blocked(user_id): return
    if not is_user_authorized(user_id):
        await show_request_access_menu(update, context)
        return
    await show_main_menu(update, context)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if is_user_blocked(uid): return
    if not is_user_authorized(uid):
        await show_request_access_menu(update, context)
        return

    state = user_state.get(uid, {})
    mode = state.get("mode")
    text = update.message.text.strip()
    if not mode or mode == State.NONE: return

    if mode == State.ADDING_USER and uid == ADMIN_ID:
        try:
            new_user_id = int(text)
            if add_user(new_user_id):
                await update.message.reply_text(f"✅ کاربر `{new_user_id}` با موفقیت اضافه شد.", parse_mode="Markdown")
                log_action(uid, f"Added user {new_user_id}")
            else:
                await update.message.reply_text("⚠️ این کاربر از قبل در لیست مجاز وجود دارد.")
        except ValueError:
            await update.message.reply_text("❌ ورودی نامعتبر است. لطفاً شناسه عددی ارسال کنید.")
        finally:
            reset_user_state(uid)
            await manage_whitelist_menu(update, context)
        return

    elif mode == State.CLONING_NEW_IP:
        new_ip = text; clone_data = user_state[uid].get("clone_data", {}); zone_id = state.get("zone_id"); full_name = clone_data.get("name")
        if not all([new_ip, clone_data, zone_id, full_name]):
            await update.message.reply_text("❌ خطای داخلی."); reset_user_state(uid, keep_zone=True); return
        await update.message.reply_text(f"⏳ در حال افزودن IP `{new_ip}`...", parse_mode="Markdown")
        try:
            if create_dns_record(zone_id, clone_data["type"], full_name, new_ip, clone_data["ttl"], clone_data["proxied"]):
                log_action(uid, f"CREATE (Clone) record '{full_name}' with IP '{new_ip}'")
                await update.message.reply_text("✅ رکورد جدید با موفقیت اضافه شد.")
            else: await update.message.reply_text("❌ عملیات ناموفق بود.")
        except Exception as e: logger.error(f"Error creating cloned record: {e}"); await update.message.reply_text("❌ خطا در ارتباط با API.")
        finally:
            reset_user_state(uid, keep_zone=True)
            await show_records_list(update, context)

    elif mode == State.EDITING_IP:
        new_content = text; record_id = state.get("record_id"); zone_id = state.get("zone_id")
        await update.message.reply_text(f"⏳ در حال به‌روزرسانی محتوا...", parse_mode="Markdown")
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
                    reset_user_state(uid, keep_zone=True)
                    await show_records_list(update, context)
            else: 
                await update.message.reply_text("❌ رکورد مورد نظر یافت نشد.")
                reset_user_state(uid, keep_zone=True)
                await show_records_list(update, context)
        except Exception: 
            await update.message.reply_text("❌ خطا در ارتباط با API.")
            reset_user_state(uid, keep_zone=True)
            await show_records_list(update, context)

    elif mode == State.ADDING_RECORD_NAME:
        user_state[uid]["record_data"]["name"] = text
        user_state[uid]["mode"] = State.ADDING_RECORD_CONTENT
        await update.message.reply_text("📌 مرحله ۳ از ۵: مقدار رکورد را وارد کنید (مثلاً IP یا آدرس):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]]))
    
    elif mode == State.ADDING_RECORD_CONTENT:
        user_state[uid]["record_data"]["content"] = text
        user_state[uid].pop("mode", None)
        keyboard = [
            [InlineKeyboardButton("Auto", callback_data="select_ttl_1"), InlineKeyboardButton("2 min", callback_data="select_ttl_120")],
            [InlineKeyboardButton("5 min", callback_data="select_ttl_300"), InlineKeyboardButton("10 min", callback_data="select_ttl_600")],
            [InlineKeyboardButton("1 hr", callback_data="select_ttl_3600"), InlineKeyboardButton("1 day", callback_data="select_ttl_86400")],
            [InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]
        ]
        await update.message.reply_text("📌 مرحله ۴ از ۵: مقدار TTL را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    uid = query.from_user.id; data = query.data

    if is_user_blocked(uid): return

    if data == "request_access":
        await handle_unauthorized_access_request(update, context)
        return

    if not is_user_authorized(uid):
        await show_request_access_menu(update, context)
        return
        
    if data.startswith(('manage_', 'delete_user_', 'block_user_', 'unblock_user_', 'access_', 'add_user_prompt', 'toggle_access_')):
        if uid != ADMIN_ID:
            await query.answer("شما اجازه دسترسی به این بخش را ندارید.", show_alert=True); return
        if data == "manage_users": await manage_users_main_menu(update, context)
        elif data == "manage_whitelist": await manage_whitelist_menu(update, context)
        elif data == "manage_blacklist": await manage_blacklist_menu(update, context)
        elif data == "manage_requests": await manage_requests_menu(update, context)
        elif data.startswith("manage_access_"): await manage_user_access_menu(update, context)
        elif data.startswith("toggle_access_"):
            parts = data.split('_')
            target_user_id_str, zone_id_to_toggle = parts[2], parts[3]
            users = load_users()
            user_data = users.get(target_user_id_str)
            if user_data and user_data.get("access") != "all":
                access_list = user_data.get("access", [])
                if zone_id_to_toggle in access_list:
                    access_list.remove(zone_id_to_toggle)
                    log_action(uid, f"Revoked access to zone {zone_id_to_toggle} for user {target_user_id_str}")
                else:
                    access_list.append(zone_id_to_toggle)
                    log_action(uid, f"Granted access to zone {zone_id_to_toggle} for user {target_user_id_str}")
                users[target_user_id_str]["access"] = access_list
                save_users(users)
                await manage_user_access_menu(update, context)
        elif data.startswith("delete_user_"):
            user_to_manage = int(data.split("_")[2])
            if remove_user(user_to_manage): log_action(uid, f"Removed user {user_to_manage}."); await query.answer("کاربر از لیست مجاز حذف شد.")
            else: await query.answer("عملیات ناموفق بود.", show_alert=True)
            await manage_whitelist_menu(update, context)
        elif data.startswith("block_user_"):
            user_to_manage = int(data.split("_")[2])
            if block_user(user_to_manage): log_action(uid, f"Blocked user {user_to_manage}."); await query.answer("کاربر مسدود شد.")
            else: await query.answer("عملیات ناموفق بود.", show_alert=True)
            await manage_whitelist_menu(update, context)
        elif data.startswith("unblock_user_"):
            user_to_manage = int(data.split("_")[2])
            if unblock_user(user_to_manage): log_action(uid, f"Unblocked user {user_to_manage}."); await query.answer("کاربر رفع انسداد شد.")
            else: await query.answer("عملیات ناموفق بود.", show_alert=True)
            await manage_blacklist_menu(update, context)
        elif data.startswith("access_"):
            action, target_user_id = data.split("_")[1], int(data.split("_")[2])
            if action == "approve":
                add_user(target_user_id); log_action(uid, f"Approved access for {target_user_id}.")
                await context.bot.send_message(chat_id=target_user_id, text="✅ درخواست دسترسی شما توسط مدیر تایید شد. برای شروع /start را بزنید."); await query.answer("دسترسی تایید شد.")
            elif action == "reject":
                log_action(uid, f"Rejected access for {target_user_id}.")
                await context.bot.send_message(chat_id=target_user_id, text="❌ درخواست دسترسی شما توسط مدیر رد شد."); await query.answer("درخواست رد شد.")
            elif action == "block":
                block_user(target_user_id); log_action(uid, f"Blocked user {target_user_id}."); await query.answer("کاربر مسدود شد.")
            remove_request(target_user_id)
            await manage_requests_menu(update, context)
        elif data == "add_user_prompt":
            user_state[uid]['mode'] = State.ADDING_USER
            await query.message.edit_text("لطفاً شناسه عددی (ID) کاربر را ارسال کنید...", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="manage_whitelist")]]))
        return

    state = user_state.get(uid, {}); zone_id = state.get("zone_id")
    if data == "noop": return
    if data in ["back_to_main", "refresh_domains"]: await show_main_menu(update, context)
    elif data == "delete_domain_menu": await show_delete_domain_menu(update, context)
    elif data == "back_to_records" or data == "refresh_records": await show_records_list(update, context)
    elif data == "show_help": await show_help(update, context)
    elif data == "show_logs": await show_logs(update, context)
    elif data == "cancel_action":
        reset_user_state(uid, keep_zone=True); await query.message.edit_text("❌ عملیات لغو شد."); await show_records_list(update, context)
    elif data.startswith("zone_"):
        selected_zone_id = data.split("_")[1]; zone_info = get_zone_info_by_id(selected_zone_id)
        if zone_info:
            user_state[uid].update({"zone_id": selected_zone_id, "zone_name": zone_info["name"]}); await show_records_list(update, context)
    elif data.startswith("record_settings_"):
        await show_record_settings(query.message, uid, zone_id, data.split("_")[-1])
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
            [InlineKeyboardButton("Auto", callback_data=f"update_ttl_{record_id}_1"), InlineKeyboardButton("2 min", callback_data=f"update_ttl_{record_id}_120")],
            [InlineKeyboardButton("5 min", callback_data=f"update_ttl_{record_id}_300"), InlineKeyboardButton("10 min", callback_data=f"update_ttl_{record_id}_600")],
            [InlineKeyboardButton("1 hr", callback_data=f"update_ttl_{record_id}_3600"), InlineKeyboardButton("1 day", callback_data=f"update_ttl_{record_id}_86400")],
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
        await query.message.edit_text("📌 مرحله ۵ از ۵: آیا پروکسی فعال باشد؟ (فقط برای رکوردهای A, AAAA, CNAME اعمال می‌شود)", reply_markup=InlineKeyboardMarkup(keyboard))
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
            if record_details:
                log_action(uid, f"DELETE record '{record_details.get('name', 'N/A')}' with content '{record_details.get('content', 'N/A')}'")
            else:
                log_action(uid, f"DELETE record with ID '{record_id}' (details not found).")
            await query.message.edit_text("✅ رکورد حذف شد.")
        else: await query.message.edit_text("❌ حذف رکورد ناموفق بود.")
        await show_records_list(update, context)

def main():
    load_users(); load_blocked_users(); load_requests()
    logger.info("Starting bot...")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("logs", show_logs))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == "__main__":
    main()
