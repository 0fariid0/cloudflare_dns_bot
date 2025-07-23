import logging
from collections import defaultdict
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters)

from cloudflare_api import *
from config import BOT_TOKEN, ADMIN_ID

logger = logging.getLogger(__name__)
RECORDS_PER_PAGE = 10
user_state = defaultdict(lambda: {"page": 0})


def reset_user_state(uid, keep_zone=False):
    if keep_zone and uid in user_state:
        zone_id = user_state[uid].get("zone_id")
        zone_name = user_state[uid].get("zone_name")
        user_state[uid] = {"zone_id": zone_id, "zone_name": zone_name, "page": 0}
    else:
        user_state.pop(uid, None)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return await (update.message or update.callback_query.message).reply_text("❌ شما اجازه دسترسی ندارید.")

    reset_user_state(user_id)

    try:
        zones = get_zones()
    except Exception as e:
        logger.error(f"Could not fetch zones: {e}")
        await (update.message or update.callback_query.message).reply_text("❌ خطا در ارتباط با Cloudflare. لطفاً بعداً تلاش کنید.")
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
    keyboard.append([InlineKeyboardButton("ℹ️ راهنما", callback_data="show_help")])

    welcome_text = "👋 به ربات مدیریت DNS خوش آمدید!\n\n🌐 دامنه‌های متصل:"
    message = update.message or update.callback_query.message
    if update.callback_query:
        await message.edit_text(welcome_text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await message.reply_text(welcome_text, reply_markup=InlineKeyboardMarkup(keyboard))


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """
📘 راهنمای کامل استفاده از ربات DNS کلودفلر

این ربات به شما امکان می‌دهد تمام عملیات موردنیاز را بدون نیاز به ورود به وب‌سایت Cloudflare، از طریق تلگرام انجام دهید.

📚 دکمه‌ها و عملکردشان

🧷 در منوی اصلی:
- ➕ افزودن دامنه: برای افزودن دامنه جدید (مثال: example.com).
- 🔄 رفرش: بارگذاری مجدد لیست دامنه‌ها از کلودفلر.
- 🗑️ حذف دامنه: حذف کامل یک دامنه از حساب کلودفلر شما.
- ℹ️ راهنما: نمایش همین راهنما.

📄 در لیست رکوردها:
- ➕ افزودن رکورد: ایجاد یک رکورد DNS جدید (A, AAAA, CNAME).
- ⚙️ تنظیمات رکورد: دسترسی به گزینه‌های ویرایش، حذف، تغییر TTL و وضعیت پروکسی.
- 🔄 رفرش: بارگذاری مجدد لیست رکوردها.
- 🔙 بازگشت: بازگشت به لیست دامنه‌ها.

⚙️ در تنظیمات رکورد:
- 🖊 تغییر IP: برای ویرایش آدرس IP رکورد.
- 🕒 تغییر TTL: تنظیم زمان اعتبار رکورد.
- 🔁 پروکسی: فعال یا غیرفعال کردن پروکسی کلودفلر (ابر نارنجی).
- 🗑 حذف: برای حذف رکورد.

❌ دکمه لغو (Cancel):
در تمام مراحل ورود داده، با کلیک روی این دکمه عملیات متوقف شده و به منوی اصلی بازمی‌گردید.

توسعه‌دهنده: Rasim Ghodrati (@rasim_gh)
"""
    keyboard = [[InlineKeyboardButton("🔙 بازگشت به منوی اصلی", callback_data="back_to_domains")]]
    await (update.callback_query.message or update.message).edit_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def refresh_records(uid, update: Update, page=0):
    zone_id = user_state[uid]["zone_id"]
    zone_name = user_state[uid].get("zone_name", "")
    
    try:
        records = get_dns_records(zone_id)
    except Exception as e:
        logger.error(f"Could not fetch records for zone {zone_id}: {e}")
        await update.callback_query.message.reply_text("❌ خطا در دریافت لیست رکوردها.")
        return

    user_state[uid]["page"] = page
    total_pages = (len(records) - 1) // RECORDS_PER_PAGE + 1
    text = f"📋 رکوردهای DNS دامنه: `{zone_name}` (صفحه {page + 1} از {total_pages})\n\n"
    start_index = page * RECORDS_PER_PAGE
    end_index = start_index + RECORDS_PER_PAGE

    keyboard = []
    for rec in records[start_index:end_index]:
        if rec["type"] in ["A", "AAAA", "CNAME"]:
            name = rec["name"].replace(f".{zone_name}", "").replace(zone_name, "@")
            content = rec["content"]
            keyboard.append([
                InlineKeyboardButton(name, callback_data="noop"),
                InlineKeyboardButton(f"{content} | ⚙️", callback_data=f"record_settings_{rec['id']}")
            ])

    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ قبلی", callback_data="page_prev"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("➡️ بعدی", callback_data="page_next"))

    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([
        InlineKeyboardButton("➕ افزودن رکورد", callback_data="add_record"),
        InlineKeyboardButton("🔄 رفرش", callback_data="refresh_records")
    ])
    keyboard.append([InlineKeyboardButton("🔙 بازگشت به دامنه‌ها", callback_data="back_to_domains")])

    await update.callback_query.message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))


