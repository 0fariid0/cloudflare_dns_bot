import logging
import json
import re
from collections import defaultdict
from enum import Enum, auto
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters)

# --- START: Mock API and Config (برای تست) ---
# این بخش را با فایل‌های اصلی خود جایگزین کنید یا مقادیر صحیح را وارد نمایید
try:
    from cloudflare_api import *
    from config import BOT_TOKEN, ADMIN_ID
except ImportError:
    BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"  # << توکن ربات خود را اینجا وارد کنید
    ADMIN_ID = 123456789             # << شناسه عددی ادمین اصلی را اینجا وارد کنید
    MOCKED_ZONES = {
        "zone1": {"id": "zone1", "name": "example.com", "status": "active"},
        "zone2": {"id": "zone2", "name": "mysite.org", "status": "active"}
    }
    def get_zones(): return list(MOCKED_ZONES.values())
    def get_dns_records(zone_id): return [{"id": "rec1", "type": "A", "name": "test.example.com", "content": "1.1.1.1"}]
    def get_record_details(zone_id, record_id): return {"id": "rec1", "name": "test.example.com", "type": "A", "content": "1.1.1.1", "ttl": 1, "proxied": True}
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
# --- END: Mock API and Config ---

# --- Setup ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# --- File Paths ---
USER_FILE = "users.json"
LOG_FILE = "bot_audit.log"
BLOCKED_USER_FILE = "blocked_users.json"
REQUEST_FILE = "access_requests.json" # NEW: File for pending requests

user_state = defaultdict(dict)

class State(Enum):
    NONE, ADDING_USER, ADDING_RECORD_NAME, ADDING_RECORD_CONTENT, EDITING_IP, EDITING_TTL, CLONING_NEW_IP = auto(), auto(), auto(), auto(), auto(), auto(), auto()

# --- START: Data Management Functions (Expanded & Corrected) ---
def log_action(user_id: int, action: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] User: {user_id} | Action: {action}\n"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(log_entry)
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

# --- Whitelist Management ---
def load_users():
    users = load_data(USER_FILE, {"authorized_ids": []})
    if ADMIN_ID not in users["authorized_ids"]:
        users["authorized_ids"].append(ADMIN_ID)
        save_data(USER_FILE, users)
    return users["authorized_ids"]

def save_users(user_list):
    save_data(USER_FILE, {"authorized_ids": sorted(list(set(user_list)))})

def is_user_authorized(user_id):
    return user_id in load_users()

def add_user(user_id):
    users = load_users()
    if user_id not in users:
        users.append(user_id)
        save_users(users)
        unblock_user(user_id)
        return True
    return False

def remove_user(user_id):
    if user_id == ADMIN_ID: return False
    users = load_users()
    if user_id in users:
        users.remove(user_id)
        save_users(users)
        return True
    return False

# --- Blacklist Management ---
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
        remove_user(user_id) # Also remove from authorized list
        return True
    return False

def unblock_user(user_id):
    blocked = load_blocked_users()
    if user_id in blocked:
        blocked.remove(user_id)
        save_blocked_users(blocked)
        return True
    return False

# --- Access Request Management ---
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
# --- END: Data Management Functions ---

def reset_user_state(uid, keep_zone=False):
    current_state = user_state.get(uid, {})
    if keep_zone:
        zone_id = current_state.get("zone_id")
        zone_name = current_state.get("zone_name")
        user_state[uid] = {"zone_id": zone_id, "zone_name": zone_name}
    else:
        user_state.pop(uid, None)

