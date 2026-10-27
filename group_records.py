"""Uniform IP rows and confirmed member controls for one DNS group."""
from __future__ import annotations

import asyncio
import secrets
import time
from telegram import InlineKeyboardButton as B
import cloudflare_api as cf

VIEWS = {}
PAGE_SIZE = 4


def require(core, adv, uid, gid):
    adv.group_clones.require(core, adv, uid, gid)


def selected_pairs(adv, uid, gid, kind='record'):
    # Copy provenance is stored for compatibility, never a separate record class.
    members = {(r['zone_id'], r['record_id']) for r in adv.members(gid)}
    copies = {(r['zone_id'], r['record_id']) for r in adv.group_clones.rows(adv, uid, gid)}
    return members | copies


async def dashboard(update, core, adv, uid, gid, page=0):
    require(core, adv, uid, gid)
    permitted = await adv.zones_for(core, uid)
    pairs = selected_pairs(adv, uid, gid)
    sem = asyncio.Semaphore(4)

    async def fetch(zid):
        async with sem:
            try:
                rr = await asyncio.to_thread(cf.get_dns_records_fresh, zid)
                return zid, {r['id']: r for r in rr}, None
            except Exception as exc:
                return zid, {}, exc

    zones = sorted({z for z, _ in pairs if z in permitted})
    results = await asyncio.gather(*(fetch(z) for z in zones))
    live = {z: rr for z, rr, error in results if error is None}
    failed = {z for z, _, error in results if error is not None}
    buckets = {}
    missing = 0
    hidden = sum(z not in permitted for z, _ in pairs)
    for kind, targets in (('record', pairs),):
        for zid, rid in sorted(targets):
            if zid not in live:
                continue
            r = live[zid].get(rid)
            if not r:
                missing += 1
                continue
            key = kind, r['type'], r['content']
            buckets.setdefault(key, []).append((zid, r))
    items = [(kind, typ, content, records) for (kind, typ, content), records in buckets.items()]
    items.sort(key=lambda x: (x[1], x[2]))
    page = max(0, min(int(page), max(0, (len(items)-1)//PAGE_SIZE)))
    token = secrets.token_hex(6)
    view = {'token': token, 'gid': gid, 'at': time.monotonic(), 'buckets': items, 'page': page}
    VIEWS[uid] = view
    g = adv.group(uid, gid)
    lines = [f"📁 {g['name']}"]
    kb = []
    for i in range(page*PAGE_SIZE, min(len(items), (page+1)*PAGE_SIZE)):
        _, typ, content, records = items[i]
        kb.append([B(adv.txt(content,48), callback_data=f'dx_br_{token}_{i}_0'),
                   B('✏️', callback_data=f'dx_be_{token}_{i}'),
                   B('🗑', callback_data=f'dx_bd_{token}_{i}')])
    if not pairs:
        lines.append('هنوز عضوی ندارد؛ از «کنترل اعضا» عضو اضافه کنید.')
    if failed: lines.append(f'⚠️ دریافت {len(failed)} دامنه ناموفق بود؛ ردیف‌های آن نمایش داده نشدند.')
    if missing: lines.append(f'⚠️ {missing} رکورد دیگر در Cloudflare موجود نیست؛ «کنترل اعضا» را بررسی کنید.')
    if hidden: lines.append(f'🔒 {hidden} رکورد خارج از دسترسی فعلی نمایش داده نشد.')
    nav = []
    if page: nav.append(B('⬅️ قبلی', callback_data=f'dx_view_{gid}_{page-1}'))
    if (page+1)*PAGE_SIZE < len(items): nav.append(B('بعدی ➡️', callback_data=f'dx_view_{gid}_{page+1}'))
    if nav: kb.append(nav)
    kb += [[B('👥 کنترل اعضا', callback_data=f'dx_members_{gid}_0'), B('➕ کلون جدید', callback_data=f'dx_cnew_{gid}')],
           [B('🔄 تازه‌سازی', callback_data=f'dx_view_{gid}_{page}')],
           adv.back('dx_groups')]
    await adv.show(update, '\n'.join(lines), kb)


def view_bucket(adv, uid, token, index):
    v = VIEWS.get(uid)
    if not v or v['token'] != token or time.monotonic()-v['at'] > 600:
        raise ValueError('این صفحه قدیمی شده است؛ گروه را دوباره باز کنید.')
    index = int(index)
    if not 0 <= index < len(v['buckets']): raise ValueError('ردیف نامعتبر است.')
    return v, v['buckets'][index]


async def records_menu(update, core, adv, uid, token, index, page=0):
    v, bucket = view_bucket(adv, uid, token, index)
    require(core, adv, uid, v['gid'])
    permitted = await adv.zones_for(core, uid)
    kind, typ, content, records = bucket
    part, page = adv.page_slice(list(enumerate(records)), page)
    kb = []
    for n, (zid, r) in part:
        if zid not in permitted: continue
        kb += [[B(adv.txt(r['name'],50), callback_data=f'dx_bo_{token}_{index}_{n}')],
               [B('✏️ ویرایش رکورد', callback_data=f'dx_bo_{token}_{index}_{n}'),
                B('🗑 حذف همین رکورد', callback_data=f'dx_bone_{token}_{index}_{n}')]]
    kb += adv.pager(page, len(records), f'dx_br_{token}_{index}_')
    kb.append(adv.back(f"dx_view_{v['gid']}_{v['page']}"))
    await adv.show(update, f'{content}\nبرای تغییر جداگانه، رکورد را انتخاب کنید.', kb)


async def preview(update, core, adv, uid, view, bucket, action, value=None, records=None):
    gid = view['gid']
    require(core, adv, uid, gid)
    kind, _, _, targets = bucket
    targets = targets if records is None else records
    if not targets or len(targets) > 200: raise ValueError('در هر عملیات ۱ تا ۲۰۰ رکورد انتخاب کنید.')
    permitted = await adv.zones_for(core, uid)
    current = selected_pairs(adv, uid, gid, kind)
    checked = []
    for zid, old in targets:
        if zid not in permitted or (zid, old['id']) not in current:
            raise ValueError('عضویت یا دسترسی رکورد تغییر کرده؛ صفحه را تازه کنید.')
        r = await asyncio.to_thread(cf.get_record_details_strict, zid, old['id'])
        if adv.key_fields(r) != adv.key_fields(old):
            raise ValueError('رکورد از زمان نمایش صفحه تغییر کرده؛ ابتدا تازه‌سازی کنید.')
        checked.append((zid, r))
    if action == 'edit': value = adv.valid_value('content', value, [r for _, r in checked])
    token = secrets.token_hex(8)
    adv.PENDING[uid] = {'kind': 'group_records', 'token': token, 'at': time.monotonic(), 'gid': gid,
                        'record_kind': kind, 'items': checked, 'action': action, 'value': value, 'page': view['page']}
    lines = [f"⚠️ تأیید {'حذف' if action=='delete' else 'تغییر'} {len(checked)} رکورد"]
    if action == 'delete':
        lines += ['این رکوردها از Cloudflare حذف می‌شوند؛ فقط برداشتن از گروه نیست.',
                  'سایر IPها حذف نمی‌شوند.']
        lines.append('عضویت همین رکوردها در گروه‌های دیگر نیز برداشته می‌شود.')
    else:lines.append('مقدار جدید: '+adv.txt(value,150))
    lines += [f"• {adv.txt(r['name'],55)} → {adv.txt(r['content'],55)}" for _,r in checked[:16]]
    if len(checked)>16:lines.append(f'… و {len(checked)-16} رکورد دیگر')
    kb = [[B('🗑 تأیید حذف' if action=='delete' else '✅ تأیید تغییر',callback_data=f'dx_apply_{token}')],
          adv.back(f"dx_view_{gid}_{view['page']}")]
    await adv.show(update,'\n'.join(lines),kb)


async def execute(update, core, adv, uid, plan):
    gid = plan['gid']
    require(core, adv, uid, gid)
    back = f"dx_view_{gid}_{plan['page']}"
    async def check(zid, r):
        require(core, adv, uid, gid)
        if not await adv.allowed(core,uid,zid):raise ValueError('دسترسی دامنه لغو شده است.')
        if (zid,r['id']) not in selected_pairs(adv,uid,gid,plan['record_kind']):
            raise ValueError('عضویت رکورد تغییر کرده است.')
        live = await asyncio.to_thread(cf.get_record_details_strict,zid,r['id'])
        if adv.key_fields(live)!=adv.key_fields(r):raise ValueError('رکورد پس از پیش‌نمایش تغییر کرده است.')
    # Validate every record before the first mutation.
    try:
        for zid,r in plan['items']:await check(zid,r)
    except Exception as exc:
        await adv.show(update,'❌ هیچ تغییری اعمال نشد: '+adv.txt(exc,240),[adv.back(back)])
        return
    await adv.show(update,'⏳ در حال اجرای عملیات؛ لطفاً منتظر نتیجه بمانید.',[])
    success=0;failures=[];last_progress=time.monotonic()
    for index,(zid,r) in enumerate(plan['items'],1):
        try:
            await check(zid,r)
            if plan['action']=='edit':
                changed=await asyncio.to_thread(cf.patch_dns_fields,zid,r['id'],{'content':plan['value']})
                adv.sync_tracked_record(zid,r['id'],changed)
            else:
                await asyncio.to_thread(cf._request,'DELETE',f"/zones/{zid}/dns_records/{r['id']}")
                cf._invalidate_records_cache(zid)
                adv.sync_tracked_record(zid,r['id'],None)
                changed={}
            adv.write_history(uid,'group_ip_'+plan['action'],zid,r['id'],r,changed,gid)
            success+=1
        except Exception as exc:failures.append(adv.txt(r['name'],50)+': '+adv.txt(exc,130))
        if time.monotonic()-last_progress>8:
            await adv.show(update,f"⏳ {index}/{len(plan['items'])} • موفق: {success}",[])
            last_progress=time.monotonic()
    lines=[f'✅ موفق: {success} • ❌ ناموفق: {len(failures)}']+['• '+r for r in failures[:10]]
    if failures:lines.append('تغییرات موفق حفظ شده‌اند؛ صفحه را تازه کنید و موارد ناموفق را بررسی کنید.')
    await adv.show(update,'\n'.join(lines),[[B('🔙 IPهای گروه',callback_data=back), B('🏠 خانه',callback_data='back_to_main')]])


async def callback(update, context, core, adv):
    data=update.callback_query.data
    if not data.startswith(('dx_view_','dx_options_','dx_members_','dx_mpick_','dx_br_','dx_be_','dx_bd_','dx_bo_','dx_bone_')):return False
    uid=update.effective_user.id
    adv.PENDING.pop(uid,None)
    adv.group_clones.PLANS.pop(uid,None)
    try:
        if data.startswith('dx_view_'):
            _,_,gid,page=data.split('_')
            await dashboard(update,core,adv,uid,gid,int(page))
        elif data.startswith('dx_options_'):
            gid=data[len('dx_options_'):]
            await members_menu(update,core,adv,uid,gid)
        elif data.startswith('dx_members_'):
            _,_,gid,page=data.split('_')
            await members_menu(update,core,adv,uid,gid,int(page))
        elif data.startswith('dx_mpick_'):
            _,_,token,index=data.split('_')
            view=MEMBER_VIEWS.get(uid)
            if not view or view['token']!=token or time.monotonic()-view['at']>600:raise ValueError('فهرست قدیمی شده؛ کنترل اعضا را دوباره باز کنید.')
            index=int(index)
            if not 0<=index<len(view['entries']):raise ValueError('عضو نامعتبر است.')
            await preview_remove(update,core,adv,uid,view['gid'],view['entries'][index])
        else:
            parts=data.split('_');kind,token,index=parts[1:4]
            view,bucket=view_bucket(adv,uid,token,index)
            gid=view['gid'];require(core,adv,uid,gid)
            if kind=='br':await records_menu(update,core,adv,uid,token,index,int(parts[4]))
            elif kind=='be':
                adv.FEATURE_INPUT[uid]={'mode':'group_ip','view':view,'bucket':bucket}
                await adv.show(update,'✏️ IP / مقدار جدید را بفرستید.\nاین تغییر فقط روی رکوردهای همین ردیف اعمال می‌شود؛ قبل از اجرا تأیید می‌گیرید.',[adv.back(f"dx_view_{gid}_{view['page']}")])
            elif kind=='bd':await preview(update,core,adv,uid,view,bucket,'delete')
            elif kind in ('bo','bone'):
                n=int(parts[4]);records=bucket[3]
                if not 0<=n<len(records):raise ValueError('رکورد نامعتبر است.')
                zid,r=records[n]
                if (zid,r['id']) not in selected_pairs(adv,uid,gid,bucket[0]):raise ValueError('عضویت رکورد تغییر کرده است.')
                if kind=='bone':await preview(update,core,adv,uid,view,bucket,'delete',records=[(zid,r)])
                else:await adv.group_clones.open_record(update,core,adv,uid,zid,r['id'],f'dx_view_{gid}_{view["page"]}')
    except Exception as exc:
        await adv.show(update,'❌ '+adv.txt(exc,300),[adv.back('dx_groups')])
    return True


MEMBER_VIEWS = {}


async def member_entries(core,adv,uid,gid):
    require(core,adv,uid,gid)
    permitted=await adv.zones_for(core,uid)
    pairs=selected_pairs(adv,uid,gid)
    by_zone={}
    sem=asyncio.Semaphore(4)
    async def fetch(zid):
        async with sem:
            rr=await asyncio.to_thread(cf.get_dns_records_fresh,zid)
            return zid,{r['id']:r for r in rr}
    zones=sorted({z for z,_ in pairs if z in permitted})
    for zid,records in await asyncio.gather(*(fetch(z) for z in zones)):
        by_zone[zid]=records
    grouped={}
    with adv.db() as c:
        tracked={(r['zone_id'],r['record_id']):dict(r) for r in c.execute('SELECT * FROM dns_group_clones WHERE owner=? AND group_id=?',(uid,gid))}
    for zid,rid in sorted(pairs):
        if zid not in permitted:continue
        r=by_zone.get(zid,{}).get(rid)
        if r:
            name,typ=r['name'],r['type']
        else:
            old=tracked.get((zid,rid),{})
            name,typ=old.get('name',rid),old.get('type','?')
        key=zid,name.casefold(),typ
        entry=grouped.setdefault(key,{'name':name,'type':typ,'pairs':set()})
        entry['pairs'].add((zid,rid))
    return sorted(grouped.values(),key=lambda r:(r['name'],r['type']))


async def members_menu(update,core,adv,uid,gid,page=0):
    entries=await member_entries(core,adv,uid,gid)
    part,page=adv.page_slice(list(enumerate(entries)),page)
    token=secrets.token_hex(6)
    MEMBER_VIEWS[uid]={'token':token,'gid':gid,'entries':entries,'at':time.monotonic()}
    buttons=[B('🗑 '+adv.txt(e['name'],36),callback_data=f'dx_mpick_{token}_{i}') for i,e in part]
    kb=adv.compact_rows(buttons)
    kb+=adv.pager(page,len(entries),f'dx_members_{gid}_')
    kb += [[B('➕ افزودن عضو',callback_data=f'dx_addz_{gid}_0')],adv.back(f'dx_g_{gid}')]
    await adv.show(update,f'👥 کنترل اعضا ({len(entries)})\nبرای برداشتن عضو از گروه، روی نام آن بزنید.',kb)


async def preview_remove(update,core,adv,uid,gid,entry):
    require(core,adv,uid,gid)
    all_pairs=selected_pairs(adv,uid,gid)
    permitted=await adv.zones_for(core,uid)
    if not entry['pairs'] or not entry['pairs']<=all_pairs:raise ValueError('عضویت تغییر کرده؛ فهرست را تازه کنید.')
    if any(z not in permitted for z,_ in entry['pairs']):raise ValueError('دسترسی عضو لغو شده است.')
    token=secrets.token_hex(8)
    adv.PENDING[uid]={'kind':'group_members','token':token,'at':time.monotonic(),'gid':gid,
                      'pairs':set(entry['pairs']),'group_pairs':all_pairs,'name':entry['name']}
    await adv.show(update,f"❓ {adv.txt(entry['name'],100)} از گروه برداشته شود؟\nارتباط این عضو با همهٔ IPهای همین گروه برداشته می‌شود؛ رکوردهای DNS حذف نمی‌شوند.",
                   [[B('✅ بله',callback_data=f'dx_apply_{token}')],adv.back(f'dx_members_{gid}_0')])


async def execute_remove(update,core,adv,uid,plan):
    gid=plan['gid'];require(core,adv,uid,gid)
    permitted=await adv.zones_for(core,uid)
    if any(z not in permitted for z,_ in plan['pairs']):raise ValueError('دسترسی عضو لغو شده است.')
    if selected_pairs(adv,uid,gid)!=plan['group_pairs']:raise ValueError('اعضای گروه تغییر کرده‌اند؛ دوباره تأیید کنید.')
    with adv.db() as c:
        for zid,rid in plan['pairs']:
            c.execute('DELETE FROM dns_group_members WHERE group_id=? AND zone_id=? AND record_id=?',(gid,zid,rid))
            c.execute('DELETE FROM dns_group_clones WHERE owner=? AND group_id=? AND zone_id=? AND record_id=?',(uid,gid,zid,rid))
    await members_menu(update,core,adv,uid,gid)
