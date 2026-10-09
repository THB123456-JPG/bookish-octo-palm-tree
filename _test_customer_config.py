"""Customer APIs and real ledger dispatch, with isolated SQLite and mocked sends."""
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import core
import customer_config as CC
import miniapp
from _test_miniapp import setup_fixture
from runners.ledger import LedgerRunner
from runners.ledger import commands as C
from runners.ledger.customer_runtime import CustomerRuntime, TZ


class CustomerTests(unittest.TestCase):
    def setUp(self):
        self.base = core.BASE_DIR
        self.tmp = tempfile.TemporaryDirectory()
        self.mgr = setup_fixture(self.tmp.name)
        self.bot = self.mgr.find('ledger1')
        self.r = LedgerRunner(self.mgr, self.bot)
        self.calls = []
        self.r.api = SimpleNamespace(call=self.call)
        self.mid = 100

    def call(self, method, **kw):
        self.calls.append((method, kw))
        return {'message_id': len(self.calls), 'status': 'administrator'}

    def tearDown(self):
        self.r.close_db()
        self.tmp.cleanup()
        core.set_base_dir(self.base)

    def save(self, section, value, uid=111, revision=None):
        return miniapp.action(self.mgr, self.bot, uid, 'customer',
            dict(section=section, value=value, revision=CC.settings(self.bot)['revision'] if revision is None else revision))

    def features(self, **patches):
        cfg = CC.settings(self.bot)['features']
        cfg.update(patches)
        self.save('features', cfg)

    def grant(self, uid, **kw):
        p = dict(manage=False, global_ops=False, broadcast=False, groups=[])
        p.update(kw)
        miniapp.action(self.mgr, self.bot, 111, 'staff', dict(user_id=uid,add=True,name='测试',grants=p))

    def test_private_help_dispatch_uses_nine_category_menu(self):
        for uid in (111, 999):
            for text in ('帮助', '/help', '使用说明', '菜单'):
                with self.subTest(uid=uid, text=text):
                    self.calls.clear()
                    self.r.handle({'message': {'message_id': 1,
                        'chat': {'id': uid, 'type': 'private'},
                        'from': {'id': uid, 'first_name': '测试'}, 'text': text}})
                    messages = [kw for method, kw in self.calls if method == 'sendMessage']
                    self.assertEqual(len(messages), 1)
                    self.assertLess(len(messages[0]['text']), 200)
                    rows = messages[0]['reply_markup']['inline_keyboard']
                    self.assertEqual([len(row) for row in rows], [3, 3, 3, 1])
                    self.assertNotIn('【记账】', messages[0]['text'])

    def send(self, text, uid=111, cid=-10011, reply=None):
        self.mid += 1
        msg = dict(message_id=self.mid, chat=dict(id=cid,type='supergroup',title='测试'),
                   text=text, **{'from':dict(id=uid,first_name='测试',username='tester')})
        if reply:
            msg['reply_to_message'] = dict(message_id=reply, **{'from':dict(id=uid,first_name='测试')})
        self.r._dispatch(cid,msg['chat'],msg,msg['from'],text)
        return self.mid

    def test_roles_are_combined_and_scoped_on_api_and_dispatch(self):
        self.grant(555, groups=[-10011])
        self.grant(556, global_ops=True)
        self.grant(557, broadcast=True)
        self.grant(558, manage=True)
        self.assertEqual([g['chat_id'] for g in miniapp.overview(self.mgr,self.bot,555)['groups']], [-10011])
        self.assertEqual(len(miniapp.overview(self.mgr,self.bot,556)['groups']), 2)
        self.assertEqual(miniapp.overview(self.mgr,self.bot,557)['groups'], [])
        self.assertNotIn(557, self.r.admins())
        self.assertFalse(self.r.can_broadcast(556))
        self.assertTrue(self.r.can_broadcast(557))
        before = len(self.r.store.entries(-10022))
        self.send('+20',555,-10022)
        self.assertEqual(len(self.r.store.entries(-10022)),before)
        self.send('+20',555)
        self.assertEqual(self.r.store.entries(-10011)[-1].operator_id,555)
        self.send('+20',556,-10022)
        self.assertEqual(len(self.r.store.entries(-10022)),before+1)
        self.send('+20',558,-10022)
        self.assertEqual(len(self.r.store.entries(-10022)),before+1)
        with self.assertRaises(PermissionError):
            self.save('features', CC.settings(self.bot)['features'], uid=556)
        with self.assertRaises(PermissionError):
            miniapp.action(self.mgr,self.bot,558,'staff',dict(user_id=999,add=True))
        with self.assertRaises(ValueError):
            miniapp.action(self.mgr,self.bot,111,'staff',dict(user_id=111,add=False))
        count = miniapp.overview(self.mgr,self.bot,111)['operator_count']
        self.assertEqual(count,2)  # original group operator plus explicit single-group grant
        self.assertTrue({555,556,557,558}.issubset({u['user_id'] for u in miniapp.overview(self.mgr,self.bot,111)['permission_users']}))

    def test_defaults_only_affect_new_groups_and_welcome_toggles(self):
        self.r.store.set_rate(-10011,'10')
        old_hour = self.r.store.get_ledger_reset_hour(-10011)
        before = [tuple(row) for row in self.r.store.conn.execute('SELECT * FROM entries')]
        b = CC.settings(self.bot)['basics']
        b.update(cutoff_hour=5,view_mode='compact',currency='U',welcome_enabled=True,
                 welcome_mention=False,welcome_nickname=True,welcome_text='你好{name}')
        self.save('basics',b)
        self.r.customer.new_group(-10011)
        self.assertEqual(self.r.store.get_ledger_reset_hour(-10011),old_hour)
        self.r.customer.new_group(-10077)
        self.assertEqual(self.r.store.get_ledger_reset_hour(-10077),5)
        self.r.customer.sync()
        C.handle_text(self.r.store,-10077,C.Actor(111,'boss','老板'),'+50',{111})
        self.assertEqual(self.r.store.entries(-10077)[0].currency,'U')
        line = self.r._welcome_line(dict(id=555,first_name='<新>',username='new_user'))
        self.assertIn('(@new_user)',line)
        self.assertNotIn('<a ',line)
        self.assertIn('&lt;新&gt;',line)
        b['welcome_enabled']=False
        self.save('basics',b)
        self.assertEqual(self.r._welcome_line(dict(id=555,first_name='新')),'')
        self.assertEqual([tuple(row) for row in self.r.store.conn.execute('SELECT * FROM entries WHERE chat_id=-10011')],before)

    def test_member_messages_defaults_customization_and_leave_switch(self):
        from runners.ledger import group_admin as G
        self.assertEqual(CC.settings(self.bot)['basics']['welcome_text'],'{name} 已加入该群。')
        self.assertEqual(G.DEFAULT_NEWBIE_WELCOME,'{name} 已加入该群。')
        member=dict(id=555,first_name='<成员>')
        self.assertEqual(self.r._welcome_line(member),G.member_mention_html(member)+' 已加入该群。')
        chat=dict(id=-10011,type='supergroup',title='测试')
        self.assertTrue(self.r._service_message(-10011,chat,dict(left_chat_member=member),dict(id=111)))
        self.assertEqual(self.calls[-1][1]['text'],G.member_mention_html(member)+' 已离开该群。')
        b=CC.settings(self.bot)['basics']
        b.update(welcome_text='自定义欢迎{name}',leave_text='再见{name} <注意>',leave_enabled=False)
        self.save('basics',b)
        self.assertIn('自定义欢迎',self.r._welcome_line(member))
        before=len(self.calls)
        self.r._service_message(-10011,chat,dict(left_chat_member=member),dict(id=111))
        self.assertEqual(len(self.calls),before)
        b['leave_enabled']=True
        self.save('basics',b)
        self.r._service_message(-10011,chat,dict(left_chat_member=member),dict(id=111))
        self.assertEqual(self.calls[-1][1]['text'],'再见'+G.member_mention_html(member)+' &lt;注意&gt;')
        before=len(self.calls)
        self.r._service_message(-10011,chat,dict(left_chat_member=dict(id=999,is_bot=True,first_name='bot')),dict(id=111))
        self.assertEqual(len(self.calls),before)
        for value in (dict(b,leave_enabled='false'),dict(b,leave_text='x'*301)):
            with self.assertRaises(ValueError):
                self.save('basics',value)
        self.r.bot['ledger']['newbie_welcome']='已有欢迎{name}'
        CC.sync_welcome(self.bot)
        self.assertEqual(CC.settings(self.bot)['basics']['welcome_text'],'已有欢迎{name}')
        self.r.bot['ledger']['newbie_welcome']=None
        CC.sync_welcome(self.bot)
        self.assertEqual(CC.settings(self.bot)['basics']['welcome_text'],'{name} 已加入该群。')
        self.assertEqual(CC.settings(self.bot)['basics']['leave_text'],'再见{name} <注意>')

    def test_welcome_images_permissions_validation_preview_and_real_send(self):
        import base64,io
        from PIL import Image
        import customer_images as images
        output=io.BytesIO()
        Image.new('RGB',(24,16),'blue').save(output,format='PNG')
        encoded='data:image/png;base64,'+base64.b64encode(output.getvalue()).decode()
        def photo(mode,uid=111,**extra):
            return miniapp.action(self.mgr,self.bot,uid,'welcomephoto',dict(mode=mode,revision=CC.settings(self.bot)['revision'],**extra))
        before=CC.settings(self.bot)['basics']['welcome_text']
        with self.assertRaises(PermissionError):
            photo('upload',uid=222,image=encoded)
        with self.assertRaises(ValueError):
            photo('upload',image='data:image/png;base64,aW52YWxpZA==')
        photo('upload',image=encoded)
        self.assertTrue(images.image_path(self.bot).exists())
        self.assertTrue(photo('preview')['image'].startswith('data:image/jpeg;base64,'))
        self.assertEqual(CC.settings(self.bot)['basics']['welcome_text'],before)
        self.assertTrue(miniapp.overview(self.mgr,self.bot,111)['welcome_photo_exists'])
        self.assertFalse(miniapp.overview(self.mgr,self.bot,222)['welcome_photo_exists'])
        upload=[]
        def send_file(method,field,filename,fileobj,**kw):
            upload.append((method,field,filename,fileobj.read(),kw))
        self.r.api.call_file=send_file
        member=dict(id=555,first_name='新人')
        self.r._service_message(-10011,dict(id=-10011,type='supergroup',title='测试'),dict(new_chat_members=[member]),dict(id=111))
        self.assertEqual(upload[0][0:3],('sendPhoto','photo','welcome.jpg'))
        self.assertIn('新人 已加入该群。',upload[0][4]['caption'])
        photo('remove')
        self.assertIsNone(photo('preview')['image'])
        self.assertEqual(CC.settings(self.bot)['basics']['welcome_text'],before)
        self.r.bot['ledger']['newbie_welcome_photo']='old_file_id'
        class Response:
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def raise_for_status(self): pass
            def iter_content(self,*args): return [output.getvalue()]
        with patch('core.TgAPI.call',return_value=dict(file_path='photos/welcome.jpg',file_size=len(output.getvalue()))),patch('customer_images.requests.get',return_value=Response()) as get:
            self.assertTrue(photo('preview')['image'].startswith('data:image/jpeg;base64,'))
            self.assertIn('api.telegram.org/file/bot',get.call_args[0][0])
        with patch('core.TgAPI.call',side_effect=Exception('secret')):
            with self.assertRaisesRegex(ValueError,'暂时无法读取'):
                photo('preview')
        self.grant(555,manage=True)
        photo('upload',uid=555,image=encoded)
        self.assertEqual(self.bot['ledger']['newbie_welcome_photo'],images.LOCAL_PHOTO)

    def test_confirmation_is_atomic_and_cannot_be_used_by_another_user(self):
        original_mid = self.send('+100u')
        original = self.r.store.entry_for_source_message(-10011,original_mid)
        self.send('+70u',reply=original_mid)
        token = next(iter(self.r.customer.pending))
        self.assertEqual(self.r.store.get_entry(original.id).amount,Decimal(100))
        callback = dict(id='fake',data='lc:edit:'+token,message=dict(chat=dict(id=-10011)),**{'from':dict(id=222)})
        self.r.customer.callback(callback)
        self.assertEqual(self.r.store.get_entry(original.id).amount,Decimal(100))
        callback['from']['id']=111
        self.r.customer.callback(callback)
        updated = self.r.store.get_entry(original.id)
        self.assertEqual(updated.amount,Decimal(70))
        self.assertEqual(updated.source_message_id,original_mid)
        self.assertEqual(updated.created_at,original.created_at)
        self.features(edit_confirm=False)
        self.send('+30u',reply=original_mid)
        self.assertEqual(self.r.store.get_entry(original.id).amount,Decimal(30))
        self.send('+40u',reply=original_mid)
        self.assertEqual(self.r.store.get_entry(original.id).amount,Decimal(40))

    def test_switch_stale_callback_and_archives(self):
        self.features(bill_switch=False,archive_bill=True)
        self.assertEqual(len(self.r.bill_keyboard(-10011)['inline_keyboard']),1)
        mode=self.r.store.get_ledger_view_mode(-10011)
        self.r._do_callback('fake',-10011,1,'ledger:view:compact:today')
        self.assertEqual(self.r.store.get_ledger_view_mode(-10011),mode)
        self.send('清空')
        self.assertEqual(self.r.store.entries(-10011),[])
        row=self.r.store.conn.execute('SELECT body FROM customer_bill_archives').fetchone()
        self.assertIn('业务回归',row[0])
        self.assertTrue(any('lc:archive:' in str(p) for _,p in self.calls))

    def test_balance_and_categories_do_not_generate_entries(self):
        self.features(carry_balance=True,categories=True,category_settlement=True)
        st=self.r.store
        self.r.customer.sync()
        entry=st.entries(-10011)[0]
        with st.conn:
            st.conn.execute('UPDATE entries SET accounting_date=? WHERE id=?',(st.previous_accounting_date(-10011),entry.id))
        self.send('+50u 采购')
        count=st.conn.execute('SELECT COUNT(*) FROM entries').fetchone()[0]
        bill=C.format_bill(st,-10011,scope='full')
        self.assertIn('上期结余（期初）',bill)
        self.assertIn('采购',bill)
        self.assertIn('备注分类结算',bill)
        self.assertEqual(st.conn.execute('SELECT COUNT(*) FROM entries').fetchone()[0],count)

    def test_deposit_thresholds_are_deduplicated(self):
        self.r.store.clear_entries(-10011)
        self.save('deposits',{'-10011':'100'})
        self.features(deposit_notice=True)
        self.send('+80u')
        self.send('+1u')
        self.send('+20u')
        notices=[p['text'] for _,p in self.calls if p.get('text','').startswith('⚠️')]
        self.assertEqual(len(notices),2)
        self.assertIn('80%',notices[0])
        self.assertIn('风险预警',notices[1])

    def test_pure_u_requires_blank_history_and_silences_old_payout(self):
        with self.assertRaises(ValueError):
            self.features(pure_u=True)
        self.bot=self.mgr.find('ledger2')
        self.r.close_db()
        self.r=LedgerRunner(self.mgr,self.bot)
        self.r.api=SimpleNamespace(call=self.call)
        self.save('features',dict(CC.settings(self.bot)['features'],pure_u=True),uid=333)
        self.send('+100',333,-10033)
        self.send('分红20',333,-10033)
        before=len(self.calls)
        self.send('下发10',333,-10033)
        self.assertEqual(len(self.calls),before)
        entries=self.r.store.entries(-10033)
        self.assertEqual([(e.kind,e.currency,e.net_amount) for e in entries], [('income','U',Decimal(100)),('payout','U',Decimal(20))])
        with self.assertRaises(ValueError):
            self.save('features',dict(CC.settings(self.bot)['features'],pure_u=False),uid=333)

    def test_rules_validate_and_stop_when_authorization_is_revoked(self):
        self.grant(555,broadcast=True)
        rule=dict(title='说明',keyword='hello',match='contains',content='回复',groups=[-10011],enabled=True)
        self.save('replies',[rule],uid=555)
        self.send('hello world',222)
        self.assertEqual(self.calls[-1][1]['text'],'回复')
        miniapp.action(self.mgr,self.bot,111,'staff',dict(user_id=555,add=False))
        self.r.customer.replied.clear()
        before=len(self.calls)
        self.send('hello again',222)
        self.assertEqual(len(self.calls),before)
        with self.assertRaises(ValueError):
            self.save('replies',[dict(rule,groups=[-999])])
        with self.assertRaises(ValueError):
            self.save('replies',[],revision=-1)

    def test_scheduled_sends_are_durable_and_targets_are_checked(self):
        self.grant(555,broadcast=True)
        now=1700000000
        rule=dict(title='广告',content='广告内容',groups=[-10011,-10022],enabled=True,
                  mode='once',at=datetime.fromtimestamp(now,TZ).isoformat(),interval=60)
        self.save('ads',[rule],uid=555)
        rt=self.r.customer
        rt.run_jobs(now-1)
        self.r.store.deactivate_bot_chat(-10022)
        rt.run_jobs(now+1)
        ads=[p for _,p in self.calls if p.get('text')=='广告内容']
        self.assertEqual([p['chat_id'] for p in ads],[-10011])
        CustomerRuntime(self.r).run_jobs(now+100)
        self.assertEqual(len([p for _,p in self.calls if p.get('text')=='广告内容']),1)

    def test_advertisement_images_and_multiple_keywords_run(self):
        import base64,io
        from PIL import Image
        from customer_images import ad_path
        self.grant(555,broadcast=True)
        output=io.BytesIO();Image.new('RGB',(24,16),'green').save(output,format='PNG')
        image='data:image/png;base64,'+base64.b64encode(output.getvalue()).decode()
        with self.assertRaises(PermissionError):
            miniapp.action(self.mgr,self.bot,222,'adphoto',dict(mode='upload',image=image))
        ref=miniapp.action(self.mgr,self.bot,555,'adphoto',dict(mode='upload',image=image))['ref']
        self.assertTrue(ad_path(self.bot,ref).is_file())
        self.assertTrue(miniapp.action(self.mgr,self.bot,555,'adphoto',dict(mode='preview',ref=ref))['image'].startswith('data:image/jpeg;base64,'))
        with self.assertRaises(ValueError):
            miniapp.action(self.mgr,self.bot,555,'adphoto',dict(mode='preview',ref='../bad'))
        now=1700000000
        rule=dict(title='图片广告',content='广告内容',image=ref,groups=[-10011],enabled=True,
                  mode='once',at=datetime.fromtimestamp(now,TZ).isoformat(),interval=60)
        self.save('ads',[rule],uid=555)
        files=[]
        self.r.api.call_file=lambda method,field,name,source,**kw:files.append((method,field,name,source.read(),kw))
        self.r.customer.run_jobs(now-1);self.r.customer.run_jobs(now+1);self.r.customer.run_jobs(now+2)
        self.assertEqual(len(files),1)
        self.assertEqual(files[0][0:3],('sendPhoto','photo','advertisement.jpg'))
        self.assertEqual(files[0][4]['caption'],'广告内容')
        self.save('ads',[dict(rule,content='')],uid=555)
        for mode,positive,negative in [('exact','欢迎','欢迎大家'),('contains','大家你好','普通消息'),('prefix','欢迎大家','大家欢迎')]:
            self.save('replies',[dict(title='多关键词',keyword='你好  欢迎',match=mode,content='匹配回复',groups=[-10011],enabled=True)],uid=555)
            self.r.customer.replied.clear()
            self.r.customer.reply(-10011,positive,500)
            self.assertEqual(self.calls[-1][1]['text'],'匹配回复')
            before=len(self.calls);self.r.customer.replied.clear()
            self.r.customer.reply(-10011,negative,501)
            self.assertEqual(len(self.calls),before)

    def test_query_and_address_gates_use_existing_handlers(self):
        address='TJRabPrwbZy45sbavfcjinPJC18kjpRTv8'
        with patch('runners.ledger.trc20.reply_trc20_verify_image') as verify, patch.object(self.r.tronw,'send_card') as card:
            self.send(address)
            verify.assert_called_once()
            card.assert_not_called()
            self.features(tron_balance=True)
            self.send(address)
            card.assert_called_once_with(-10011,address)
        self.features(lookup=False,okx=False,tron_verify=False,show_address=False)
        with patch.object(self.r,'reply_lookup') as lookup, patch.object(self.r,'reply_price') as price, patch('runners.ledger.trc20.reply_trc20_verify_image') as verify:
            self.send('13800138000')
            self.send('币价')
            self.send('TJRabPrwbZy45sbavfcjinPJC18kjpRTv8')
            self.send('下发地址')
            lookup.assert_not_called();price.assert_not_called();verify.assert_not_called()
        self.assertNotIn('火币',str(CC.FEATURES))
        self.assertNotIn('距离',str(CC.FEATURES))

    def test_group_address_flow_is_scoped_confirmed_and_local_overrides_default(self):
        address='TJRabPrwbZy45sbavfcjinPJC18kjpRTv8'
        b=CC.settings(self.bot)['basics']
        b['payout_address']=address
        self.save('basics',b)
        self.send('下发地址',uid=999)
        self.assertEqual(self.calls[-1][1]['text'],'✅️本群下发地址：\n<code>'+address+'</code>\n⚠️请认真核对该地址再进行转入。')
        self.send('设置下发地址',uid=999)
        self.assertIn('没有',self.calls[-1][1]['text'])
        self.send('设置下发地址')
        self.assertIn('请输入地址',self.calls[-1][1]['text'])
        self.send('invalid')
        self.assertIn('地址无效',self.calls[-1][1]['text'])
        self.send(address)
        self.assertIn('设置成功',self.calls[-1][1]['text'])
        self.send('删除下发地址')
        self.send('1')
        self.assertIn('请输入“确认”',self.calls[-1][1]['text'])
        self.send('确认',uid=999)
        self.assertEqual(self.r.customer.addresses(-10011),[address])
        self.send('确认')
        self.assertEqual(self.r.customer.addresses(-10011),[])
        self.assertEqual(self.r.customer.addresses(-10022),[address])
        self.send('下发地址',uid=999)
        self.assertIn('尚未设置',self.calls[-1][1]['text'])
        self.assertIn('设置下发地址', __import__('customer_ui').help_view(self.r, 111, 'tools')[0])

    def test_panel_addresses_merge_legacy_and_sync_every_group(self):
        a='TJRabPrwbZy45sbavfcjinPJC18kjpRTv8'
        b='TFMsoNbmzTEU7toDB8LvUhicPHhVQN1KWf'
        basics=CC.settings(self.bot)['basics']
        basics['payout_address']=a
        self.save('basics',basics)
        self.bot['ledger']['group_addresses']={'-10011':[b],'-10022':[]}
        panel=miniapp.overview(self.mgr,self.bot,111)
        self.assertEqual(panel['customer']['addresses'],[a,b])
        self.assertEqual(self.r.customer.addresses(-10011),[b])
        with self.assertRaises(PermissionError):
            self.save('addresses',[a,b],uid=222)
        with self.assertRaises(ValueError):
            self.save('addresses',[a,a])
        self.save('addresses',[a,b])
        for cid in (-10011,-10022,-10077):
            self.assertEqual(self.r.customer.addresses(cid),[a,b])
        self.assertEqual(self.bot['ledger']['group_addresses'],{})
        self.save('addresses',[b])
        for cid in (-10011,-10022):
            self.assertEqual(self.r.customer.addresses(cid),[b])
        self.save('addresses',[])
        for cid in (-10011,-10022):
            self.assertEqual(self.r.customer.addresses(cid),[])
        # Older cached miniapps still save a single address through basics.
        basics=CC.settings(self.bot)['basics'];basics['payout_address']=a
        self.bot['ledger']['group_addresses']={'-10011':[b]}
        self.save('basics',basics)
        self.assertEqual(self.r.customer.addresses(-10011),[a])
        self.assertEqual(self.r.customer.addresses(-10022),[a])

    def test_statistics_keep_cleared_rows_exclude_voids_and_use_natural_dates(self):
        st=self.r.store
        self.r.customer.sync()
        st.clear_entries(-10011)
        self.send('+90u')
        old=st.entries(-10011)[-1]
        st.void_entry(-10011,old.id)
        self.send('+30u')
        st.clear_entries(-10011)
        self.send('+20u')
        month,today=miniapp.month_bounds()
        stats=miniapp.statistics(self.bot,111,dict(start=month,end=today,chat_id=''))
        # fixture's 1000/9 remains in monthly statistics after the first clear.
        self.assertEqual(Decimal(stats['summary']['payable']),Decimal('161.11'))
        bills=miniapp.bill_query(self.bot,111,dict(chat_id=''))['bills']
        records=[r for bill in bills for r in bill['entries']]
        self.assertFalse(any(r['id']==old.id for r in records))
        self.assertTrue(any(r['cleared'] and r['amount']=='30.00' for r in records))
        self.assertTrue(any(not r['cleared'] and r['amount']=='20.00' for r in records))
        with self.assertRaises(PermissionError):
            miniapp.statistics(self.bot,222,dict(start=month,end=today,chat_id=-10022))
        with self.assertRaises(ValueError):
            miniapp.statistics(self.bot,111,dict(start='2000-01-01',end=today))

    def test_month_cleanup_preserves_financial_opening_and_purges_old_bills(self):
        st=self.r.store
        st.set_ledger_cutoff_enabled(-10011,False)
        current=st.current_accounting_date(-10011)
        entry=st.add_entry(-10011,'income','100','U','旧月',111,'老板',source_message_id=901,rate='1')
        with st.conn:
            st.conn.execute("UPDATE entries SET created_at='2000-01-01T00:00:00+00:00',accounting_date=? WHERE id=?",(current,entry.id))
        st.prune_month()
        self.assertIsNone(st.conn.execute('SELECT id FROM entries WHERE id=?',(entry.id,)).fetchone())
        self.assertEqual(st.opening_balance(-10011,current),Decimal(100))
        self.assertIn('月初结转余额：100',C.format_bill(st,-10011))
        st.prune_month()
        self.assertEqual(st.opening_balance(-10011,current),Decimal(100))
        st.clear_entries(-10011)
        self.assertEqual(st.opening_balance(-10011,current),Decimal(0))

    def test_personal_configuration_preserves_snapshots_and_source_commands_share_settings(self):
        st=self.r.store
        before=[tuple(r) for r in st.conn.execute('SELECT * FROM entries')]
        miniapp.action(self.mgr,self.bot,111,'pricing',dict(chat_id=-10011,user_id=222,settings=dict(rate='10',fee_percent='3')))
        self.assertEqual(st.get_user_settings(-10011,222),(Decimal(10),Decimal(3)))
        self.assertEqual([tuple(r) for r in st.conn.execute('SELECT * FROM entries')],before)
        self.send('+1000',uid=222)
        self.assertEqual(st.entries(-10011)[-1].net_amount,Decimal(97))
        self.send('出款10')
        self.send('下发20')
        out=miniapp.bill_query(self.bot,111,dict(kind='out'))['bills']
        self.assertEqual(sum(len(b['entries']) for b in out),1)
        payout=miniapp.bill_query(self.bot,111,dict(kind='payout'))['bills']
        self.assertEqual(sum(len(b['entries']) for b in payout),1)


    def test_statistics_group_detail_matches_totals_and_limits_permissions(self):
        today = miniapp.month_bounds()[1]
        payload = dict(chat_id=-10011, start=today, end=today)
        before = [tuple(r) for r in self.r.store.conn.execute('SELECT * FROM entries')]
        detail = miniapp.statistics(self.bot, 222, payload)
        self.assertEqual([g['chat_id'] for g in detail['groups']], [-10011])
        self.assertEqual(len(detail['entries']), detail['summary']['count'])
        self.assertEqual(miniapp.monetary(detail['entries']), detail['summary'])
        self.assertTrue(detail['entries'])
        self.assertTrue(all('token' not in row and 'chat_id' not in row for row in detail['entries']))
        self.assertNotIn('entries', miniapp.statistics(self.bot, 111, dict(start=today, end=today)))
        with self.assertRaises(PermissionError):
            miniapp.statistics(self.bot, 222, dict(payload, chat_id=-10022))
        self.assertEqual([tuple(r) for r in self.r.store.conn.execute('SELECT * FROM entries')], before)


    def test_user_changes_notify_group_once_with_before_and_after_values(self):
        self.features(user_notice=True)
        self.r.store.remember_user(-10011, 222, 'old_user', '原昵称')
        self.calls.clear()
        msg = dict(message_id=501, chat=dict(id=-10011, type='supergroup', title='有权限测试群'),
                   text='普通消息', **{'from': dict(id=222, username='new_user', first_name='新<昵称>')})
        self.r.handle({'message': msg})
        notices = [kw for method, kw in self.calls if method == 'sendMessage']
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]['chat_id'], -10011)
        self.assertEqual(notices[0]['text'], '用户修改信息通知：@new_user\n用户名：@old_user → @new_user\n昵称：原昵称 → 新&lt;昵称&gt;')
        self.calls.clear()
        msg['message_id'] += 1
        self.r.handle({'message': msg})
        self.assertFalse(self.calls)
        msg['message_id'] += 1
        msg['from']['first_name'] = '再改昵称'
        self.r.handle({'message': msg})
        notice = [kw for method, kw in self.calls if method == 'sendMessage'][0]
        self.assertEqual(notice['text'], '用户修改信息通知：@new_user\n昵称：新&lt;昵称&gt; → 再改昵称')
        self.assertEqual(notice['chat_id'], -10011)
        self.features(user_notice=False)
        self.calls.clear()
        msg['from']['username'] = 'disabled_notice'
        self.r.handle({'message': msg})
        self.assertFalse(self.calls)


if __name__=='__main__':
    unittest.main()
