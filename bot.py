import logging
import json
from collections import defaultdict
from enum import Enum, auto
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters)
# from unittest.mock import Mock # دیگر نیازی به این نیست

# مطمئن شوید که این فایل‌ها به درستی در کنار ربات شما وجود دارند
from cloudflare_api import *
from config import BOT_TOKEN, ADMIN_ID

# --- Setup ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
USER_FILE = "users.json"
user_state = defaultdict(dict)

class State(Enum):
    NONE = auto()
    ADDING_USER = auto()
    ADDING_DOMAIN = auto()
    ADDING_RECORD_NAME = auto()
    ADDING_RECORD_CONTENT = auto()
    EDITING_IP = auto()
    EDITING_TTL = auto()
    CLONING_NEW_NAME = auto()
    CLONING_NEW_IP = auto()

# --- User Management (بدون تغییر) ---
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
            InlineKeyboardButton("🗑", callback_data=f"confirm_delete_zone_{zone['id']}")
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

    # برای جلوگیری از خطا، چک می‌کنیم که آیا پیام قبلی برای ویرایش وجود دارد یا نه
    if update.callback_query:
        await update.effective_message.edit_text(welcome_text, reply_markup=reply_markup)
    else:
        await update.effective_message.reply_text(welcome_text, reply_markup=reply_markup)

# <--- MODIFIED & CLEANED FUNCTION --- >
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
            
            # ردیف اول: اطلاعات رکورد
            keyboard.append([
                InlineKeyboardButton(f"{name}: {content}", callback_data=f"record_settings_{rec['id']}")
            ])
            
            # ردیف دوم: دکمه‌های عملیاتی
            action_buttons = []
            if rec["type"] == "A":
                action_buttons.append(InlineKeyboardButton("🐑 کلون", callback_data=f"clone_record_{rec['id']}"))
            action_buttons.append(InlineKeyboardButton("⚙️ تنظیمات", callback_data=f"record_settings_{rec['id']}"))
            keyboard.append(action_buttons)

    keyboard.extend([
        [InlineKeyboardButton("➕ افزودن رکورد", callback_data="add_record")],
        [InlineKeyboardButton("🔄 رفرش", callback_data="refresh_records")],
        [InlineKeyboardButton("🔙 بازگشت به دامنه‌ها", callback_data="back_to_main")]
    ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.effective_message.edit_text(text, parse_mode="Markdown", reply_markup=reply_markup)
    else:
        await context.bot.send_message(chat_id=uid, text=text, parse_mode="Markdown", reply_markup=reply_markup)


# --- بقیه توابع نمایش منو بدون تغییر باقی می‌مانند ---
# show_record_settings, manage_users_menu, show_help
async def show_record_settings(message, uid, zone_id, record_id):
    # (کد این تابع بدون تغییر است)
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
            InlineKeyboardButton("🗑 حذف", callback_data=f"confirm_delete_record_{record_id}"),
            InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_records")
        ]
    ]
    await message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

# --- Command and Callback Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_user_authorized(update.effective_user.id):
        await update.message.reply_text("❌ شما اجازه دسترسی ندارید.")
        return
    await show_main_menu(update, context)


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

    # Navigation
    if data in ["back_to_main", "refresh_domains"]:
        await show_main_menu(update, context)
    elif data == "back_to_records" or data == "refresh_records":
        await show_records_list(update, context)
    elif data == "show_help":
        pass # show_help را در اینجا فراخوانی کنید
    elif data == "cancel_action":
        reset_user_state(uid, keep_zone=True)
        await query.message.edit_text("❌ عملیات لغو شد.")
        await show_records_list(update, context)

    # <--- NEW CLONE WORKFLOW (START) --- >
    elif data.startswith("clone_record_"):
        record_id = data.split("_")[2]
        try:
            original_record = get_record_details(zone_id, record_id)
            if not original_record:
                await query.answer("❌ رکورد اصلی برای کلون یافت نشد.", show_alert=True)
                return

            # ذخیره اطلاعات رکورد اصلی برای کلون کردن
            user_state[uid]["clone_data"] = {
                "type": original_record["type"],
                "ttl": original_record["ttl"],
                "proxied": original_record.get("proxied", False)
            }
            user_state[uid]["mode"] = State.CLONING_NEW_NAME

            await query.message.edit_text(
                "🐑 **کلون کردن رکورد**\n\n"
                "📌 مرحله ۱ از ۲: لطفاً **نام** ساب‌دامین جدید را وارد کنید (مثلاً `new-sub`).",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]])
            )
        except Exception as e:
            logger.error(f"Error starting clone for record {record_id}: {e}")
            await query.answer("❌ خطا در شروع فرآیند کلون.", show_alert=True)
            
    # --- بقیه callback ها بدون تغییر باقی می‌مانند ---
    # ...


# <--- MODIFIED & FIXED FUNCTION --- >
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_user_authorized(uid):
        await update.message.reply_text("❌ شما اجازه دسترسی ندارید.")
        return

    state = user_state.get(uid, {})
    mode = state.get("mode")
    text = update.message.text.strip()
    if not mode or mode == State.NONE: return

    # --- CLONE WORKFLOW (MESSAGE HANDLING) ---
    if mode == State.CLONING_NEW_NAME:
        user_state[uid]["clone_data"]["new_name"] = text
        user_state[uid]["mode"] = State.CLONING_NEW_IP
        await update.message.reply_text(
            "🐑 **کلون کردن رکورد**\n\n"
            f"نام جدید: `{text}`\n"
            "📌 مرحله ۲ از ۲: لطفاً **IP** جدید را وارد کنید:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]])
        )

    elif mode == State.CLONING_NEW_IP:
        new_ip = text
        clone_data = user_state[uid].get("clone_data", {})
        zone_id = state.get("zone_id")
        zone_name = state.get("zone_name")

        # ساخت نام کامل دامنه برای رکورد جدید
        new_name = clone_data.get("new_name")
        if new_name == "@":
            full_name = zone_name
        elif not new_name.endswith(f".{zone_name}"):
            full_name = f"{new_name}.{zone_name}"
        else:
            full_name = new_name

        await update.message.reply_text(f"⏳ در حال ایجاد رکورد کلون شده `{full_name}` با IP `{new_ip}`...", parse_mode="Markdown")

        try:
            success = create_dns_record(
                zone_id,
                clone_data["type"],
                full_name,
                new_ip,
                clone_data["ttl"],
                clone_data["proxied"]
            )
            if success:
                await update.message.reply_text("✅ رکورد جدید با موفقیت کلون و ایجاد شد.")
            else:
                await update.message.reply_text("❌ عملیات ایجاد رکورد کلون شده ناموفق بود.")
        except Exception as e:
            logger.error(f"Error creating cloned record: {e}")
            await update.message.reply_text("❌ خطا در ارتباط با API هنگام ایجاد رکورد.")
        finally:
            # ریست کردن وضعیت و نمایش مجدد لیست رکوردها
            reset_user_state(uid, keep_zone=True)
            await update.message.reply_text("🔄 در حال بارگذاری لیست به‌روز شده رکوردها...")
            await show_records_list(update, context) # <--- FIXED
    
    # --- بقیه حالت‌های handle_message بدون تغییر باقی می‌مانند ---
    # ...


# --- Main Application ---
def main():
    load_users()
    logger.info("Starting bot...")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    app.run_polling()

if __name__ == "__main__":
    main()
