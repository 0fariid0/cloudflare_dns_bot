"""Confirmed group clones; deletion targets only records created and tracked here."""
from __future__ import annotations
import asyncio
import ipaddress
import json
import logging
import re
import secrets
import time
from telegram import InlineKeyboardButton as B
import cloudflare_api as cf

LOG = logging.getLogger(__name__)
PLANS = {}
BUSY = set()
LIMIT = 200


def initialize(adv):
    with adv.db() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS dns_group_clones(
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner INTEGER NOT NULL,
            group_id TEXT NOT NULL, zone_id TEXT NOT NULL, record_id TEXT NOT NULL,
            ip TEXT NOT NULL, name TEXT NOT NULL, type TEXT NOT NULL,
            created_at TEXT NOT NULL, snapshot TEXT NOT NULL,
            UNIQUE(zone_id,record_id))''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_clones_group ON dns_group_clones(owner,group_id)')


def rows(adv, uid, gid):
    with adv.db() as c:
        return c.execute('SELECT * FROM dns_group_clones WHERE owner=? AND group_id=? ORDER BY ip,name,id', (uid,gid)).fetchall()


def parse_ips(text):
    parts = [p for p in re.split(r'[,،;\s]+', text.strip()) if p]
    if not parts or len(parts) > 20:
        raise ValueError('بین ۱ تا ۲۰ IP با فاصله یا خط جدید بفرستید.')
    return list(dict.fromkeys(str(ipaddress.ip_address(p)) for p in parts))


def signature(r):
    return {k:r.get(k) for k in ('name','type','content','ttl','proxied','comment','tags','settings','modified_on')}


def identity(r):
    return (r['name'].rstrip('.').casefold(), r['type'], str(ipaddress.ip_address(r['content'])))


def member_set(adv,gid):
    return {(m['zone_id'],m['record_id']) for m in adv.members(gid)}


def is_source_member(adv,zid,rid):
    with adv.db() as c:
        return c.execute('SELECT 1 FROM dns_group_members WHERE zone_id=? AND record_id=? LIMIT 1',(zid,rid)).fetchone() is not None


def require(core,adv,uid,gid):
    if core.is_user_blocked(uid) or not core.is_user_authorized(uid) or not adv.group(uid,gid):
        raise ValueError('گروه پیدا نشد یا اجازه دسترسی ندارید.')


def visible_rows(adv, uid, permitted):
    with adv.db() as c:
        rr = c.execute("""SELECT cl.*,g.name group_name FROM dns_group_clones cl
            JOIN dns_groups g ON g.id=cl.group_id AND g.owner=cl.owner
            WHERE cl.owner=? ORDER BY g.name,cl.ip,cl.name,cl.id""", (uid,)).fetchall()
    return [r for r in rr if r['zone_id'] in permitted]


async def hub(update,core,adv,uid,page=0,by_ip=False):
    permitted=await adv.zones_for(core,uid)
    rr=visible_rows(adv,uid,permitted)
    buckets={}
    for r in rr:
        key=r['ip'] if by_ip else r['group_id']
        buckets.setdefault(key,[]).append(r)
    items,p=adv.page_slice(list(buckets.items()),page)
    lines=[f'🧬 مرکز کلون‌ها • {len(rr)} رکورد',
           'کلون‌های گروهی و تکیِ ثبت‌شده، یکجا در این بخش هستند.',
           'نمایش بر اساس '+('IP' if by_ip else 'گروه')]
    kb=[]
    for key,items_in in items:
        first=items_in[0]
        label=f"🌐 {key} · {len(items_in)} کلون" if by_ip else f"📁 {adv.txt(first['group_name'],28)} · {len(items_in)} کلون"
        target=f"dx_caip_{first['id']}_0" if by_ip else f"dx_cm_{key}"
        kb.append([B(label,callback_data=target)])
    if not rr: lines.append('هنوز کلونی ثبت نشده؛ از داخل گروه یا تنظیمات رکورد، کلون بسازید.')
    kb+=adv.pager(p,len(buckets),'dx_chip_' if by_ip else 'dx_chg_')
    kb += [[B('📁 بر اساس گروه',callback_data='dx_clones'),B('🌐 بر اساس IP',callback_data='dx_chip_0')],
           [B('🔎 کلون‌های قدیمی / هم‌نام',callback_data='dx_legacy')],
           [B('📁 گروه‌ها',callback_data='dx_groups'),B('🏠 خانه',callback_data='back_to_main')]]
    await adv.show(update,'\n'.join(lines),kb)


