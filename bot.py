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
    # توابع شبیه‌ساز برای جلوگیری از خطا
    def get_zones(): return []
    def get_dns_records(zone_id): return []
    def get_record_details(zone_id, record_id): return None
    def get_zone_info_by_id(zone_id): return None
    def create_dns_record(zone_id, type, name, content, ttl, proxied): return True
    def update_dns_record(zone_id, record_id, name, type, content, ttl, proxied): return True
    def delete_dns_record(zone_id, record_id): return True
    def toggle_proxied_status(zone_id, record_id): return True

# --- Setup ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
USER_FILE = "users.json"
LOG_FILE = "bot_audit.log" # <--- ADDED: Log file name
user_state = defaultdict(dict)

class State(Enum):
    NONE = auto()
    ADDING_USER = auto()
    EDITING_IP = auto()
    CLONING_NEW_IP = auto()

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
        with open(USER_FILE, 'r') as f: data = json.load(f)
        if ADMIN_ID not in data.get('authorized_ids', []): data['authorized_ids'].append(ADMIN_ID)
        return data['authorized_ids']
    except (FileNotFoundError, json.JSONDecodeError):
        save_users([ADMIN_ID]); return [ADMIN_ID]

def save_users(users_list):
    with open(USER_FILE, 'w') as f: json.dump({"authorized_ids": sorted(list(set(users_list)))}, f, indent=4)

def is_user_authorized(user_id): return user_id in load_users()

def reset_user_state(uid, keep_zone=False):
    # ... (code unchanged)
    pass

# --- UI and Menu Generation (Unchanged) ---
async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (code unchanged)
    pass

async def show_records_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (code unchanged from last version)
    pass

async def show_record_settings(message, uid, zone_id, record_id):
    # ... (code unchanged)
    pass

# --- Command & Callback Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_user_authorized(update.effective_user.id):
        await update.message.reply_text("❌ شما اجازه دسترسی ندارید."); return
    await update.message.reply_text("برای نمایش منوی اصلی، لطفاً /menu را ارسال کنید.")


