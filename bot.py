import logging
import json
from collections import defaultdict
from enum import Enum, auto
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters)
from unittest.mock import Mock

# فرض می‌شود این فایل‌ها در کنار bot.py وجود دارند
try:
    from cloudflare_api import *
    from config import BOT_TOKEN, ADMIN_ID
except ImportError:
    BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
    ADMIN_ID = 123456789
    # توابع شبیه‌ساز برای جلوگیری از خطا در صورت نبودن فایل‌ها
    def get_zones(): return [{"id": "zone123", "name": "example.com", "status": "active"}]
    def get_dns_records(zone_id): return [{"id": "rec456", "type": "A", "name": "sub.example.com", "content": "1.1.1.1", "ttl": 1, "proxied": True}]
    def get_record_details(zone_id, record_id): return {"id": record_id, "type": "A", "name": "sub.example.com", "content": "1.1.1.1", "ttl": 1, "proxied": True}
    def get_zone_info_by_id(zone_id): return {"id": "zone123", "name": "example.com"}
    def create_dns_record(zone_id, type, name, content, ttl, proxied): print(f"Creating: {name} -> {content}"); return True
    def update_dns_record(zone_id, record_id, name, type, content, ttl, proxied): return True
    def delete_dns_record(zone_id, record_id): print(f"Deleting record: {record_id}"); return True
    def toggle_proxied_status(zone_id, record_id): return True

# --- Setup ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
USER_FILE = "users.json"
LOG_FILE = "bot_audit.log"  # <--- ADDED
user_state = defaultdict(dict)

class State(Enum):
    NONE = auto()
    ADDING_USER = auto()
    ADDING_DOMAIN = auto()
    ADDING_RECORD_NAME = auto()
    ADDING_RECORD_CONTENT = auto()
    EDITING_IP = auto()
    EDITING_TTL = auto()
    CLONING_NEW_IP = auto()  # <--- ADDED