async def menu(update,core,adv,uid,gid,page=0):
    require(core,adv,uid,gid)
    permitted=await adv.zones_for(core,uid)
    rr=[r for r in rows(adv,uid,gid) if r['zone_id'] in permitted]
    ips={}
    for r in rr: ips.setdefault(r['ip'],[]).append(r)
    part,p=adv.page_slice(list(ips.items()),page)
    g=adv.group(uid,gid)
    lines=[f"🧬 {adv.txt(g['name'],48)}",f'{len(rr)} کلون روی {len(ips)} IP',
           'IP را انتخاب کنید تا رکوردها را ببینید، ویرایش یا حذف کنید.']
    kb=[[B(f"🌐 {ip} · {len(items)} رکورد",callback_data=f"dx_cv_{gid}_{items[0]['id']}_0")] for ip,items in part]
    kb+=adv.pager(p,len(ips),f'dx_cp_{gid}_')
    kb += [[B('➕ کلون روی چند IP',callback_data=f'dx_cnew_{gid}'),B('🗑 حذف بر اساس IP',callback_data=f'dx_cips_{gid}_0')],
           [B('🧬 همه کلون‌ها',callback_data='dx_clones'),B('🔙 گروه',callback_data=f'dx_g_{gid}')]]
    await adv.show(update,'\n'.join(lines),kb)


async def records_menu(update,core,adv,uid,rowid,page=0,gid=None):
    permitted=await adv.zones_for(core,uid)
    rr=visible_rows(adv,uid,permitted)
    selected=next((r for r in rr if r['id']==int(rowid)),None)
    if not selected: raise ValueError('کلون در دسترس نیست؛ فهرست را تازه کنید.')
    if gid and selected['group_id']!=gid: raise ValueError('گروه نامعتبر است.')
    targets=[r for r in rr if r['ip']==selected['ip'] and (not gid or r['group_id']==gid)]
    part,p=adv.page_slice(targets,page)
    kb=[[B(f"{adv.txt(r['name'],32)} · {adv.txt(r['group_name'],18)}",callback_data=f"dx_ccard_{r['group_id']}_{r['id']}")] for r in part]
    prefix=f'dx_cv_{gid}_{rowid}_' if gid else f'dx_caip_{rowid}_'
    kb+=adv.pager(p,len(targets),prefix)
    if gid:
        kb.append([B('🗑 حذف کلون‌های این IP از گروه',callback_data=f'dx_cdip_{gid}_{rowid}')])
    kb.append(adv.back(f'dx_cm_{gid}' if gid else 'dx_chip_0'))
    await adv.show(update,f"🌐 {selected['ip']}\n{len(targets)} کلون؛ رکورد را برای مدیریت انتخاب کنید.",kb)


async def card(update,core,adv,uid,gid,rowid):
    require(core,adv,uid,gid)
    row=next((r for r in rows(adv,uid,gid) if r['id']==int(rowid)),None)
    if not row or not await adv.allowed(core,uid,row['zone_id']):raise ValueError('کلون در دسترس نیست.')
    await adv.show(update,f"🧬 {adv.txt(row['name'],100)}\nنوع: {row['type']}\nIP هنگام ساخت: {row['ip']}\nبرای دیدن مقدار فعلی و ویرایش، تنظیمات رکورد را باز کنید.",
        [[B('⚙️ تنظیمات و ویرایش',callback_data=f'dx_copen_{gid}_{rowid}'), B('🗑 حذف کلون',callback_data=f'dx_cdel_{gid}_{rowid}')],
         adv.back(f'dx_cv_{gid}_{rowid}_0')])


