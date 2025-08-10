import logging
import json
from collections import defaultdict
from enum import Enum, auto
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters)
from unittest.mock import Mock

# فرض می‌شود این فایل‌ها در کنار bot.py وجود دارند
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
    # +++ NEW STATES +++
    CLONING_SUBDOMAIN_SOURCE = auto()
    CLONING_SUBDOMAIN_DEST = auto()

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
    
    # +++ ADDED NEW BUTTON +++
    keyboard.extend([
        [InlineKeyboardButton("➕ افزودن رکورد", callback_data="add_record")],
        [InlineKeyboardButton("🐑 کپی کردن ساب‌دامنه", callback_data="clone_subdomain_start")],
        [InlineKeyboardButton("🔄 رفرش", callback_data="refresh_records")],
        [InlineKeyboardButton("🔙 بازگشت به دامنه‌ها", callback_data="back_to_main")]
    ])
    
    await update.effective_message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

# (The rest of the UI functions like show_record_settings, manage_users_menu, show_help remain unchanged)
# ...
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
    help_text = """
🤖 *راهنمای ربات مدیریت Cloudflare DNS*

این ربات به شما اجازه می‌دهد تا دامنه‌ها و رکوردهای DNS خود را در حساب Cloudflare به راحتی مدیریت کنید.

---
### **بخش ۱: مدیریت دامنه‌ها**

-   *نمایش دامنه‌ها:* در منوی اصلی، لیست تمام دامنه‌های شما نمایش داده می‌شود.
-   *افزودن دامنه:* با زدن دکمه `➕ افزودن دامنه`، می‌توانید نام دامنه جدیدی (مثلاً `example.com`) را وارد کنید. پس از افزودن، باید **Name Server** های دامنه خود را به مواردی که ربات اعلام می‌کند تغییر دهید.
-   *حذف دامنه:* با زدن دکمه `🗑` کنار هر دامنه، می‌توانید آن را از حساب Cloudflare خود حذف کنید. (این عمل غیرقابل بازگشت است!)

---
### **بخش ۲: مدیریت رکوردها**

برای مدیریت رکوردهای یک دامنه، کافیست روی نام آن در لیست کلیک کنید.

-   *افزودن رکورد:*
    1.  دکمه `➕ افزودن رکورد` را بزنید.
    2.  **نوع رکورد** را انتخاب کنید (`A`, `AAAA`, `CNAME`).
    3.  **نام رکورد** را وارد کنید. برای دامنه اصلی (root)، از علامت `@` استفاده کنید. برای ساب‌دامین، نام آن را وارد کنید (مثلاً `sub`).
    4.  **مقدار رکورد** را وارد کنید (مثلاً آدرس IP برای رکورد `A` یا یک دامنه دیگر برای `CNAME`).
    5.  **TTL** (Time To Live) را انتخاب کنید. مقدار `Auto` توصیه می‌شود.
    6.  **وضعیت پروکسی** را مشخص کنید. فعال بودن پروکسی (`✅`) باعث می‌شود ترافیک شما از طریق Cloudflare عبور کرده و IP اصلی سرور شما مخفی بماند.

-   *کپی کردن ساب‌دامنه:*
    1.  دکمه `🐑 کپی کردن ساب‌دامنه` را بزنید.
    2.  نام ساب‌دامنه **مبدا** را وارد کنید (مثلاً `staging`).
    3.  نام ساب‌دامنه **مقصد** را وارد کنید (مثلاً `production`).
    4.  ربات تمام رکوردهای `staging.yourdomain.com` (و زیرمجموعه‌های آن مانند `api.staging.yourdomain.com`) را در `production.yourdomain.com` کپی می‌کند.

-   *ویرایش رکورد:*
    -   با کلیک بر روی دکمه `⚙️` کنار هر رکورد، وارد تنظیمات آن می‌شوید.
    -   *تغییر IP:* برای به‌روزرسانی آدرس IP رکورد.
    -   *تغییر TTL:* برای تغییر زمان کش شدن اطلاعات DNS.
    -   *پروکسی:* برای فعال/غیرفعال کردن پروکسی Cloudflare.

-   *حذف رکورد:* در منوی تنظیمات هر رکورد، با زدن دکمه `🗑 حذف` می‌توانید آن را پاک کنید.

---
برای بازگشت به منوی قبل از دکمه‌های `🔙 بازگشت` و برای لغو عملیات از دکمه `❌ لغو` استفاده کنید.
    """
    keyboard = [[InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="back_to_main")]]
    await update.effective_message.edit_text(
        help_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
        disable_web_page_preview=True
    )


