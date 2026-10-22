"""Opt-in DNS group, bulk-update, search, history and alert workflows.

Additive to the existing bot. Uses its own SQLite database for stable group IDs,
monitor state and audit history. No Cloudflare credentials are persisted.
"""
from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import secrets
import socket
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from contextlib import contextmanager
from typing import Any

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup

import cloudflare_api as cf
from bot_ui import render, compact_rows

try:
    import dns.resolver
except ImportError:
    dns = None

LOG = logging.getLogger(__name__)
DB_PATH = Path(__file__).resolve().with_name("dns_advanced.sqlite3")
# Only A, AAAA and CNAME are exposed by the existing record editor.
GROUP_TYPES = {"A", "AAAA", "CNAME"}
PAGE_SIZE = 10
MONITOR_INTERVAL = 300
FEATURE_INPUT: dict[int, dict] = {}
PENDING: dict[int, dict] = {}
SEARCH_CACHE: dict[int, list[dict]] = {}
BULK_SELECTION: dict[int, set[tuple[str, str]]] = {}


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def initialize():
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript("""
            CREATE TABLE IF NOT EXISTS dns_groups(
                id TEXT PRIMARY KEY, owner INTEGER NOT NULL, name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(owner, name)
            );
            CREATE TABLE IF NOT EXISTS dns_group_members(
                group_id TEXT NOT NULL REFERENCES dns_groups(id) ON DELETE CASCADE,
                zone_id TEXT NOT NULL, record_id TEXT NOT NULL,
                PRIMARY KEY(group_id,zone_id,record_id)
            );
            CREATE TABLE IF NOT EXISTS dns_change_history(
                id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                actor TEXT NOT NULL, source TEXT NOT NULL, zone_id TEXT NOT NULL,
                record_id TEXT NOT NULL, record_name TEXT NOT NULL,
                record_type TEXT NOT NULL, before_json TEXT NOT NULL,
                after_json TEXT NOT NULL, group_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_dns_history_date ON dns_change_history(id DESC);
            CREATE TABLE IF NOT EXISTS dns_monitors(
                owner INTEGER NOT NULL, zone_id TEXT NOT NULL, record_id TEXT NOT NULL,
                port INTEGER NOT NULL DEFAULT 443, enabled INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'unknown', failures INTEGER NOT NULL DEFAULT 0,
                successes INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
                checked_at TEXT NOT NULL DEFAULT '', record_name TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(owner,zone_id,record_id)
            );
        """)


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def write_history(actor, source, zone_id, record_id, before, after, group_id=None):
    """Never block a successful Cloudflare write if local audit storage fails."""
    try:
        with db() as c:
            c.execute("""INSERT INTO dns_change_history
                (created_at,actor,source,zone_id,record_id,record_name,record_type,
                before_json,after_json,group_id) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (stamp(), str(actor), source, str(zone_id), str(record_id),
                 before.get("name", after.get("name", "?")),
                 before.get("type", after.get("type", "?")),
                 json.dumps(before, ensure_ascii=False),
                 json.dumps(after, ensure_ascii=False), group_id))
    except Exception:
        LOG.exception("DNS audit history write failed")


def key_fields(record):
    # Include metadata revision to catch changes from the Cloudflare dashboard
    # between confirmation and write (PATCH still preserves unrelated metadata).
    return {k: record.get(k) for k in ("name", "type", "content", "ttl", "proxied", "comment", "modified_on", "comment_modified_on", "tags_modified_on")}


def group(owner, group_id):
    with db() as c:
        return c.execute("SELECT * FROM dns_groups WHERE owner=? AND id=?", (owner, group_id)).fetchone()


def members(group_id):
    with db() as c:
        return c.execute("SELECT zone_id,record_id FROM dns_group_members WHERE group_id=? ORDER BY rowid", (group_id,)).fetchall()


def zones_for(core, uid):
    return {str(z["id"]): z for z in core.get_user_accessible_zones(uid)}


def allowed(core, uid, zone_id):
    return str(zone_id) in zones_for(core, uid)


def back(data):
    return [Button("🔙 بازگشت", callback_data=data)]


def txt(s, n=48):
    s = str(s).replace("\n", " ").replace("\r", " ")
    return s[:n-1] + "…" if len(s) > n else s


async def show(update, text, keyboard, *, edit=True):
    if edit:
        await render(update, text[:3900], keyboard)
    else:
        await update.effective_message.reply_text(text[:3900], reply_markup=Markup(keyboard))


def name_of(zone_id, record):
    return txt(record.get("name", record.get("id", "?")), 32)


def page_slice(items, page):
    page = max(0, min(int(page), max(0, (len(items) - 1) // PAGE_SIZE)))
    return items[page * PAGE_SIZE:(page+1)*PAGE_SIZE], page


def pager(page, count, prefix):
    arr = []
    if page > 0:
        arr.append(Button("⬅️ قبل", callback_data=f"{prefix}{page-1}"))
    if (page + 1) * PAGE_SIZE < count:
        arr.append(Button("بعد ➡️", callback_data=f"{prefix}{page+1}"))
    return [arr] if arr else []


async def groups_menu(update, core, uid, page=0):
    with db() as c:
        rows = c.execute("SELECT * FROM dns_groups WHERE owner=? ORDER BY name", (uid,)).fetchall()
    items,p = page_slice(rows,page)
    keyboard = compact_rows([row[0] for row in [[Button(f"📁 {txt(row['name'], 42)}", callback_data=f"dx_g_{row['id']}")] for row in items]])
    keyboard += pager(p,len(rows),"dx_groups_")
    keyboard += [[Button("➕ ساخت گروه جدید", callback_data="dx_gnew")], back("back_to_main")]
    await show(update, f"📁 گروه‌های DNS ({len(rows)})\nبا انتخاب گروه، رکوردهای عضو و تنظیمات گروهی را مدیریت کنید.", keyboard)


async def group_menu(update, core, uid, gid, page=0):
    g = group(uid, gid)
    if not g:
        await show(update, "گروه پیدا نشد یا مجاز نیست.", [back("dx_groups")]); return
    permitted = zones_for(core, uid)
    mm = members(gid)
    lines = [f"📁 گروه: {g['name']}", f"👥 تعداد رکوردها: {len(mm)}", ""]
    keyboard = []
    part,p = page_slice(mm,page)
    for m in part:
        if m["zone_id"] not in permitted:
            lines.append("🔒 عضو خارج از دسترسی فعلی")
            continue
        try:
            r = await asyncio.to_thread(cf.get_record_details_strict, m["zone_id"], m["record_id"])
            lines.append(f"• {name_of(m['zone_id'],r)} ({r['type']}) = {txt(r.get('content',''),26)}")
            keyboard.append([Button(f"➖ حذف {txt(r['name'],30)} از گروه", callback_data=f"dx_rm_{gid}_{r['id']}")])
        except Exception:
            lines.append("⚠️ رکورد حذف شده یا دریافت ناموفق")
            keyboard.append([Button("➖ حذف عضو نامعتبر", callback_data=f"dx_rm_{gid}_{m['record_id']}")])
    keyboard += pager(p,len(mm),f"dx_gp_{gid}_")
    keyboard += [[Button("➕ عضو جدید", callback_data=f"dx_addz_{gid}_0"), Button("✏️ ویرایش اعضا", callback_data=f"dx_ge_{gid}")],
                 [Button("🖊 نام گروه", callback_data=f"dx_grename_{gid}"), Button("🗑 حذف گروه", callback_data=f"dx_gdelq_{gid}")], back("dx_groups")]
    await show(update, "\n".join(lines), keyboard)


async def add_zone_menu(update, core, uid, gid, page):
    if not group(uid, gid): return
    zones = list(zones_for(core, uid).values())
    items,p = page_slice(zones, page)
    keyboard = compact_rows([row[0] for row in [[Button(txt(z["name"], 42), callback_data=f"dx_addr_{gid}_{z['id']}_0")] for z in items]])
    keyboard += pager(p,len(zones),f"dx_addz_{gid}_") + [back(f"dx_g_{gid}")]
    await show(update, "🌐 دامنه‌ای را برای انتخاب رکورد مشخص کنید:", keyboard)


async def add_record_menu(update, core, uid, gid, zone_id, page):
    if not group(uid,gid) or not allowed(core,uid,zone_id):
        await show(update,"دسترسی به این دامنه ندارید.",[back("dx_groups")]);return
    try:
        rr = [r for r in await asyncio.to_thread(cf.get_dns_records_fresh, zone_id) if r.get("type") in GROUP_TYPES]
    except Exception as e:
        await show(update, f"خطا در دریافت رکوردها: {txt(e,120)}", [back(f"dx_addz_{gid}_0")]); return
    part,p = page_slice(rr,page)
    # The zone is retained per-user, preventing need for oversized Telegram callbacks.
    FEATURE_INPUT[uid] = {"zone_pick":zone_id,"group_pick":gid}
    exists = {(x['zone_id'],x['record_id']) for x in members(gid)}
    keyboard = [[Button(("✅ " if (zone_id,r['id']) in exists else "➕ ")+f"{txt(r['name'],26)} | {r['type']}",
                        callback_data=f"dx_join_{gid}_{r['id']}")] for r in part]
    keyboard += pager(p,len(rr),f"dx_addr_{gid}_{zone_id}_") + [back(f"dx_addz_{gid}_0")]
    await show(update,"ساب‌دامنه را برای افزودن به گروه انتخاب کنید:",keyboard)


async def group_edit_menu(update, core, uid, gid):
    if not group(uid,gid): return
    await show(update,"✏️ ویرایش گروهی: فقط تنظیم انتخاب‌شده در تمام اعضا تغییر می‌کند.\nقبل از اجرا پیش‌نمایش و تأیید گرفته می‌شود.",
               [[Button("🌐 IP / Content",callback_data=f"dx_field_{gid}_content"), Button("⏱ TTL",callback_data=f"dx_field_{gid}_ttl")],
                [Button("☁️ Proxy",callback_data=f"dx_field_{gid}_proxied"), Button("📝 توضیحات",callback_data=f"dx_field_{gid}_comment")],back(f"dx_g_{gid}")])


def valid_value(field, value, records):
    if not records:
        raise ValueError("ابتدا حداقل یک رکورد اضافه کنید.")
    types = {r['type'] for r in records}
    if field == "content":
        if len(types) != 1:
            raise ValueError("برای تغییر IP/Content همه اعضا باید از یک نوع باشند.")
        kind = next(iter(types))
        if kind in ("A", "AAAA"):
            ip = ipaddress.ip_address(value)
            if ip.version != (4 if kind == "A" else 6):
                raise ValueError("نوع IP با نوع رکورد سازگار نیست.")
            return str(ip)
        if kind == "CNAME" and isinstance(value,str) and 0 < len(value) <= 253 and " " not in value:
            return value.rstrip(".")
        raise ValueError("نوع رکورد قابل تغییر محتوا نیست.")
    if field == "ttl":
        ttl = int(value)
        if ttl != 1 and not (60 <= ttl <= 86400):
            raise ValueError("TTL باید خودکار (1) یا بین ۶۰ تا ۸۶۴۰۰ ثانیه باشد.")
        if ttl != 1 and any(r.get("proxied") for r in records):
            raise ValueError("رکورد Proxied باید TTL خودکار (1) داشته باشد.")
        return ttl
    if field == "proxied":
        if any(r['type'] not in GROUP_TYPES or not r.get("proxiable", r['type'] in GROUP_TYPES) for r in records):
            raise ValueError("بعضی رکوردها Proxy را پشتیبانی نمی‌کنند.")
        return bool(value)
    if field == "comment":
        if not isinstance(value,str) or len(value) > 500:
            raise ValueError("توضیح حداکثر ۵۰۰ کاراکتر باشد.")
        return value
    raise ValueError("فیلد ناشناخته")


async def begin_preview(update, core, uid, pairs, field, value, *, gid=None, source="bulk"):
    """Build an immutable confirmation preview with live snapshots."""
    permitted = zones_for(core, uid)
    if len(pairs) > 200:
        await show(update,"حداکثر ۲۰۰ رکورد در هر عملیات مجاز است.",[back("dx_groups" if gid else "dx_bulk")]);return
    records = []
    try:
        for zone_id, record_id in pairs:
            if zone_id not in permitted:
                raise ValueError("دسترسی به یکی از دامنه‌ها لغو شده است.")
            rec = await asyncio.to_thread(cf.get_record_details_strict, zone_id, record_id)
            records.append((zone_id,rec))
        target = valid_value(field,value,[r for _,r in records])
    except Exception as e:
        await show(update,f"❌ پیش‌نمایش ناموفق: {txt(e,240)}",[back(f"dx_ge_{gid}" if gid else "dx_bulk")]);return
    token = secrets.token_hex(8)
    PENDING[uid] = {"token":token,"at":time.monotonic(),"items":records,"field":field,
                    "value":target,"gid":gid,"source":source}
    field_labels={"content":"IP / Content","ttl":"TTL","proxied":"Proxy","comment":"توضیحات"}
    lines=["⚠️ تأیید تغییر گروهی",f"فیلد: {field_labels[field]}",f"مقدار جدید: {txt(target,160)}",f"رکوردها: {len(records)}", ""]
    for zid,r in records[:20]:
        lines.append(f"• {txt(r['name'],45)}: {txt(r.get(field,''),30)} ← {txt(target,30)}")
    if len(records)>20: lines.append(f"… و {len(records)-20} رکورد دیگر")
    lines.append("\nدر صورت تغییر بیرونی رکورد بعد از پیش‌نمایش، آن رکورد اعمال نمی‌شود.")
    await show(update,"\n".join(lines),[[Button("✅ تأیید و اجرا",callback_data=f"dx_apply_{token}")],
                    [Button("❌ لغو",callback_data="dx_abort")]])


async def execute(update, core, uid, token):
    plan = PENDING.pop(uid,None)
    if not plan or plan['token'] != token or time.monotonic()-plan['at']>600:
        await show(update,"⏰ پیش‌نمایش منقضی شده است؛ دوباره انتخاب کنید.",[back("back_to_main")]);return
    gid=plan['gid']
    if gid and not group(uid,gid):
        await show(update,"گروه حذف شده است.",[back("dx_groups")]);return
    if gid and {(r['zone_id'],r['record_id']) for r in members(gid)} != {(z,r['id']) for z,r in plan['items']}:
        await show(update,"اعضای گروه پس از پیش‌نمایش تغییر کرده‌اند؛ دوباره تأیید کنید.",[back(f"dx_g_{gid}")]);return
    # Recheck every live record before *any* mutation, to reduce partial apply.
    try:
        zones=zones_for(core,uid)
        for zid,r in plan['items']:
            if zid not in zones: raise ValueError("دسترسی دامنه تغییر کرده است")
            live=await asyncio.to_thread(cf.get_record_details_strict,zid,r['id'])
            if key_fields(live)!=key_fields(r):
                raise ValueError(f"رکورد {r['name']} بعد از پیش‌نمایش تغییر کرده")
    except Exception as e:
        await show(update,f"❌ هیچ تغییری اعمال نشد: {txt(e,180)}",[back(f"dx_g_{gid}" if gid else "dx_bulk")]);return
    successes=[]; failures=[]
    for zid,r in plan['items']:
        try:
            # Recheck again before each write, as writes are not atomic across zones.
            fresh=await asyncio.to_thread(cf.get_record_details_strict,zid,r['id'])
            if key_fields(fresh)!=key_fields(r):
                raise ValueError("رکورد به‌صورت هم‌زمان ویرایش شده")
            if r.get(plan['field']) == plan['value']:
                successes.append(f"بدون تغییر: {r['name']}")
                continue
            changed=await asyncio.to_thread(cf.patch_dns_fields,zid,r['id'],{plan['field']:plan['value']})
            write_history(uid,plan['source'],zid,r['id'],r,changed or {**r,plan['field']:plan['value']},gid)
            successes.append(r['name'])
        except Exception as e:
            failures.append(f"{r['name']}: {txt(e,95)}")
    result=[f"✅ موفق/بدون تغییر: {len(successes)}",f"❌ ناموفق: {len(failures)}"]
    if failures:
        result += ["", "موارد ناموفق:"] + ["• "+x for x in failures[:18]]
        result.append("سایر تغییرات موفق به‌طور خودکار بازگردانده نشده‌اند؛ تاریخچه را بررسی کنید.")
    await show(update,"\n".join(result),[back(f"dx_g_{gid}" if gid else "dx_bulk")])


async def bulk_menu(update, core, uid, page=0):
    selected=BULK_SELECTION.setdefault(uid,set())
    zones=list(zones_for(core,uid).values())
    items,p=page_slice(zones,page)
    kb= compact_rows([row[0] for row in [[Button(txt(z['name'],43),callback_data=f"dx_bz_{z['id']}_0")] for z in items]])
    kb+=pager(p,len(zones),"dx_bulk_")
    kb += [[Button(f"✅ انتخاب‌شده: {len(selected)} | تغییر IP",callback_data="dx_bedit")],
           [Button("🧹 پاک‌کردن انتخاب‌ها",callback_data="dx_bclear")],back("back_to_main")]
    await show(update,"🌐 تغییر گروهی IP (بدون نیاز به ساخت گروه)\nابتدا رکوردهای A یا AAAA را از دامنه‌های مختلف انتخاب کنید.",kb)


async def bulk_records(update, core, uid, zid, page):
    if not allowed(core,uid,zid): return
    try:
        records=[r for r in await asyncio.to_thread(cf.get_dns_records_fresh,zid) if r['type'] in ('A','AAAA')]
    except Exception as e:
        await show(update,f"❌ {txt(e,150)}",[back("dx_bulk")]);return
    part,p=page_slice(records,page)
    selected=BULK_SELECTION.setdefault(uid,set())
    kb=[[Button(("✅ " if (zid,r['id']) in selected else "▫️ ")+txt(r['name'],36)+f" [{r['type']}]",
                callback_data=f"dx_bs_{r['id']}")] for r in part]
    FEATURE_INPUT[uid]={"bulk_zone":zid,"bulk_page":p}
    kb+=pager(p,len(records),f"dx_bz_{zid}_") + [back("dx_bulk")]
    await show(update,f"انتخاب رکوردها | تعداد انتخاب‌شده: {len(selected)}",kb)


async def search_menu(update):
    await show(update,"🔎 جست‌وجو در همه دامنه‌های مجاز\nمعیار را انتخاب کنید:",
               [[Button("🌐 IP / Content",callback_data="dx_sf_content"),Button("📝 نام",callback_data="dx_sf_name")],
                [Button("🔤 نوع رکورد",callback_data="dx_sf_type"),Button("☁️ Proxy",callback_data="dx_sf_proxied")],
                [Button("💬 توضیحات",callback_data="dx_sf_comment")],back("back_to_main")])


async def search_records(update, core, uid, field, term):
    found=[]; failures=[]
    zones=list(zones_for(core,uid).items())
    semaphore=asyncio.Semaphore(5)

    async def fetch(zid):
        async with semaphore:
            return await asyncio.to_thread(cf.get_dns_records_fresh,zid)

    # Fetch concurrently, but process in a stable order and respect CF rate limits.
    results=await asyncio.gather(*(fetch(zid) for zid,_ in zones),return_exceptions=True)
    for (zid,zone),result in zip(zones,results):
        if isinstance(result, BaseException):
            failures.append(zone['name'])
            continue
        for r in result:
            v=r.get(field,"")
            if (str(v).casefold()==term.casefold() if field in ("type","proxied")
                else term.casefold() in str(v).casefold()):
                found.append({"zone_id":zid,"record_id":r['id'],"name":r.get('name',''),
                              "type":r.get('type',''),"content":r.get('content','')})
    SEARCH_CACHE[uid]=found[:500]
    notice=f"⚠️ دریافت ناموفق از {len(failures)} دامنه.\n" if failures else ""
    if len(found)>500: notice+="⚠️ فقط ۵۰۰ نتیجه اول نشان داده می‌شود.\n"
    await search_results(update,uid,0,notice)


async def search_results(update,uid,page,notice=""):
    results=SEARCH_CACHE.get(uid,[])
    part,p=page_slice(list(enumerate(results)),page)
    kb= compact_rows([row[0] for row in [[Button(f"{txt(r['name'],28)} {r['type']} | {txt(r['content'],12)}",callback_data=f"dx_sd_{i}")] for i,r in part]])
    kb+=pager(p,len(results),"dx_sp_")+ [back("dx_search")]
    await show(update,f"🔎 نتایج جست‌وجو ({len(results)})\n{notice}",kb)


async def history_menu(update,core,uid,page):
    zones=zones_for(core,uid)
    if not zones:
        rows=[]
    else:
        placeholders=",".join("?" for _ in zones)
        with db() as c:
            rows=c.execute(f"SELECT * FROM dns_change_history WHERE zone_id IN ({placeholders}) ORDER BY id DESC LIMIT 400", tuple(zones)).fetchall()
    part,p=page_slice(rows,page)
    lines=[f"📜 تاریخچه تغییرات DNS ({len(rows)} مورد اخیر قابل مشاهده)","زمان‌ها: تهران",""]
    for row in part:
        before=json.loads(row['before_json']);after=json.loads(row['after_json'])
        diffs=[f"{key}: {txt(before.get(key),18)}→{txt(after.get(key),18)}" for key in ('content','ttl','proxied','comment') if before.get(key)!=after.get(key)]
        lines += [f"• {datetime.fromisoformat(row['created_at']).astimezone(timezone(timedelta(hours=3, minutes=30))).strftime('%Y/%m/%d %H:%M')} | {txt(row['record_name'],35)}",
                  f"  کاربر: {row['actor']} | {row['source']}",
                  "  "+(", ".join(diffs) if diffs else "بدون تغییر قابل مشاهده")]
    if not part: lines.append("تغییری ثبت نشده است.")
    kb=pager(p,len(rows),"dx_h_")+[back("back_to_main")]
    await show(update,"\n".join(lines),kb)


async def monitors_menu(update,core,uid,page=0):
    zones=zones_for(core,uid)
    with db() as c:
        rows=c.execute("SELECT * FROM dns_monitors WHERE owner=? ORDER BY zone_id,record_id",(uid,)).fetchall()
    visible=[m for m in rows if m['zone_id'] in zones]
    part,p=page_slice(visible,page)
    lines=[f"📡 مانیتورینگ DNS و سرور ({len(visible)})", "بررسی DNS عمومی + اتصال TCP به IP اصلی، هر ۵ دقیقه.",
           "پس از دو خطای متوالی هشدار ارسال می‌شود؛ پس از دو موفقیت، رفع هشدار.",""]
    kb=[]
    for m in part:
        label=f"{m['status']} | {txt(m['record_name'] or m['record_id'][:8],24)} | TCP:{m['port']}"
        lines.append(f"• {label}")
        if m['last_error']:
            lines.append(f"  علت آخرین خطا: {txt(m['last_error'],95)}")
        kb.append([Button("🔕 حذف "+txt(m['record_name'] or m['record_id'][:8],36),callback_data=f"dx_mdel_{m['record_id']}")])
    if not visible:lines.append("هنوز مانیتوری ساخته نشده.")
    kb += pager(p,len(visible),"dx_monitor_")
    kb += [[Button("➕ افزودن مانیتور",callback_data="dx_mz_0")],
           [Button("🔄 بررسی همین حالا",callback_data="dx_mcheck")],back("back_to_main")]
    await show(update,"\n".join(lines),kb)


async def monitor_zones(update,core,uid,page):
    zones=list(zones_for(core,uid).values());part,p=page_slice(zones,page)
    kb= compact_rows([row[0] for row in [[Button(txt(z['name']),callback_data=f"dx_mr_{z['id']}_0")] for z in part]])
    kb+=pager(p,len(zones),"dx_mz_")+[back("dx_monitor")]
    await show(update,"دامنه مانیتور را انتخاب کنید:",kb)


async def monitor_records(update,core,uid,zid,page):
    if not allowed(core,uid,zid):return
    try:records=[r for r in await asyncio.to_thread(cf.get_dns_records_fresh,zid) if r['type'] in ('A','AAAA')]
    except Exception as e:
        await show(update,f"خطا: {txt(e,150)}",[back("dx_monitor")]);return
    part,p=page_slice(records,page)
    FEATURE_INPUT[uid]={"monitor_zone":zid}
    kb= compact_rows([row[0] for row in [[Button(f"{txt(r['name'],30)} → {txt(r['content'],20)}",callback_data=f"dx_madd_{r['id']}")] for r in part]])
    kb+=pager(p,len(records),f"dx_mr_{zid}_")+[back("dx_monitor")]
    await show(update,"رکورد A / AAAA برای مانیتورینگ انتخاب کنید:",kb)


async def check_monitor(m):
    """Return health and human-readable reason, no DNS mutation."""
    zid,rid=m['zone_id'],m['record_id']
    r=await asyncio.to_thread(cf.get_record_details_strict,zid,rid)
    if r['type'] not in ('A','AAAA'):
        return False,"نوع رکورد دیگر A/AAAA نیست"
    original=str(ipaddress.ip_address(r['content']))
    kind=r['type']
    if dns is None:
        return False,"dnspython نصب نشده است"

    def query_dns():
        resolver=dns.resolver.Resolver(configure=False)
        resolver.nameservers=["1.1.1.1","8.8.8.8"]
        resolver.timeout=2
        resolver.lifetime=5
        return {str(answer) for answer in resolver.resolve(r['name'],kind)}
    try:
        answers=await asyncio.to_thread(query_dns)
    except Exception as e:
        return False,"DNS: "+txt(e,100)
    if not answers: return False,"پاسخ DNS خالی است"
    if not r.get('proxied') and original not in answers:
        return False,f"DNS IP موردانتظار {original} را برنمی‌گرداند: {', '.join(sorted(answers))[:120]}"
    # Public Cloudflare proxied DNS returns edge IP, not the origin.
    try:
        ip=ipaddress.ip_address(original)
        if not ip.is_global:
            return False,"IP اصلی عمومی نیست؛ بررسی TCP برای امنیت انجام نشد"
        _reader,writer=await asyncio.wait_for(asyncio.open_connection(original,int(m['port'])),timeout=4)
        writer.close()
        await writer.wait_closed()
    except Exception as e:
        return False,"TCP: "+txt(e,120)
    return True,"DNS و TCP پاسخ‌گو هستند"


async def monitor_job(context,core,owner_filter=None):
    with db() as c:
        if owner_filter is None:
            entries=c.execute("SELECT * FROM dns_monitors WHERE enabled=1").fetchall()
        else:
            entries=c.execute("SELECT * FROM dns_monitors WHERE enabled=1 AND owner=?",(owner_filter,)).fetchall()
    access_cache={}
    # Cap concurrent checks to avoid load on Cloudflare/Telegram.
    semaphore=asyncio.Semaphore(5)
    async def one(m):
        uid=m['owner']
        try:
            if core.is_user_blocked(uid) or not core.is_user_authorized(uid):return
            if uid not in access_cache:
                access_cache[uid]=set(zones_for(core,uid))
            if m['zone_id'] not in access_cache[uid]:return
            async with semaphore:
                ok,reason=await check_monitor(m)
        except Exception as e:
            ok,reason=False,"مانیتور: "+txt(e,120)
        previous=m['status'];failures=0 if ok else m['failures']+1
        successes=m['successes']+1 if ok else 0
        status=previous
        alert=None
        if failures>=2 and previous!="down":
            status="down";alert=f"🚨 هشدار DNS/سرور\nرکورد: {m['record_name'] or m['record_id']}\nZone: {m['zone_id']}\nعلت: {reason}"
        elif successes>=2 and previous=="down":
            status="up";alert=f"✅ رفع هشدار DNS/سرور\nرکورد: {m['record_name'] or m['record_id']}\nZone: {m['zone_id']}\n{reason}"
        elif successes>=2 and previous=="unknown":status="up"
        with db() as c:
            c.execute("""UPDATE dns_monitors SET failures=?,successes=?,status=?,last_error=?,checked_at=?
                WHERE owner=? AND zone_id=? AND record_id=?""",
                (failures,successes,status,"" if ok else reason,stamp(),uid,m['zone_id'],m['record_id']))
        if alert:
            try:await context.bot.send_message(chat_id=uid,text=alert)
            except Exception:LOG.exception("Cannot notify DNS monitor owner %s",uid)
    results = await asyncio.gather(*(one(m) for m in entries),return_exceptions=True)
    for err in results:
        if isinstance(err,Exception):
            LOG.error("DNS monitor check failed: %s",err)


async def handle_message(update,context,core):
    uid=update.effective_user.id
    state=FEATURE_INPUT.get(uid)
    if not state or not state.get('mode'):return False
    if core.is_user_blocked(uid) or not core.is_user_authorized(uid):
        FEATURE_INPUT.pop(uid,None);return False
    text=update.message.text.strip()
    mode=state['mode']
    FEATURE_INPUT.pop(uid,None)
    if mode in ('group_new','group_rename'):
        if not text or len(text)>48 or any(ord(c)<32 for c in text):
            FEATURE_INPUT[uid]=state
            await update.message.reply_text("نام گروه باید ۱ تا ۴۸ کاراکتر بدون نویسه کنترلی باشد.");return True
        try:
            if mode=='group_new':
                with db() as c:
                    gid=secrets.token_hex(6)
                    c.execute("INSERT INTO dns_groups(id,owner,name,created_at) VALUES(?,?,?,?)",(gid,uid,text,stamp()))
                await update.message.reply_text("✅ گروه ایجاد شد.")
            else:
                gid=state['gid']
                with db() as c:
                    changed=c.execute("UPDATE dns_groups SET name=? WHERE owner=? AND id=?",(text,uid,gid)).rowcount
                if not changed: raise ValueError("گروه پیدا نشد.")
                await update.message.reply_text("✅ نام گروه تغییر کرد.")
                await group_menu(update,core,uid,gid)
                return True
        except sqlite3.IntegrityError:
            await update.message.reply_text("این نام گروه قبلاً استفاده شده است.")
        except ValueError as exc:
            await update.message.reply_text(str(exc))
        await groups_menu(update,core,uid);return True
    if mode=='search':
        field=state['field']
        if field=='proxied':
            if text.casefold() not in ('true','false','on','off','1','0','فعال','غیرفعال'):
                FEATURE_INPUT[uid]=state
                await update.message.reply_text("برای پروکسی: on یا off بنویسید.");return True
            text='True' if text.casefold() in ('true','on','1','فعال') else 'False'
        await search_records(update,core,uid,field,text);return True
    if mode=='edit':
        field=state['field'];gid=state.get('gid')
        value="" if field=='comment' and text=="-" else text
        if field=='ttl':
            try:value=int(text)
            except ValueError:pass
        if gid:
            if not group(uid,gid):return True
            pairs=[(m['zone_id'],m['record_id']) for m in members(gid)]
        else:
            pairs=list(BULK_SELECTION.get(uid,set()))
        await begin_preview(update,core,uid,pairs,field,value,gid=gid,source='group' if gid else 'bulk')
        return True
    if mode=='monitor_port':
        try:
            port=int(text)
            if not (1<=port<=65535):raise ValueError()
            zid,rid=state['zone_id'],state['record_id']
            if not allowed(core,uid,zid):raise ValueError()
            record=await asyncio.to_thread(cf.get_record_details_strict,zid,rid)
            if record['type'] not in ('A','AAAA'):raise ValueError()
            with db() as c:
                c.execute("""INSERT INTO dns_monitors(owner,zone_id,record_id,port,record_name)
                    VALUES(?,?,?,?,?) ON CONFLICT(owner,zone_id,record_id) DO UPDATE SET
                    port=excluded.port,record_name=excluded.record_name,
                    enabled=1,status='unknown',failures=0,successes=0""",
                    (uid,zid,rid,port,record['name']))
            await update.message.reply_text("✅ مانیتور ثبت شد؛ هر ۵ دقیقه بررسی خواهد شد.")
        except (ValueError,cf.CloudflareAPIError):
            await update.message.reply_text("❌ پورت باید بین ۱ تا ۶۵۵۳۵ باشد؛ یا رکورد نامعتبر است.")
        await monitors_menu(update,core,uid);return True
    return False


async def handle_callback(update,context,core):
    data=update.callback_query.data
    if not data.startswith('dx_'):return False
    uid=update.effective_user.id
    if core.is_user_blocked(uid) or not core.is_user_authorized(uid):return True
    # Forget only text-input state on navigation (maintain zone_pick/bulk selection flow).
    if not data.startswith(('dx_join_','dx_bs_','dx_madd_')):
        FEATURE_INPUT.pop(uid,None)
    try:
        if data=='dx_groups' or data.startswith('dx_groups_'):
            await groups_menu(update,core,uid,int(data[len('dx_groups_'):]) if data!='dx_groups' else 0)
        elif data=='dx_gnew':
            FEATURE_INPUT[uid]={"mode":"group_new"}
            await show(update,"نام دلخواه گروه را بفرستید:",[back("dx_groups")])
        elif data.startswith('dx_g_'):
            await group_menu(update,core,uid,data[5:])
        elif data.startswith('dx_gp_'):
            _,_,gid,p=data.split('_');await group_menu(update,core,uid,gid,int(p))
        elif data.startswith('dx_addz_'):
            _,_,gid,p=data.split('_');await add_zone_menu(update,core,uid,gid,int(p))
        elif data.startswith('dx_addr_'):
            _,_,gid,zid,p=data.split('_');await add_record_menu(update,core,uid,gid,zid,int(p))
        elif data.startswith('dx_join_'):
            _,_,gid,rid=data.split('_')
            prev=FEATURE_INPUT.get(uid,{})
            zid=prev.get('zone_pick')
            if not zid or prev.get('group_pick')!=gid or not group(uid,gid) or not allowed(core,uid,zid):
                raise ValueError("انتخاب رکورد منقضی شد؛ مجدداً دامنه را باز کنید.")
            r=await asyncio.to_thread(cf.get_record_details_strict,zid,rid)
            if r['type'] not in GROUP_TYPES:raise ValueError("نوع رکورد پشتیبانی نمی‌شود")
            with db() as c:
                c.execute("INSERT OR IGNORE INTO dns_group_members(group_id,zone_id,record_id) VALUES(?,?,?)",(gid,zid,rid))
            await group_menu(update,core,uid,gid)
        elif data.startswith('dx_rm_'):
            _,_,gid,rid=data.split('_')
            if not group(uid,gid):raise ValueError("گروه نامعتبر")
            with db() as c:
                hits=c.execute("SELECT zone_id FROM dns_group_members WHERE group_id=? AND record_id=?",(gid,rid)).fetchall()
                # Never delete two zone memberships due to an ID collision.
                if len(hits)!=1:raise ValueError("عضو یکتا نبود؛ فهرست را تازه‌سازی کنید")
                c.execute("DELETE FROM dns_group_members WHERE group_id=? AND zone_id=? AND record_id=?",(gid,hits[0]['zone_id'],rid))
            await group_menu(update,core,uid,gid)
        elif data.startswith('dx_grename_'):
            gid=data.split('_')[-1]
            if not group(uid,gid):raise ValueError("گروه پیدا نشد")
            FEATURE_INPUT[uid]={"mode":"group_rename","gid":gid}
            await show(update,"نام جدید گروه را بفرستید:",[back(f"dx_g_{gid}")])
        elif data.startswith('dx_gdelq_'):
            gid=data.split('_')[-1]
            if not group(uid,gid):raise ValueError("گروه پیدا نشد")
            await show(update,"فقط گروه و ارتباط اعضا حذف می‌شود؛ هیچ رکورد DNS حذف نمی‌شود.",
                       [[Button("🗑️ تأیید حذف گروه",callback_data=f"dx_gdel_{gid}")],back(f"dx_g_{gid}")])
        elif data.startswith('dx_gdel_'):
            gid=data.split('_')[-1]
            with db() as c:c.execute("DELETE FROM dns_groups WHERE owner=? AND id=?",(uid,gid))
            await groups_menu(update,core,uid)
        elif data.startswith('dx_ge_'):
            await group_edit_menu(update,core,uid,data[6:])
        elif data.startswith('dx_field_'):
            _,_,gid,field=data.split('_')
            if not group(uid,gid):raise ValueError("گروه نامعتبر")
            if field=='proxied':
                kb=[[Button("✅ روشن",callback_data=f"dx_pval_{gid}_1"),Button("❌ خاموش",callback_data=f"dx_pval_{gid}_0")],back(f"dx_ge_{gid}")]
                await show(update,"وضعیت Proxy را برای همه اعضا مشخص کنید:",kb)
            elif field=='ttl':
                kb=[[Button(str(x),callback_data=f"dx_tval_{gid}_{x}") for x in [1,120,300]],
                    [Button(str(x),callback_data=f"dx_tval_{gid}_{x}") for x in [600,3600,86400]],
                    [Button("✍️ TTL دلخواه",callback_data=f"dx_customttl_{gid}")],back(f"dx_ge_{gid}")]
                await show(update,"TTL جدید (1 = Auto):",kb)
            else:
                FEATURE_INPUT[uid]={"mode":"edit","gid":gid,"field":field}
                await show(update,"مقدار جدید را بفرستید. برای حذف توضیح، علامت - بفرستید.", [back(f"dx_ge_{gid}")])
        elif data.startswith('dx_customttl_'):
            gid=data.split('_')[-1]
            if not group(uid,gid):raise ValueError("گروه نامعتبر")
            FEATURE_INPUT[uid]={"mode":"edit","gid":gid,"field":"ttl"}
            await show(update,"TTL را به ثانیه بفرستید (1 = خودکار، یا 60 تا 86400):",[back(f"dx_ge_{gid}")])
        elif data.startswith('dx_pval_') or data.startswith('dx_tval_'):
            _,kind,gid,val=data.split('_')
            if not group(uid,gid):raise ValueError("گروه نامعتبر")
            mm=[(m['zone_id'],m['record_id']) for m in members(gid)]
            await begin_preview(update,core,uid,mm,'proxied' if kind=='pval' else 'ttl',bool(int(val)) if kind=='pval' else int(val),gid=gid,source='group')
        elif data=='dx_abort':
            PENDING.pop(uid,None);await show(update,"❎ عملیات لغو شد. هیچ رکوردی تغییر نکرد.",[back("back_to_main")])
        elif data.startswith('dx_apply_'):
            await execute(update,core,uid,data.split('_')[-1])
        elif data=='dx_bulk' or data.startswith('dx_bulk_'):
            await bulk_menu(update,core,uid,int(data.split('_')[-1]) if data!='dx_bulk' else 0)
        elif data.startswith('dx_bz_'):
            _,_,zid,p=data.split('_');await bulk_records(update,core,uid,zid,int(p))
        elif data.startswith('dx_bs_'):
            rid=data.split('_')[-1];v=FEATURE_INPUT.get(uid,{})
            zid=v.get('bulk_zone')
            if not zid or not allowed(core,uid,zid):raise ValueError("دامنه معتبر نیست")
            r=await asyncio.to_thread(cf.get_record_details_strict,zid,rid)
            if r['type'] not in ('A','AAAA'):raise ValueError("فقط رکوردهای IP")
            selected=BULK_SELECTION.setdefault(uid,set());pair=(zid,rid)
            if pair in selected:selected.remove(pair)
            elif len(selected)>=200:raise ValueError("حداکثر ۲۰۰ رکورد")
            else:selected.add(pair)
            await bulk_records(update,core,uid,zid,v.get('bulk_page',0))
        elif data=='dx_bclear':
            BULK_SELECTION.pop(uid,None);await bulk_menu(update,core,uid)
        elif data=='dx_bedit':
            if not BULK_SELECTION.get(uid):raise ValueError("هیچ رکوردی انتخاب نشده")
            FEATURE_INPUT[uid]={"mode":"edit","field":"content","gid":None}
            await show(update,"IP جدید برای رکوردهای انتخاب‌شده را بفرستید. رکوردهای A و AAAA باید جداگانه انتخاب شوند.",[back("dx_bulk")])
        elif data=='dx_search':await search_menu(update)
        elif data.startswith('dx_sf_'):
            field=data[6:]
            if field not in ('content','name','type','proxied','comment'):raise ValueError("جست‌وجوی نامعتبر")
            FEATURE_INPUT[uid]={"mode":"search","field":field}
            await show(update,"متن موردنظر را بفرستید (برای Proxy: on/off):",[back("dx_search")])
        elif data.startswith('dx_sp_'):
            await search_results(update,uid,int(data[6:]))
        elif data.startswith('dx_sd_'):
            i=int(data[6:]);r=SEARCH_CACHE.get(uid,[])[i]
            if not allowed(core,uid,r['zone_id']):raise ValueError("دسترسی ندارید")
            await asyncio.to_thread(cf.get_record_details_strict,r['zone_id'],r['record_id'])
            z=await asyncio.to_thread(cf.get_zone_info_by_id,r['zone_id'])
            core.user_state.setdefault(uid,{}).update({"zone_id":r['zone_id'],"zone_name":z['name'] if z else "", "record_id":r['record_id']})
            await core.show_record_settings(update.effective_message,uid,r['zone_id'],r['record_id'])
        elif data=='dx_history' or data.startswith('dx_h_'):
            await history_menu(update,core,uid,int(data[5:]) if data!='dx_history' else 0)
        elif data=='dx_monitor' or data.startswith('dx_monitor_'):
            await monitors_menu(update,core,uid,0 if data=='dx_monitor' else int(data[11:]))
        elif data=='dx_mcheck':
            await monitor_job(context,core,owner_filter=uid)
            await monitors_menu(update,core,uid)
        elif data.startswith('dx_mz_'):
            await monitor_zones(update,core,uid,int(data[6:]))
        elif data.startswith('dx_mr_'):
            _,_,zid,p=data.split('_');await monitor_records(update,core,uid,zid,int(p))
        elif data.startswith('dx_madd_'):
            rid=data.split('_')[-1];zid=FEATURE_INPUT.get(uid,{}).get('monitor_zone')
            if not zid or not allowed(core,uid,zid):raise ValueError("دامنه انتخاب نشده")
            FEATURE_INPUT[uid]={"mode":"monitor_port","zone_id":zid,"record_id":rid}
            await show(update,"پورت TCP مقصد را بفرستید (مثلاً 443 یا 80):",[back("dx_monitor")])
        elif data.startswith('dx_mdel_'):
            rid=data.split('_')[-1]
            with db() as c:
                hits=c.execute("SELECT zone_id FROM dns_monitors WHERE owner=? AND record_id=?",(uid,rid)).fetchall()
                if len(hits)!=1:raise ValueError("مانیتور یکتا نبود؛ فهرست را تازه‌سازی کنید")
                c.execute("DELETE FROM dns_monitors WHERE owner=? AND zone_id=? AND record_id=?",(uid,hits[0]['zone_id'],rid))
            await monitors_menu(update,core,uid)
        else:
            await show(update,"دستور نامعتبر است.",[back("back_to_main")])
    except (ValueError,IndexError,KeyError,cf.CloudflareAPIError,sqlite3.Error) as e:
        LOG.warning("Advanced DNS action error for user %s: %s",uid,e)
        await show(update,f"❌ {txt(e,200)}",[back("back_to_main")])
    return True