async def open_record(update,core,adv,uid,zid,rid,back_to):
    if not await adv.allowed(core,uid,zid):raise ValueError('دسترسی دامنه لغو شده است.')
    z=await asyncio.to_thread(cf.get_zone_info_by_id,zid)
    core.reset_user_state(uid)
    core.user_state[uid].update(zone_id=zid,zone_name=(z or {}).get('name',''),record_id=rid,record_back=back_to)
    await core.show_record_settings(update.effective_message,uid,zid,rid)


async def start_single(update,core,adv,uid,zid,rid):
    import hashlib
    if not await adv.allowed(core,uid,zid):raise ValueError('دسترسی دامنه معتبر نیست.')
    r=await asyncio.to_thread(cf.get_record_details_strict,zid,rid)
    if r['type'] not in ('A','AAAA'):raise ValueError('فقط رکورد A یا AAAA کلون می‌شود.')
    gid=hashlib.sha256(f'{uid}:{zid}:{rid}'.encode()).hexdigest()[:12]
    with adv.db() as c:
        c.execute('INSERT OR IGNORE INTO dns_groups(id,owner,name,created_at) VALUES(?,?,?,?)',
                  (gid,uid,f"تکی · {adv.txt(r['name'],25)} · {gid[:6]}",adv.stamp()))
        c.execute('INSERT OR IGNORE INTO dns_group_members(group_id,zone_id,record_id) VALUES(?,?,?)',(gid,zid,rid))
    adv.FEATURE_INPUT[uid]={'mode':'clone_ips','gid':gid}
    await adv.show(update,'🧬 IPهای مقصد را با فاصله یا در چند خط بفرستید.\nقبل از ساخت، پیش‌نمایش و تأیید نمایش داده می‌شود.\nکلون‌ها در «مرکز کلون‌ها» ثبت خواهند شد.',[adv.back(f'dx_cm_{gid}')])


DISCOVERIES={}

async def legacy_scan(update,core,adv,uid):
    zones=await adv.zones_for(core,uid)
    sem=asyncio.Semaphore(4)
    async def fetch(zid):
        async with sem:
            try:return zid,await asyncio.to_thread(cf.get_dns_records_fresh,zid),None
            except Exception as exc:return zid,[],exc
    found=[];failed=0
    for zid,rr,error in await asyncio.gather(*(fetch(z) for z in zones)):
        if error: failed+=1;continue
        same={}
        for r in rr:
            if r.get('type') in ('A','AAAA'):same.setdefault((r['name'].casefold(),r['type']),[]).append(r)
        for bucket in same.values():
            if len(bucket)>1:found.extend((zid,r) for r in bucket)
    DISCOVERIES[uid]=found
    await legacy_menu(update,core,adv,uid,0,failed)


async def legacy_menu(update,core,adv,uid,page,failed=0):
    permitted=await adv.zones_for(core,uid)
    rr=[r for r in DISCOVERIES.get(uid,[]) if r[0] in permitted]
    DISCOVERIES[uid]=rr
    part,p=adv.page_slice(list(enumerate(rr)),page)
    lines=[f'🔎 رکوردهای هم‌نام / کلون‌های قدیمی ({len(rr)})',
           'این رکوردها نام و نوع یکسان دارند؛ ممکن است کلون قدیمی یا رکورد عادی چند IP باشند. چیزی خودکار حذف یا ثبت نمی‌شود.']
    if failed:lines.append(f'⚠️ دریافت {failed} دامنه ناموفق بود؛ نتیجه کامل نیست.')
    kb=[[B(f"{adv.txt(r['name'],28)} → {adv.txt(r['content'],24)}",callback_data=f'dx_lopen_{i}')] for i,(_,r) in part]
    kb+=adv.pager(p,len(rr),'dx_lp_')+[[B('🔄 اسکن دوباره',callback_data='dx_legacy')],adv.back('dx_clones')]
    await adv.show(update,'\n'.join(lines),kb)


