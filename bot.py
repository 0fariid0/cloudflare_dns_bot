import logging
import json
from collections import defaultdict
from enum import Enum, auto
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters)

# --- START: این بخش‌ها باید با اطلاعات واقعی شما پر شوند ---
try:
    from cloudflare_api import *
    from config import BOT_TOKEN, ADMIN_ID
except ImportError:
    BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
    ADMIN_ID = 123456789
    print("WARNING: 'config.py' or 'cloudflare_api.py' not found. Using placeholder values.")
    def get_zones(): return [{"id": "zone123", "name": "wolfnet-vip.site", "status": "active"}]
    def get_dns_records(zone_id): return [{"id": "rec456", "type": "A", "name": "wolf.wolfnet-vip.site", "content": "1.1.1.1", "ttl": 1, "proxied": True}]
    def get_record_details(zone_id, record_id): return {"id": "rec456", "type": "A", "name": "wolf.wolfnet-vip.site", "content": "1.1.1.1", "ttl": 1, "proxied": True}
    def get_zone_info_by_id(zone_id): return {"id": "zone123", "name": "wolfnet-vip.site"}
    def create_dns_record(zone_id, type, name, content, ttl, proxied): print(f"Creating: {name} -> {content}"); return True
    def update_dns_record(zone_id, record_id, name, type, content, ttl, proxied): return True
    def delete_dns_record(zone_id, record_id): return True
    def toggle_proxied_status(zone_id, record_id): return True
# --- END: ---

# --- Setup ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
USER_FILE = "users.json"
user_state = defaultdict(dict)

class State(Enum):
    NONE = auto()
    ADDING_USER = auto()
    EDITING_IP = auto()
    CLONING_NEW_IP = auto()

# --- User Management ---
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
    current_state = user_state.get(uid, {})
    if keep_zone:
        zone_id = current_state.get("zone_id"); zone_name = current_state.get("zone_name")
        user_state[uid] = {"zone_id": zone_id, "zone_name": zone_name}
    else: user_state.pop(uid, None)

