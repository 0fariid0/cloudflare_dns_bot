"""Offline regression suite. No Telegram or Cloudflare credentials/network needed."""
import asyncio
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
if 'config' not in sys.modules:
    config=types.ModuleType('config')
    config.ADMIN_ID=1
    config.BOT_TOKEN='123456789:OFFLINE_TEST_TOKEN_ONLY'
    config.CLOUDFLARE_API_KEY='offline-token'
    config.CLOUDFLARE_EMAIL=''
    sys.modules['config']=config

import bot
import cloudflare_api as cf
import dns_advanced as adv
import dns_group_clones as clones
from bot_runtime import PerUserProcessor,api_error_scope
from telegram import InlineKeyboardButton
from telegram.error import BadRequest

Z='a'*32
Z2='b'*32
R='c'*32
G='0123456789ab'


class Message:
    def __init__(self,text=''):
        self.text=text
        self.frames=[]

    async def edit_text(self,text,reply_markup=None,**kw):
        self.text=text
        buttons=reply_markup.inline_keyboard if reply_markup else ()
        if len(text)>4096:raise AssertionError('Message exceeds Telegram limit')
        for row in buttons:
            for button in row:
                if button.callback_data and len(button.callback_data.encode())>64:
                    raise AssertionError('Oversized callback: '+button.callback_data)
        self.frames.append((text,buttons))
        return self

    async def reply_text(self,text,reply_markup=None,**kw):
        return await self.edit_text(text,reply_markup,**kw)


class Update:
    def __init__(self,data='',uid=1,text=''):
        self.effective_user=types.SimpleNamespace(id=uid,first_name='Test',last_name='',username='test')
        self.effective_message=Message(text)
        self.message=self.effective_message
        self.update_id=id(self)
        async def answer(*args,**kwargs):pass
        self.callback_query=types.SimpleNamespace(data=data,from_user=self.effective_user,message=self.message,
            answer=answer,edit_message_text=self.message.edit_text) if data else None