async def ip_menu(update,core,adv,uid,gid,page):
    require(core,adv,uid,gid)
    permitted=await adv.zones_for(core,uid)
    ips={}
    for r in rows(adv,uid,gid):
        if r['zone_id'] in permitted:
            ips.setdefault(r['ip'],[]).append(r)
    part,p=adv.page_slice(list(ips.items()),page)
    kb=[[B(f"🗑 {ip} ({len(rr)} رکورد)",callback_data=f"dx_cdip_{gid}_{rr[0]['id']}")] for ip,rr in part]
    kb+=adv.pager(p,len(ips),f'dx_cips_{gid}_')+[adv.back(f'dx_cm_{gid}')]
    await adv.show(update,'IP را انتخاب کنید؛ فقط کلون‌های ثبت‌شده همین گروه حذف می‌شوند.\nمرحله بعد پیش‌نمایش دارد.',kb)


async def preview_create(update,core,adv,uid,gid,text):
    require(core,adv,uid,gid)
    ips=parse_ips(text)
    pairs=member_set(adv,gid)
    if not pairs:raise ValueError('گروه عضو ندارد؛ ابتدا رکورد اضافه کنید.')
    permitted=await adv.zones_for(core,uid)
    sources=[]; skipped=0; desired={}
    # Fetch every source before presenting any mutation plan.
    for zid,rid in sorted(pairs):
        if zid not in permitted:raise ValueError('دسترسی یکی از اعضای گروه لغو شده است.')
        r=await asyncio.to_thread(cf.get_record_details_strict,zid,rid)
        if r['type'] not in ('A','AAAA'):
            skipped+=1;continue
        sources.append((zid,r))
        for ip in ips:
            if ipaddress.ip_address(ip).version != (4 if r['type']=='A' else 6):continue
            payload={k:r[k] for k in ('name','type','ttl','proxied','comment','tags','settings') if k in r and r[k] is not None}
            payload['content']=ip
            if payload.get('proxied'):payload['ttl']=1
            desired[(zid,identity(payload))]=(zid,r,payload)
    if not desired:raise ValueError('IPها با اعضای A/AAAA گروه سازگار نیستند؛ CNAME کلون نمی‌شود.')
    if len(desired)>LIMIT:raise ValueError('حداکثر ۲۰۰ کلون در هر عملیات مجاز است.')
    existing={}
    for zid in {z for z,_,_ in desired.values()}:
        existing[zid]={identity(r) for r in await asyncio.to_thread(cf.get_dns_records_fresh,zid) if r['type'] in ('A','AAAA')}
    items=[v for (z,key),v in desired.items() if key not in existing[z]]
    duplicate=len(desired)-len(items)
    if not items:
        await adv.show(update,f'✅ رکوردها از قبل وجود دارند؛ چیزی ساخته نشد.\nتکراری: {duplicate}',[adv.back(f'dx_cm_{gid}')]);return
    plan={'kind':'create','gid':gid,'items':items,'members':pairs,'at':time.monotonic()}
    token=secrets.token_hex(8);PLANS[uid]=(token,plan)
    lines=['🧬 تأیید کلون گروه',f'ساخت: {len(items)} رکورد • موجود/تکراری: {duplicate}',f'اعضای غیر IP ردشده: {skipped}',
           'هر رکورد با همان نام، TTL و وضعیت پروکسی ساخته می‌شود.']
    lines += [f"• {adv.txt(r['name'],40)} → {p['content']}" for _,r,p in items[:16]]
    if len(items)>16:lines.append(f'… و {len(items)-16} رکورد دیگر')
    lines.append('رکوردهای موجود تغییر نمی‌کنند و به‌عنوان کلون جدید ثبت نمی‌شوند.')
    await adv.show(update,'\n'.join(lines),[[B('✅ ساخت کلون‌ها',callback_data=f'dx_capply_{token}')],[B('❌ لغو',callback_data=f'dx_cm_{gid}')]])


