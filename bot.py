import logging
import json
import io
import csv
from collections import defaultdict
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters)

# ماژول‌های خودت (Cloudflare API و config)
from cloudflare_api import *
from config import BOT_TOKEN, ADMIN_ID

# --- تنظیمات اولیه ---
logger = logging.getLogger(__name__)
RECORDS_PER_PAGE = 10
user_state = defaultdict(lambda: {"page": 0})
USER_FILE = "users.json"


# --- مدیریت کاربران (فایل JSON ساده) ---
def load_users():
    """لیست کاربران مجاز را از فایل JSON بارگذاری می‌کند."""
    try:
        with open(USER_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if ADMIN_ID not in data.get('authorized_ids', []):
                data['authorized_ids'].append(ADMIN_ID)
            return data.get('authorized_ids', [])
    except (FileNotFoundError, json.JSONDecodeError):
        save_users([ADMIN_ID])
        return [ADMIN_ID]


def save_users(users_list):
    """لیست کاربران را در فایل JSON ذخیره می‌کند."""
    with open(USER_FILE, 'w', encoding='utf-8') as f:
        json.dump({"authorized_ids": sorted(list(set(users_list)))}, f, indent=4, ensure_ascii=False)


def is_user_authorized(user_id):
    """بررسی می‌کند آیا کاربر در لیست مجاز قرار دارد یا خیر."""
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
    # Authorization
    if not is_user_authorized(user_id):
        # اگر callback داشته باشیم، پیام در callback.message است، در غیر این صورت update.message
        msg = (update.callback_query.message if update.callback_query else update.message)
        return await msg.reply_text("❌ شما اجازه دسترسی ندارید.")

    reset_user_state(user_id)
    try:
        zones = get_zones()
    except Exception as e:
        logger.exception("Could not fetch zones")
        msg = (update.callback_query.message if update.callback_query else update.message)
        return await msg.reply_text("❌ خطا در ارتباط با Cloudflare.")

    keyboard = []
    for zone in zones:
        status_icon = "✅" if zone.get("status") == "active" else "⏳"
        keyboard.append([
            InlineKeyboardButton(f"{zone.get('name')} {status_icon}", callback_data=f"zone_{zone.get('id')}"),
            InlineKeyboardButton("🗑", callback_data=f"confirm_delete_zone_{zone.get('id')}")
        ])

    keyboard.append([
        InlineKeyboardButton("➕ افزودن دامنه", callback_data="add_domain"),
        InlineKeyboardButton("🔄 رفرش", callback_data="refresh_domains")
    ])
    if user_id == ADMIN_ID:
        keyboard.append([InlineKeyboardButton("👥 مدیریت کاربران", callback_data="manage_users")])
    keyboard.append([InlineKeyboardButton("ℹ️ راهنما", callback_data="show_help")])

    welcome_text = (
        "👋 به ربات مدیریت DNS خوش آمدید!\n\n"
        "🌐 دامنه‌های متصل:"
    )

    if update.callback_query:
        await update.callback_query.message.edit_text(welcome_text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.message.reply_text(welcome_text, reply_markup=InlineKeyboardMarkup(keyboard))


async def manage_users_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """منوی مدیریت کاربران را نمایش می‌دهد."""
    message = update.callback_query.message
    users = load_users()
    keyboard = []
    text = "👥 *لیست کاربران مجاز:*\n\n"
    for uid in users:
        user_text = f"👤 `{uid}`"
        buttons = []
        if uid == ADMIN_ID:
            user_text += " (ادمین اصلی)"
        else:
            buttons.append(InlineKeyboardButton("🗑 حذف", callback_data=f"delete_user_{uid}"))
        keyboard.append([InlineKeyboardButton(user_text, callback_data="noop")] + buttons)
    keyboard.append([InlineKeyboardButton("➕ افزودن کاربر جدید", callback_data="add_user_prompt")])
    keyboard.append([InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="back_to_main")])
    await message.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "راهنما:\n"
        "• از منوی اصلی، دامنه را انتخاب کنید.\n"
        "• در صفحه رکوردها می‌توانید رکوردها را مشاهده، ویرایش یا حذف کنید.\n"
        "• فقط کاربران مجاز توانایی استفاده از ربات را دارند."
    )
    msg = (update.callback_query.message if update.callback_query else update.message)
    await msg.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="back_to_domains")]]))


