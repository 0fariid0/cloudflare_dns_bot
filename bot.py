import logging
import json
from collections import defaultdict
from enum import Enum, auto
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters)

# --- START: این بخش‌ها باید با اطلاعات واقعی شما پر شوند ---
# فرض می‌شود این فایل‌ها در کنار bot.py وجود دارند
try:
    from cloudflare_api import *
    from config import BOT_TOKEN, ADMIN_ID
except ImportError:
    # اگر فایل‌ها وجود نداشتند، از مقادیر نمونه استفاده کن تا ربات حداقل اجرا شود
    # در این حالت ربات کار نخواهد کرد مگر اینکه مقادیر زیر را دستی پر کنید
    BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"  # <--- توکن ربات خود را اینجا وارد کنید
    ADMIN_ID = 123456789  # <--- آیدی عددی ادمین را اینجا وارد کنید
    print("WARNING: 'config.py' or 'cloudflare_api.py' not found. Using placeholder values.")
    # توابع شبیه‌ساز برای جلوگیری از خطا
    def get_zones(): return []
    def get_dns_records(zone_id): return []
    def get_record_details(zone_id, record_id): return None
    def get_zone_info_by_id(zone_id): return None
    def create_dns_record(zone_id, type, name, content, ttl, proxied): return False
    def update_dns_record(zone_id, record_id, name, type, content, ttl, proxied): return False
    def delete_dns_record(zone_id, record_id): return False
    def toggle_proxied_status(zone_id, record_id): return False
# --- END: ---

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

# --- User Management ---
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
        text = "❌ خطا در ارتباط با Cloudflare. لطفاً از صحیح بودن توکن و کلیدهای API خود در فایل‌های `config.py` و `cloudflare_api.py` اطمینان حاصل کنید."
        if update.callback_query:
            await update.effective_message.edit_text(text)
        else:
            await update.effective_message.reply_text(text)
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

    if update.callback_query:
        await update.effective_message.edit_text(welcome_text, reply_markup=reply_markup)
    else:
        await update.effective_message.reply_text(welcome_text, reply_markup=reply_markup)

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
            
            keyboard.append([
                InlineKeyboardButton(f"{name}: {content}", callback_data=f"record_settings_{rec['id']}")
            ])
            
            action_buttons = []
            if rec["type"] == "A": # دکمه کلون فقط برای رکوردهای A
                action_buttons.append(InlineKeyboardButton("🐑 کلون", callback_data=f"clone_record_{rec['id']}"))
            action_buttons.append(InlineKeyboardButton("⚙️ تنظیمات", callback_data=f"record_settings_{rec['id']}"))
            keyboard.append(action_buttons)

    keyboard.extend([
        [InlineKeyboardButton("➕ افزودن رکورد", callback_data="add_record")],
        [InlineKeyboardButton("🔄 رفرش", callback_data="refresh_records")],
        [InlineKeyboardButton("🔙 بازگشت به دامنه‌ها", callback_data="back_to_main")]
    ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # ارسال پیام جدید یا ویرایش پیام موجود
    if update.callback_query:
        await update.effective_message.edit_text(text, parse_mode="Markdown", reply_markup=reply_markup)
    else:
        # اگر از طرف handle_message فراخوانی شود، یک پیام جدید می‌فرستد
        await context.bot.send_message(chat_id=uid, text=text, parse_mode="Markdown", reply_markup=reply_markup)


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
        ],
        [
            InlineKeyboardButton("🔁 پروکسی", callback_data=f"toggle_proxy_{record_id}"),
            InlineKeyboardButton("🗑 حذف", callback_data=f"confirm_delete_record_{record_id}"),
        ],
        [
            InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_records")
        ]
    ]
    await message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))