# --- START: UI and Menu Functions ---

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    reset_user_state(user_id)
    try:
        zones = get_zones()
    except Exception as e:
        logger.error(f"Could not fetch zones: {e}")
        await update.effective_message.reply_text("❌ خطا در ارتباط با Cloudflare.")
        return

    keyboard = []
    for zone in zones:
        status_icon = "✅" if zone["status"] == "active" else "⏳"
        keyboard.append([InlineKeyboardButton(f"{zone['name']} {status_icon}", callback_data=f"zone_{zone['id']}")])
    
    action_buttons = [
        InlineKeyboardButton("🔄 رفرش", callback_data="refresh_domains"),
        InlineKeyboardButton("🗑️ حذف دامنه", callback_data="delete_domain_menu")
    ]
    if user_id == ADMIN_ID:
        action_buttons.append(InlineKeyboardButton("👥 مدیریت کاربران", callback_data="manage_users"))
    
    action_buttons.extend([
        InlineKeyboardButton("📜 نمایش لاگ‌ها", callback_data="show_logs"),
        InlineKeyboardButton("ℹ️ راهنما", callback_data="show_help")
    ])

    for i in range(0, len(action_buttons), 2):
        keyboard.append(action_buttons[i:i + 2])

    welcome_text = "👋 به ربات مدیریت DNS خوش آمدید!\n\n🌐 برای مدیریت رکوردها، دامنه خود را انتخاب کنید:"
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
    await update.effective_message.edit_text(
        " لطفا بخش مورد نظر برای مدیریت کاربران را انتخاب کنید:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def manage_whitelist_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users = load_users()
    keyboard = []
    text = "👤 *لیست کاربران مجاز (Whitelist):*\n\n"
    for uid in users:
        user_text = f"`{uid}`"
        if uid == ADMIN_ID:
            user_text += " (ادمین اصلی)"
            keyboard.append([InlineKeyboardButton(user_text, callback_data="noop")])
        else:
            buttons = [
                InlineKeyboardButton("🗑 حذف", callback_data=f"delete_user_{uid}"),
                InlineKeyboardButton("🚫 بلاک", callback_data=f"block_user_{uid}")
            ]
            keyboard.append([InlineKeyboardButton(user_text, callback_data="noop")] + buttons)
    
    keyboard.append([InlineKeyboardButton("➕ افزودن کاربر جدید", callback_data="add_user_prompt")])
    keyboard.append([InlineKeyboardButton("🔙 بازگشت", callback_data="manage_users")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.callback_query:
        await update.effective_message.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await update.effective_message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")


async def manage_blacklist_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    blocked_users = load_blocked_users()
    keyboard = []
    text = "🚫 *لیست کاربران مسدود (Blacklist):*\n\n"
    if not blocked_users:
        text += "لیست کاربران مسدود خالی است."
    else:
        for uid in blocked_users:
            keyboard.append([
                InlineKeyboardButton(f"`{uid}`", callback_data="noop"),
                InlineKeyboardButton("✅ رفع انسداد", callback_data=f"unblock_user_{uid}")
            ])
    
    keyboard.append([InlineKeyboardButton("🔙 بازگشت", callback_data="manage_users")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.callback_query:
        await update.effective_message.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await update.effective_message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

async def manage_requests_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    requests = load_requests()
    keyboard = []
    text = "📨 *لیست درخواست‌های در انتظار:*\n\n"
    if not requests:
        text += "هیچ درخواست جدیدی وجود ندارد."
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
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.callback_query:
        await update.effective_message.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await update.effective_message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

async def show_delete_domain_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    zones = get_zones()
    if not zones:
        await update.effective_message.edit_text("هیچ دامنه‌ای برای حذف یافت نشد.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_main")]]))
        return

    keyboard = [[InlineKeyboardButton(f"🗑️ {z['name']}", callback_data=f"confirm_delete_zone_{z['id']}")] for z in zones]
    keyboard.append([InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="back_to_main")])
    text = " لطفا دامنه‌ای که قصد حذف آن را دارید انتخاب کنید.\n\n**توجه:** این عمل غیرقابل بازگشت است!"
    await update.effective_message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def show_records_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid, state = update.effective_user.id, user_state.get(update.effective_user.id, {})
    zone_id, zone_name = state.get("zone_id"), state.get("zone_name", "")

    if not zone_id:
        await update.effective_message.edit_text("خطا: دامنه انتخاب نشده است.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("بازگشت", callback_data="back_to_main")]]))
        return

    records = get_dns_records(zone_id)
    text = f"📋 رکوردهای DNS دامنه: `{zone_name}`\n\n"
    keyboard = [[InlineKeyboardButton(rec["name"].replace(f".{zone_name}", "").replace(zone_name, "@"), callback_data="noop"), InlineKeyboardButton(f"{rec['content']} | ⚙️", callback_data=f"record_settings_{rec['id']}")] for rec in records if rec["type"] in ["A", "AAAA", "CNAME"]]
    
    keyboard.extend([
        [InlineKeyboardButton("➕ افزودن رکورد", callback_data="add_record")],
        [InlineKeyboardButton("🔄 رفرش", callback_data="refresh_records")],
        [InlineKeyboardButton("🔙 بازگشت به دامنه‌ها", callback_data="back_to_main")]
    ])
    await update.effective_message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def show_record_settings(update: Update, context: ContextTypes.DEFAULT_TYPE, record_id: str):
    uid, state = update.effective_user.id, user_state.get(update.effective_user.id)
    zone_id = state.get("zone_id")
    record = get_record_details(zone_id, record_id)

    if not record:
        await update.effective_message.edit_text("❌ رکورد یافت نشد.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("بازگشت", callback_data="back_to_records")]]))
        return

    state["record_id"] = record_id
    proxied_status = '✅ فعال' if record.get('proxied') else '❌ غیرفعال'
    text = (f"⚙️ تنظیمات رکورد: `{record['name']}`\n"
            f"**Type:** `{record['type']}`\n"
            f"**IP:** `{record['content']}`\n"
            f"**TTL:** `{record['ttl']}`\n"
            f"**Proxied:** {proxied_status}")

    keyboard = [
        [InlineKeyboardButton("🖊 تغییر IP", callback_data=f"editip_{record_id}"), InlineKeyboardButton("🕒 تغییر TTL", callback_data=f"edittll_{record_id}")],
        [InlineKeyboardButton("🔁 پروکسی", callback_data=f"toggle_proxy_{record_id}"), InlineKeyboardButton("🗑️ حذف", callback_data=f"confirm_delete_record_{record_id}")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_records")]
    ]
    if record['type'] == 'A':
        keyboard.insert(2, [InlineKeyboardButton("🐑 کلون", callback_data=f"clone_record_{record_id}")])

    await update.effective_message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # This function is complete and correct from your previous code
    pass

async def show_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # This function is complete and correct from your previous code
    pass
# --- END: UI and Menu Functions ---

# --- START: Access Request Flow ---
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
# --- END: Access Request Flow ---

# --- START: Handlers ---
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
    # ... Other message handlers for DNS management ...
    # (The rest of your handle_message logic remains here)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    data = query.data

    if is_user_blocked(uid): return

    # --- Unauthorized Flow ---
    if data == "request_access":
        await handle_unauthorized_access_request(update, context)
        return

    # --- Authorization Check for all other actions ---
    if not is_user_authorized(uid):
        await show_request_access_menu(update, context)
        return
        
    # --- Admin-only User Management Routing ---
    if data.startswith(('manage_', 'delete_user_', 'block_user_', 'unblock_user_', 'access_', 'add_user_prompt')):
        if uid != ADMIN_ID:
            await query.answer("شما اجازه دسترسی به این بخش را ندارید.", show_alert=True)
            return

        if data == "manage_users": await manage_users_main_menu(update, context)
        elif data == "manage_whitelist": await manage_whitelist_menu(update, context)
        elif data == "manage_blacklist": await manage_blacklist_menu(update, context)
        elif data == "manage_requests": await manage_requests_menu(update, context)
        elif data.startswith("delete_user_"):
            user_to_manage = int(data.split("_")[2])
            if remove_user(user_to_manage):
                log_action(uid, f"Removed user {user_to_manage} from whitelist.")
                await query.answer("کاربر از لیست مجاز حذف شد.")
            else: await query.answer("عملیات ناموفق بود.", show_alert=True)
            await manage_whitelist_menu(update, context)
        elif data.startswith("block_user_"):
            user_to_manage = int(data.split("_")[2])
            if block_user(user_to_manage):
                log_action(uid, f"Blocked user {user_to_manage}.")
                await query.answer("کاربر مسدود شد.")
            else: await query.answer("عملیات ناموفق بود.", show_alert=True)
            await manage_whitelist_menu(update, context)
        elif data.startswith("unblock_user_"):
            user_to_manage = int(data.split("_")[2])
            if unblock_user(user_to_manage):
                log_action(uid, f"Unblocked user {user_to_manage}.")
                await query.answer("کاربر رفع انسداد شد.")
            else: await query.answer("عملیات ناموفق بود.", show_alert=True)
            await manage_blacklist_menu(update, context)
        elif data.startswith("access_"):
            action, target_user_id = data.split("_")[1], int(data.split("_")[2])
            if action == "approve":
                add_user(target_user_id); log_action(uid, f"Approved access for {target_user_id}.")
                await context.bot.send_message(chat_id=target_user_id, text="✅ درخواست دسترسی شما توسط مدیر تایید شد.")
                await query.answer("دسترسی تایید شد.")
            elif action == "reject":
                log_action(uid, f"Rejected access for {target_user_id}.")
                await context.bot.send_message(chat_id=target_user_id, text="❌ درخواست دسترسی شما توسط مدیر رد شد.")
                await query.answer("درخواست رد شد.")
            elif action == "block":
                block_user(target_user_id); log_action(uid, f"Blocked user {target_user_id} from requests.")
                await query.answer("کاربر مسدود شد.")
            remove_request(target_user_id)
            await manage_requests_menu(update, context)
        elif data == "add_user_prompt":
            user_state[uid]['mode'] = State.ADDING_USER
            text = "لطفاً شناسه عددی (ID) کاربر مورد نظر را برای افزودن به لیست مجاز ارسال کنید..."
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="manage_whitelist")]]))
        return

    # --- General & DNS Management ---
    if data in ["back_to_main", "refresh_domains"]: await show_main_menu(update, context)
    elif data == "delete_domain_menu": await show_delete_domain_menu(update, context)
    elif data == "back_to_records" or data == "refresh_records": await show_records_list(update, context)
    elif data.startswith("zone_"):
        zone_id = data.split("_")[1]
        zone_info = get_zone_info_by_id(zone_id)
        if zone_info:
            user_state[uid].update({"zone_id": zone_id, "zone_name": zone_info["name"]})
            await show_records_list(update, context)
    elif data.startswith("record_settings_"):
        await show_record_settings(update, context, data.split("_")[-1])
    elif data.startswith("confirm_delete_"):
        parts, item_type, item_id = data.split('_'), data.split('_')[2], data.split('_')[-1]
        back_action = "delete_domain_menu" if item_type == "zone" else f"record_settings_{item_id}"
        text = f"آیا از حذف این {'دامنه' if item_type == 'zone' else 'رکورد'} مطمئن هستید؟"
        keyboard = [[InlineKeyboardButton("✅ بله، حذف شود", callback_data=f"delete_{item_type}_{item_id}")], [InlineKeyboardButton("❌ خیر، لغو", callback_data=back_action)]]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    elif data.startswith("delete_zone_"):
        zone_id = data.split("_")[-1]
        zone_name = get_zone_info_by_id(zone_id)['name']
        await query.message.edit_text(f"⏳ در حال حذف دامنه {zone_name}...")
        if delete_zone(zone_id):
            log_action(uid, f"DELETED ZONE: '{zone_name}'")
            await query.message.edit_text("✅ دامنه با موفقیت حذف شد.")
        else: await query.message.edit_text("❌ حذف دامنه ناموفق بود.")
        await show_main_menu(update, context)
    elif data.startswith("delete_record_"):
        record_id = data.split("_")[-1]
        zone_id = user_state.get(uid, {}).get("zone_id")
        await query.message.edit_text("⏳ در حال حذف رکورد...")
        if delete_dns_record(zone_id, record_id):
            await query.message.edit_text("✅ رکورد حذف شد.")
        else: await query.message.edit_text("❌ حذف رکورد ناموفق بود.")
        await show_records_list(update, context)
    # ... (the rest of your DNS management callbacks) ...

# --- Main Application ---
def main():
    load_users(); load_blocked_users(); load_requests()
    logger.info("Starting bot...")
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_command))
    # app.add_handler(CommandHandler("logs", show_logs)) # Add back if you have the function
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    app.run_polling()

if __name__ == "__main__":
    main()