async def preview_delete(update,core,adv,uid,gid,rowid,by_ip=False):
    require(core,adv,uid,gid)
    rr=rows(adv,uid,gid)
    selected=next((r for r in rr if r['id']==int(rowid)),None)
    if not selected:raise ValueError('کلون پیدا نشد؛ فهرست را تازه کنید.')
    targets=[r for r in rr if r['ip']==selected['ip']] if by_ip else [selected]
    if len(targets)>LIMIT:raise ValueError('بیش از ۲۰۰ رکورد؛ کلون‌ها را جداگانه حذف کنید.')
    permitted=await adv.zones_for(core,uid);items=[];missing=[]
    for row in targets:
        if row['zone_id'] not in permitted:raise ValueError('به یکی از دامنه‌ها دسترسی ندارید؛ چیزی حذف نشد.')
        try:r=await asyncio.to_thread(cf.get_record_details_strict,row['zone_id'],row['record_id'])
        except cf.CloudflareAPIError as exc:
            if exc.status_code==404:
                missing.append(row['id']);continue
            raise
        original=json.loads(row['snapshot'])
        if identity(r)!=identity(original):
            raise ValueError('نام، نوع یا IP یکی از کلون‌ها بیرون از این بخش تغییر کرده؛ برای جلوگیری از حذف اشتباه عملیات متوقف شد.')
        if is_source_member(adv,row['zone_id'],row['record_id']):
            raise ValueError('یکی از کلون‌ها عضو اصلی یک گروه است؛ ابتدا آن را از اعضای آن گروه خارج کنید.')
        items.append((dict(row),r))
    # Forget only externally deleted IDs; no Cloudflare record is deleted here.
    with adv.db() as c:
        for rowid in missing:c.execute('DELETE FROM dns_group_clones WHERE id=? AND owner=?',(rowid,uid))
    if not items:
        await adv.show(update,'کلون‌ها قبلاً حذف شده‌اند؛ فهرست اصلاح شد.',[adv.back(f'dx_cm_{gid}')]);return
    token=secrets.token_hex(8)
    PLANS[uid]=(token,{'kind':'delete','gid':gid,'items':items,'at':time.monotonic()})
    lines=[f'🗑 تأیید حذف {len(items)} کلون', 'فقط کلون‌های زیر حذف می‌شوند؛ اعضای اصلی و رکوردهای از قبل موجود حفظ می‌شوند.']
    lines += [f"• {adv.txt(r['name'],40)} → {r['content']}" for _,r in items[:16]]
    if len(items)>16:lines.append(f'… و {len(items)-16} کلون دیگر')
    await adv.show(update,'\n'.join(lines),[[B('🗑 تأیید حذف',callback_data=f'dx_capply_{token}')],[B('❌ لغو',callback_data=f'dx_cm_{gid}')]])


async def apply(update,core,adv,uid,token):
    saved=PLANS.get(uid)
    if not saved or saved[0]!=token or time.monotonic()-saved[1]['at']>600:
        raise ValueError('پیش‌نمایش منقضی شده؛ دوباره انتخاب کنید.')
    plan=saved[1];gid=plan['gid'];require(core,adv,uid,gid)
    if (uid,gid) in BUSY:raise ValueError('عملیات قبلی این گروه هنوز در حال اجراست.')
    PLANS.pop(uid,None);BUSY.add((uid,gid))
    try:
        await adv.show(update,'⏳ در حال بررسی و اجرای عملیات کلون؛ لطفاً منتظر نتیجه بمانید.',[])
        await _apply(update,core,adv,uid,plan)
    finally:BUSY.discard((uid,gid))


