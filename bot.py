import logging
import json
from collections import defaultdict
from enum import Enum, auto
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters)
from unittest.mock import Mock

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
                InlineKeyboardButton(name, callback_data="noop"),
                InlineKeyboardButton(f"{content} | ⚙️", callback_data=f"record_settings_{rec['id']}")
            ])

    keyboard.extend([
        [InlineKeyboardButton("➕ افزودن رکورد", callback_data="add_record")],
        [InlineKeyboardButton("🔄 رفرش", callback_data="refresh_records")],
        [InlineKeyboardButton("🔙 بازگشت به دامنه‌ها", callback_data="back_to_domains")]
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
            InlineKeyboardButton("🗑 حذف", callback_data=f"confirm_delete_{record_id}"),
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
    if data in ["back_to_domains", "refresh_domains", "back_to_main"]:
        await show_main_menu(update, context)
    elif data == "back_to_records" or data == "refresh_records":
        await show_records_list(update, context)
    elif data == "cancel_action":
        reset_user_state(uid, keep_zone=True)
        await query.message.edit_text("❌ عملیات لغو شد.")
        await show_records_list(update, context)

    # User Management
    elif data == "manage_users" and uid == ADMIN_ID:
        await manage_users_menu(update, context)
    elif data == "add_user_prompt" and uid == ADMIN_ID:
        user_state[uid]['mode'] = State.ADDING_USER
        text = "لطفاً شناسه عددی (ID) کاربر مورد نظر را ارسال کنید.\n\nراهنمایی: از کاربر بخواهید یک پیام به ربات @userinfobot ارسال کند تا شناسه خود را دریافت نماید."
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="manage_users")]]))
    elif data.startswith("delete_user_") and uid == ADMIN_ID:
        user_to_delete = int(data.split("_")[2])
        if remove_user(user_to_delete):
            await query.answer("✅ کاربر با موفقیت حذف شد.", show_alert=True)
        else:
            await query.answer("❌ حذف ناموفق بود. ادمین اصلی قابل حذف نیست.", show_alert=True)
        await manage_users_menu(update, context)

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

    # TTL Editing
    elif data.startswith("edittll_"):
        record_id = data.split("_")[2] # Corrected from _[1]
        user_state[uid].update({"mode": State.EDITING_TTL, "record_id": record_id})
        keyboard = [
            [InlineKeyboardButton("Auto", callback_data=f"update_ttl_{record_id}_1"), InlineKeyboardButton("1 دقیقه", callback_data=f"update_ttl_{record_id}_60")],
            [InlineKeyboardButton("2 دقیقه", callback_data=f"update_ttl_{record_id}_120"), InlineKeyboardButton("5 دقیقه", callback_data=f"update_ttl_{record_id}_300")],
            [InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]
        ]
        await query.message.edit_text("⏱ مقدار جدید TTL را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data.startswith("update_ttl_"):
        parts = data.split("_")
        record_id, ttl = parts[2], int(parts[3])
        try:
            record = get_record_details(zone_id, record_id)
            if record:
                success = update_dns_record(zone_id, record_id, record["name"], record["type"], record["content"], ttl, record.get("proxied", False))
                await query.answer("✅ TTL تغییر یافت." if success else "❌ عملیات ناموفق بود.")
                if success: await show_record_settings(query.message, uid, zone_id, record_id)
        except Exception:
            await query.answer("❌ خطا در ارتباط با API.", show_alert=True)

    # Add Record Workflow
    elif data == "add_record":
        user_state[uid]["record_data"] = {}
        keyboard = [
            [InlineKeyboardButton("A", callback_data="select_type_A"), InlineKeyboardButton("AAAA", callback_data="select_type_AAAA"), InlineKeyboardButton("CNAME", callback_data="select_type_CNAME")],
            [InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]
        ]
        await query.message.edit_text("📌 مرحله ۱ از ۵: نوع رکورد را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("select_type_"):
        user_state[uid]["record_data"]["type"] = data.split("_")[2]
        user_state[uid]["mode"] = State.ADDING_RECORD_NAME
        await query.message.edit_text("📌 مرحله ۲ از ۵: نام رکورد را وارد کنید (مثال: sub یا @ برای ریشه)", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]]))
    
    elif data.startswith("select_ttl_"):
        user_state[uid]["record_data"]["ttl"] = int(data.split("_")[2])
        keyboard = [
            [InlineKeyboardButton("✅ بله", callback_data="select_proxied_true"), InlineKeyboardButton("❌ خیر", callback_data="select_proxied_false")],
            [InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]
        ]
        await query.message.edit_text("📌 مرحله ۵ از ۵: آیا پروکسی فعال باشد؟", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("select_proxied_"):
        user_state[uid]["record_data"]["proxied"] = data.endswith("true")
        r_data = user_state[uid]["record_data"]
        zone_name = state["zone_name"]
        name = r_data["name"]
        if name == "@":
            name = zone_name
        elif not name.endswith(f".{zone_name}"):
            name = f"{name}.{zone_name}"
        
        await query.message.edit_text("⏳ در حال ایجاد رکورد...")
        try:
            success = create_dns_record(zone_id, r_data["type"], name, r_data["content"], r_data["ttl"], r_data["proxied"])
            await query.message.edit_text("✅ رکورد با موفقیت اضافه شد." if success else "❌ افزودن رکورد ناموفق بود.")
        except Exception:
            await query.message.edit_text("❌ خطا در ایجاد رکورد.")
        finally:
            reset_user_state(uid, keep_zone=True)
            await show_records_list(update, context)

    # Deletion Confirmation
    elif data.startswith("confirm_delete_"):
        item_type, item_id = data.split("_")[2], data.split("_")[3]
        text = f"❗ آیا از حذف این {'رکورد' if item_type == 'record' else 'دامنه'} مطمئن هستید؟"
        keyboard = [
            [InlineKeyboardButton("✅ بله، حذف شود", callback_data=f"delete_{item_type}_{item_id}")],
            [InlineKeyboardButton("❌ لغو", callback_data="back_to_records" if item_type == 'record' else 'back_to_domains')]
        ]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("delete_record_"):
        record_id = data.split("_")[2]
        await query.message.edit_text("⏳ در حال حذف رکورد...")
        try:
            success = delete_dns_record(zone_id, record_id)
            await query.message.edit_text("✅ رکورد حذف شد." if success else "❌ حذف رکورد ناموفق بود.")
        except Exception:
            await query.message.edit_text("❌ خطا در حذف رکورد.")
        finally:
            await show_records_list(update, context)

# --- Message Handler ---
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_user_authorized(uid):
        await update.message.reply_text("❌ شما اجازه دسترسی ندارید.")
        return

    state = user_state.get(uid, {})
    mode = state.get("mode")
    text = update.message.text.strip()
    if not mode or mode == State.NONE: return

    # Admin: Add User
    if mode == State.ADDING_USER and uid == ADMIN_ID:
        try:
            new_user_id = int(text)
            if add_user(new_user_id):
                await update.message.reply_text(f"✅ کاربر `{new_user_id}` با موفقیت اضافه شد.", parse_mode="Markdown")
            else:
                await update.message.reply_text("⚠️ این کاربر از قبل در لیست وجود دارد.")
        except ValueError:
            await update.message.reply_text("❌ ورودی نامعتبر است. لطفاً فقط شناسه عددی کاربر را ارسال کنید.")
        
        reset_user_state(uid)
        mock_query = Mock(from_user=update.effective_user, message=update.message)
        mock_update = Mock(callback_query=mock_query, effective_message=update.message, effective_user=update.effective_user)
        await manage_users_menu(mock_update, context)

    # Edit Record IP
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
                    new_msg = await update.message.reply_text("...در حال بارگذاری تنظیمات جدید")
                    await show_record_settings(new_msg, uid, zone_id, record_id)
                else:
                    await update.message.reply_text("❌ به‌روزرسانی ناموفق بود.")
            else:
                await update.message.reply_text("❌ رکورد مورد نظر یافت نشد.")
        except Exception:
            await update.message.reply_text("❌ خطا در ارتباط با API.")
        finally:
            reset_user_state(uid, keep_zone=True)

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