class Workflows(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.scope=api_error_scope.set({'error':None})
        self.old_db=adv.DB_PATH
        adv.DB_PATH=Path(self.temp.name)/'test.sqlite3'
        adv.initialize()
        self.file_patchers=[]
        for name in ('USER_FILE','BLOCKED_USER_FILE','REQUEST_FILE','IP_LIST_FILE','SMART_SETTINGS_FILE','LOG_FILE'):
            pp=patch.object(bot,name,str(Path(self.temp.name)/name))
            pp.start();self.file_patchers.append(pp)
        bot._DATA_CACHE.clear();bot.user_state.clear()
        adv.FEATURE_INPUT.clear();adv.PENDING.clear();adv.SEARCH_CACHE.clear();adv.BULK_SELECTION.clear()
        clones.PLANS.clear();clones.BUSY.clear()
        adv.group_records.MEMBER_VIEWS.clear()
        adv.group_records.VIEWS.clear()
        cf._invalidate_zones_cache();cf._invalidate_records_cache();cf._set_last_error(None)
        bot.load_users()
        bot.add_user(2);bot.set_user_access(2,[Z])
        bot.add_user(3);bot.set_user_access(3,[])
        self.records={(Z,R):dict(id=R,name='node.example.com',type='A',content='1.1.1.1',ttl=120,proxied=False,
                                    comment='keep me',tags=['owner:test'],settings={},modified_on='original')}
        self.zones=[dict(id=Z,name='example.com',status='active'),dict(id=Z2,name='second.test',status='active')]
        self.writes=[];self.serial=0;self.request_lock=threading.RLock()
        self.request_patch=patch.object(cf,'_request',self.request);self.request_patch.start()
        self.old_observer=cf.WRITE_OBSERVER;cf.WRITE_OBSERVER=adv.sync_tracked_record
        self.context=types.SimpleNamespace(user_data={},bot=types.SimpleNamespace(send_message=self.send_message),job_queue=None)
        with adv.db() as c:
            c.execute('INSERT INTO dns_groups VALUES(?,?,?,?)',(G,1,'گروه آزمایشی',adv.stamp()))
            c.execute('INSERT INTO dns_group_members VALUES(?,?,?)',(G,Z,R))

    async def asyncTearDown(self):
        self.request_patch.stop()
        for p in self.file_patchers:p.stop()
        cf.WRITE_OBSERVER=self.old_observer
        adv.DB_PATH=self.old_db
        api_error_scope.reset(self.scope)
        self.temp.cleanup()

    async def send_message(self,*args,**kwargs):pass

    def request(self,method,path,params=None,json=None):
        with self.request_lock:
            cf._set_last_error(None)
            if method=='GET' and path=='/zones':return {'success':True,'result':copy.deepcopy(self.zones)}
            parts=path.strip('/').split('/')
            zid=parts[1]
            if len(parts)==2:return {'success':True,'result':next(z for z in self.zones if z['id']==zid)}
            if method=='GET' and len(parts)==3:
                return {'success':True,'result':[copy.deepcopy(r) for (z,_),r in self.records.items() if z==zid]}
            if method=='POST':
                self.serial+=1;rid=f'{self.serial:032x}'
                record=copy.deepcopy(json);record.update(id=rid,modified_on=f'new-{self.serial}')
                self.records[(zid,rid)]=record
            else:
                rid=parts[3]
                if (zid,rid) not in self.records:raise cf.CloudflareAPIError('not found',status_code=404)
                record=copy.deepcopy(self.records[(zid,rid)])
                if method=='GET':return {'success':True,'result':record}
                if method=='DELETE':del self.records[(zid,rid)]
                else:
                    record.update(json);record['modified_on']='updated'
                    self.records[(zid,rid)]=record
                if cf.WRITE_OBSERVER:cf.WRITE_OBSERVER(zid,rid,None if method=='DELETE' else record)
            self.writes.append((method,path,copy.deepcopy(json)))
            return {'success':True,'result':record}

    async def click(self,data,uid=1):
        update=Update(data,uid)
        await bot.handle_callback(update,self.context)
        return update

    async def create(self,ips='2.2.2.2 3.3.3.3',gid=G):
        await clones.preview_create(Update(),bot,adv,1,gid,ips)
        token=clones.PLANS[1][0]
        await clones.apply(Update('dx_capply_'+token),bot,adv,1,token)
        return clones.rows(adv,1,gid)

    async def test_confirmed_clone_preserves_original_metadata_and_deduplicates(self):
        rr=await self.create('1.1.1.1 2.2.2.2 2.2.2.2')
        self.assertEqual(len(rr),1)
        self.assertEqual(self.records[(Z,R)]['content'],'1.1.1.1')
        clone=self.records[(Z,rr[0]['record_id'])]
        self.assertEqual(clone['comment'],'keep me')
        self.assertEqual(clone['tags'],['owner:test'])
        await clones.preview_create(Update(),bot,adv,1,G,'2.2.2.2')
        self.assertNotIn(1,clones.PLANS)
        self.assertEqual(len(self.writes),1)

    async def test_clone_delete_ip_only_targets_tracked_records(self):
        rr=await self.create()
        self.records[(Z,'d'*32)]=dict(self.records[(Z,R)],id='d'*32,content='2.2.2.2',name='untracked.example.com')
        row=next(r for r in rr if r['ip']=='2.2.2.2')
        await clones.preview_delete(Update(),bot,adv,1,G,row['id'],True)
        token=clones.PLANS[1][0]
        await clones.apply(Update(),bot,adv,1,token)
        self.assertIn((Z,R),self.records)
        self.assertIn((Z,'d'*32),self.records)
        self.assertEqual(len(clones.rows(adv,1,G)),1)

    async def test_external_change_blocks_delete(self):
        row=(await self.create('2.2.2.2'))[0]
        self.records[(Z,row['record_id'])]['content']='9.9.9.9'
        with self.assertRaises(ValueError):await clones.preview_delete(Update(),bot,adv,1,G,row['id'])
        self.assertEqual(len(self.writes),1)

    async def test_internal_edit_syncs_tracking_and_can_be_deleted(self):
        row=(await self.create('2.2.2.2'))[0]
        cf.patch_dns_fields(Z,row['record_id'],{'content':'8.8.8.8'})
        refreshed=clones.rows(adv,1,G)[0]
        self.assertEqual(refreshed['ip'],'8.8.8.8')
        await clones.preview_delete(Update(),bot,adv,1,G,row['id'])
        await clones.apply(Update(),bot,adv,1,clones.PLANS[1][0])
        self.assertEqual(len(clones.rows(adv,1,G)),0)

    async def test_clone_used_as_source_in_another_group_is_protected(self):
        row=(await self.create('2.2.2.2'))[0]
        with adv.db() as c:
            c.execute('INSERT INTO dns_groups VALUES(?,?,?,?)',('other-group',1,'گروه دوم',adv.stamp()))
            c.execute('INSERT INTO dns_group_members VALUES(?,?,?)',('other-group',Z,row['record_id']))
        with self.assertRaises(ValueError):await clones.preview_delete(Update(),bot,adv,1,G,row['id'])
        self.assertEqual(len(self.writes),1)

    async def test_application_builder_accepts_processor_and_registers_handlers(self):
        clean_env={k:v for k,v in os.environ.items() if not k.lower().endswith('_proxy')}
        with patch.dict(os.environ,clean_env,clear=True), patch('telegram.ext.Application.run_polling') as polling:
            bot.main()
            polling.assert_called_once()

    async def test_source_changed_after_preview_blocks_all_writes(self):
        await clones.preview_create(Update(),bot,adv,1,G,'2.2.2.2')
        token=clones.PLANS[1][0]
        self.records[(Z,R)]['ttl']=300
        with self.assertRaises(ValueError):await clones.apply(Update(),bot,adv,1,token)
        self.assertEqual(self.writes,[])
        self.assertFalse(clones.BUSY)

    async def test_clone_plan_single_use(self):
        await clones.preview_create(Update(),bot,adv,1,G,'2.2.2.2')
        token=clones.PLANS[1][0]
        await clones.apply(Update(),bot,adv,1,token)
        with self.assertRaises(ValueError):await clones.apply(Update(),bot,adv,1,token)
        self.assertEqual(len(self.writes),1)

    async def test_clone_plan_expiry_and_permission_recheck(self):
        await clones.preview_create(Update(),bot,adv,1,G,'2.2.2.2')
        token,plan=clones.PLANS[1];plan['at']-=601
        with self.assertRaises(ValueError):await clones.apply(Update(),bot,adv,1,token)
        plan['at']+=601
        with patch.object(bot,'get_user_accessible_zones',return_value=[]):
            with self.assertRaises(ValueError):await clones.apply(Update(),bot,adv,1,token)
        self.assertEqual(self.writes,[])

    async def test_cancel_navigation_invalidates_clone_and_bulk_plans(self):
        await clones.preview_create(Update(),bot,adv,1,G,'2.2.2.2')
        adv.PENDING[1]={'token':'old'}
        await self.click('back_to_main')
        self.assertNotIn(1,clones.PLANS)
        self.assertNotIn(1,adv.PENDING)

    async def test_other_users_cannot_see_or_mutate_clones(self):
        rr=await self.create()
        with self.assertRaises(ValueError):await clones.preview_delete(Update(),bot,adv,2,G,rr[0]['id'])
        with self.assertRaises(ValueError):await clones.preview_create(Update(),bot,adv,2,G,'4.4.4.4')
        update=await self.click('dx_clones',2)
        self.assertNotIn('node.example.com',update.message.text)
        self.assertNotIn('مرکز کلون',update.message.text)

    async def test_all_clone_menu_routes_and_callback_limits(self):
        rr=await self.create()
        rid=rr[0]['id']
        for route in ('back_to_main','ui_domains','dx_groups',f'dx_g_{G}',f'dx_gp_{G}_0',
                      'dx_clones','dx_chip_0','dx_chg_0',f'dx_cm_{G}',f'dx_cp_{G}_0',
                      f'dx_cv_{G}_{rid}_0',f'dx_caip_{rid}_0',f'dx_ccard_{G}_{rid}',
                      f'dx_cips_{G}_0',f'dx_copen_{G}_{rid}','dx_legacy','dx_lp_0','dx_lopen_0'):
            with self.subTest(route=route):
                update=await self.click(route)
                self.assertTrue(update.message.frames)
        self.assertNotIn('مرکز کلون',update.message.text)

    async def test_membership_removal_requires_confirmation_and_can_be_readded(self):
        await self.click(f'dx_addr_{G}_{Z}_0')
        await self.click(f'dx_join_{G}_{R}')
        self.assertEqual(len(adv.members(G)),1)
        self.assertIn(1,adv.PENDING)
        await self.click('dx_apply_'+adv.PENDING[1]['token'])
        self.assertEqual(len(adv.members(G)),0)
        self.assertIn((Z,R),self.records)
        await self.click(f'dx_addr_{G}_{Z}_0')
        await self.click(f'dx_join_{G}_{R}')
        self.assertEqual(len(adv.members(G)),1)

    async def test_single_clone_uses_managed_confirmed_flow(self):
        await self.click('zone_'+Z)
        await self.click('clone_record_'+R)
        state=adv.FEATURE_INPUT[1]
        self.assertEqual(state['mode'],'clone_ips')
        self.assertEqual(self.writes,[])
        update=Update(text='4.4.4.4')
        await bot.handle_message(update,self.context)
        self.assertIn(1,clones.PLANS)
        await self.click('dx_capply_'+clones.PLANS[1][0])
        self.assertEqual(len(clones.rows(adv,1,state['gid'])),1)
        group=await self.click('dx_g_'+state['gid'])
        labels=[b.text for row in group.message.frames[-1][1] for b in row]
        self.assertIn('4.4.4.4',labels)
        self.assertNotIn('مرکز کلون',group.message.text)

    async def test_delete_group_preserves_all_dns_records(self):
        await self.create()
        await self.click('dx_gdel_'+G)
        self.assertIsNone(adv.group(1,G))
        self.assertEqual(len(self.records),3)
        self.assertEqual(len(clones.rows(adv,1,G)),0)

    async def test_removed_clone_center_routes_redirect_without_writing(self):
        self.records[(Z,'e'*32)]=dict(self.records[(Z,R)],id='e'*32,content='2.2.2.2')
        for route in ('dx_clones','dx_legacy','dx_chip_0','dx_chg_0'):
            update=await self.click(route)
            self.assertNotIn('مرکز کلون',update.message.text)
            self.assertNotIn('همه کلون',update.message.text)
        self.assertEqual(self.writes,[])

    async def test_ipv6_clone_and_bad_inputs(self):
        self.records[(Z,R)].update(type='AAAA',content='2001:db8::1')
        rr=await self.create('2001:db8::2 2.2.2.2')
        self.assertEqual(len(rr),1)
        self.assertEqual(rr[0]['type'],'AAAA')
        with self.assertRaises(ValueError):clones.parse_ips('1.2.3.999')
        with self.assertRaises(ValueError):clones.parse_ips('')
        self.assertEqual(clones.parse_ips('2.2.2.2، 2.2.2.2'),['2.2.2.2'])

    async def test_bulk_confirmed_change_and_stale_snapshot_rejection(self):
        await adv.begin_preview(Update(),bot,1,[(Z,R)],'content','5.5.5.5',gid=G,source='group')
        self.records[(Z,R)]['comment']='external edit'
        await adv.execute(Update(),bot,1,adv.PENDING[1]['token'])
        self.assertEqual(self.writes,[])
        await adv.begin_preview(Update(),bot,1,[(Z,R)],'content','5.5.5.5',gid=G,source='group')
        await adv.execute(Update(),bot,1,adv.PENDING[1]['token'])
        self.assertEqual(self.records[(Z,R)]['content'],'5.5.5.5')
        self.assertEqual(self.records[(Z,R)]['comment'],'external edit')

    async def test_reserve_pool_merge_retains_concurrent_addition(self):
        before={'reserve':['2.2.2.2'],'deprecated':[]}
        bot.save_ip_lists({'reserve':['2.2.2.2','3.3.3.3'],'deprecated':[]})
        bot.merge_ip_pool_changes(before,{'reserve':[],'deprecated':['2.2.2.2']})
        self.assertEqual(bot.load_ip_lists(),{'reserve':['3.3.3.3'],'deprecated':['2.2.2.2']})

    async def test_migration_preserves_existing_groups_and_clone_rows(self):
        await self.create()
        adv.initialize()
        self.assertEqual(len(adv.members(G)),1)
        self.assertEqual(len(clones.rows(adv,1,G)),2)

    async def test_large_lists_paginate_and_empty_summary_needs_no_record_fetch(self):
        for n in range(25):
            with adv.db() as c:c.execute('INSERT INTO dns_groups VALUES(?,?,?,?)',(f'{n:012x}',1,f'گروه {n:02}',adv.stamp()))
        update=await self.click('dx_groups_1')
        self.assertLessEqual(len(update.message.frames[-1][1]),13)
        with patch.object(cf,'get_record_details_strict',side_effect=AssertionError('Summary should not fetch records')):
            await self.click('dx_g_'+G)

    async def test_group_page_shows_live_original_and_clone_ips_with_actions(self):
        await self.create('2.2.2.2')
        update=await self.click('dx_g_'+G)
        labels=[b.text for row in update.message.frames[-1][1] for b in row]
        self.assertIn('1.1.1.1',labels)
        self.assertIn('2.2.2.2',labels)
        buttons=[b for row in update.message.frames[-1][1] for b in row]
        self.assertTrue(any(b.callback_data=='dx_cnew_'+G for b in buttons))
        self.assertEqual(sum(b.callback_data.startswith('dx_be_') for b in buttons),2)
        self.assertEqual(sum(b.callback_data.startswith('dx_bd_') for b in buttons),2)
        self.records[(Z,R)]['content']='9.9.9.9'
        update=await self.click('dx_g_'+G)
        labels=[b.text for row in update.message.frames[-1][1] for b in row]
        self.assertIn('9.9.9.9',labels)
        self.assertNotIn('1.1.1.1',labels)

    async def test_group_page_edits_only_selected_original_ip_bucket(self):
        await self.create('2.2.2.2')
        second='f'*32
        self.records[(Z,second)]=dict(self.records[(Z,R)],id=second,name='another.example.com')
        with adv.db() as c:c.execute('INSERT INTO dns_group_members VALUES(?,?,?)',(G,Z,second))
        await self.click('dx_g_'+G)
        view=adv.group_records.VIEWS[1]
        index=next(i for i,b in enumerate(view['buckets']) if b[2]=='1.1.1.1')
        await self.click(f'dx_be_{view["token"]}_{index}')
        await bot.handle_message(Update(text='8.8.8.8'),self.context)
        self.assertEqual(self.records[(Z,R)]['content'],'1.1.1.1')
        await self.click('dx_apply_'+adv.PENDING[1]['token'])
        self.assertEqual(self.records[(Z,R)]['content'],'8.8.8.8')
        self.assertEqual(self.records[(Z,second)]['content'],'8.8.8.8')
        row=clones.rows(adv,1,G)[0]
        self.assertEqual(self.records[(Z,row['record_id'])]['content'],'2.2.2.2')

    async def test_group_page_edits_clone_and_updates_its_tracking(self):
        rr=await self.create('2.2.2.2')
        await self.click('dx_g_'+G)
        view=adv.group_records.VIEWS[1]
        index=next(i for i,b in enumerate(view['buckets']) if b[2]=='2.2.2.2')
        await self.click(f'dx_be_{view["token"]}_{index}')
        await bot.handle_message(Update(text='7.7.7.7'),self.context)
        await self.click('dx_apply_'+adv.PENDING[1]['token'])
        self.assertEqual(self.records[(Z,R)]['content'],'1.1.1.1')
        self.assertEqual(clones.rows(adv,1,G)[0]['ip'],'7.7.7.7')
        update=await self.click('dx_g_'+G)
        labels=[b.text for row in update.message.frames[-1][1] for b in row]
        self.assertIn('7.7.7.7',labels)
        self.assertNotIn('2.2.2.2',labels)

    async def test_group_page_deletes_original_but_preserves_clones(self):
        await self.create('2.2.2.2')
        await self.click('dx_g_'+G)
        view=adv.group_records.VIEWS[1]
        index=next(i for i,b in enumerate(view['buckets']) if b[2]=='1.1.1.1')
        update=await self.click(f'dx_bd_{view["token"]}_{index}')
        self.assertIn('از Cloudflare حذف',update.message.text)
        self.assertIn((Z,R),self.records)
        token=adv.PENDING[1]['token']
        await self.click('dx_apply_'+token)
        self.assertNotIn((Z,R),self.records)
        self.assertEqual(len(clones.rows(adv,1,G)),1)
        self.assertEqual(len(adv.members(G)),0)
        self.assertEqual(len(self.records),1)
        writes=len(self.writes)
        await self.click('dx_apply_'+token)
        self.assertEqual(len(self.writes),writes)

    async def test_group_page_can_delete_a_single_record_from_shared_ip(self):
        second='f'*32
        self.records[(Z,second)]=dict(self.records[(Z,R)],id=second,name='another.example.com')
        with adv.db() as c:c.execute('INSERT INTO dns_group_members VALUES(?,?,?)',(G,Z,second))
        await self.click('dx_g_'+G)
        view=adv.group_records.VIEWS[1]
        await self.click(f'dx_br_{view["token"]}_0_0')
        await self.click(f'dx_bone_{view["token"]}_0_0')
        target=adv.PENDING[1]['items'][0][1]['id']
        await self.click('dx_apply_'+adv.PENDING[1]['token'])
        self.assertNotIn((Z,target),self.records)
        self.assertEqual(len(self.records),1)
        self.assertEqual(len(adv.members(G)),1)

    async def test_group_page_delete_stops_if_record_changed_after_confirmation(self):
        await self.create('2.2.2.2')
        await self.click('dx_g_'+G)
        view=adv.group_records.VIEWS[1]
        index=next(i for i,b in enumerate(view['buckets']) if b[2]=='2.2.2.2')
        await self.click(f'dx_bd_{view["token"]}_{index}')
        token=adv.PENDING[1]['token']
        row=clones.rows(adv,1,G)[0]
        self.records[(Z,row['record_id'])]['content']='9.9.9.9'
        update=await self.click('dx_apply_'+token)
        self.assertIn('هیچ تغییری اعمال نشد',update.message.text)
        self.assertEqual(len(self.writes),1)

    async def test_group_page_tokens_cannot_cross_users_or_outlive_refresh(self):
        await self.click('dx_g_'+G)
        token=adv.group_records.VIEWS[1]['token']
        await self.click(f'dx_bd_{token}_0',2)
        self.assertNotIn(2,adv.PENDING)
        await self.click('dx_g_'+G)
        await self.click(f'dx_bd_{token}_0')
        self.assertNotIn(1,adv.PENDING)
        self.assertEqual(self.writes,[])

    async def test_group_page_invalid_ip_and_cancellation_do_not_mutate_dns(self):
        await self.click('dx_g_'+G)
        view=adv.group_records.VIEWS[1]
        await self.click(f'dx_be_{view["token"]}_0')
        await bot.handle_message(Update(text='999.1.1.1'),self.context)
        self.assertNotIn(1,adv.PENDING)
        self.assertEqual(adv.FEATURE_INPUT[1]['mode'],'group_ip')
        await bot.handle_message(Update(text='8.8.8.8'),self.context)
        self.assertIn(1,adv.PENDING)
        await self.click(f'dx_view_{G}_0')
        self.assertNotIn(1,adv.PENDING)
        self.assertEqual(self.writes,[])

    async def test_group_page_clone_delete_preserves_original_and_other_clones(self):
        await self.create()
        await self.click('dx_g_'+G)
        view=adv.group_records.VIEWS[1]
        index=next(i for i,b in enumerate(view['buckets']) if b[2]=='2.2.2.2')
        await self.click(f'dx_bd_{view["token"]}_{index}')
        await self.click('dx_apply_'+adv.PENDING[1]['token'])
        self.assertIn((Z,R),self.records)
        self.assertEqual(len(self.records),2)
        self.assertEqual(clones.rows(adv,1,G)[0]['ip'],'3.3.3.3')

    async def test_compact_ip_rows_have_three_buttons_and_only_required_controls(self):
        await self.create('2.2.2.2')
        update=await self.click('dx_g_'+G)
        rows=update.message.frames[-1][1]
        ip_rows=[row for row in rows if any(b.callback_data.startswith('dx_be_') for b in row)]
        self.assertEqual(len(ip_rows),2)
        for row in ip_rows:
            self.assertEqual(len(row),3)
            self.assertEqual([b.text for b in row[1:]],['✏️','🗑'])
        labels=[b.text for row in rows for b in row]
        for removed in ('اصلی','مدیریت کلون','تنظیمات گروه','اعضای اصلی','انتخاب اعضا'):
            self.assertFalse(any(removed in label for label in labels))
        self.assertTrue(any('کنترل اعضا' in label for label in labels))
        for route in ('back_to_main','dx_groups'):
            update=await self.click(route)
            self.assertFalse(any('کلون' in b.text for row in update.message.frames[-1][1] for b in row))

    async def test_same_ip_is_one_row_even_if_one_record_was_copied(self):
        row=(await self.create('2.2.2.2'))[0]
        cf.patch_dns_fields(Z,row['record_id'],{'content':'1.1.1.1'})
        await self.click('dx_g_'+G)
        buckets=adv.group_records.VIEWS[1]['buckets']
        self.assertEqual(len(buckets),1)
        self.assertEqual(len(buckets[0][3]),2)
        view=adv.group_records.VIEWS[1]
        await self.click(f'dx_be_{view["token"]}_0')
        await bot.handle_message(Update(text='8.8.8.8'),self.context)
        await self.click('dx_apply_'+adv.PENDING[1]['token'])
        self.assertEqual({r['content'] for r in self.records.values()},{'8.8.8.8'})

    async def test_members_control_groups_name_across_ips_and_confirms_removal(self):
        await self.create('2.2.2.2')
        update=await self.click(f'dx_members_{G}_0')
        view=adv.group_records.MEMBER_VIEWS[1]
        self.assertEqual(len(view['entries']),1)
        self.assertEqual(len(view['entries'][0]['pairs']),2)
        await self.click(f'dx_mpick_{view["token"]}_0')
        self.assertEqual(len(adv.group_records.selected_pairs(adv,1,G)),2)
        await self.click('dx_apply_'+adv.PENDING[1]['token'])
        self.assertEqual(adv.group_records.selected_pairs(adv,1,G),set())
        self.assertEqual(len(self.records),2)
        self.assertEqual(len(self.writes),1)

    async def test_member_removal_cancel_and_changed_membership_are_safe(self):
        await self.click(f'dx_members_{G}_0')
        view=adv.group_records.MEMBER_VIEWS[1]
        await self.click(f'dx_mpick_{view["token"]}_0')
        await self.click(f'dx_members_{G}_0')
        self.assertNotIn(1,adv.PENDING)
        self.assertEqual(len(adv.members(G)),1)
        view=adv.group_records.MEMBER_VIEWS[1]
        await self.click(f'dx_mpick_{view["token"]}_0')
        token=adv.PENDING[1]['token']
        with adv.db() as c:c.execute('INSERT INTO dns_group_members VALUES(?,?,?)',(G,Z,'f'*32))
        with self.assertRaises(ValueError):await adv.execute(Update(),bot,1,token)
        self.assertEqual(len(adv.members(G)),2)
        self.assertEqual(self.writes,[])

    async def test_member_control_rejects_another_users_keyboard(self):
        await self.click(f'dx_members_{G}_0')
        view=adv.group_records.MEMBER_VIEWS[1]
        await self.click(f'dx_mpick_{view["token"]}_0',2)
        self.assertNotIn(2,adv.PENDING)
        self.assertEqual(len(adv.members(G)),1)

    async def test_can_copy_again_after_the_first_ip_was_deleted(self):
        await self.create('2.2.2.2')
        cf.delete_dns_record(Z,R)
        self.assertEqual(len(adv.members(G)),0)
        await self.click(f'dx_cnew_{G}')
        await bot.handle_message(Update(text='3.3.3.3'),self.context)
        self.assertIn(1,clones.PLANS)
        await self.click('dx_capply_'+clones.PLANS[1][0])
        self.assertEqual({r['content'] for r in self.records.values()},{'2.2.2.2','3.3.3.3'})

    async def test_four_members_on_two_ips_render_as_exactly_two_three_button_rows(self):
        for n in range(3):
            rid=f'{n+100:032x}'
            self.records[(Z,rid)]=dict(self.records[(Z,R)],id=rid,name=f'node{n}.example.com')
            with adv.db() as c:c.execute('INSERT INTO dns_group_members VALUES(?,?,?)',(G,Z,rid))
        await self.create('2.2.2.2')
        update=await self.click('dx_g_'+G)
        ip_rows=[row for row in update.message.frames[-1][1] if len(row)==3 and row[1].text=='✏️' and row[2].text=='🗑']
        self.assertEqual(len(ip_rows),2)
        self.assertEqual({row[0].text for row in ip_rows},{'1.1.1.1','2.2.2.2'})
        self.assertEqual(len(self.records),8)

    async def test_proxy_enabled_always_uses_auto_ttl(self):
        self.assertTrue(cf.update_dns_record(Z,R,'node.example.com','A','1.1.1.1',300,True))
        self.assertEqual(self.records[(Z,R)]['ttl'],1)
        self.assertTrue(self.records[(Z,R)]['proxied'])


class Runtime(unittest.IsolatedAsyncioTestCase):
    async def test_same_user_serialized_other_user_can_progress(self):
        processor=PerUserProcessor(8)
        started=asyncio.Event();release=asyncio.Event();events=[]
        async def first():
            events.append('first-start');started.set();await release.wait();events.append('first-end')
        async def second():events.append('second')
        async def other():events.append('other');release.set()
        a=asyncio.create_task(processor.process_update(Update(uid=1),first()))
        await started.wait()
        b=asyncio.create_task(processor.process_update(Update(uid=1),second()))
        c=asyncio.create_task(processor.process_update(Update(uid=2),other()))
        await asyncio.gather(a,b,c)
        self.assertLess(events.index('other'),events.index('first-end'))
        self.assertLess(events.index('first-end'),events.index('second'))
        self.assertEqual(processor._locks,{})

    async def test_worker_errors_isolated_per_update(self):
        processor=PerUserProcessor(8)
        errors={}
        async def task(uid):
            await asyncio.to_thread(cf._set_last_error,f'error-{uid}')
            await asyncio.sleep(0)
            errors[uid]=cf.get_last_error()
        await asyncio.gather(*(processor.process_update(Update(uid=i),task(i)) for i in range(4)))
        self.assertEqual(errors,{i:f'error-{i}' for i in range(4)})

    async def test_success_without_snapshot_cannot_remove_tracking(self):
        response=types.SimpleNamespace(status_code=200,json=lambda:{'success':True})
        session=types.SimpleNamespace(request=lambda *a,**k:response)
        observer=unittest.mock.Mock()
        with patch.object(cf,'_session',return_value=session),patch.object(cf,'WRITE_OBSERVER',observer):
            cf._request('PATCH',f'/zones/{Z}/dns_records/{R}',json={'content':'2.2.2.2'})
            observer.assert_not_called()
            cf._request('DELETE',f'/zones/{Z}/dns_records/{R}')
            observer.assert_called_once_with(Z,R,None)

    async def test_bad_response_and_bounded_retry(self):
        response=types.SimpleNamespace(status_code=200,json=lambda:[])
        session=types.SimpleNamespace(request=lambda *a,**k:response)
        with patch.object(cf,'_session',return_value=session):
            with self.assertRaises(cf.CloudflareAPIError):cf._request('GET','/zones')
        retry=cf.BoundedRetry(total=1)
        self.assertEqual(retry.get_retry_after(types.SimpleNamespace(headers={'Retry-After':'10000'})),5)
        self.assertNotIn('POST',cf._session().get_adapter('https://').max_retries.allowed_methods)
        self.assertNotIn('PATCH',cf._session().get_adapter('https://').max_retries.allowed_methods)


if __name__=='__main__':unittest.main()