async def show_record_settings(message, uid, zone_id, record_id):
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
            InlineKeyboardButton("🔁 پروکسی", callback_data=f"toggle_proxy_{record_id}"),
        ],
        [
            InlineKeyboardButton("🗑 حذف", callback_data=f"confirm_delete_{record_id}"),
            InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_records"),
        ],
    ]
    await message.edit_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    data = query.data
    
    if uid != ADMIN_ID:
        return await query.message.reply_text("❌ شما اجازه دسترسی ندارید.")

    if data == "back_to_domains" or data == "refresh_domains":
        await start(update, context)
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
            logger.error(f"Could not get zone info for {zone_id}: {e}")
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
            updated_record = toggle_proxied_status(zone_id, record_id)
            if updated_record:
                await query.answer(f"✅ پروکسی {'فعال' if updated_record.get('proxied') else 'غیرفعال'} شد.")
                await show_record_settings(query.message, uid, zone_id, record_id)
            else:
                await query.answer("❌ عملیات ناموفق بود.", show_alert=True)
        except Exception as e:
            logger.error(f"Error toggling proxy for {record_id}: {e}")
            await query.answer("❌ خطا در ارتباط با API.", show_alert=True)
            
    elif data.startswith("update_ttl_"):
        parts = data.split("_")
        record_id, ttl = parts[2], int(parts[3])
        try:
            record = get_record_details(zone_id, record_id)
            if not record: return await query.answer("❌ رکورد یافت نشد.", show_alert=True)
            
            success = update_dns_record(zone_id, record_id, record["name"], record["type"], record["content"], ttl, record.get("proxied", False))
            if success:
                await query.answer(f"✅ TTL به {ttl} تغییر یافت.")
                await show_record_settings(query.message, uid, zone_id, record_id)
            else:
                await query.answer("❌ عملیات ناموفق بود.", show_alert=True)
        except Exception as e:
            logger.error(f"Error updating TTL for {record_id}: {e}")
            await query.answer("❌ خطا در ارتباط با API.", show_alert=True)
            
    elif data.startswith("editip_"):
        record_id = data.split("_")[1]
        user_state[uid].update({"mode": "editing_ip", "record_id": record_id})
        await query.message.edit_text("📝 لطفاً IP جدید را وارد کنید:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ لغو", callback_data="cancel_action")]]))

    # Incomplete: Add other handlers for adding/deleting records/zones here


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != ADMIN_ID:
        return await update.message.reply_text("❌ شما اجازه دسترسی ندارید.")
        
    state = user_state.get(uid, {})
    mode = state.get("mode")
    text = update.message.text.strip()

    if not mode: return

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
                    reset_user_state(uid, keep_zone=True)
                    new_msg = await update.message.reply_text("...در حال بارگذاری تنظیمات جدید")
                    await show_record_settings(new_msg, uid, zone_id, record_id)
                else:
                    await update.message.reply_text("❌ به‌روزرسانی ناموفق بود.")
            else:
                 await update.message.reply_text("❌ رکورد مورد نظر یافت نشد.")
        except Exception as e:
            logger.error(f"Error updating IP for {record_id}: {e}")
            await update.message.reply_text("❌ خطا در ارتباط با API.")
        finally:
            reset_user_state(uid, keep_zone=True)
    
    # Incomplete: Add other message handlers for adding domains/records here


if __name__ == "__main__":
    logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
    logger.info("Starting bot...")
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    app.run_polling()
