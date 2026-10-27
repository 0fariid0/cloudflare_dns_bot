"""One group page for live original records and tracked clones, with scoped actions."""
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


def selected_pairs(adv, uid, gid, kind):
    if kind == 'original':
        return {(r['zone_id'], r['record_id']) for r in adv.members(gid)}
    return {(r['zone_id'], r['record_id']) for r in adv.group_clones.rows(adv, uid, gid)}


async def dashboard(update, core, adv, uid, gid, page=0):
    require(core, adv, uid, gid)
    permitted = await adv.zones_for(core, uid)
    originals = selected_pairs(adv, uid, gid, 'original')
    clone_pairs = selected_pairs(adv, uid, gid, 'clone') - originals
    pairs = originals | clone_pairs
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
    for kind, targets in (('original', originals), ('clone', clone_pairs)):
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
    items.sort(key=lambda x: (x[0] != 'original', x[1], x[2]))
    page = max(0, min(int(page), max(0, (len(items)-1)//PAGE_SIZE)))
    token = secrets.token_hex(6)
    view = {'token': token, 'gid': gid, 'at': time.monotonic(), 'buckets': items, 'page': page}
    VIEWS[uid] = view
    g = adv.group(uid, gid)
    lines = [f"📁 {g['name']}", f'⭐ {len(originals)} عضو اصلی • 🧬 {len(clone_pairs)} کلون',
             'IPهای اصلی و کلون‌ها؛ هر دکمه روی رکوردهای همان ردیف اعمال می‌شود.']
    kb = []
    for i in range(page*PAGE_SIZE, min(len(items), (page+1)*PAGE_SIZE)):
        kind, typ, content, records = items[i]
        label = '⭐ اصلی' if kind == 'original' else '🧬 کلون'
        lines += ['', f'{label} [{typ}] → {adv.txt(content,100)}',
                  f"{len(records)} رکورد: " + adv.txt('، '.join(r['name'] for _, r in records), 120)]
        kb.append([B(f'{label} · {adv.txt(content,40)} ({len(records)})', callback_data=f'dx_br_{token}_{i}_0')])
        kb.append([B('✏️ تغییر IP / مقدار', callback_data=f'dx_be_{token}_{i}'),
                   B('🗑 حذف رکوردها', callback_data=f'dx_bd_{token}_{i}')])
    if not pairs:
        lines.append('هنوز عضوی ندارد؛ «انتخاب اعضا» را بزنید.')
    if failed: lines.append(f'⚠️ دریافت {len(failed)} دامنه ناموفق بود؛ ردیف‌های آن نمایش داده نشدند.')
    if missing: lines.append(f'⚠️ {missing} رکورد دیگر در Cloudflare موجود نیست؛ فهرست اعضا یا مدیریت کلون‌ها را بررسی کنید.')
    if hidden: lines.append(f'🔒 {hidden} رکورد خارج از دسترسی فعلی نمایش داده نشد.')
    nav = []
    if page: nav.append(B('⬅️ قبلی', callback_data=f'dx_view_{gid}_{page-1}'))
    if (page+1)*PAGE_SIZE < len(items): nav.append(B('بعدی ➡️', callback_data=f'dx_view_{gid}_{page+1}'))
    if nav: kb.append(nav)
    kb += [[B('➕ کلون جدید', callback_data=f'dx_cnew_{gid}'), B('➕ انتخاب اعضا', callback_data=f'dx_addz_{gid}_0')],
           [B('🔄 تازه‌سازی', callback_data=f'dx_view_{gid}_{page}'), B('📋 اعضای اصلی', callback_data=f'dx_gp_{gid}_0')],
           [B('⚙️ تنظیمات گروه', callback_data=f'dx_options_{gid}'), B('🧬 مدیریت کلون‌ها', callback_data=f'dx_cm_{gid}')],
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
    label = 'اصلی' if kind == 'original' else 'کلون'
    await adv.show(update, f'{label} [{typ}] → {content}\nبرای تغییر جداگانه، رکورد را انتخاب کنید.', kb)


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
        if action == 'delete' and kind == 'clone' and adv.group_clones.is_source_member(adv,zid,r['id']):
            raise ValueError('این کلون عضو اصلی گروه دیگری است؛ ابتدا آن عضویت را بردارید.')
        checked.append((zid, r))
    if action == 'edit': value = adv.valid_value('content', value, [r for _, r in checked])
    token = secrets.token_hex(8)
    adv.PENDING[uid] = {'kind': 'group_records', 'token': token, 'at': time.monotonic(), 'gid': gid,
                        'record_kind': kind, 'items': checked, 'action': action, 'value': value, 'page': view['page']}
    label = 'اصلی' if kind == 'original' else 'کلون'
    lines = [f"⚠️ تأیید {'حذف' if action=='delete' else 'تغییر'} {len(checked)} رکورد {label}"]
    if action == 'delete':
        lines += ['این رکوردها از Cloudflare حذف می‌شوند؛ فقط برداشتن از گروه نیست.',
                  'سایر IPهای اصلی و کلون‌ها حذف نمی‌شوند.']
        if kind == 'original':lines.append('عضویت همین رکوردها در گروه‌های دیگر نیز برداشته می‌شود.')
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
        if plan['action']=='delete' and plan['record_kind']=='clone' and adv.group_clones.is_source_member(adv,zid,r['id']):
            raise ValueError('کلون اکنون عضو اصلی یک گروه است؛ حذف نشد.')
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
    await adv.show(update,'\n'.join(lines),[[B('🔙 IPهای اصلی و کلون‌ها',callback_data=back), B('🏠 خانه',callback_data='back_to_main')]])


async def callback(update, context, core, adv):
    data=update.callback_query.data
    if not data.startswith(('dx_view_','dx_options_','dx_br_','dx_be_','dx_bd_','dx_bo_','dx_bone_')):return False
    uid=update.effective_user.id
    adv.PENDING.pop(uid,None)
    adv.group_clones.PLANS.pop(uid,None)
    try:
        if data.startswith('dx_view_'):
            _,_,gid,page=data.split('_')
            await dashboard(update,core,adv,uid,gid,int(page))
        elif data.startswith('dx_options_'):
            gid=data[len('dx_options_'):];require(core,adv,uid,gid)
            await adv.show(update,'⚙️ تنظیمات گروه',[
                [B('✏️ ویرایش همه اعضای اصلی',callback_data=f'dx_ge_{gid}')],
                [B('🖊 تغییر نام',callback_data=f'dx_grename_{gid}'),B('🗑 حذف گروه',callback_data=f'dx_gdelq_{gid}')],
                adv.back(f'dx_g_{gid}')])
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
