import logging
import json
import re
from collections import defaultdict
from enum import Enum, auto
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters)

# فرض می‌شود این فایل‌ها در کنار bot.py وجود دارند
try:
    from cloudflare_api import *
    from config import BOT_TOKEN, ADMIN_ID
except ImportError:
    # مقادیر پیش‌فرض برای تست در صورت نبودن فایل‌های اصلی
    BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"
    ADMIN_ID = 123456789 # شناسه ادمین اصلی را اینجا وارد کنید
    def get_zones(): return [{"id": "zone1", "name": "example.com", "status": "active"}]
    def get_dns_records(zone_id): return [{"id": "rec1", "type": "A", "name": "test.example.com", "content": "1.1.1.1"}]
    def get_record_details(zone_id, record_id): return {"id": "rec1", "name": "test.example.com", "type": "A", "content": "1.1.1.1", "ttl": 1, "proxied": True}
    def get_zone_info_by_id(zone_id): return {"id": "zone1", "name": "example.com"}
    def create_dns_record(zone_id, type, name, content, ttl, proxied): return True
    def update_dns_record(zone_id, record_id, name, type, content, ttl, proxied): return True
    def delete_dns_record(zone_id, record_id): return True
    def toggle_proxied_status(zone_id, record_id): return True

# --- Setup ---
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
USER_FILE = "users.json"
LOG_FILE = "bot_audit.log"
BLOCKED_USER_FILE = "blocked_users.json"

user_state = defaultdict(dict)

class State(Enum):
    NONE = auto()
    ADDING_USER = auto()
    ADDING_DOMAIN = auto()
    ADDING_RECORD_NAME = auto()
    ADDING_RECORD_CONTENT = auto()
    EDITING_IP = auto()
    EDITING_TTL = auto()
    CLONING_NEW_IP = auto()

# --- Logging Function ---
def log_action(user_id: int, action: str):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] User: {user_id} | Action: {action}\n"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(log_entry)
    except Exception as e:
        logger.error(f"Failed to write to log file: {e}")

# --- User Management ---
def load_users():
    try:
        with open(USER_FILE, 'r') as f:
            data = json.load(f)
            # اطمینان از اینکه ادمین اصلی همیشه در لیست کاربران مجاز است
            if ADMIN_ID not in data.get('authorized_ids', []):
                data['authorized_ids'].append(ADMIN_ID)
                save_users(data['authorized_ids'])
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
        return False # ادمین اصلی نباید حذف شود
    users = load_users()
    if user_id in users:
        users.remove(user_id)
        save_users(users)
        return True
    return False