# +++ NEW HELPER FUNCTION +++
async def clone_subdomain_records(zone_id: str, zone_name: str, source_sub: str, dest_sub: str):
    """
    Fetches all records for a source subdomain and creates them for a destination subdomain.
    Returns a tuple of (success_count, failure_count).
    """
    logger.info(f"Cloning '{source_sub}' to '{dest_sub}' in zone '{zone_name}' ({zone_id})")
    success_count = 0
    failure_count = 0
    
    # Define the domain patterns to search for
    source_full_domain = f"{source_sub}.{zone_name}"
    source_suffix = f".{source_sub}.{zone_name}"
    dest_full_domain = f"{dest_sub}.{zone_name}"

    try:
        all_records = get_dns_records(zone_id)
    except Exception as e:
        logger.error(f"Failed to get DNS records for cloning: {e}")
        return 0, -1 # Indicate total failure

    # Filter records that match the source subdomain
    records_to_clone = [
        r for r in all_records
        if r['name'] == source_full_domain or r['name'].endswith(source_suffix)
    ]

    if not records_to_clone:
        logger.warning(f"No records found for source subdomain '{source_sub}' in zone '{zone_name}'.")
        return 0, 0

    # Get existing records for the destination to avoid creating duplicates
    existing_dest_records = {
        (r['type'], r['name']) for r in all_records
        if r['name'].startswith(f"{dest_sub}.") or r['name'] == dest_full_domain
    }
    
    for record in records_to_clone:
        # Construct the new record name by replacing the source subdomain with the destination one
        # The '1' ensures we only replace the first occurrence, which is safer.
        new_name = record['name'].replace(source_sub, dest_sub, 1)

        # Skip creating if a record with the same type and name already exists at the destination
        if (record['type'], new_name) in existing_dest_records:
            logger.warning(f"Skipping duplicate record creation: {record['type']} {new_name}")
            failure_count += 1
            continue

        try:
            # Create the new DNS record using the existing API function
            success = create_dns_record(
                zone_id=zone_id,
                record_type=record['type'],
                name=new_name,
                content=record['content'],
                ttl=record['ttl'],
                proxied=record.get('proxied', False)
            )
            if success:
                logger.info(f"Successfully cloned record: {record['type']} {new_name}")
                success_count += 1
            else:
                logger.error(f"Failed to clone record (API returned false): {new_name}")
                failure_count += 1
        except Exception as e:
            logger.error(f"Exception while creating cloned record {new_name}: {e}")
            failure_count += 1
            
    return success_count, failure_count


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
        reset_user_state(uid, keep_zone=True) # Clear any pending modes
        await show_records_list(update, context)
    elif data == "show_help":
        await show_help(update, context)
    elif data == "cancel_action":
        reset_user_state(uid, keep_zone=True)
        await query.message.edit_text("❌ عملیات لغو شد.")
        await show_records_list(update, context)

    # ... (User Management code remains the same) ...
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
    
    # +++ NEW WORKFLOW FOR CLONING +++
    elif data == "clone_subdomain_start":
        reset_user_state(uid, keep_zone=True) # Ensure a clean state
        user_state[uid]['mode'] = State.CLONING_SUBDOMAIN_SOURCE
        text = "🐑 مرحله ۱ از ۲: نام ساب‌دامنه‌ای که می‌خواهید از آن کپی بگیرید را وارد کنید (مثال: `staging`)."
        await query.message.edit_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="back_to_records")]])
        )

    # ... (Rest of the callback handler remains mostly the same) ...
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
        record_id = data.split("_")[2]
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
        item_type = "record" if data.startswith("confirm_delete_record_") else "zone"
        item_id = data.split("_")[-1]
        text = f"❗ آیا از حذف این {'رکورد' if item_type == 'record' else 'دامنه'} مطمئن هستید؟"
        back_action = "back_to_records" if item_type == 'record' else 'back_to_main'
        keyboard = [
            [InlineKeyboardButton("✅ بله، حذف شود", callback_data=f"delete_{item_type}_{item_id}")],
            [InlineKeyboardButton("❌ لغو", callback_data=back_action)]
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
    text = update.message.text.strip().lower() # Standardize input
    if not mode or mode == State.NONE: return

    # ... (Admin: Add User code remains the same) ...
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


    # +++ NEW MESSAGE HANDLING FOR CLONING +++
    elif mode == State.CLONING_SUBDOMAIN_SOURCE:
        if not text or '@' in text or ' ' in text:
            await update.message.reply_text("❌ نام ساب‌دامنه نامعتبر است. لطفاً دوباره تلاش کنید.")
            return
        
        user_state[uid]['source_subdomain'] = text
        user_state[uid]['mode'] = State.CLONING_SUBDOMAIN_DEST
        reply_text = (
            f"✅ ساب‌دامنه مبدا: `{text}`\n\n"
            f"🐑 مرحله ۲ از ۲: حالا نام ساب‌دامنه **مقصد** را وارد کنید (مثال: `production`)."
        )
        await update.message.reply_text(
            reply_text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="back_to_records")]])
        )

    elif mode == State.CLONING_SUBDOMAIN_DEST:
        dest_subdomain = text
        source_subdomain = state.get('source_subdomain')
        zone_id = state.get('zone_id')
        zone_name = state.get('zone_name')

        if not dest_subdomain or '@' in dest_subdomain or ' ' in dest_subdomain:
            await update.message.reply_text("❌ نام ساب‌دامنه نامعتبر است. لطفاً دوباره تلاش کنید.")
            return

        if dest_subdomain == source_subdomain:
            await update.message.reply_text("❌ نام ساب‌دامنه مبدا و مقصد نمی‌تواند یکسان باشد. لطفاً نام دیگری وارد کنید.")
            return

        await update.message.reply_text(f"⏳ در حال کپی کردن رکوردهای `{source_subdomain}` به `{dest_subdomain}`... این عملیات ممکن است کمی طول بکشد.")
        
        success_count, failure_count = await clone_subdomain_records(zone_id, zone_name, source_subdomain, dest_subdomain)

        if failure_count == -1: # Special case for total API failure
            result_text = "❌ خطا در ارتباط با Cloudflare. نتوانستم لیست رکوردها را دریافت کنم."
        elif success_count == 0 and failure_count == 0:
            result_text = f"⚠️ هیچ رکوردی برای ساب‌دامنه `{source_subdomain}` پیدا نشد تا کپی شود."
        else:
            result_text = (
                f"✅ عملیات کپی کامل شد!\n\n"
                f"    - 📄 رکوردهای با موفقیت ایجاد شده: *{success_count}*\n"
                f"    - ⚠️ رکوردهای ناموفق یا تکراری: *{failure_count}*"
            )

        await update.message.reply_text(result_text, parse_mode="Markdown")
        
        reset_user_state(uid, keep_zone=True)
        # Refresh the records list to show the new records
        # We create a mock update to call the function that expects a callback query
        mock_query = Mock(from_user=update.effective_user, message=update.message)
        mock_update = Mock(callback_query=mock_query, effective_message=update.message, effective_user=update.effective_user)
        await show_records_list(mock_update, context)

    # ... (Rest of the message handler remains the same) ...
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