# --- Command & Callback Handlers ---
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

    if data in ["back_to_main", "refresh_domains"]:
        await show_main_menu(update, context)
    elif data == "back_to_records" or data == "refresh_records":
        await show_records_list(update, context)
    elif data == "cancel_action":
        reset_user_state(uid, keep_zone=True)
        await query.message.edit_text("❌ عملیات لغو شد.")
        await show_records_list(update, context)
    elif data.startswith("zone_"):
        selected_zone_id = data.split("_")[1]
        try:
            zone_info = get_zone_info_by_id(selected_zone_id)
            user_state[uid].update({"zone_id": selected_zone_id, "zone_name": zone_info["name"]})
            await show_records_list(update, context)
        except Exception as e:
            logger.error(f"Error selecting zone {selected_zone_id}: {e}")
            await query.message.reply_text("❌ دریافت اطلاعات دامنه ناموفق بود.")
    
    elif data.startswith("record_settings_"):
        record_id = data.split("_")[-1]
        await show_record_settings(query.message, uid, zone_id, record_id)
        
    elif data.startswith("clone_record_"):
        record_id = data.split("_")[2]
        try:
            original_record = get_record_details(zone_id, record_id)
            if not original_record:
                await query.answer("❌ رکورد اصلی برای کلون یافت نشد.", show_alert=True)
                return

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
            
    elif data.startswith("editip_"):
        record_id = data.split("_")[-1] # Use -1 to be safe
        user_state[uid].update({"mode": State.EDITING_IP, "record_id": record_id})
        await query.message.edit_text("📝 لطفاً IP جدید را وارد کنید:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data=f"record_settings_{record_id}")]]))
        
    # Other callbacks for TTL, proxy, delete etc. would go here...


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_user_authorized(uid):
        await update.message.reply_text("❌ شما اجازه دسترسی ندارید.")
        return

    state = user_state.get(uid, {})
    mode = state.get("mode")
    text = update.message.text.strip()
    if not mode or mode == State.NONE:
        return

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

        new_name = clone_data.get("new_name")
        if not new_name:
            await update.message.reply_text("❌ خطای داخلی: نام جدید یافت نشد. لطفاً دوباره تلاش کنید.")
            reset_user_state(uid, keep_zone=True)
            return

        full_name = f"{new_name}.{zone_name}" if new_name != "@" else zone_name
        
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
                await update.message.reply_text("❌ عملیات ایجاد رکورد کلون شده ناموفق بود. (ممکن است رکورد با این نام از قبل موجود باشد)")
        except Exception as e:
            logger.error(f"Error creating cloned record: {e}")
            await update.message.reply_text("❌ خطا در ارتباط با API هنگام ایجاد رکورد.")
        finally:
            reset_user_state(uid, keep_zone=True)
            await update.message.reply_text("🔄 در حال بارگذاری لیست به‌روز شده رکوردها...")
            await show_records_list(update, context)
            
    elif mode == State.EDITING_IP:
        new_ip = text
        record_id = state.get("record_id")
        zone_id = state.get("zone_id")
        await update.message.reply_text(f"⏳ در حال به‌روزرسانی IP به `{new_ip}`...", parse_mode="Markdown")
        try:
            record = get_record_details(zone_id, record_id)
            if record:
                success = update_dns_record(zone_id, record_id, record["name"], record["type"], new_ip, record["ttl"], record.get("proxied", False))
                if success:
                    await update.message.reply_text("✅ آی‌پی با موفقیت به‌روز شد.")
                else:
                    await update.message.reply_text("❌ به‌روزرسانی ناموفق بود.")
            else:
                await update.message.reply_text("❌ رکورد مورد نظر یافت نشد.")
        except Exception as e:
            logger.error(f"Error updating IP for record {record_id}: {e}")
            await update.message.reply_text("❌ خطا در ارتباط با API.")
        finally:
            reset_user_state(uid, keep_zone=True)
            await update.message.reply_text("...در حال بارگذاری تنظیمات جدید")
            # برای نمایش مجدد منوی تنظیمات، به یک پیام برای ویرایش نیاز داریم
            # پس از آپدیت، کاربر باید دستی به منو برگردد
            await show_records_list(update, context)


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