# --- Blocked User Management ---
def load_blocked_users():
    try:
        with open(BLOCKED_USER_FILE, 'r') as f:
            return json.load(f).get('blocked_ids', [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []

def save_blocked_users(blocked_list):
    with open(BLOCKED_USER_FILE, 'w') as f:
        json.dump({"blocked_ids": sorted(list(set(blocked_list)))}, f, indent=4)

def is_user_blocked(user_id):
    return user_id in load_blocked_users()

def block_user(user_id):
    if user_id == ADMIN_ID:
        return False # ادمین اصلی نباید بلاک شود
    
    blocked_users = load_blocked_users()
    if user_id not in blocked_users:
        blocked_users.append(user_id)
        save_blocked_users(blocked_users)
        # کاربر بلاک شده باید از لیست کاربران مجاز نیز حذف شود
        remove_user(user_id)
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

# --- Unauthorized Access Handlers (REWRITTEN & FIXED) ---
async def show_request_access_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """این تابع منوی درخواست دسترسی را به کاربر غیرمجاز نمایش می‌دهد."""
    keyboard = [[InlineKeyboardButton("✉️ ارسال درخواست دسترسی", callback_data="request_access")]]
    text = "❌ شما به این ربات دسترسی ندارید. برای ارسال درخواست به مدیر، دکمه زیر را فشار دهید."
    
    # اگر این تابع از یک دکمه (callback_query) فراخوانی شده، پیام را ویرایش می‌کند
    if update.callback_query:
        await update.effective_message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    # در غیر این صورت (مثلا بعد از دستور /start)، پیام جدیدی ارسال می‌کند
    else:
        await update.effective_message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_unauthorized_access_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """این تابع درخواست کاربر را برای ادمین ارسال می‌کند."""
    query = update.callback_query
    user = query.from_user
    logger.info(f"Access request initiated by user {user.id} ({user.first_name})")
    
    # ساخت دکمه‌ها برای پیام ادمین
    keyboard = [[
        InlineKeyboardButton("✅ تایید", callback_data=f"access_approve_{user.id}"),
        InlineKeyboardButton("❌ رد", callback_data=f"access_reject_{user.id}"),
        InlineKeyboardButton("🚫 بلاک", callback_data=f"access_block_{user.id}")
    ]]
    text = (f"❗️ درخواست دسترسی جدید\n\n"
            f"**نام:** {user.first_name}\n"
            f"**یوزرنیم:** @{user.username or 'ندارد'}\n"
            f"**شناسه:** `{user.id}`\n\n"
            f"آیا به این کاربر اجازه دسترسی داده شود؟")
    
    try:
        # ارسال پیام به ادمین
        await context.bot.send_message(chat_id=ADMIN_ID, text=text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
        # ویرایش پیام کاربر برای اطلاع‌رسانی
        await query.edit_message_text("✅ درخواست شما با موفقیت برای مدیر ارسال شد. لطفاً منتظر بمانید.")
    except Exception as e:
        logger.error(f"Failed to send access request to admin: {e}")
        await query.edit_message_text("خطا در ارسال درخواست. لطفاً بعداً تلاش کنید.")


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
            InlineKeyboardButton(f"{zone['name']} {status_icon}", callback_data=f"zone_{zone['id']}")
        ])
    
    keyboard.extend([
        # [InlineKeyboardButton("➕ افزودن دامنه", callback_data="add_domain")], # این بخش در کد شما ناقص بود، موقتا کامنت شد
        [InlineKeyboardButton("🔄 رفرش", callback_data="refresh_domains")]
    ])

    if user_id == ADMIN_ID:
        keyboard.append([InlineKeyboardButton("👥 مدیریت کاربران", callback_data="manage_users")])
    
    keyboard.append([InlineKeyboardButton("📜 نمایش لاگ‌ها", callback_data="show_logs")])
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
        [InlineKeyboardButton("🔙 بازگشت به دامنه‌ها", callback_data="back_to_main")]
    ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        await update.effective_message.edit_text(text, parse_mode="Markdown", reply_markup=reply_markup)
    else:
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
    
    keyboard = [[
        InlineKeyboardButton("🖊 تغییر IP", callback_data=f"editip_{record_id}"),
        InlineKeyboardButton("🕒 تغییر TTL", callback_data=f"edittll_{record_id}"),
        InlineKeyboardButton("🔁 پروکسی", callback_data=f"toggle_proxy_{record_id}")
    ]]
    
    action_row = []
    if record['type'] == 'A':
        action_row.append(InlineKeyboardButton("🐑 کلون", callback_data=f"clone_record_{record_id}"))
    
    action_row.append(InlineKeyboardButton("🗑️ حذف", callback_data=f"confirm_delete_record_{record_id}"))
    
    if action_row:
        keyboard.append(action_row)
    
    keyboard.append([InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_records")])
    
    await message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

# --- User Management Menu (REWRITTEN & FIXED) ---
async def manage_users_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    این تابع منوی مدیریت کاربران را نمایش می‌دهد.
    می‌تواند یک پیام موجود را ویرایش کند (اگر از دکمه فراخوانی شود)
    یا یک پیام جدید ارسال کند (اگر پس از یک دستور متنی فراخوانی شود).
    """
    users = load_users()
    keyboard = []
    text = "👥 *لیست کاربران مجاز:*\n\n"
    
    for user_id in users:
        user_info = [f"👤 `{user_id}`"]
        if user_id == ADMIN_ID:
            user_info.append("(ادمین اصلی)")
        
        user_text = " ".join(user_info)
        buttons = []
        if user_id != ADMIN_ID:
            buttons.append(InlineKeyboardButton("🗑 حذف", callback_data=f"delete_user_{user_id}"))
        
        keyboard.append([InlineKeyboardButton(user_text, callback_data="noop")] + buttons)
    
    keyboard.extend([
        [InlineKeyboardButton("➕ افزودن کاربر جدید", callback_data="add_user_prompt")],
        [InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="back_to_main")]
    ])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # اگر این تابع از یک دکمه فراخوانی شده باشد، پیام را ویرایش می‌کند
    if update.callback_query:
        await update.effective_message.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    # در غیر این صورت، یک پیام جدید ارسال می‌کند (مفید برای پس از افزودن کاربر)
    else:
        await update.effective_message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
🤖 *راهنمای ربات مدیریت Cloudflare DNS*

این ربات به شما اجازه می‌دهد تا دامنه‌ها و رکوردهای DNS خود را در حساب Cloudflare به راحتی مدیریت کنید.

---
### **بخش ۱: مدیریت دامنه‌ها**

-   *نمایش دامنه‌ها:* در منوی اصلی، لیست تمام دامنه‌های شما نمایش داده می‌شود.
-   *افزودن دامنه:* با زدن دکمه `➕ افزودن دامنه`، می‌توانید نام دامنه جدیدی (مثلاً `example.com`) را وارد کنید. پس از افزودن، باید **Name Server** های دامنه خود را به مواردی که ربات اعلام می‌کند تغییر دهید.
-   *حذف دامنه:* با زدن دکمه `🗑` کنار هر دامنه، می‌توانید آن را از حساب Cloudflare خود حذف کنید. (این عمل غیرقابل بازگشت است!)

---
### **بخش ۲: مدیریت رکوردها**

برای مدیریت رکوردهای یک دامنه، کافیست روی نام آن در لیست کلیک کنید.

-   *افزودن رکورد:*
    1.  دکمه `➕ افزودن رکورد` را بزنید.
    2.  **نوع رکورد** را انتخاب کنید (`A`, `AAAA`, `CNAME`).
    3.  **نام رکورد** را وارد کنید. برای دامنه اصلی (root)، از علامت `@` استفاده کنید. برای ساب‌دامین، نام آن را وارد کنید (مثلاً `sub`).
    4.  **مقدار رکورد** را وارد کنید (مثلاً آدرس IP برای رکورد `A` یا یک دامنه دیگر برای `CNAME`).
    5.  **TTL** (Time To Live) را انتخاب کنید. مقدار `Auto` توصیه می‌شود.
    6.  **وضعیت پروکسی** را مشخص کنید. فعال بودن پروکسی (`✅`) باعث می‌شود ترافیک شما از طریق Cloudflare عبور کرده و IP اصلی سرور شما مخفی بماند.

-   *ویرایش رکورد:*
    -   با کلیک بر روی دکمه `⚙️` کنار هر رکورد، وارد تنظیمات آن می‌شوید.
    -   *تغییر IP:* برای به‌روزرسانی آدرس IP رکورد.
    -   *تغییر TTL:* برای تغییر زمان کش شدن اطلاعات DNS.
    -   *پروکسی:* برای فعال/غیرفعال کردن پروکسی Cloudflare.

-   *حذف رکورد:* در منوی تنظیمات هر رکورد، با زدن دکمه `🗑 حذف` می‌توانید آن را پاک کنید.

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

async def show_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    message = update.effective_message
    if not is_user_authorized(user_id):
        if is_user_blocked(user_id): return
        await show_request_access_menu(update, context); return
    
    try:
        with open(LOG_FILE, 'r', encoding='utf-8') as f:
            last_lines = f.readlines()[-15:]
    except FileNotFoundError:
        await message.reply_text("هنوز هیچ فعالیتی ثبت نشده است.")
        return
        
    if not last_lines:
        await message.reply_text("هنوز هیچ فعالیتی ثبت نشده است.")
        return

    formatted_log = "📜 **۱۵ فعالیت آخر ربات:**\n"
    for line in reversed(last_lines):
        match = re.search(r'\[(.*?)\] User: (\d+) \| Action: (.*)', line)
        if not match: continue
        timestamp, log_user_id, action = match.groups()
        dt_obj = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
        formatted_time = dt_obj.strftime("%H:%M | %Y/%m/%d")
        icon = "⚙️"
        if "UPDATE IP" in action: icon = "✏️"
        elif "CREATE" in action: icon = "➕"
        elif "DELETE" in action: icon = "🗑️"
        elif "Toggled proxy" in action: icon = "🔁"
        elif "Updated TTL" in action: icon = "🕒"
        formatted_log += f"\n{icon} `{action}`\n_ (توسط کاربر {log_user_id} در {formatted_time})_\n"

    reply_markup = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_main")]])
    if update.callback_query:
        await message.edit_text(formatted_log, parse_mode="Markdown", reply_markup=reply_markup)
    else:
        await message.reply_text(formatted_log, parse_mode="Markdown", reply_markup=reply_markup)


# --- Command and Callback Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_user_authorized(user_id):
        if is_user_blocked(user_id): return
        await show_request_access_menu(update, context)
        return
    await show_main_menu(update, context)

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    uid = query.from_user.id; data = query.data
    
    # --- رسیدگی به درخواست دسترسی (FIXED) ---
    if data == "request_access":
        if is_user_blocked(uid): return
        await handle_unauthorized_access_request(update, context)
        return
    
    # --- رسیدگی به تصمیم ادمین (FIXED) ---
    if data.startswith("access_"):
        if uid != ADMIN_ID:
            await query.answer("این دکمه‌ها فقط برای ادمین است.", show_alert=True); return
        
        parts = data.split("_")
        action, target_user_id_str = parts[1], parts[2]
        target_user_id = int(target_user_id_str)
        
        original_message = query.message.text # ذخیره متن اصلی پیام برای استفاده در پاسخ
        
        if action == "approve":
            add_user(target_user_id); log_action(uid, f"Approved access for user {target_user_id}")
            await query.edit_message_text(f"{original_message}\n\n---\n✅ کاربر `{target_user_id}` تایید شد.", parse_mode="Markdown")
            await context.bot.send_message(chat_id=target_user_id, text="✅ دسترسی شما به ربات تایید شد. برای شروع /start را بزنید.")
        elif action == "reject":
            log_action(uid, f"Rejected access for user {target_user_id}")
            await query.edit_message_text(f"{original_message}\n\n---\n❌ درخواست کاربر `{target_user_id}` رد شد.", parse_mode="Markdown")
            await context.bot.send_message(chat_id=target_user_id, text="❌ متاسفانه درخواست دسترسی شما توسط مدیر رد شد.")
        elif action == "block":
            block_user(target_user_id); log_action(uid, f"Blocked user {target_user_id}")
            await query.edit_message_text(f"{original_message}\n\n---\n🚫 کاربر `{target_user_id}` بلاک شد.", parse_mode="Markdown")
        return

    # --- بررسی دسترسی برای سایر دستورات ---
    if not is_user_authorized(uid):
        if is_user_blocked(uid): return
        await show_request_access_menu(update, context)
        return

    state = user_state.get(uid, {}); zone_id = state.get("zone_id")
    if data == "noop": return

    # --- مدیریت منوهای اصلی ---
    if data in ["back_to_main", "refresh_domains"]: await show_main_menu(update, context)
    elif data == "back_to_records" or data == "refresh_records": await show_records_list(update, context)
    elif data == "show_help": await show_help(update, context)
    elif data == "show_logs": await show_logs(update, context)
    elif data == "cancel_action":
        reset_user_state(uid, keep_zone=True); await query.message.edit_text("❌ عملیات لغو شد."); await show_records_list(update, context)

    # --- مدیریت کاربران (FIXED) ---
    elif data == "manage_users" and uid == ADMIN_ID: await manage_users_menu(update, context)
    elif data == "add_user_prompt" and uid == ADMIN_ID:
        user_state[uid]['mode'] = State.ADDING_USER
        text = "لطفاً شناسه عددی (ID) کاربر مورد نظر را ارسال کنید..."
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="manage_users")]]))
    elif data.startswith("delete_user_") and uid == ADMIN_ID:
        user_to_delete = int(data.split("_")[2])
        if remove_user(user_to_delete): await query.answer("✅ کاربر با موفقیت حذف شد.", show_alert=True)
        else: await query.answer("❌ حذف ناموفق بود (ادمین اصلی قابل حذف نیست).", show_alert=True)
        await manage_users_menu(update, context)

    # --- سایر بخش‌ها بدون تغییر باقی مانده‌اند ---
    elif data.startswith("zone_"):
        selected_zone_id = data.split("_")[1]
        try:
            zone_info = get_zone_info_by_id(selected_zone_id)
            user_state[uid].update({"zone_id": selected_zone_id, "zone_name": zone_info["name"]})
            await show_records_list(update, context)
        except Exception as e: await query.message.edit_text("❌ دریافت اطلاعات دامنه ناموفق بود.")

    elif data.startswith("record_settings_"):
        await show_record_settings(query.message, uid, zone_id, data.split("_")[-1])
    
    elif data.startswith("clone_record_"):
        record_id = data.split("_")[-1]
        try:
            original_record = get_record_details(zone_id, record_id)
            if not original_record: await query.answer("❌ رکورد اصلی یافت نشد.", show_alert=True); return
            user_state[uid]["clone_data"] = { "name": original_record["name"], "type": original_record["type"], "ttl": original_record["ttl"], "proxied": original_record.get("proxied", False) }
            user_state[uid]["mode"] = State.CLONING_NEW_IP
            await query.message.edit_text(f"🐑 **کلون کردن رکورد**\n\n`{original_record['name']}`\n\nلطفاً **IP جدید** را برای افزودن به این رکورد وارد کنید:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]]))
        except Exception as e: logger.error(f"Error starting clone: {e}"); await query.answer("❌ خطا در شروع فرآیند کلون.", show_alert=True)
            
    elif data.startswith("toggle_proxy_"):
        record_id = data.split("_")[-1]
        try:
            record_details = get_record_details(zone_id, record_id)
            success = toggle_proxied_status(zone_id, record_id)
            if success:
                log_action(uid, f"Toggled proxy for '{record_details.get('name', record_id)}'")
                await show_record_settings(query.message, uid, zone_id, record_id)
            else: await query.answer("❌ عملیات ناموفق بود.", show_alert=True)
        except Exception: await query.answer("❌ خطا در ارتباط با API.", show_alert=True)

    elif data.startswith("editip_"):
        record_id = data.split("_")[-1]
        user_state[uid].update({"mode": State.EDITING_IP, "record_id": record_id})
        await query.message.edit_text("📝 لطفاً IP جدید را وارد کنید:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]]))

    elif data.startswith("edittll_"):
        record_id = data.split("_")[-1]
        user_state[uid].update({"mode": State.EDITING_TTL, "record_id": record_id})
        keyboard = [[InlineKeyboardButton("Auto", callback_data=f"update_ttl_{record_id}_1"), InlineKeyboardButton("1 دقیقه", callback_data=f"update_ttl_{record_id}_60")], [InlineKeyboardButton("2 دقیقه", callback_data=f"update_ttl_{record_id}_120"), InlineKeyboardButton("5 دقیقه", callback_data=f"update_ttl_{record_id}_300")], [InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]]
        await query.message.edit_text("⏱ مقدار جدید TTL را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))
    
    elif data.startswith("update_ttl_"):
        parts = data.split("_"); record_id, ttl = parts[2], int(parts[3])
        try:
            record = get_record_details(zone_id, record_id)
            if record:
                success = update_dns_record(zone_id, record_id, record["name"], record["type"], record["content"], ttl, record.get("proxied", False))
                if success:
                    log_action(uid, f"Updated TTL for '{record['name']}' to {ttl}")
                    await query.answer("✅ TTL تغییر یافت."); await show_record_settings(query.message, uid, zone_id, record_id)
                else: await query.answer("❌ عملیات ناموفق بود.")
        except Exception: await query.answer("❌ خطا در ارتباط با API.", show_alert=True)

    elif data == "add_record":
        user_state[uid]["record_data"] = {}
        keyboard = [[InlineKeyboardButton("A", callback_data="select_type_A"), InlineKeyboardButton("AAAA", callback_data="select_type_AAAA"), InlineKeyboardButton("CNAME", callback_data="select_type_CNAME")], [InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]]
        await query.message.edit_text("📌 مرحله ۱ از ۵: نوع رکورد را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("select_type_"):
        user_state[uid]["record_data"]["type"] = data.split("_")[2]
        user_state[uid]["mode"] = State.ADDING_RECORD_NAME
        await query.message.edit_text("📌 مرحله ۲ از ۵: نام رکورد را وارد کنید (مثال: sub یا @ برای ریشه)", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]]))
    
    elif data.startswith("select_ttl_"):
        user_state[uid]["record_data"]["ttl"] = int(data.split("_")[2])
        keyboard = [[InlineKeyboardButton("✅ بله", callback_data="select_proxied_true"), InlineKeyboardButton("❌ خیر", callback_data="select_proxied_false")], [InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]]
        await query.message.edit_text("📌 مرحله ۵ از ۵: آیا پروکسی فعال باشد؟", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("select_proxied_"):
        user_state[uid]["record_data"]["proxied"] = data.endswith("true")
        r_data = user_state[uid]["record_data"]; zone_name = state["zone_name"]; name = r_data["name"]
        full_name = f"{name}.{zone_name}" if name != "@" and not name.endswith(f".{zone_name}") else (zone_name if name == "@" else name)
        await query.message.edit_text("⏳ در حال ایجاد رکورد...")
        try:
            success = create_dns_record(zone_id, r_data["type"], full_name, r_data["content"], r_data["ttl"], r_data["proxied"])
            if success:
                log_action(uid, f"CREATE record '{full_name}' with content '{r_data['content']}'")
                await query.message.edit_text("✅ رکورد با موفقیت اضافه شد.")
            else: await query.message.edit_text("❌ افزودن رکورد ناموفق بود.")
        except Exception as e: logger.error(f"Error creating record: {e}"); await query.message.edit_text("❌ خطا در ایجاد رکورد.")
        finally: reset_user_state(uid, keep_zone=True); await show_records_list(update, context)

    elif data.startswith("confirm_delete_"):
        item_type = "record" if data.startswith("confirm_delete_record_") else "zone"
        item_id = data.split("_")[-1]
        text = f"❗ آیا از حذف این {'رکورد' if item_type == 'record' else 'دامنه'} مطمئن هستید؟"
        back_action = f"record_settings_{item_id}" if item_type == 'record' else 'back_to_main'
        keyboard = [[InlineKeyboardButton("✅ بله، حذف شود", callback_data=f"delete_{item_type}_{item_id}")], [InlineKeyboardButton("❌ لغو", callback_data=back_action)]]
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("delete_record_"):
        record_id = data.split("_")[-1]
        await query.message.edit_text("⏳ در حال حذف رکورد...")
        try:
            record_details = get_record_details(zone_id, record_id)
            success = delete_dns_record(zone_id, record_id)
            if success:
                log_action(uid, f"DELETE record '{record_details.get('name', record_id)}'")
                await query.message.edit_text("✅ رکورد حذف شد.")
            else: await query.message.edit_text("❌ حذف رکورد ناموفق بود.")
        except Exception: await query.message.edit_text("❌ خطا در حذف رکورد.")
        finally: await show_records_list(update, context)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_user_authorized(uid):
        if is_user_blocked(uid): return
        # اگر کاربر غیرمجاز پیامی ارسال کند، به او منوی درخواست دسترسی نمایش داده می‌شود
        await show_request_access_menu(update, context)
        return
    
    state = user_state.get(uid, {}); mode = state.get("mode"); text = update.message.text.strip()
    if not mode or mode == State.NONE: return

    # --- مدیریت پیام‌های متنی بر اساس حالت کاربر (FIXED) ---
    if mode == State.ADDING_USER and uid == ADMIN_ID:
        try:
            new_user_id = int(text)
            if add_user(new_user_id):
                await update.message.reply_text(f"✅ کاربر `{new_user_id}` با موفقیت اضافه شد.", parse_mode="Markdown")
                log_action(uid, f"Added user {new_user_id}")
            else:
                await update.message.reply_text("⚠️ این کاربر از قبل در لیست وجود دارد.")
        except ValueError:
            await update.message.reply_text("❌ ورودی نامعتبر است. لطفاً شناسه عددی ارسال کنید.")
        
        # پاک کردن حالت و نمایش مجدد منوی مدیریت کاربران
        reset_user_state(uid)
        await manage_users_menu(update, context) # این تابع یک منوی جدید ارسال خواهد کرد
        return

    # --- سایر بخش‌ها بدون تغییر ---
    if mode == State.CLONING_NEW_IP:
        new_ip = text; clone_data = user_state[uid].get("clone_data", {}); zone_id = state.get("zone_id"); full_name = clone_data.get("name")
        if not all([new_ip, clone_data, zone_id, full_name]):
            await update.message.reply_text("❌ خطای داخلی."); reset_user_state(uid, keep_zone=True); return
        await update.message.reply_text(f"⏳ در حال افزودن IP `{new_ip}`...", parse_mode="Markdown")
        try:
            success = create_dns_record(zone_id, clone_data["type"], full_name, new_ip, clone_data["ttl"], clone_data["proxied"])
            if success:
                log_action(uid, f"CREATE (Clone) record '{full_name}' with IP '{new_ip}'")
                await update.message.reply_text("✅ رکورد جدید با موفقیت اضافه شد.")
            else: await update.message.reply_text("❌ عملیات ناموفق بود.")
        except Exception as e: logger.error(f"Error creating cloned record: {e}"); await update.message.reply_text("❌ خطا در ارتباط با API.")
        finally: reset_user_state(uid, keep_zone=True); await show_records_list(update, context)

    elif mode == State.EDITING_IP:
        new_ip = text; record_id = state.get("record_id"); zone_id = state.get("zone_id")
        await update.message.reply_text(f"⏳ در حال به‌روزرسانی IP...", parse_mode="Markdown")
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
        user_state[uid]["record_data"]["name"] = text
        user_state[uid]["mode"] = State.ADDING_RECORD_CONTENT
        await update.message.reply_text("📌 مرحله ۳ از ۵: مقدار رکورد را وارد کنید (مثلاً IP یا آدرس):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]]))
    
    elif mode == State.ADDING_RECORD_CONTENT:
        user_state[uid]["record_data"]["content"] = text
        user_state[uid].pop("mode", None) 
        keyboard = [[InlineKeyboardButton("Auto", callback_data="select_ttl_1"), InlineKeyboardButton("1 دقیقه", callback_data="select_ttl_60")], [InlineKeyboardButton("2 دقیقه", callback_data="select_ttl_120"), InlineKeyboardButton("5 دقیقه", callback_data="select_ttl_300")], [InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]]
        await update.message.reply_text("📌 مرحله ۴ از ۵: مقدار TTL را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))

# --- Main Application ---
def main():
    load_users() # بارگذاری اولیه کاربران و اطمینان از وجود ادمین
    logger.info("Starting bot...")
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("logs", show_logs))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    app.run_polling()

if __name__ == "__main__":
    main()