async def refresh_records(uid, update: Update, page=0):
    """بارگذاری و نمایش رکوردها با pagination بهبود یافته."""
    # بررسی وجود zone برای کاربر
    if uid not in user_state or "zone_id" not in user_state[uid]:
        msg = (update.callback_query.message if update.callback_query else update.message)
        try:
            await msg.reply_text("⚠️ ابتدا یک دامنه انتخاب کنید.")
        except Exception:
            logger.warning("No message object to reply in refresh_records")
        return

    zone_id = user_state[uid]["zone_id"]
    zone_name = user_state[uid].get("zone_name", "")

    try:
        records = get_dns_records(zone_id) or []
    except Exception as e:
        logger.exception("Could not fetch records")
        msg = (update.callback_query.message if update.callback_query else update.message)
        try:
            await msg.reply_text("❌ خطا در دریافت لیست رکوردها.")
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

    # نوار پیشرفت
    if total_pages == 0:
        dots = "(هیچ رکوردی)"
    else:
        max_dots = min(total_pages, 7)
        start_dot = max(0, min(page - max_dots // 2, total_pages - max_dots))
        dots_list = ["○"] * total_pages
        for i in range(start_dot, start_dot + max_dots):
            dots_list[i] = "●" if i == page else "○"
        dots = "".join(dots_list[start_dot:start_dot + max_dots])

    header = f"📋 رکوردهای DNS دامنه: `{zone_name}` — {total_records} رکورد (صفحه {page_display} از {total_pages})\n{dots}\n\n"

    body_text = header
    keyboard = []

    if total_records == 0:
        body_text += "هیچ رکوردی برای این دامنه ثبت نشده است."
    else:
        start_index = page * RECORDS_PER_PAGE
        end_index = min(start_index + RECORDS_PER_PAGE, total_records)
        for rec in records[start_index:end_index]:
            try:
                if rec.get("type") in ["A", "AAAA", "CNAME"]:
                    name = rec.get("name", "")
                    if zone_name and name.endswith(f".{zone_name}"):
                        name = name[: -(len(zone_name) + 1)]
                    elif name == zone_name:
                        name = "@"
                    content = rec.get("content", "")
                    # نمایش خلاصه رکورد؛ استفاده از یک دکمه برای رفتن به تنظیمات رکورد
                    label = f"{name} — {content} ({rec.get('type')})"
                    keyboard.append([InlineKeyboardButton(label, callback_data=f"record_settings_{rec.get('id')}")])
            except Exception:
                logger.exception("Error while building record row")

    # ناوبری صفحات
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

    # عمل‌ها
    keyboard.append([InlineKeyboardButton("➕ افزودن رکورد", callback_data="add_record"), InlineKeyboardButton("🔄 رفرش", callback_data="refresh_records")])
    keyboard.append([InlineKeyboardButton("📤 Export JSON", callback_data="export_json"), InlineKeyboardButton("📤 Export CSV", callback_data="export_csv")])
    keyboard.append([InlineKeyboardButton("🔙 بازگشت به دامنه‌ها", callback_data="back_to_domains")])

    # ارسال یا ویرایش متن (بسته به نوع update)
    if update.callback_query:
        try:
            await update.callback_query.message.edit_text(body_text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception:
            try:
                await update.callback_query.message.reply_text(body_text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
            except Exception:
                logger.exception("Failed to send refresh_records message (callback)")
    else:
        try:
            await update.message.reply_text(body_text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception:
            logger.exception("Failed to send refresh_records message (message)")

async def show_record_settings(message, uid, zone_id, record_id):
    try:
        record = get_record_details(zone_id, record_id)
        if not record:
            await message.reply_text("❌ رکورد یافت نشد. ممکن است حذف شده باشد.")
            return
    except Exception as e:
        logger.exception("Could not fetch record details")
        await message.reply_text("❌ خطا در دریافت اطلاعات رکورد.")
        return

    user_state[uid]["record_id"] = record_id
    text = (
        f"⚙️ تنظیمات رکورد: `{record['name']}`\n\n"
        f"**Type:** `{record['type']}`\n"
        f"**IP:** `{record['content']}`\n"
        f"**TTL:** `{record['ttl']}`\n"
        f"**Proxied:** {'✅ فعال' if record.get('proxied') else '❌ غیرفعال'}"
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


# ---------- Callback handling ----------
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    data = query.data

    # Authorization
    if not is_user_authorized(uid):
        return await query.message.reply_text("❌ شما اجازه دسترسی به این ربات را ندارید.")

    # کوتاه‌سازی رفتارهای ساده
    if data in ("back_to_domains", "refresh_domains", "back_to_main"):
        await start(update, context)
        return

    if data == "manage_users":
        if uid == ADMIN_ID:
            await manage_users_menu(update, context)
        return

    if data == "add_user_prompt":
        if uid == ADMIN_ID:
            user_state[uid]['mode'] = 'adding_user'
            text = (
                "لطفاً شناسه عددی (ID) کاربر مورد نظر را ارسال کنید.\n\n"
                "راهنمایی: از کاربر بخواهید یک پیام به ربات @userinfobot ارسال کند تا شناسه خود را دریافت نماید."
            )
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="manage_users")]]))
        return

    if data.startswith("delete_user_"):
        if uid == ADMIN_ID:
            try:
                user_to_delete = int(data.split("_")[2])
            except Exception:
                await query.answer("❌ شناسه نامعتبر.", show_alert=True)
                return
            if remove_user(user_to_delete):
                await query.answer("✅ کاربر با موفقیت حذف شد.", show_alert=True)
            else:
                await query.answer("❌ حذف ناموفق بود.", show_alert=True)
            await manage_users_menu(update, context)
        return

    # برگشت به رکوردها
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

    # انتخاب دامنه
    if data.startswith("zone_"):
        zone_id = data.split("_", 1)[1]
        try:
            zone_info = get_zone_info_by_id(zone_id)
            user_state[uid].update({"zone_id": zone_id, "zone_name": zone_info.get("name", "")})
            await refresh_records(uid, update)
        except Exception:
            await query.message.reply_text("❌ دریافت اطلاعات دامنه ناموفق بود.")
        return

    # رفرش رکوردها / صفحات
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
            p = int(data.split("_")[-1])
            await refresh_records(uid, update, page=max(0, p - 1))
        except Exception:
            await query.answer("❌ شماره صفحه نامعتبر است.", show_alert=True)
        return

    # Export handlers
    if data == "export_json":
        try:
            records = get_dns_records(user_state[uid].get("zone_id")) or []
            payload = json.dumps(records, ensure_ascii=False, indent=2)
            bio = io.BytesIO(payload.encode("utf-8"))
            bio.name = f"{user_state[uid].get('zone_name','records')}.json"
            await context.bot.send_document(chat_id=uid, document=bio)
        except Exception:
            await query.answer("❌ امکان تهیه خروجی وجود ندارد.", show_alert=True)
        return

    if data == "export_csv":
        try:
            records = get_dns_records(user_state[uid].get("zone_id")) or []
            si = io.StringIO()
            writer = csv.writer(si)
            writer.writerow(["id","type","name","content","ttl","proxied"])
            for r in records:
                writer.writerow([
                    r.get("id",""), r.get("type",""), r.get("name",""),
                    r.get("content",""), r.get("ttl",""), r.get("proxied", False)
                ])
            bio = io.BytesIO(si.getvalue().encode("utf-8"))
            bio.name = f"{user_state[uid].get('zone_name','records')}.csv"
            await context.bot.send_document(chat_id=uid, document=bio)
        except Exception:
            await query.answer("❌ امکان تهیه خروجی وجود ندارد.", show_alert=True)
        return

    # بررسی انتخاب رکورد (رفتن به صفحه تنظیمات رکورد)
    if data.startswith("record_settings_"):
        record_id = data.split("_", 2)[2]
        await show_record_settings(query.message, uid, user_state[uid].get("zone_id"), record_id)
        return

    # toggle proxy
    if data.startswith("toggle_proxy_"):
        record_id = data.split("_", 2)[2]
        try:
            success = toggle_proxied_status(user_state[uid].get("zone_id"), record_id)
            await query.answer("✅ وضعیت پروکسی تغییر کرد." if success else "❌ عملیات ناموفق بود.")
            if success:
                await show_record_settings(query.message, uid, user_state[uid].get("zone_id"), record_id)
        except Exception:
            await query.answer("❌ خطا در ارتباط با API.", show_alert=True)
        return

    # edit ttl (show options)
    if data.startswith("edittll_"):
        record_id = data.split("_")[1]
        user_state[uid].update({"mode": "editing_ttl", "record_id": record_id})
        keyboard = [
            [InlineKeyboardButton("Auto (خودکار)", callback_data=f"update_ttl_{record_id}_1"), InlineKeyboardButton("1 دقیقه", callback_data=f"update_ttl_{record_id}_60")],
            [InlineKeyboardButton("2 دقیقه", callback_data=f"update_ttl_{record_id}_120"), InlineKeyboardButton("5 دقیقه", callback_data=f"update_ttl_{record_id}_300")],
            [InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]
        ]
        await query.message.edit_text("⏱ مقدار جدید TTL را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data.startswith("update_ttl_"):
        parts = data.split("_")
        if len(parts) >= 4:
            record_id, ttl = parts[2], int(parts[3])
            try:
                record = get_record_details(user_state[uid].get("zone_id"), record_id)
                if record:
                    success = update_dns_record(user_state[uid].get("zone_id"), record_id, record["name"], record["type"], record["content"], ttl, record.get("proxied", False))
                    await query.answer("✅ TTL تغییر یافت." if success else "❌ عملیات ناموفق بود.")
                    if success:
                        await show_record_settings(query.message, uid, user_state[uid].get("zone_id"), record_id)
            except Exception:
                await query.answer("❌ خطا در ارتباط با API.", show_alert=True)
        else:
            await query.answer("❌ پارامترهای نامعتبر.", show_alert=True)
        return

    # edit ip -> set mode, then handle in message handler
    if data.startswith("editip_"):
        record_id = data.split("_")[1]
        user_state[uid].update({"mode": "editing_ip", "record_id": record_id})
        await query.message.edit_text("📝 لطفاً IP جدید را وارد کنید:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]]))
        return

    # add record flow (start)
    if data == "add_record":
        user_state[uid].update({"mode": "adding_record_step", "record_step": 0, "record_data": {}})
        keyboard = [
            [InlineKeyboardButton("A", callback_data="select_type_A"), InlineKeyboardButton("AAAA", callback_data="select_type_AAAA"), InlineKeyboardButton("CNAME", callback_data="select_type_CNAME")],
            [InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]
        ]
        await query.message.edit_text("📌 مرحله ۱ از ۵: نوع رکورد را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data.startswith("select_type_"):
        user_state[uid]["record_data"] = {"type": data.split("_")[2]}
        user_state[uid]["record_step"] = 1
        await query.message.edit_text("📌 مرحله ۲ از ۵: نام رکورد را وارد کنید (مثال: sub یا @ برای ریشه)", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]]))
        return

    if data.startswith("select_ttl_"):
        user_state[uid]["record_data"]["ttl"] = int(data.split("_")[2])
        user_state[uid]["record_step"] = 4
        keyboard = [
            [InlineKeyboardButton("✅ بله", callback_data="select_proxied_true"), InlineKeyboardButton("❌ خیر", callback_data="select_proxied_false")],
            [InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]
        ]
        await query.message.edit_text("📌 مرحله ۵ از ۵: آیا پروکسی فعال باشد؟", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data.startswith("select_proxied_"):
        user_state[uid]["record_data"]["proxied"] = data.endswith("true")
        r_data = user_state[uid]["record_data"]
        zone_name = user_state[uid].get("zone_name", "")
        name = r_data.get("name", "")
        if name == "@":
            name = zone_name
        elif name and zone_name and not name.endswith(f".{zone_name}"):
            name = f"{name}.{zone_name}"
        await query.message.edit_text("⏳ در حال ایجاد رکورد...")
        try:
            success = create_dns_record(user_state[uid].get("zone_id"), r_data["type"], name, r_data["content"], r_data["ttl"], r_data["proxied"])
            await query.message.edit_text("✅ رکورد با موفقیت اضافه شد." if success else "❌ افزودن رکورد ناموفق بود.")
        except Exception:
            await query.message.edit_text("❌ خطا در ایجاد رکورد.")
        finally:
            reset_user_state(uid, keep_zone=True)
            await refresh_records(uid, update)
        return

    # delete confirm
    if data.startswith("confirm_delete_"):
        record_id = data.split("_")[2]
        keyboard = [
            [InlineKeyboardButton("✅ بله، حذف شود", callback_data=f"delete_record_{record_id}")],
            [InlineKeyboardButton("❌ لغو", callback_data="back_to_records")]
        ]
        await query.message.edit_text("❗ آیا از حذف این رکورد مطمئن هستید؟", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if data.startswith("delete_record_"):
        record_id = data.split("_")[2]
        await query.message.edit_text("⏳ در حال حذف رکورد...")
        try:
            success = delete_dns_record(user_state[uid].get("zone_id"), record_id)
            await query.message.edit_text("✅ رکورد حذف شد." if success else "❌ حذف رکورد ناموفق بود.")
        except Exception:
            await query.message.edit_text("❌ خطا در حذف رکورد.")
        finally:
            await refresh_records(uid, update, page=user_state[uid].get("page", 0))
        return

    # default: unknown callback
    await query.answer()


# ---------- Message handling ----------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_user_authorized(uid):
        return await update.message.reply_text("❌ شما اجازه دسترسی ندارید.")

    state = user_state.get(uid, {})
    mode = state.get("mode")
    text = update.message.text.strip()
    if not mode:
        return

    # adding user (admin)
    if mode == "adding_user" and uid == ADMIN_ID:
        try:
            new_user_id = int(text)
            added = add_user(new_user_id)
            await update.message.reply_text(f"✅ کاربر `{new_user_id}` با موفقیت اضافه شد." if added else "⚠️ این کاربر از قبل در لیست وجود دارد.")
        except ValueError:
            await update.message.reply_text("❌ ورودی نامعتبر است. لطفاً فقط شناسه عددی کاربر را ارسال کنید.")
        reset_user_state(uid)
        # regenerate manage_users_menu
        from unittest.mock import Mock
        mock_query = Mock(from_user=update.effective_user, message=update.message)
        mock_update = Mock(callback_query=mock_query)
        await manage_users_menu(mock_update, context)
        return

    # adding domain flow
    if mode == "adding_domain":
        await update.message.reply_text(f"⏳ در حال افزودن دامنه `{text}`...")
        try:
            success, result = add_domain_to_cloudflare(text)
            if success:
                zone_info = get_zone_info_by_id(result['id'])
                ns = "\n".join(zone_info.get("name_servers", ["N/A"]))
                await update.message.reply_text(
                    f"✅ دامنه `{text}` با موفقیت اضافه شد.\n"
                    f"**وضعیت:** `{zone_info.get('status','')}`\n\n"
                    f"❗️ لطفاً Name Server های دامنه خود را به موارد زیر تغییر دهید:\n"
                    f"`{ns}`",
                    parse_mode="Markdown"
                )
            else:
                await update.message.reply_text(f"❌ خطا در افزودن دامنه: {result}")
        except Exception:
            await update.message.reply_text("❌ خطا در افزودن دامنه.")
        finally:
            reset_user_state(uid)
            await start(update, context)
        return

    # editing ip
    if mode == "editing_ip" and state.get("zone_id") and state.get("record_id"):
        zone_id = state.get("zone_id")
        record_id = state.get("record_id")
        new_ip = text
        await update.message.reply_text(f"⏳ در حال به‌روزرسانی IP به `{new_ip}`...")
        try:
            record = get_record_details(zone_id, record_id)
            if record:
                success = update_dns_record(zone_id, record_id, record["name"], record["type"], new_ip, record["ttl"], record.get("proxied", False))
                if success:
                    await update.message.reply_text("✅ آی‌پی با موفقیت به‌روز شد.")
                    # show updated settings
                    await show_record_settings(update.message, uid, zone_id, record_id)
                else:
                    await update.message.reply_text("❌ به‌روزرسانی ناموفق بود.")
            else:
                await update.message.reply_text("❌ رکورد مورد نظر یافت نشد.")
        except Exception:
            await update.message.reply_text("❌ خطا در ارتباط با API.")
        finally:
            reset_user_state(uid, keep_zone=True)
        return

    # add record flow (multi-step)
    if mode == "adding_record_step":
        step = state.get("record_step", 0)
        if step == 1:
            user_state[uid]["record_data"]["name"] = text
            user_state[uid]["record_step"] = 2
            await update.message.reply_text("📌 مرحله ۳ از ۵: مقدار رکورد را وارد کنید (مثلاً IP یا آدرس):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]]))
            return
        elif step == 2:
            user_state[uid]["record_data"]["content"] = text
            user_state[uid]["record_step"] = 3
            keyboard = [
                [InlineKeyboardButton("Auto (خودکار)", callback_data="select_ttl_1"), InlineKeyboardButton("1 دقیقه", callback_data="select_ttl_60")],
                [InlineKeyboardButton("2 دقیقه", callback_data="select_ttl_120"), InlineKeyboardButton("5 دقیقه", callback_data="select_ttl_300")],
                [InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]
            ]
            await update.message.reply_text("📌 مرحله ۴ از ۵: مقدار TTL را انتخاب کنید:", reply_markup=InlineKeyboardMarkup(keyboard))
            return

    # if no other mode matched, ignore
    return


# --- main ---
if __name__ == "__main__":
    load_users()
    logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
    logger.info("Starting bot...")
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()