async def _apply(update,core,adv,uid,plan):
    gid=plan['gid'];permitted=await adv.zones_for(core,uid)
    # Preflight everything before the first write.
    existing={}
    if plan['kind']=='create':
        if member_set(adv,gid)!=plan['members']:raise ValueError('اعضای گروه تغییر کرده‌اند؛ پیش‌نمایش جدید بگیرید.')
        checked=set()
        for zid,source,payload in plan['items']:
            if zid not in permitted:raise ValueError('دسترسی دامنه تغییر کرده است.')
            if (zid,source['id']) not in checked:
                live=await asyncio.to_thread(cf.get_record_details_strict,zid,source['id'])
                if signature(live)!=signature(source):raise ValueError('رکورد منبع تغییر کرده؛ پیش‌نمایش جدید بگیرید.')
                checked.add((zid,source['id']))
        for zid in {z for z,_,_ in plan['items']}:
            existing[zid]={identity(r) for r in await asyncio.to_thread(cf.get_dns_records_fresh,zid) if r['type'] in ('A','AAAA')}
    else:
        for row,snapshot in plan['items']:
            if row['zone_id'] not in permitted:raise ValueError('دسترسی دامنه تغییر کرده است.')
            live=await asyncio.to_thread(cf.get_record_details_strict,row['zone_id'],row['record_id'])
            if signature(live)!=signature(snapshot):raise ValueError('کلون پس از تأیید تغییر کرده؛ دوباره پیش‌نمایش بگیرید.')
    success=0;skipped=0;failures=[]
    last_progress=time.monotonic()
    for index,entry in enumerate(plan['items'],1):
        if time.monotonic()-last_progress>8:
            await adv.show(update,f'⏳ در حال اجرا: {index-1}/{len(plan["items"])}\nموفق: {success} • ناموفق: {len(failures)}',[])
            last_progress=time.monotonic()
        label = (entry[2]['name'] + ' → ' + entry[2]['content']) if plan['kind']=='create' else (entry[1]['name'] + ' → ' + entry[1]['content'])
        try:
            require(core,adv,uid,gid)
            if plan['kind']=='create':
                zid,source,payload=entry
                if not await adv.allowed(core,uid,zid):raise ValueError('دسترسی دامنه لغو شد.')
                if member_set(adv,gid)!=plan['members']:raise ValueError('اعضای گروه هم‌زمان تغییر کردند.')
                key=identity(payload)
                if key in existing[zid]:skipped+=1;continue
                live=await asyncio.to_thread(cf.get_record_details_strict,zid,source['id'])
                if signature(live)!=signature(source):raise ValueError('رکورد منبع هم‌زمان تغییر کرد.')
                result=await asyncio.to_thread(cf._request,'POST',f'/zones/{zid}/dns_records',json=payload)
                cf._invalidate_records_cache(zid)
                created=result.get('result') or {}
                if not created.get('id'):
                    raise ValueError('پاسخ ساخت شناسه نداشت؛ وضعیت رکورد را در Cloudflare بررسی کنید.')
                try:
                    with adv.db() as c:
                        c.execute('''INSERT INTO dns_group_clones(owner,group_id,zone_id,record_id,ip,name,type,created_at,snapshot)
                            VALUES(?,?,?,?,?,?,?,?,?)''',(uid,gid,zid,created['id'],payload['content'],payload['name'],payload['type'],adv.stamp(),json.dumps(created)))
                except Exception:
                    LOG.exception('Created clone not registered: zone=%s record=%s',zid,created['id'])
                    raise ValueError(f"رکورد ساخته شد ولی ثبت مدیریت ناموفق بود؛ ID: {created['id']}؛ قبل از تلاش مجدد Cloudflare را بررسی کنید.")
                adv.write_history(uid,'group_clone',zid,created['id'],{},created,gid)
                existing[zid].add(key)
            else:
                row,snapshot=entry;zid=row['zone_id'];rid=row['record_id']
                if not await adv.allowed(core,uid,zid):raise ValueError('دسترسی دامنه لغو شد.')
                with adv.db() as c:
                    tracked=c.execute('SELECT 1 FROM dns_group_clones WHERE id=? AND owner=? AND group_id=? AND record_id=?',(row['id'],uid,gid,rid)).fetchone()
                if not tracked:raise ValueError('کلون دیگر در فهرست نیست.')
                if is_source_member(adv,zid,rid):raise ValueError('کلون هم‌اکنون عضو اصلی گروه است؛ حذف نشد.')
                live=await asyncio.to_thread(cf.get_record_details_strict,zid,rid)
                if signature(live)!=signature(snapshot):raise ValueError('کلون هم‌زمان تغییر کرد؛ حذف نشد.')
                await asyncio.to_thread(cf._request,'DELETE',f'/zones/{zid}/dns_records/{rid}')
                cf._invalidate_records_cache(zid)
                adv.write_history(uid,'group_clone_delete',zid,rid,live,{},gid)
                with adv.db() as c:c.execute('DELETE FROM dns_group_clones WHERE id=? AND owner=?',(row['id'],uid))
            success+=1
        except Exception as exc:
            LOG.warning('Group clone operation failed: %s',exc)
            failures.append(adv.txt(label,95) + ': ' + adv.txt(exc,220))
    verb='ساخته‌شده' if plan['kind']=='create' else 'حذف‌شده'
    lines=[f'✅ {verb}: {success}',f'↩️ از قبل موجود: {skipped}',f'❌ ناموفق/نیازمند بررسی: {len(failures)}']
    lines+=['• '+x for x in failures[:8]]
    if failures:lines.append('تغییرات موفق حفظ شده‌اند. اگر خطای ارتباطی هنگام ساخت بود، پیش از تکرار وضعیت Cloudflare را بررسی کنید.')
    await adv.show(update,'\n'.join(lines),[adv.back(f'dx_cm_{gid}')])


