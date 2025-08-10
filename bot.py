import logging
import json
from collections import defaultdict
from enum import Enum, auto
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters)
from unittest.mock import Mock

# فرض می‌شود این فایل‌ها در کنار bot.py وجود دارند
# from cloudflare_api import *
# from config import BOT_TOKEN, ADMIN_ID

# --- Mockups for testing without real API/config ---
class MockCloudflareAPI:
    def get_zones(self): return [{"id": "zone123", "name": "wolfnet-vip.site", "status": "active"}]
    def get_dns_records(self, zone_id): return [{"id": "rec456", "type": "A", "name": "wolf.wolfnet-vip.site", "content": "1.1.1.1", "ttl": 1, "proxied": True}, {"id": "rec789", "type": "CNAME", "name": "sub.wolfnet-vip.site", "content": "another.site", "ttl": 300, "proxied": False}]
    def get_record_details(self, zone_id, record_id): return {"id": record_id, "type": "A", "name": "wolf.wolfnet-vip.site", "content": "1.1.1.1", "ttl": 1, "proxied": True}
    def get_zone_info_by_id(self, zone_id): return {"id": "zone123", "name": "wolfnet-vip.site"}
    def create_dns_record(self, zone_id, type, name, content, ttl, proxied): print(f"Creating record: {name}, {content}"); return True
    def update_dns_record(self, zone_id, record_id, name, type, content, ttl, proxied): print(f"Updating record: {record_id} to {content}"); return True
    def delete_dns_record(self, zone_id, record_id): print(f"Deleting record: {record_id}"); return True
    def toggle_proxied_status(self, zone_id, record_id): print(f"Toggling proxy for {record_id}"); return True

# Mock the API functions
mock_api = MockCloudflareAPI()
get_zones = mock_api.get_zones
get_dns_records = mock_api.get_dns_records
get_record_details = mock_api.get_record_details
get_zone_info_by_id = mock_api.get_zone_info_by_id
create_dns_record = mock_api.create_dns_record
update_dns_record = mock_api.update_dns_record
delete_dns_record = mock_api.delete_dns_record
toggle_proxied_status = mock_api.toggle_proxied_status

# Mock config
BOT_TOKEN = "YOUR_BOT_TOKEN" # توکن ربات خود را اینجا قرار دهید
ADMIN_ID = 123456789 # آیدی عددی ادمین را اینجا قرار دهید
# --- End of Mockups ---

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
    CLONING_NEW_NAME = auto()      # <--- ADDED
    CLONING_NEW_IP = auto()        # <--- ADDED


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

    if update.callback_query:
        await update.effective_message.edit_text(welcome_text, reply_markup=reply_markup)
    else:
        await update.effective_message.reply_text(welcome_text, reply_markup=reply_markup)


