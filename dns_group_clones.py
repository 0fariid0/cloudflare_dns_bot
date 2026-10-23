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


def require(core,adv,uid,gid):
    if core.is_user_blocked(uid) or not core.is_user_authorized(uid) or not adv.group(uid,gid):
        raise ValueError('گروه پیدا نشد یا اجازه دسترسی ندارید.')


async def menu(update,core,adv,uid,gid,page=0):
    require(core,adv,uid,gid)
    all_rows=rows(adv,uid,gid)
    part,p=adv.page_slice(all_rows,page)
    allowed=adv.zones_for(core,uid)
    lines=[f"🧬 کلون‌های گروه ({len(all_rows)})",'کلون‌ها رکوردهایی با همان نام روی IPهای جدید هستند.',
           'اعضای اصلی گروه جدا می‌مانند؛ ویرایش گروه روی کلون‌ها اعمال نمی‌شود.','']
    kb=[]
    for item in part:
        if item['zone_id'] not in allowed:
            lines.append('🔒 کلون خارج از دسترسی فعلی');continue
        lines.append(f"• {adv.txt(item['name'],45)} → {item['ip']}")
        kb.append([B(f"🗑 {adv.txt(item['name'],23)} · {adv.txt(item['ip'],24)}",callback_data=f"dx_cdel_{gid}_{item['id']}")])
    kb+=adv.pager(p,len(all_rows),f'dx_cp_{gid}_')
    kb += [[B('🧬 کلون روی چند IP',callback_data=f'dx_cnew_{gid}'),B('🗑 حذف بر اساس IP',callback_data=f'dx_cips_{gid}_0')],adv.back(f'dx_g_{gid}')]
    await adv.show(update,'\n'.join(lines),kb)


async def ip_menu(update,core,adv,uid,gid,page):
    require(core,adv,uid,gid)
    permitted=adv.zones_for(core,uid)
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
    permitted=adv.zones_for(core,uid)
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
    permitted=adv.zones_for(core,uid);items=[];missing=[]
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
        if (row['zone_id'],row['record_id']) in member_set(adv,gid):
            raise ValueError('یکی از کلون‌ها به اعضای اصلی گروه اضافه شده؛ ابتدا آن را از گروه خارج کنید.')
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
        await _apply(update,core,adv,uid,plan)
    finally:BUSY.discard((uid,gid))


async def _apply(update,core,adv,uid,plan):
    gid=plan['gid'];permitted=adv.zones_for(core,uid)
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
    for entry in plan['items']:
        label = (entry[2]['name'] + ' → ' + entry[2]['content']) if plan['kind']=='create' else (entry[1]['name'] + ' → ' + entry[1]['content'])
        try:
            require(core,adv,uid,gid)
            if plan['kind']=='create':
                zid,source,payload=entry
                if not adv.allowed(core,uid,zid):raise ValueError('دسترسی دامنه لغو شد.')
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
                if not adv.allowed(core,uid,zid):raise ValueError('دسترسی دامنه لغو شد.')
                with adv.db() as c:
                    tracked=c.execute('SELECT 1 FROM dns_group_clones WHERE id=? AND owner=? AND group_id=? AND record_id=?',(row['id'],uid,gid,rid)).fetchone()
                if not tracked:raise ValueError('کلون دیگر در فهرست نیست.')
                if (zid,rid) in member_set(adv,gid):raise ValueError('کلون هم‌اکنون عضو اصلی گروه است؛ حذف نشد.')
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
    if not data.startswith(('dx_cm_','dx_cp_','dx_cnew_','dx_cips_','dx_cdel_','dx_cdip_','dx_capply_')):return False
    uid=update.effective_user.id
    try:
        if data.startswith('dx_capply_'):await apply(update,core,adv,uid,data[10:])
        else:
            PLANS.pop(uid,None)
            parts=data.split('_');kind=parts[1];gid=parts[2]
            require(core,adv,uid,gid)
            if kind=='cm':await menu(update,core,adv,uid,gid)
            elif kind=='cp':await menu(update,core,adv,uid,gid,int(parts[3]))
            elif kind=='cnew':
                adv.FEATURE_INPUT[uid]={'mode':'clone_ips','gid':gid}
                await adv.show(update,'🧬 IPهای مقصد را با فاصله یا در چند خط بفرستید.\nمثال:\n1.2.3.4\n5.6.7.8\nA روی IPv4 و AAAA روی IPv6 کلون می‌شود؛ CNAME رد می‌شود.\nرکوردهای ساخته‌شده از این بخش قابل حذف تکی یا بر اساس IP هستند.',[adv.back(f'dx_cm_{gid}')])
            elif kind=='cips':await ip_menu(update,core,adv,uid,gid,int(parts[3]))
            elif kind in ('cdel','cdip'):await preview_delete(update,core,adv,uid,gid,parts[3],kind=='cdip')
    except Exception as exc:
        LOG.exception('Clone workflow failed')
        await adv.show(update,'❌ '+adv.txt(exc,300),[adv.back('dx_groups')])
    return True