# --- UI and Menu Generation ---
async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    reset_user_state(user_id)
    try: zones = get_zones()
    except Exception as e:
        logger.error(f"Could not fetch zones: {e}")
        text = "❌ خطا در ارتباط با Cloudflare."
        if update.callback_query: await update.effective_message.edit_text(text)
        else: await update.effective_message.reply_text(text)
        return

    keyboard = [[InlineKeyboardButton(f"{z['name']} {'✅' if z['status'] == 'active' else '⏳'}", callback_data=f"zone_{z['id']}")] for z in zones]
    keyboard.append([InlineKeyboardButton("🔄 رفرش", callback_data="refresh_domains")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    welcome_text = "👋 به ربات مدیریت DNS خوش آمدید!\n\n🌐 دامنه‌های متصل:"
    if update.callback_query: await update.effective_message.edit_text(welcome_text, reply_markup=reply_markup)
    else: await update.effective_message.reply_text(welcome_text, reply_markup=reply_markup)

# <--- THE ONLY MODIFIED FUNCTION ---
async def show_records_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = user_state.get(uid, {}); zone_id = state.get("zone_id"); zone_name = state.get("zone_name", "")
    if not zone_id:
        await update.effective_message.reply_text("خطا: دامنه انتخاب نشده است."); return await show_main_menu(update, context)

    try: records = get_dns_records(zone_id)
    except Exception as e:
        logger.error(f"Could not fetch records for zone {zone_id}: {e}"); await update.effective_message.reply_text("❌ خطا در دریافت لیست رکوردها."); return

    text = f"📋 رکوردهای DNS دامنه: `{zone_name}`"
    keyboard = []
    
    # Filter for relevant record types
    for rec in filter(lambda r: r["type"] in ["A", "AAAA", "CNAME"], records):
        name = rec["name"].replace(f".{zone_name}", "").replace(zone_name, "@")
        
        # --- Build a single row of buttons ---
        button_row = [
            InlineKeyboardButton(f"{name}: {rec['content']}", callback_data="noop")
        ]
        
        # Add clone button if it's an 'A' record
        if rec["type"] == "A":
            button_row.append(InlineKeyboardButton("🐑", callback_data=f"clone_record_{rec['id']}"))
        
        # Add settings button
        button_row.append(InlineKeyboardButton("⚙️", callback_data=f"record_settings_{rec['id']}"))
        
        keyboard.append(button_row)
        # --- End of single row logic ---

    keyboard.extend([[InlineKeyboardButton("🔄 رفرش", callback_data="refresh_records")], [InlineKeyboardButton("🔙 بازگشت به دامنه‌ها", callback_data="back_to_main")]])
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if update.callback_query: await update.effective_message.edit_text(text, parse_mode="Markdown", reply_markup=reply_markup)
    else: await context.bot.send_message(chat_id=uid, text=text, parse_mode="Markdown", reply_markup=reply_markup)


async def show_record_settings(message, uid, zone_id, record_id):
    try: record = get_record_details(zone_id, record_id)
    except Exception as e: logger.error(f"Could not fetch record details: {e}"); await message.edit_text("❌ خطا در دریافت اطلاعات رکورد."); return
    if not record: await message.edit_text("❌ رکورد یافت نشد."); return

    user_state[uid]["record_id"] = record_id
    text = (f"⚙️ تنظیمات رکورد: `{record['name']}`\n\n"
            f"**Type:** `{record['type']}`\n**IP:** `{record['content']}`\n"
            f"**TTL:** `{record['ttl']}`\n**Proxied:** {'✅ فعال' if record.get('proxied') else '❌ غیرفعال'}")
    keyboard = [
        [InlineKeyboardButton("🖊 تغییر IP", callback_data=f"editip_{record_id}")],
        [InlineKeyboardButton("🔁 پروکسی", callback_data=f"toggle_proxy_{record_id}"), InlineKeyboardButton("🗑 حذف", callback_data=f"confirm_delete_record_{record_id}")],
        [InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_records")]
    ]
    await message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

# --- Command & Callback Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_user_authorized(update.effective_user.id): await update.message.reply_text("❌ شما اجازه دسترسی ندارید."); return
    await show_main_menu(update, context)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    uid = query.from_user.id; data = query.data
    if not is_user_authorized(uid): await query.message.reply_text("❌ شما اجازه دسترسی به این ربات را ندارید."); return

    state = user_state.get(uid, {}); zone_id = state.get("zone_id", "")

    if data in ["back_to_main", "refresh_domains"]: await show_main_menu(update, context)
    elif data == "back_to_records" or data == "refresh_records": await show_records_list(update, context)
    elif data == "noop": return # Do nothing for the info button
    elif data == "cancel_action":
        reset_user_state(uid, keep_zone=True); await query.message.edit_text("❌ عملیات لغو شد."); await show_records_list(update, context)
    elif data.startswith("zone_"):
        zone_id = data.split("_")[1]
        try:
            zone_info = get_zone_info_by_id(zone_id)
            user_state[uid].update({"zone_id": zone_id, "zone_name": zone_info["name"]}); await show_records_list(update, context)
        except Exception as e: logger.error(f"Error selecting zone: {e}"); await query.message.reply_text("❌ دریافت اطلاعات دامنه ناموفق بود.")
    elif data.startswith("record_settings_"):
        await show_record_settings(query.message, uid, zone_id, data.split("_")[-1])
    
    elif data.startswith("clone_record_"):
        record_id = data.split("_")[2]
        try:
            original_record = get_record_details(zone_id, record_id)
            if not original_record: await query.answer("❌ رکورد اصلی یافت نشد.", show_alert=True); return
            
            user_state[uid]["clone_data"] = {
                "name": original_record["name"], "type": original_record["type"],
                "ttl": original_record["ttl"], "proxied": original_record.get("proxied", False)
            }
            user_state[uid]["mode"] = State.CLONING_NEW_IP
            await query.message.edit_text(
                f"🐑 **افزودن IP جدید به رکورد**\n\n"
                f"نام رکورد: `{original_record['name']}`\n\n"
                "لطفاً **IP جدید** را برای افزودن به این رکورد وارد کنید:",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]])
            )
        except Exception as e: logger.error(f"Error starting clone: {e}"); await query.answer("❌ خطا در شروع فرآیند کلون.", show_alert=True)
            
    elif data.startswith("editip_"):
        record_id = data.split("_")[-1]
        user_state[uid].update({"mode": State.EDITING_IP, "record_id": record_id})
        await query.message.edit_text("📝 لطفاً IP جدید را وارد کنید:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data=f"record_settings_{record_id}")]]))
    elif data.startswith("toggle_proxy_"):
        record_id = data.split("_")[-1]; success = toggle_proxied_status(zone_id, record_id)
        if success: await show_record_settings(query.message, uid, zone_id, record_id)
        else: await query.answer("❌ عملیات ناموفق بود", show_alert=True)
    elif data.startswith("confirm_delete_record_"):
        record_id = data.split("_")[-1]
        keyboard = [[InlineKeyboardButton("✅ بله، حذف شود", callback_data=f"delete_record_{record_id}")], [InlineKeyboardButton("❌ لغو", callback_data=f"record_settings_{record_id}")]]
        await query.message.edit_text("❗ آیا از حذف این رکورد مطمئن هستید؟", reply_markup=InlineKeyboardMarkup(keyboard))
    elif data.startswith("delete_record_"):
        record_id = data.split("_")[-1]
        await query.message.edit_text("⏳ در حال حذف رکورد..."); success = delete_dns_record(zone_id, record_id)
        await query.message.edit_text("✅ رکورد حذف شد." if success else "❌ حذف رکord ناموفق بود.")
        await show_records_list(update, context)


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
            await update.message.reply_text("❌ خطای داخلی. لطفاً دوباره تلاش کنید."); reset_user_state(uid, keep_zone=True); return
        
        await update.message.reply_text(f"⏳ در حال افزودن IP `{new_ip}` به رکورد `{full_name}`...", parse_mode="Markdown")
        try:
            success = create_dns_record(
                zone_id, clone_data["type"], full_name, new_ip, clone_data["ttl"], clone_data["proxied"]
            )
            await update.message.reply_text("✅ رکورد جدید با موفقیت اضافه شد." if success else "❌ عملیات ناموفق بود.")
        except Exception as e:
            logger.error(f"Error creating cloned record: {e}"); await update.message.reply_text("❌ خطا در ارتباط با API.")
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
                    await update.message.reply_text("✅ آی‌پی با موفقیت به‌روز شد.")
                    new_msg = await update.message.reply_text("بارگذاری تنظیمات...")
                    await show_record_settings(new_msg, uid, zone_id, record_id)
                else: await update.message.reply_text("❌ به‌روزرسانی ناموفق بود.")
            else: await update.message.reply_text("❌ رکورد مورد نظر یافت نشد.")
        except Exception as e: logger.error(f"Error updating IP: {e}"); await update.message.reply_text("❌ خطا در ارتباط با API.")
        finally: reset_user_state(uid, keep_zone=True)

# --- Main Application ---
def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE" or ADMIN_ID == 123456789:
        logger.warning("Bot is running with placeholder credentials. Please update BOT_TOKEN and ADMIN_ID.")
    logger.info("Starting bot...")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == "__main__":
    main()