# --- ADDED: Logs Command Handler ---
async def logs_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_user_authorized(user_id):
        await update.message.reply_text("❌ شما اجازه دسترسی ندارید."); return
    
    try:
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            # Read all lines and get the last 15
            last_lines = f.readlines()[-15:]
        if not last_lines:
            await update.message.reply_text("ยังไม่มีกิจกรรมที่บันทึกไว้")
            return
        
        log_text = "📜 **۱۵ فعالیت آخر ربات:**\n\n" + "".join(last_lines)
        await update.message.reply_text(log_text, parse_mode="Markdown")

    except FileNotFoundError:
        await update.message.reply_text("هنوز هیچ فعالیتی ثبت نشده است.")
    except Exception as e:
        logger.error(f"Could not read log file: {e}")
        await update.message.reply_text("❌ خطا در خواندن فایل لاگ.")


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler for the /menu command."""
    if not is_user_authorized(update.effective_user.id):
        await update.message.reply_text("❌ شما اجازه دسترسی ندارید."); return
    # We pass a non-callback query update to show_main_menu
    mock_update = Mock(callback_query=None, effective_message=update.message, effective_user=update.effective_user)
    await show_main_menu(mock_update, context)


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    uid = query.from_user.id; data = query.data
    if not is_user_authorized(uid): await query.message.reply_text("❌ شما اجازه دسترسی به این ربات را ندارید."); return

    state = user_state.get(uid, {}); zone_id = state.get("zone_id", "")

    # ... (navigation and other callbacks are the same)

    elif data.startswith("clone_record_"):
        # ... (code for starting clone is the same)
        pass
            
    elif data.startswith("editip_"):
        # ... (code for starting editip is the same)
        pass

    elif data.startswith("toggle_proxy_"):
        record_id = data.split("_")[-1]
        try:
            success = toggle_proxied_status(zone_id, record_id)
            if success:
                log_action(uid, f"Toggled proxy for record ID {record_id} in zone {zone_id}")
                await show_record_settings(query.message, uid, zone_id, record_id)
            else:
                await query.answer("❌ عملیات ناموفق بود", show_alert=True)
        except Exception: await query.answer("❌ خطا در ارتباط با API.", show_alert=True)
            
    elif data.startswith("confirm_delete_record_"):
        # ... (code is the same)
        pass
        
    elif data.startswith("delete_record_"):
        record_id = data.split("_")[-1]
        await query.message.edit_text("⏳ در حال حذف رکورد...")
        try:
            success = delete_dns_record(zone_id, record_id)
            if success:
                log_action(uid, f"Deleted record ID {record_id} from zone {zone_id}")
                await query.message.edit_text("✅ رکورد حذف شد.")
            else:
                await query.message.edit_text("❌ حذف رکورد ناموفق بود.")
        except Exception as e:
            logger.error(f"Error deleting record: {e}")
            await query.message.edit_text("❌ خطا در حذف رکورد.")
        finally:
            # Create a mock update to refresh the list via message
            mock_update = Mock(callback_query=None, effective_message=query.message, effective_user=query.from_user)
            await show_records_list(mock_update, context)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_user_authorized(uid): await update.message.reply_text("❌ شما اجازه دسترسی ندارید."); return

    state = user_state.get(uid, {}); mode = state.get("mode"); text = update.message.text.strip()
    if not mode or mode == State.NONE: return

    if mode == State.CLONING_NEW_IP:
        new_ip = text
        clone_data = user_state[uid].get("clone_data", {}); zone_id = state.get("zone_id")
        full_name = clone_data.get("name")
        if not all([new_ip, clone_data, zone_id, full_name]):
            # ... (error handling)
            return
        
        await update.message.reply_text(f"⏳ در حال افزودن IP `{new_ip}` به رکورد `{full_name}`...", parse_mode="Markdown")
        try:
            success, new_record_info = create_dns_record(
                zone_id, clone_data["type"], full_name, new_ip, clone_data["ttl"], clone_data["proxied"]
            )
            if success:
                log_action(uid, f"CREATE (Clone) record '{full_name}' with IP '{new_ip}'")
                await update.message.reply_text("✅ رکورد جدید با موفقیت اضافه شد.")
            else:
                await update.message.reply_text("❌ عملیات ناموفق بود.")
        except Exception as e: logger.error(f"Error creating cloned record: {e}"); await update.message.reply_text("❌ خطا در ارتباط با API.")
        finally:
            reset_user_state(uid, keep_zone=True); await show_records_list(update, context)

    elif mode == State.EDITING_IP:
        new_ip = text; record_id = state.get("record_id"); zone_id = state.get("zone_id")
        await update.message.reply_text(f"⏳ در حال به‌روزرسانی IP...", parse_mode="Markdown")
        try:
            record = get_record_details(zone_id, record_id)
            if record:
                success = update_dns_record(zone_id, record_id, record["name"], record["type"], new_ip, record["ttl"], record.get("proxied", False))
                if success:
                    log_action(uid, f"UPDATE IP for record '{record['name']}' to '{new_ip}'")
                    await update.message.reply_text("✅ آی‌پی با موفقیت به‌روز شد.")
                    # ... (show updated settings)
                else: await update.message.reply_text("❌ به‌روزرسانی ناموفق بود.")
            else: await update.message.reply_text("❌ رکورد مورد نظر یافت نشد.")
        except Exception as e: logger.error(f"Error updating IP: {e}"); await update.message.reply_text("❌ خطا در ارتباط با API.")
        finally: reset_user_state(uid, keep_zone=True)

# --- Main Application ---
def main():
    # ... (unchanged)
    app = Application.builder().token(BOT_TOKEN).build()
    
    # --- ADDED /logs and /menu handlers ---
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("logs", logs_command))
    app.add_handler(CommandHandler("menu", menu_command))

    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    app.run_polling()

if __name__ == "__main__":
    main()
