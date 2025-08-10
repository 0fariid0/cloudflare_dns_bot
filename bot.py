import logging
import json
from collections import defaultdict
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters)

# فرض می‌شود این فایل‌ها در کنار bot.py وجود دارند
from cloudflare_api import *
from config import BOT_TOKEN, ADMIN_ID

# --- راه‌اندازی اولیه ---
logger = logging.getLogger(__name__)
RECORDS_PER_PAGE = 10
user_state = defaultdict(lambda: {"page": 0})
USER_FILE = "users.json"


# --- توابع مدیریت کاربر ---
def load_users():
    """لیست کاربران مجاز را از فایل JSON بارگذاری می‌کند."""
    try:
        with open(USER_FILE, 'r') as f:
            data = json.load(f)
            if ADMIN_ID not in data['authorized_ids']:
                data['authorized_ids'].append(ADMIN_ID)
            return data['authorized_ids']
    except (FileNotFoundError, json.JSONDecodeError):
        save_users([ADMIN_ID])
        return [ADMIN_ID]

def save_users(users_list):
    """لیست کاربران را در فایل JSON ذخیره می‌کند."""
    with open(USER_FILE, 'w') as f:
        json.dump({"authorized_ids": sorted(list(set(users_list)))}, f, indent=4)

def is_user_authorized(user_id):
    """بررسی می‌کند آیا کاربر در لیست مجاز قرار دارد یا خیر."""
    return user_id in load_users()

def add_user(user_id):
    """کاربر جدید را به لیست اضافه می‌کند."""
    users = load_users()
    if user_id not in users:
        users.append(user_id)
        save_users(users)
        return True
    return False

def remove_user(user_id):
    """کاربر را از لیست حذف می‌کند. ادمین اصلی قابل حذف نیست."""
    if user_id == ADMIN_ID:
        return False
    users = load_users()
    if user_id in users:
        users.remove(user_id)
        save_users(users)
        return True
    return False

def reset_user_state(uid, keep_zone=False):
    """وضعیت کاربر را ریست می‌کند."""
    if keep_zone and uid in user_state:
        zone_id = user_state[uid].get("zone_id")
        zone_name = user_state[uid].get("zone_name")
        user_state[uid] = {"zone_id": zone_id, "zone_name": zone_name, "page": 0}
    else:
        user_state.pop(uid, None)