async def callback(update,context,core,adv):
    data=update.callback_query.data
    prefixes=('dx_cm_','dx_cp_','dx_cnew_','dx_cips_','dx_cdel_','dx_cdip_','dx_capply_',
              'dx_chg_','dx_chip_','dx_cv_','dx_caip_','dx_ccard_','dx_copen_','dx_lp_','dx_lopen_')
    if data not in ('dx_clones','dx_legacy') and not data.startswith(prefixes):return False
    uid=update.effective_user.id
    try:
        if data.startswith('dx_capply_'):
            await apply(update,core,adv,uid,data[10:])
        else:
            PLANS.pop(uid,None)
            adv.PENDING.pop(uid,None)
            parts=data.split('_');kind=parts[1]
            if data=='dx_clones':await hub(update,core,adv,uid)
            elif kind in ('chg','chip'):await hub(update,core,adv,uid,int(parts[2]),kind=='chip')
            elif kind=='caip':await records_menu(update,core,adv,uid,parts[2],int(parts[3]))
            elif data=='dx_legacy':await legacy_scan(update,core,adv,uid)
            elif kind=='lp':await legacy_menu(update,core,adv,uid,int(parts[2]))
            elif kind=='lopen':
                rr=DISCOVERIES.get(uid,[]);index=int(parts[2])
                if not 0<=index<len(rr):raise ValueError('فهرست منقضی شده؛ دوباره اسکن کنید.')
                zid,r=rr[index]
                await open_record(update,core,adv,uid,zid,r['id'],'dx_lp_0')
            else:
                gid=parts[2];require(core,adv,uid,gid)
                if kind=='cm':await menu(update,core,adv,uid,gid)
                elif kind=='cp':await menu(update,core,adv,uid,gid,int(parts[3]))
                elif kind=='cv':await records_menu(update,core,adv,uid,parts[3],int(parts[4]),gid)
                elif kind=='ccard':await card(update,core,adv,uid,gid,parts[3])
                elif kind=='copen':
                    row=next((r for r in rows(adv,uid,gid) if r['id']==int(parts[3])),None)
                    if not row:raise ValueError('کلون پیدا نشد.')
                    await open_record(update,core,adv,uid,row['zone_id'],row['record_id'],f"dx_ccard_{gid}_{row['id']}")
                elif kind=='cnew':
                    adv.FEATURE_INPUT[uid]={'mode':'clone_ips','gid':gid}
                    await adv.show(update,'🧬 IPهای مقصد را با فاصله یا در چند خط بفرستید.\nA روی IPv4 و AAAA روی IPv6 کلون می‌شود؛ CNAME رد می‌شود.\nقبل از اجرا پیش‌نمایش و تأیید نمایش داده خواهد شد.',[adv.back(f'dx_cm_{gid}')])
                elif kind=='cips':await ip_menu(update,core,adv,uid,gid,int(parts[3]))
                elif kind in ('cdel','cdip'):await preview_delete(update,core,adv,uid,gid,parts[3],kind=='cdip')
    except Exception as exc:
        LOG.exception('Clone workflow failed')
        await adv.show(update,'❌ '+adv.txt(exc,300),[adv.back('dx_clones')])
    return True