# <--- MODIFIED FUNCTION --- >
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
            # Create a row of buttons for each record
            button_row = [
                InlineKeyboardButton(name, callback_data="noop"),
                InlineKeyboardButton(f"⚙️", callback_data=f"record_settings_{rec['id']}")
            ]
            # Add the clone button only for A, AAAA records
            if rec["type"] == "A":
                button_row.insert(1, InlineKeyboardButton("🐑 کلون", callback_data=f"clone_record_{rec['id']}"))

            # The content button is separate for better layout
            keyboard.append([InlineKeyboardButton(f"{content}", callback_data=f"record_settings_{rec['id']}")])
            keyboard.append(button_row)


    keyboard.extend([
        [InlineKeyboardButton("➕ افزودن رکورد", callback_data="add_record")],
        [InlineKeyboardButton("🔄 رفرش", callback_data="refresh_records")],
        [InlineKeyboardButton("🔙 بازگشت به دامنه‌ها", callback_data="back_to_main")]
    ])

    await update.effective_message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))


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
            InlineKeyboardButton("🗑 حذف", callback_data=f"confirm_delete_record_{record_id}"),
            InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_records")
        ]
    ]
    await message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def manage_users_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    # This function is long and unchanged, so it's collapsed for brevity.
    help_text = "..." # The original help text remains here.
    keyboard = [[InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="back_to_main")]]
    await update.effective_message.edit_text(help_text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown", disable_web_page_preview=True)

# --- Command and Callback Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_user_authorized(update.effective_user.id):
        await update.message.reply_text("❌ شما اجازه دسترسی ندارید.")
        return
    await show_main_menu(update, context)


# <--- MODIFIED FUNCTION --- >
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
        await show_help(update, context)
    elif data == "cancel_action":
        reset_user_state(uid, keep_zone=True)
        await query.message.edit_text("❌ عملیات لغو شد.")
        await show_records_list(update, context)

    # User Management (Unchanged)
    elif data == "manage_users" and uid == ADMIN_ID:
        await manage_users_menu(update, context)
    # ... other user management callbacks remain the same

    # Zone and Record Selection
    elif data.startswith("zone_"):
        selected_zone_id = data.split("_")[1]
        try:
            zone_info = get_zone_info_by_id(selected_zone_id)
            user_state[uid].update({"zone_id": selected_zone_id, "zone_name": zone_info["name"]})
            await show_records_list(update, context)
        except Exception as e:
            await query.message.reply_text("❌ دریافت اطلاعات دامنه ناموفق بود.")

    # Record Settings and Actions
    elif data.startswith("record_settings_"):
        record_id = data.split("_")[2]
        await show_record_settings(query.message, uid, zone_id, record_id)

    # <--- NEW CLONE WORKFLOW --- >
    elif data.startswith("clone_record_"):
        record_id = data.split("_")[2]
        try:
            original_record = get_record_details(zone_id, record_id)
            if not original_record:
                await query.answer("❌ رکورد اصلی برای کلون یافت نشد.", show_alert=True)
                return

            # Store original record's info for cloning
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

    elif data.startswith("toggle_proxy_"):
        record_id = data.split("_")[2]
        try:
            success = toggle_proxied_status(zone_id, record_id)
            await query.answer("✅ وضعیت پروکسی تغییر کرد." if success else "❌ عملیات ناموفق بود.")
            if success: await show_record_settings(query.message, uid, zone_id, record_id)
        except Exception:
            await query.answer("❌ خطا در ارتباط با API.", show_alert=True)

    elif data.startswith("editip_"):
        record_id = data.split("_")[1]
        user_state[uid].update({"mode": State.EDITING_IP, "record_id": record_id})
        await query.message.edit_text("📝 لطفاً IP جدید را وارد کنید:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]]))

    # TTL Editing and other callbacks remain the same...
    # ...

    # Add Record Workflow
    elif data == "add_record":
        user_state[uid]["record_data"] = {}
        keyboard = [
            [InlineKeyboardButton("A", callback_data="select_type_A"), InlineKeyboardButton("AAAA", callback_data="select_type_AAAA"), InlineKeyboardButton("CNAME", callback_data="select_type_CNAME")],
            [InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]
        ]
        await query.message.edit_text("📌 مرحله ۱ از ۵: نوع رکورد را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))
    # ... and so on for the rest of the original function.
    # The logic for add, edit, delete remains unchanged.


# <--- MODIFIED FUNCTION --- >
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_user_authorized(uid):
        await update.message.reply_text("❌ شما اجازه دسترسی ندارید.")
        return

    state = user_state.get(uid, {})
    mode = state.get("mode")
    text = update.message.text.strip()
    if not mode or mode == State.NONE: return

    # Admin: Add User (Unchanged)
    if mode == State.ADDING_USER and uid == ADMIN_ID:
        # ... logic remains the same
        pass

    # Edit Record IP (Unchanged)
    elif mode == State.EDITING_IP:
        # ... logic remains the same
        pass

    # <--- NEW CLONE WORKFLOW (Message Handling) --- >
    elif mode == State.CLONING_NEW_NAME:
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

        # Construct the full domain name for the new record
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
            reset_user_state(uid, keep_zone=True)
            # Create a mock update to call the list function
            mock_query = Mock(from_user=update.effective_user, message=update.message)
            mock_update = Mock(callback_query=mock_query, effective_message=update.message)
            await show_records_list(mock_update, context)

    # Add Record Workflow (by message)
    elif mode == State.ADDING_RECORD_NAME:
        user_state[uid]["record_data"]["name"] = text
        user_state[uid]["mode"] = State.ADDING_RECORD_CONTENT
        await update.message.reply_text("📌 مرحله ۳ از ۵: مقدار رکورد را وارد کنید (مثلاً IP یا آدرس):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]]))

    elif mode == State.ADDING_RECORD_CONTENT:
        user_state[uid]["record_data"]["content"] = text
        user_state[uid].pop("mode", None)
        keyboard = [
            [InlineKeyboardButton("Auto", callback_data="select_ttl_1"), InlineKeyboardButton("1 دقیقه", callback_data="select_ttl_60")],
            [InlineKeyboardButton("2 دقیقه", callback_data="select_ttl_120"), InlineKeyboardButton("5 دقیقه", callback_data="select_ttl_300")],
            [InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]
        ]
        await update.message.reply_text("📌 مرحله ۴ از ۵: مقدار TTL را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))


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