# --- توابع اصلی ربات ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_user_authorized(user_id):
        return await (update.message or update.callback_query.message).reply_text("❌ شما اجازه دسترسی ندارید.")

    reset_user_state(user_id)
    try:
        zones = get_zones()
    except Exception as e:
        logger.error(f"Could not fetch zones: {e}")
        await (update.message or update.callback_query.message).reply_text("❌ خطا در ارتباط با Cloudflare.")
        return

    keyboard = []
    for zone in zones:
        status_icon = "✅" if zone["status"] == "active" else "⏳"
        keyboard.append([
            InlineKeyboardButton(f"{zone['name']} {status_icon}", callback_data=f"zone_{zone['id']}"),
            InlineKeyboardButton("🗑", callback_data=f"confirm_delete_zone_{zone['id']}")
        ])
    keyboard.append([
        InlineKeyboardButton("➕ افزودن دامنه", callback_data="add_domain"),
        InlineKeyboardButton("🔄 رفرش", callback_data="refresh_domains")
    ])
    if user_id == ADMIN_ID:
        keyboard.append([InlineKeyboardButton("👥 مدیریت کاربران", callback_data="manage_users")])
    keyboard.append([InlineKeyboardButton("ℹ️ راهنما", callback_data="show_help")])

    welcome_text = "👋 به ربات مدیریت DNS خوش آمدید!\n\n🌐 دامنه‌های متصل:"
    message = update.message or update.callback_query.message
    if update.callback_query:
        await message.edit_text(welcome_text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await message.reply_text(welcome_text, reply_markup=InlineKeyboardMarkup(keyboard))


async def manage_users_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """منوی مدیریت کاربران را نمایش می‌دهد."""
    message = update.callback_query.message
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
    keyboard.append([InlineKeyboardButton("➕ افزودن کاربر جدید", callback_data="add_user_prompt")])
    keyboard.append([InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="back_to_main")])
    await message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # This function is complete
    text = "..." # متن راهنما
    keyboard = [[InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="back_to_domains")]]
    await (update.callback_query.message or update.message).edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def refresh_records(uid, update: Update, page=0):
    """بارگذاری و نمایش رکوردها با pagination مدرن‌تر و مقاوم در برابر خطا.

    این نسخه:
    - از متن ساده (plain) استفاده می‌کند تا مشکلات فرمتینگ با parse_mode کاهش یابد.
    - از وریفای برای تعیین پیام (edit یا reply) استفاده می‌کند.
    - دکمه‌های شماره‌گذاری، نوار پیشرفت و export ساده را نگه می‌دارد.
    """
    zone_id = user_state[uid].get("zone_id")
    zone_name = user_state[uid].get("zone_name", "")

    # تلاش برای گرفتن رکوردها
    try:
        records = get_dns_records(zone_id)
    except Exception as e:
        logger.error(f"Could not fetch records for zone {zone_id}: {e}")
        # سعی در پاسخ به پیام کاربر (اگر ممکن باشد)
        if hasattr(update, "callback_query") and update.callback_query:
            try:
                await update.callback_query.message.reply_text("❌ خطا در دریافت لیست رکوردها.")
            except Exception:
                logger.exception("Failed to notify user about fetch error")
        elif hasattr(update, "message") and update.message:
            try:
                await update.message.reply_text("❌ خطا در دریافت لیست رکوردها.")
            except Exception:
                logger.exception("Failed to notify user about fetch error")
        return

    total_records = len(records)
    total_pages = 0 if total_records == 0 else (total_records - 1) // RECORDS_PER_PAGE + 1

    # clamp page
    if page < 0:
        page = 0
    if total_pages > 0 and page > total_pages - 1:
        page = total_pages - 1

    user_state[uid]["page"] = page
    page_display = page + 1 if total_pages > 0 else 0

    # progress dots
    if total_pages == 0:
        dots = "(هیچ رکوردی)"
    else:
        max_dots = min(total_pages, 7)
        center = page
        start_dot = max(0, min(center - max_dots // 2, total_pages - max_dots))
        dots_list = ["○"] * total_pages
        for i in range(start_dot, start_dot + max_dots):
            dots_list[i] = "●" if i == page else "○"
        dots = "".join(dots_list[start_dot:start_dot + max_dots])

    header = f"📋 رکوردهای DNS — {zone_name}
{total_records} رکورد • صفحه {page_display}/{total_pages}
{dots}

"

    # build keyboard and body lines
    keyboard = []
    body_text = header

    if total_records == 0:
        body_text += "هیچ رکوردی برای این دامنه ثبت نشده است."
    else:
        start_index = page * RECORDS_PER_PAGE
        end_index = min(start_index + RECORDS_PER_PAGE, total_records)
        for rec in records[start_index:end_index]:
            if rec.get("type") in ["A", "AAAA", "CNAME"]:
                name = rec.get("name", "")
                if zone_name and name.endswith(f".{zone_name}"):
                    name = name.replace(f".{zone_name}", "")
                elif name == zone_name:
                    name = "@"
                content = rec.get("content", "")
                summary = f"{name} — {content} ({rec.get('type')})"
                # use a single-button row pointing to record settings (safer label length)
                keyboard.append([InlineKeyboardButton(summary, callback_data=f"record_settings_{rec.get('id')}")])

    # navigation
    if total_pages > 1:
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("⏮️", callback_data="goto_page_1"))
            nav_row.append(InlineKeyboardButton("⬅️", callback_data="page_prev"))
        num_buttons = min(5, total_pages)
        start_num = max(1, page_display - num_buttons // 2)
        if start_num + num_buttons - 1 > total_pages:
            start_num = max(1, total_pages - num_buttons + 1)
        num_row = []
        for p in range(start_num, start_num + num_buttons):
            label = f"[{p}]" if p == page_display else str(p)
            num_row.append(InlineKeyboardButton(label, callback_data=f"goto_page_{p}"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton("➡️", callback_data="page_next"))
            nav_row.append(InlineKeyboardButton("⏭️", callback_data=f"goto_page_{total_pages}"))
        if nav_row:
            keyboard.append(nav_row)
        keyboard.append(num_row)

    # action and export rows
    keyboard.append([InlineKeyboardButton("➕ افزودن رکورد", callback_data="add_record"), InlineKeyboardButton("🔄 رفرش", callback_data="refresh_records")])
    keyboard.append([InlineKeyboardButton("📤 Export JSON", callback_data="export_json"), InlineKeyboardButton("📤 Export CSV", callback_data="export_csv")])
    keyboard.append([InlineKeyboardButton("🔙 بازگشت به دامنه‌ها", callback_data="back_to_domains")])

    # choose message object (edit if callback, otherwise reply)
    message_obj = None
    if hasattr(update, "callback_query") and update.callback_query and getattr(update.callback_query, "message", None):
        message_obj = update.callback_query.message
    elif hasattr(update, "message") and update.message:
        message_obj = update.message

    if not message_obj:
        logger.warning("No message object available to send refresh_records output")
        return

    try:
        # prefer edit_text to keep chat tidy
        await message_obj.edit_text(body_text, reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception:
        try:
            await message_obj.reply_text(body_text, reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception:
            logger.exception("Failed to send refresh_records message")

async def show_record_settings(message, uid, zone_id, record_id):
    # This function is complete
    try:
        record = get_record_details(zone_id, record_id)
        if not record:
            await message.reply_text("❌ رکورد یافت نشد. ممکن است حذف شده باشد.")
            return
    except Exception as e:
        logger.error(f"Could not fetch record details for {record_id}: {e}")
        await message.reply_text("❌ خطا در دریافت اطلاعات رکورد.")
        return
    user_state[uid]["record_id"] = record_id
    text = (f"⚙️ تنظیمات رکورد: `{record['name']}`\n\n**Type:** `{record['type']}`\n**IP:** `{record['content']}`\n**TTL:** `{record['ttl']}`\n**Proxied:** {'✅ فعال' if record.get('proxied') else '❌ غیرفعال'}")
    keyboard = [[InlineKeyboardButton("🖊 تغییر IP", callback_data=f"editip_{record_id}"), InlineKeyboardButton("🕒 تغییر TTL", callback_data=f"edittll_{record_id}"), InlineKeyboardButton("🔁 پروکسی", callback_data=f"toggle_proxy_{record_id}")], [InlineKeyboardButton("🗑 حذف", callback_data=f"confirm_delete_{record_id}"), InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_records")]]
    await message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    data = query.data
    
    if not is_user_authorized(uid):
        return await query.message.reply_text("❌ شما اجازه دسترسی به این ربات را ندارید.")

    if data == "back_to_domains" or data == "refresh_domains" or data == "back_to_main":
        await start(update, context)
        return
        
    if data == "manage_users":
        if uid == ADMIN_ID: await manage_users_menu(update, context)
        return

    if data == "add_user_prompt":
        if uid == ADMIN_ID:
            user_state[uid]['mode'] = 'adding_user'
            text = "لطفاً شناسه عددی (ID) کاربر مورد نظر را ارسال کنید.\n\nراهنمایی: از کاربر بخواهید یک پیام به ربات @userinfobot ارسال کند تا شناسه خود را دریافت نماید."
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="manage_users")]]))
        return

    if data.startswith("delete_user_"):
        if uid == ADMIN_ID:
            user_to_delete = int(data.split("_")[2])
            if remove_user(user_to_delete):
                await query.answer("✅ کاربر با موفقیت حذف شد.", show_alert=True)
            else:
                await query.answer("❌ حذف ناموفق بود.", show_alert=True)
            await manage_users_menu(update, context)
        return

    if data == "back_to_records":
        await refresh_records(uid, update, page=user_state[uid].get("page", 0))
        return

    if data == "show_help":
        await show_help(update, context)
        return
    
    if data == "cancel_action":
        reset_user_state(uid, keep_zone=True)
        await query.message.edit_text("❌ عملیات لغو شد.")
        await refresh_records(uid, update, page=user_state[uid].get("page", 0))
        return
        
    if data.startswith("zone_"):
        zone_id = data.split("_")[1]
        try:
            zone_info = get_zone_info_by_id(zone_id)
            user_state[uid].update({"zone_id": zone_id, "zone_name": zone_info["name"]})
            await refresh_records(uid, update)
        except Exception as e:
            await query.message.reply_text("❌ دریافت اطلاعات دامنه ناموفق بود.")
        return

    if data == "refresh_records":
        await query.answer("🔄 در حال بروزرسانی...")
        await refresh_records(uid, update, page=user_state[uid].get("page", 0))
        return

    if data == "page_next":
        await refresh_records(uid, update, page=user_state[uid].get("page", 0) + 1)
        return

    if data == "page_prev":
        await refresh_records(uid, update, page=user_state[uid].get("page", 0) - 1)
        return

    if data.startswith("goto_page_"):
        try:
            p = int(data.split("_")[2])
            # goto_page uses 1-based indexing in the button labels
            await refresh_records(uid, update, page=max(0, p - 1))
        except Exception:
            await query.answer("❌ شماره صفحه نامعتبر است.", show_alert=True)
        return

    if data == "export_json":
        try:
            zone_id_local = user_state[uid].get("zone_id")
            records = get_dns_records(zone_id_local)
            text = json.dumps(records, ensure_ascii=False, indent=2)
            from io import BytesIO
            bio = BytesIO(text.encode('utf-8'))
            bio.name = f"{user_state[uid].get('zone_name','records')}.json"
            await context.bot.send_document(chat_id=uid, document=bio)
        except Exception:
            await query.answer("❌ امکان تهیه خروجی وجود ندارد.", show_alert=True)
        return

    if data == "export_csv":
        try:
            zone_id_local = user_state[uid].get("zone_id")
            records = get_dns_records(zone_id_local)
            import csv
            from io import StringIO, BytesIO
            si = StringIO()
            writer = csv.writer(si)
            writer.writerow(["id","type","name","content","ttl","proxied"])
            for r in records:
                writer.writerow([r.get('id',''), r.get('type',''), r.get('name',''), r.get('content',''), r.get('ttl',''), r.get('proxied',False)])
            csv_bytes = si.getvalue().encode('utf-8')
            bio = BytesIO(csv_bytes)
            bio.name = f"{user_state[uid].get('zone_name','records')}.csv"
            await context.bot.send_document(chat_id=uid, document=bio)
        except Exception:
            await query.answer("❌ امکان تهیه خروجی وجود ندارد.", show_alert=True)
        return

    zone_id = user_state[uid].get("zone_id")
    if not zone_id:
        await query.message.reply_text("لطفاً ابتدا یک دامنه را انتخاب کنید.")
        await start(update, context)
        return

    if data.startswith("record_settings_"):
        record_id = data.split("_")[2]
        await show_record_settings(query.message, uid, zone_id, record_id)

    elif data.startswith("toggle_proxy_"):
        record_id = data.split("_")[2]
        try:
            success = toggle_proxied_status(zone_id, record_id)
            await query.answer("✅ وضعیت پروکسی تغییر کرد." if success else "❌ عملیات ناموفق بود.")
            if success: await show_record_settings(query.message, uid, zone_id, record_id)
        except Exception as e:
            await query.answer("❌ خطا در ارتباط با API.", show_alert=True)
            
    elif data.startswith("edittll_"):
        record_id = data.split("_")[1]
        user_state[uid].update({"mode": "editing_ttl", "record_id": record_id})
        keyboard = [[InlineKeyboardButton("Auto (خودکار)", callback_data=f"update_ttl_{record_id}_1"), InlineKeyboardButton("1 دقیقه", callback_data=f"update_ttl_{record_id}_60")], [InlineKeyboardButton("2 دقیقه", callback_data=f"update_ttl_{record_id}_120"), InlineKeyboardButton("5 دقیقه", callback_data=f"update_ttl_{record_id}_300")], [InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]]
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
        except Exception as e:
            await query.answer("❌ خطا در ارتباط با API.", show_alert=True)
            
    elif data.startswith("editip_"):
        record_id = data.split("_")[1]
        user_state[uid].update({"mode": "editing_ip", "record_id": record_id})
        await query.message.edit_text("📝 لطفاً IP جدید را وارد کنید:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]]))

    elif data == "add_record":
        user_state[uid].update({"mode": "adding_record_step", "record_step": 0, "record_data": {}})
        keyboard = [[InlineKeyboardButton("A", callback_data="select_type_A"), InlineKeyboardButton("AAAA", callback_data="select_type_AAAA"), InlineKeyboardButton("CNAME", callback_data="select_type_CNAME")], [InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]]
        await query.message.edit_text("📌 مرحله ۱ از ۵: نوع رکورد را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("select_type_"):
        user_state[uid]["record_data"] = {"type": data.split("_")[2]}
        user_state[uid]["record_step"] = 1
        await query.message.edit_text("📌 مرحله ۲ از ۵: نام رکورد را وارد کنید (مثال: sub یا @ برای ریشه)", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]]))

    elif data.startswith("select_ttl_"):
        user_state[uid]["record_data"]["ttl"] = int(data.split("_")[2])
        user_state[uid]["record_step"] = 4
        keyboard = [[InlineKeyboardButton("✅ بله", callback_data="select_proxied_true"), InlineKeyboardButton("❌ خیر", callback_data="select_proxied_false")], [InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]]
        await query.message.edit_text("📌 مرحله ۵ از ۵: آیا پروکسی فعال باشد؟", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("select_proxied_"):
        user_state[uid]["record_data"]["proxied"] = data.endswith("true")
        r_data = user_state[uid]["record_data"]
        zone_name = user_state[uid]["zone_name"]
        name = r_data["name"]
        if name == "@": name = zone_name
        elif not name.endswith(f".{zone_name}"): name = f"{name}.{zone_name}"
        await query.message.edit_text("⏳ در حال ایجاد رکورد...")
        try:
            success = create_dns_record(zone_id, r_data["type"], name, r_data["content"], r_data["ttl"], r_data["proxied"])
            await query.message.edit_text("✅ رکورد با موفقیت اضافه شد." if success else "❌ افزودن رکورد ناموفق بود.")
        except Exception as e:
            await query.message.edit_text("❌ خطا در ایجاد رکورد.")
        finally:
            reset_user_state(uid, keep_zone=True)
            await refresh_records(uid, update)

    elif data.startswith("confirm_delete_"):
        record_id = data.split("_")[2]
        keyboard = [[InlineKeyboardButton("✅ بله، حذف شود", callback_data=f"delete_record_{record_id}")], [InlineKeyboardButton("❌ لغو", callback_data="back_to_records")]]
        await query.message.edit_text("❗ آیا از حذف این رکورد مطمئن هستید؟", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("delete_record_"):
        record_id = data.split("_")[2]
        await query.message.edit_text("⏳ در حال حذف رکورد...")
        try:
            success = delete_dns_record(zone_id, record_id)
            await query.message.edit_text("✅ رکورد حذف شد." if success else "❌ حذف رکورد ناموفق بود.")
        except Exception as e:
            await query.message.edit_text("❌ خطا در حذف رکورد.")
        finally:
            await refresh_records(uid, update, page=user_state[uid].get("page", 0))


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_user_authorized(uid):
        return await update.message.reply_text("❌ شما اجازه دسترسی ندارید.")
        
    state = user_state.get(uid, {})
    mode = state.get("mode")
    text = update.message.text.strip()
    if not mode: return

    if mode == "adding_user":
        if uid == ADMIN_ID:
            try:
                new_user_id = int(text)
                if add_user(new_user_id):
                    await update.message.reply_text(f"✅ کاربر `{new_user_id}` با موفقیت اضافه شد.")
                else:
                    await update.message.reply_text("⚠️ این کاربر از قبل در لیست وجود دارد.")
            except ValueError:
                await update.message.reply_text("❌ ورودی نامعتبر است. لطفاً فقط شناسه عددی کاربر را ارسال کنید.")
            reset_user_state(uid)
            # Create a mock update to resend the management menu
            from unittest.mock import Mock
            mock_query = Mock(from_user=update.effective_user, message=update.message)
            mock_update = Mock(callback_query=mock_query)
            await manage_users_menu(mock_update, context)
        return

    if mode == "adding_domain":
        await update.message.reply_text(f"⏳ در حال افزودن دامنه `{text}`...")
        try:
            success, result = add_domain_to_cloudflare(text)
            if success:
                zone_info = get_zone_info_by_id(result['id'])
                ns = "\n".join(zone_info.get("name_servers", ["N/A"]))
                await update.message.reply_text(f"✅ دامنه `{text}` با موفقیت اضافه شد.\n**وضعیت:** `{zone_info['status']}`\n\n❗️ لطفاً Name Server های دامنه خود را به موارد زیر تغییر دهید:\n`{ns}`", parse_mode="Markdown")
            else:
                await update.message.reply_text(f"❌ خطا در افزودن دامنه: {result}")
        except Exception as e:
            await update.message.reply_text(f"❌ خطا در افزودن دامنه.")
        finally:
            reset_user_state(uid)
            await start(update, context)
        return

    zone_id = state.get("zone_id")
    record_id = state.get("record_id")

    if mode == "editing_ip" and zone_id and record_id:
        new_ip = text
        await update.message.reply_text(f"⏳ در حال به‌روزرسانی IP به `{new_ip}`...")
        try:
            record = get_record_details(zone_id, record_id)
            if record:
                success = update_dns_record(zone_id, record_id, record["name"], record["type"], new_ip, record["ttl"], record.get("proxied", False))
                if success:
                    await update.message.reply_text("✅ آی‌پی با موفقیت به‌روز شد.")
                    new_msg = await update.message.reply_text("...در حال بارگذاری تنظیمات جدید")
                    await show_record_settings(new_msg, uid, zone_id, record_id)
                else: await update.message.reply_text("❌ به‌روزرسانی ناموفق بود.")
            else: await update.message.reply_text("❌ رکورد مورد نظر یافت نشد.")
        except Exception as e:
            await update.message.reply_text("❌ خطا در ارتباط با API.")
        finally:
            reset_user_state(uid, keep_zone=True)

    elif mode == "adding_record_step":
        step = state.get("record_step", 0)
        if step == 1:
            user_state[uid]["record_data"]["name"] = text
            user_state[uid]["record_step"] = 2
            await update.message.reply_text("📌 مرحله ۳ از ۵: مقدار رکورد را وارد کنید (مثلاً IP یا آدرس):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]]))
        elif step == 2:
            user_state[uid]["record_data"]["content"] = text
            user_state[uid]["record_step"] = 3
            keyboard = [[InlineKeyboardButton("Auto (خودکار)", callback_data="select_ttl_1"), InlineKeyboardButton("1 دقیقه", callback_data="select_ttl_60")], [InlineKeyboardButton("2 دقیقه", callback_data="select_ttl_120"), InlineKeyboardButton("5 دقیقه", callback_data="select_ttl_300")], [InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]]
            await update.message.reply_text("📌 مرحله ۴ از ۵: مقدار TTL را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))


if __name__ == "__main__":
    load_users()
    logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
    logger.info("Starting bot...")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()