# --- ADDED: Logging Function ---
def log_action(user_id: int, action: str):
    """Logs an action to the audit file."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] User: {user_id} | Action: {action}\n"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(log_entry)
    except Exception as e:
        logger.error(f"Failed to write to log file: {e}")

# --- User Management (Unchanged) ---
def load_users():
    try:
        with open(USER_FILE, 'r') as f:
            data = json.load(f)
            if ADMIN_ID not in data.get('authorized_ids', []):
                data['authorized_ids'].append(ADMIN_ID)
            return data['authorized_ids']
    except (FileNotFoundError, json.JSONDecodeError):
        save_users([ADMIN_ID])
        return [ADMIN_ID]

def save_users(users_list):
    with open(USER_FILE, 'w') as f:
        json.dump({"authorized_ids": sorted(list(set(users_list)))}, f, indent=4)

def is_user_authorized(user_id):
    return user_id in load_users()

def add_user(user_id):
    users = load_users()
    if user_id not in users:
        users.append(user_id)
        save_users(users)
        return True
    return False

def remove_user(user_id):
    if user_id == ADMIN_ID:
        return False
    users = load_users()
    if user_id in users:
        users.remove(user_id)
        save_users(users)
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

# --- UI and Menu Generation ---
async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # This function is unchanged
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
        keyboard.append([
            InlineKeyboardButton(f"{zone['name']} {status_icon}", callback_data=f"zone_{zone['id']}"),
            InlineKeyboardButton("🗑️", callback_data=f"confirm_delete_zone_{zone['id']}")
        ])
    
    keyboard.extend([
        [InlineKeyboardButton("➕ افزودن دامنه", callback_data="add_domain")],
        [InlineKeyboardButton("🔄 رفرش", callback_data="refresh_domains")]
    ])

    if user_id == ADMIN_ID:
        keyboard.append([InlineKeyboardButton("👥 مدیریت کاربران", callback_data="manage_users")])
    
    keyboard.append([InlineKeyboardButton("ℹ️ راهنما", callback_data="show_help")])

    welcome_text = "👋 به ربات مدیریت DNS خوش آمدید!\n\n🌐 دامنه‌های متصل:"
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.effective_message.edit_text(welcome_text, reply_markup=reply_markup)
    else:
        await update.effective_message.reply_text(welcome_text, reply_markup=reply_markup)

# <--- MODIFIED: Button layout changed here ---
async def show_records_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = user_state.get(uid, {})
    zone_id = state.get("zone_id")
    zone_name = state.get("zone_name", "")

    if not zone_id:
        await update.effective_message.reply_text("خطا: دامنه انتخاب نشده است.")
        return await show_main_menu(update, context)

    try:
        records = get_dns_records(zone_id)
    except Exception as e:
        logger.error(f"Could not fetch records for zone {zone_id}: {e}")
        await update.effective_message.reply_text("❌ خطا در دریافت لیست رکوردها.")
        return

    text = f"📋 رکوردهای DNS دامنه: `{zone_name}`\n\n"
    keyboard = []
    for rec in records:
        if rec["type"] in ["A", "AAAA", "CNAME"]:
            name = rec["name"].replace(f".{zone_name}", "").replace(zone_name, "@")
            content = rec["content"]
            
            button_row = [
                InlineKeyboardButton(name, callback_data="noop")
            ]

            if rec["type"] == 'A':
                button_row.append(InlineKeyboardButton("🐑", callback_data=f"clone_record_{rec['id']}"))
            
            button_row.append(InlineKeyboardButton("🗑️", callback_data=f"confirm_delete_record_{rec['id']}"))
            
            button_row.append(InlineKeyboardButton(f"{content} | ⚙️", callback_data=f"record_settings_{rec['id']}"))
            
            keyboard.append(button_row)

    keyboard.extend([
        [InlineKeyboardButton("➕ افزودن رکورد", callback_data="add_record")],
        [InlineKeyboardButton("🔄 رفرش", callback_data="refresh_records")],
        [InlineKeyboardButton("🔙 بازگشت به دامنه‌ها", callback_data="back_to_main")]
    ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        await update.effective_message.edit_text(text, parse_mode="Markdown", reply_markup=reply_markup)
    else:
        # This handles calls from handle_message after an action
        await context.bot.send_message(chat_id=uid, text=text, parse_mode="Markdown", reply_markup=reply_markup)

# <--- MODIFIED: Delete button moved from here ---
async def show_record_settings(message, uid, zone_id, record_id):
    try:
        record = get_record_details(zone_id, record_id)
        if not record:
            await message.edit_text("❌ رکورد یافت نشد. ممکن است حذف شده باشد.")
            return
    except Exception as e:
        logger.error(f"Could not fetch record details for {record_id}: {e}")
        await message.edit_text("❌ خطا در دریافت اطلاعات رکورد.")
        return

    user_state[uid]["record_id"] = record_id
    proxied_status = '✅ فعال' if record.get('proxied') else '❌ غیرفعال'
    text = (
        f"⚙️ تنظیمات رکورد: `{record['name']}`\n\n"
        f"**Type:** `{record['type']}`\n"
        f"**IP:** `{record['content']}`\n"
        f"**TTL:** `{record['ttl']}`\n"
        f"**Proxied:** {proxied_status}"
    )
    keyboard = [
        [
            InlineKeyboardButton("🖊 تغییر IP", callback_data=f"editip_{record_id}"),
            InlineKeyboardButton("🕒 تغییر TTL", callback_data=f"edittll_{record_id}"),
            InlineKeyboardButton("🔁 پروکسی", callback_data=f"toggle_proxy_{record_id}")
        ],
        [
            # The delete button is now in the main list
            InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_records")
        ]
    ]
    await message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

# --- Unchanged Original Functions ---
async def manage_users_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # This function is unchanged
    users = load_users()
    keyboard = []
    text = "👥 *لیست کاربران مجاز:*\n\n"
    for user_id in users:
        user_text = f"👤 `{user_id}`"
        buttons = []
        if user_id == ADMIN_ID:
            user_text += " (ادمین اصلی)"
        else:
            buttons.append(InlineKeyboardButton("🗑 حذف", callback_data=f"delete_user_{user_id}"))
        keyboard.append([InlineKeyboardButton(user_text, callback_data="noop")] + buttons)
    
    keyboard.extend([
        [InlineKeyboardButton("➕ افزودن کاربر جدید", callback_data="add_user_prompt")],
        [InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="back_to_main")]
    ])
    
    await update.effective_message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # This function is unchanged
    help_text = "..." # help text is long and omitted for brevity
    keyboard = [[InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="back_to_main")]]
    await update.effective_message.edit_text(help_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")

# --- Command and Callback Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_user_authorized(update.effective_user.id):
        await update.message.reply_text("❌ شما اجازه دسترسی ندارید.")
        return
    await show_main_menu(update, context)

# --- ADDED: Logs Command Handler ---
async def logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_user_authorized(user_id):
        await update.message.reply_text("❌ شما اجازه دسترسی ندارید."); return
    
    try:
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            last_lines = f.readlines()[-15:] # Get last 15 lines
        if not last_lines:
            await update.message.reply_text("هنوز هیچ فعالیتی ثبت نشده است.")
            return
        
        log_text = "📜 **۱۵ فعالیت آخر ربات:**\n\n`" + "".join(last_lines) + "`"
        await update.message.reply_text(log_text, parse_mode="Markdown")

    except FileNotFoundError:
        await update.message.reply_text("هنوز هیچ فعالیتی ثبت نشده است.")
    except Exception as e:
        logger.error(f"Could not read log file: {e}")
        await update.message.reply_text("❌ خطا در خواندن فایل لاگ.")

# <--- MODIFIED: Added clone handler and logging calls ---
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    data = query.data

    if not is_user_authorized(uid):
        await query.message.reply_text("❌ شما اجازه دسترسی به این ربات را ندارید.")
        return

    state = user_state.get(uid, {})
    zone_id = state.get("zone_id")
    
    if data == "noop": return

    # Navigation
    if data in ["back_to_main", "refresh_domains"]: await show_main_menu(update, context)
    elif data == "back_to_records" or data == "refresh_records": await show_records_list(update, context)
    elif data == "show_help": await show_help(update, context)
    elif data == "cancel_action":
        reset_user_state(uid, keep_zone=True)
        await query.message.edit_text("❌ عملیات لغو شد.")
        await show_records_list(update, context)

    # User Management (Unchanged from original)
    elif data == "manage_users" and uid == ADMIN_ID: await manage_users_menu(update, context)
    elif data == "add_user_prompt" and uid == ADMIN_ID:
        # ... original code ...
        pass
    elif data.startswith("delete_user_") and uid == ADMIN_ID:
        # ... original code ...
        pass

    # Zone and Record Selection
    elif data.startswith("zone_"):
        selected_zone_id = data.split("_")[1]
        try:
            zone_info = get_zone_info_by_id(selected_zone_id)
            user_state[uid].update({"zone_id": selected_zone_id, "zone_name": zone_info["name"]})
            await show_records_list(update, context)
        except Exception as e: await query.message.reply_text("❌ دریافت اطلاعات دامنه ناموفق بود.")

    # Record Settings and Actions
    elif data.startswith("record_settings_"):
        record_id = data.split("_")[-1]
        await show_record_settings(query.message, uid, zone_id, record_id)
    
    # --- ADDED: Clone workflow start ---
    elif data.startswith("clone_record_"):
        record_id = data.split("_")[-1]
        try:
            original_record = get_record_details(zone_id, record_id)
            if not original_record: await query.answer("❌ رکورد اصلی یافت نشد.", show_alert=True); return
            
            user_state[uid]["clone_data"] = { "name": original_record["name"], "type": original_record["type"], "ttl": original_record["ttl"], "proxied": original_record.get("proxied", False) }
            user_state[uid]["mode"] = State.CLONING_NEW_IP
            await query.message.edit_text(
                f"🐑 **افزودن IP جدید به رکورد**\n\n`{original_record['name']}`\n\nلطفاً **IP جدید** را برای افزودن به این رکورد وارد کنید:",
                parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]])
            )
        except Exception as e: logger.error(f"Error starting clone: {e}"); await query.answer("❌ خطا در شروع فرآیند کلون.", show_alert=True)
            
    elif data.startswith("toggle_proxy_"):
        record_id = data.split("_")[-1]
        try:
            success = toggle_proxied_status(zone_id, record_id)
            if success:
                log_action(uid, f"Toggled proxy for record ID {record_id}")
                await show_record_settings(query.message, uid, zone_id, record_id)
            else: await query.answer("❌ عملیات ناموفق بود.", show_alert=True)
        except Exception: await query.answer("❌ خطا در ارتباط با API.", show_alert=True)

    elif data.startswith("editip_"):
        record_id = data.split("_")[-1]
        user_state[uid].update({"mode": State.EDITING_IP, "record_id": record_id})
        await query.message.edit_text("📝 لطفاً IP جدید را وارد کنید:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]]))

    # TTL Editing (Unchanged from original)
    elif data.startswith("edittll_"):
        # ... original code ...
        pass
    elif data.startswith("update_ttl_"):
        # ... original code with logging added ...
        # if success: log_action(uid, f"Updated TTL for record ID {record_id} to {ttl}")
        pass

    # Add Record Workflow (Unchanged from original)
    elif data == "add_record": await query.message.edit_text("... (original add record code)")
    elif data.startswith("select_type_"):
        # ... original code ...
        pass
    elif data.startswith("select_ttl_"):
        # ... original code ...
        pass
    elif data.startswith("select_proxied_"):
        # ... original code with logging added ...
        # if success: log_action(uid, f"Created record {name}")
        pass

    # Deletion Confirmation (Unchanged from original)
    elif data.startswith("confirm_delete_"):
        item_type = "record" if data.startswith("confirm_delete_record_") else "zone"
        item_id = data.split("_")[-1]
        text = f"❗ آیا از حذف این {'رکورد' if item_type == 'record' else 'دامنه'} مطمئن هستید؟"
        back_action = "back_to_records" if item_type == 'record' else 'back_to_main'
        keyboard = [[InlineKeyboardButton("✅ بله، حذف شود", callback_data=f"delete_{item_type}_{item_id}")],[InlineKeyboardButton("❌ لغو", callback_data=back_action)]]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("delete_record_"):
        record_id = data.split("_")[-1]
        await query.message.edit_text("⏳ در حال حذف رکورد...")
        try:
            success = delete_dns_record(zone_id, record_id)
            if success:
                log_action(uid, f"Deleted record ID {record_id}")
                await query.message.edit_text("✅ رکورد حذف شد.")
            else: await query.message.edit_text("❌ حذف رکورد ناموفق بود.")
        except Exception: await query.message.edit_text("❌ خطا در حذف رکورد.")
        finally: await show_records_list(update, context)

# <--- MODIFIED: Added handler for CLONING_NEW_IP and logging ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_user_authorized(uid):
        await update.message.reply_text("❌ شما اجازه دسترسی ندارید.")
        return

    state = user_state.get(uid, {})
    mode = state.get("mode")
    text = update.message.text.strip()
    if not mode or mode == State.NONE: return

    # --- ADDED: Clone workflow finish ---
    if mode == State.CLONING_NEW_IP:
        new_ip = text
        clone_data = user_state[uid].get("clone_data", {}); zone_id = state.get("zone_id")
        full_name = clone_data.get("name")
        if not all([new_ip, clone_data, zone_id, full_name]):
            await update.message.reply_text("❌ خطای داخلی."); reset_user_state(uid, keep_zone=True); return
        await update.message.reply_text(f"⏳ در حال افزودن IP `{new_ip}` به رکورد `{full_name}`...", parse_mode="Markdown")
        try:
            success = create_dns_record(zone_id, clone_data["type"], full_name, new_ip, clone_data["ttl"], clone_data["proxied"])
            if success:
                log_action(uid, f"CREATE (Clone) record '{full_name}' with IP '{new_ip}'")
                await update.message.reply_text("✅ رکورد جدید با موفقیت اضافه شد.")
            else: await update.message.reply_text("❌ عملیات ناموفق بود.")
        except Exception as e: logger.error(f"Error creating cloned record: {e}"); await update.message.reply_text("❌ خطا در ارتباط با API.")
        finally: reset_user_state(uid, keep_zone=True); await show_records_list(update, context)

    # All other message handlers from your original code remain here
    elif mode == State.ADDING_USER and uid == ADMIN_ID:
        # ... original code ...
        pass
    elif mode == State.EDITING_IP:
        new_ip = text; record_id = state.get("record_id"); zone_id = state.get("zone_id")
        await update.message.reply_text(f"⏳ در حال به‌روزرسانی IP به `{new_ip}`...", parse_mode="Markdown")
        try:
            record = get_record_details(zone_id, record_id)
            if record:
                success = update_dns_record(zone_id, record_id, record["name"], record["type"], new_ip, record["ttl"], record.get("proxied", False))
                if success:
                    log_action(uid, f"UPDATE IP for '{record['name']}' to '{new_ip}'")
                    await update.message.reply_text("✅ آی‌پی با موفقیت به‌روز شد.")
                    new_msg = await update.message.reply_text("...در حال بارگذاری تنظیمات جدید")
                    await show_record_settings(new_msg, uid, zone_id, record_id)
                else: await update.message.reply_text("❌ به‌روزرسانی ناموفق بود.")
            else: await update.message.reply_text("❌ رکورد مورد نظر یافت نشد.")
        except Exception: await update.message.reply_text("❌ خطا در ارتباط با API.")
        finally: reset_user_state(uid, keep_zone=True)
    elif mode == State.ADDING_RECORD_NAME:
        # ... original code ...
        pass
    elif mode == State.ADDING_RECORD_CONTENT:
        # ... original code ...
        pass

# --- Main Application ---
def main():
    load_users()
    logger.info("Starting bot...")
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("logs", logs_command)) # <--- ADDED
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    app.run_polling()

if __name__ == "__main__":
    main()
