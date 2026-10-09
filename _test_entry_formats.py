"""Reference entry forms, monetary snapshots, signed reversals and real dispatch."""
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from runners.ledger.commands import Actor, handle_text, format_bill, _mine_totals
from runners.ledger.storage import LedgerStore


class EntryFormatsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'test.sqlite3'
        self.st = LedgerStore(self.path)
        self.boss = Actor(111, 'boss', '老板')
        self.user = Actor(222, 'aaaa', '张三')
        self.st.remember_user(-1001, 222, 'aaaa', '张三')
        self.st.set_rate(-1001, '10')
        self.st.set_fee_percent(-1001, '10')
        self.mid = 0

    def tearDown(self):
        self.st.close()
        self.tmp.cleanup()

    def send(self, text, actor=None, reply=None, mid=None):
        self.mid += 1
        return handle_text(self.st, -1001, actor or self.boss, text, {111},
                           reply_user=reply, message_id=mid if mid is not None else self.mid)

    def test_fee_percent_ratio_rebate_and_priority(self):
        cases = [('+1000*0.12', '12', '880', '88'), ('+1000*12', '12', '880', '88'),
                 ('+1000*3', '3', '970', '97'), ('+1000*1', '1', '990', '99'),
                 ('+1000*0', '0', '1000', '100'), ('+1000*-5', '-5', '1050', '105'),
                 ('+1000*-0.05', '-5', '1050', '105'), ('-1000*0.12', '12', '-880', '-88'),
                 ('+1000/5*0.12', '12', '880', '176'), ('+1000*0.12/5', '12', '880', '176')]
        self.send('设置汇率 @aaaa 8')
        self.send('设置费率 @aaaa 3')
        for text, fee, payable, net in cases:
            self.assertTrue(self.send(text).changed, text)
            e = self.st.entries(-1001)[-1]
            self.assertEqual((e.fee_percent, e.payable_amount, e.net_amount),
                             tuple(map(Decimal, (fee, payable, net))), text)
        self.send('+1000*0.12', reply=self.user)
        e = self.st.entries(-1001)[-1]
        self.assertEqual((e.rate, e.fee_percent, e.net_amount), tuple(map(Decimal, ('8', '12', '110'))))
        self.assertEqual(self.st.get_settings(-1001), (Decimal('10'), Decimal('10')))
        self.assertEqual(self.st.get_user_settings(-1001, 222), (Decimal('8'), Decimal('3')))
        self.assertIn('费率12%', format_bill(self.st, -1001))

    def test_usdt_conversion_never_charges_income_fee_and_keeps_snapshots(self):
        self.send('设置汇率 @aaaa 8')
        self.send('设置费率 @aaaa 3')
        for text, reply, rate, fiat in [('+1000u', None, '10', '10000'),
                                       ('+1000U', self.user, '8', '8000'),
                                       ('+1000u/7.3', self.user, '7.3', '7300'),
                                       ('-1000u/7.3', None, '7.3', '-7300')]:
            self.assertTrue(self.send(text, reply=reply).changed)
            e = self.st.entries(-1001)[-1]
            self.assertEqual(e.currency, 'U')
            self.assertEqual((e.rate, e.fee_percent, e.fee_amount, e.payable_amount),
                             (Decimal(rate), Decimal(0), Decimal(0), Decimal(fiat)))
            self.assertEqual(e.net_amount, Decimal('-1000' if text.startswith('-') else '1000'))
        self.send('+1000')
        original = [tuple(r) for r in self.st.conn.execute('SELECT * FROM entries')]
        self.send('设置汇率20')
        self.send('设置费率5')
        summary = self.st.summary(-1001)
        self.assertEqual((summary.income, summary.income_usdt), (Decimal('19000'), Decimal('2090')))
        bill = format_bill(self.st, -1001, show_all_records=True)
        self.assertIn('7300/7.3=1000U', bill)
        self.assertNotIn('折合', bill)
        self.assertNotIn('1000/7.3=1000U', bill)
        self.st.close()
        self.st = LedgerStore(self.path)
        self.assertEqual(original, [tuple(r) for r in self.st.conn.execute('SELECT * FROM entries')])

    def test_names_replies_notes_payout_forms_and_corrections(self):
        for text, kind, note in [('+1000 张三', 'income', '张三'), ('+1000 定金', 'income', '定金'),
                                 ('张三下发1000', 'payout', '张三'), ('下发1000 结算', 'payout', '结算')]:
            self.assertTrue(self.send(text).changed)
            e = self.st.entries(-1001)[-1]
            self.assertEqual((e.kind, e.note, e.operator_id), (kind, note, 111))
        self.send('+1000', reply=self.user)
        self.assertEqual(self.st.entries(-1001)[-1].note, '张三')
        for text in ('下发100', '下发100u', '/下发1000', '/下发1000u', '下发1000'):
            self.assertTrue(self.send(text, reply=self.user).changed)
            e = self.st.entries(-1001)[-1]
            self.assertEqual(e.net_amount, e.amount)
            self.assertEqual(e.fee_amount, 0)
        self.send('下发-1000')
        e = self.st.entries(-1001)[-1]
        self.assertEqual((e.kind, e.net_amount), ('payout', Decimal('-1000')))
        self.assertIn('+1000U', format_bill(self.st, -1001))
        self.assertEqual(_mine_totals([e])[1], Decimal('-1000'))

    def test_income_requires_leading_sign_and_amount(self):
        self.send('+100')
        original = [tuple(row) for row in self.st.conn.execute('SELECT * FROM entries')]
        for text in ('13-17的', '13-17', '张三+1000', '张三-100', '这笔+100', '备注 -100',
                     '+备注100', '-备注100', '++100', '--100', '+-100', '-+100',
                     '入款100', '收款100', '上分100', '/in100', '/income100'):
            with self.subTest(text=text):
                self.assertIsNone(self.send(text, reply=self.user))
                self.assertEqual([tuple(row) for row in self.st.conn.execute('SELECT * FROM entries')], original)
        for text in ('+100', '-17', '+100.50 备注', '-20u', '+1000/7.3', '+1000*0.12', '+ 100'):
            self.assertTrue(self.send(text).changed, text)

    def test_invalid_parameters_permissions_duplicate_delivery_and_calculator(self):
        from runners.ledger.commands import is_entry_text
        self.assertTrue(is_entry_text('+1000*0.12'))
        self.assertTrue(is_entry_text('+1000*-5'))
        self.assertTrue(is_entry_text('+1000u/7.3'))
        self.assertTrue(is_entry_text('+1000/0'))
        self.assertFalse(is_entry_text('1000*0.12'))
        self.assertFalse(is_entry_text('+1000+200'))
        for text in ('+1000/0', '+1000/-3', '+1000*100', '+1000*0.99999999',
                     '+1000*abc', '+1000/7/8', '+1000*3*4', '下发1000/7.3'):
            result = self.send(text)
            self.assertFalse(result.changed, text)
        self.assertFalse(self.st.entries(-1001))
        self.send('取消全员')
        self.assertFalse(self.send('+1000*0.12', actor=self.user).changed)
        self.assertTrue(self.send('+1000u', mid=999).changed)
        self.assertFalse(self.send('+1000u', mid=999).changed)
        self.assertEqual(len(self.st.entries(-1001)), 1)

    def test_real_dispatch_and_miniapp_totals(self):
        import core, miniapp
        from runners.ledger import LedgerRunner
        original = core.BASE_DIR
        runner = None
        try:
            core.set_base_dir(self.tmp.name)
            Path(core.DATA_DIR).mkdir(exist_ok=True)
            mgr = SimpleNamespace(cfg={'miniapp_base_url': 'https://example.test'}, save=lambda: None,
                                  is_expired=lambda b: False, runners={})
            bot = {'id': 'entry-test', 'type': 'ledger', 'token': 'FAKE', 'owner_id': 111,
                   'admin_ids': [111], 'archive': {'enabled': False}}
            runner = LedgerRunner(mgr, bot)
            mgr.runners[bot['id']] = runner
            calls = []
            runner.api = SimpleNamespace(call=lambda method, **kw: calls.append((method, kw)) or {'message_id': len(calls)})
            for mid, text in enumerate(('设置汇率10', '设置费率10', '+1000*0.12', '+1000u/7.3',
                                        '张三下发1000', '下发-1000', '1000*0.12', '13-17的',
                                        '张三+1000', '+备注100', '--100', '入款100'), 1):
                runner.handle({'message': {'message_id': mid, 'chat': {'id': -1001, 'type': 'supergroup', 'title': '测试群'},
                                           'from': {'id': 111, 'username': 'boss', 'first_name': '老板'}, 'text': text}})
            es = runner.store.entries(-1001)
            self.assertEqual(len(es), 4)
            self.assertEqual([e.net_amount for e in es], list(map(Decimal, ('88', '1000', '1000', '-1000'))))
            state = miniapp.statistics(bot, 111, {})
            group = next(g for g in state['groups'] if g['chat_id'] == -1001)
            self.assertEqual((Decimal(group['income']), Decimal(group['payable']), Decimal(group['paid'])),
                             (Decimal('8300'), Decimal('1088'), Decimal(0)))
        finally:
            if runner is not None:
                runner.close_db()
            core.set_base_dir(original)

    def test_bill_is_sent_directly_and_source_reply_still_works(self):
        import core
        from runners.ledger import LedgerRunner
        original = core.BASE_DIR
        runner = None
        try:
            core.set_base_dir(self.tmp.name)
            Path(core.DATA_DIR).mkdir(exist_ok=True)
            mgr = SimpleNamespace(cfg={}, save=lambda: None, is_expired=lambda b: False)
            runner = LedgerRunner(mgr, {'id': 'bill-test', 'type': 'ledger', 'token': 'FAKE',
                'owner_id': 111, 'admin_ids': [111], 'archive': {'enabled': False}})
            calls = []
            runner.api = SimpleNamespace(call=lambda method, **kw: calls.append((method, kw)) or {'message_id': len(calls)})

            def dispatch(text, mid, reply=None):
                msg = {'message_id': mid, 'chat': {'id': -1001, 'type': 'supergroup', 'title': '测试群'},
                       'from': {'id': 111, 'username': 'boss', 'first_name': '老板'}, 'text': text}
                if reply:
                    msg['reply_to_message'] = reply
                runner.handle({'message': msg})
                return calls[-1][1]

            runner.store.set_user_setting(-1001, 222, 'rate', '2')
            person = {'message_id': 900, 'from': {'id': 222, 'username': 'aaaa', 'first_name': '张三'}, 'text': '你好'}
            for mid, text in enumerate(('+100', '-10', '+20u', '下发5'), 1):
                sent = dispatch(text, mid, person if mid == 1 else None)
                self.assertIn('今日账单', sent['text'])
                self.assertNotIn('reply_to_message_id', sent)
                self.assertNotIn('reply_parameters', sent)
                switch = sent['reply_markup']['inline_keyboard'][1][0]
                self.assertEqual(switch, {'text': '↪️ 切换详细', 'callback_data': 'ledger:view:detailed:today'})
            first = runner.store.entry_for_source_message(-1001, 1)
            self.assertEqual((first.note, first.rate, first.net_amount), ('张三', Decimal(2), Decimal(50)))
            for mode in ('detailed', 'compact'):
                runner.on_callback({'id': 'FAKE_CALLBACK', 'from': {'id': 111},
                                    'data': 'ledger:view:'+mode+':today',
                                    'message': {'message_id': 777, 'chat': {'id': -1001, 'type': 'supergroup'}}})
                edited = [params['text'] for method, params in calls if method == 'editMessageText'][-1]
                if mode == 'detailed':
                    self.assertIn('入款分类：', edited)
                    self.assertIn('张三(1笔) 100 | 50U', edited)
                else:
                    self.assertNotIn('入款分类：', edited)
            dispatch('撤销', 5, {'message_id': 1, 'text': '+100', 'from': person['from']})
            self.assertEqual(len(runner.store.entries(-1001)), 3)
            self.assertIsNone(runner.store.entry_for_source_message(-1001, 1))
            self.assertEqual(dispatch('100/2', 6)['reply_to_message_id'], 6)
            runner.store.set_ledger_view_mode(-1001, 'detailed')
            self.assertEqual(runner.bill_keyboard(-1001)['inline_keyboard'][1][0]['text'], '↪️ 切换简洁')
        finally:
            if runner is not None:
                runner.close_db()
            core.set_base_dir(original)

    def test_recent_income_label_and_time_spacing(self):
        self.send('+100')
        self.send('下发5')
        self.st.set_ledger_view_mode(-1001, 'detailed')
        bill = format_bill(self.st, -1001)
        self.assertRegex(bill, r'#1 入款\d{2}:\d{2}:\d{2}：')
        self.assertRegex(bill, r'#2 下发\d{2}:\d{2}:\d{2}：')
        self.assertNotIn('加分', bill)

    def test_detailed_income_categories_use_attribution_and_all_income_snapshots(self):
        self.st.set_fee_percent(-1001, '0')
        self.send('+100')
        self.send('+200')
        self.send('-50')
        self.send('+80 指定备注', reply=self.user)
        self.send('+120', reply=self.user)
        self.send('+100u/8 项目A')
        self.send('+40 项目A')
        self.send('下发999 项目A')
        self.send('+999 不计入')
        self.st.void_last_entry(-1001)
        # Changing current parameters must not reprice the historical categories.
        self.st.set_rate(-1001, '20')
        self.st.set_fee_percent(-1001, '5')
        original = [tuple(row) for row in self.st.conn.execute('SELECT * FROM entries')]
        self.st.set_ledger_view_mode(-1001, 'compact')
        self.assertNotIn('入款分类：', format_bill(self.st, -1001))
        self.st.set_ledger_view_mode(-1001, 'detailed')
        bill = format_bill(self.st, -1001)
        section = bill.split('入款分类：', 1)[1].split('--------------------------------', 1)[0]
        self.assertEqual(section.strip().splitlines(), ['老板(3笔) 250 | 25U',
                                                      '张三(2笔) 200 | 20U',
                                                      '项目A(2笔) 840 | 104U'])
        self.assertNotIn('指定备注', section)
        self.assertNotIn('不计入', section)
        self.assertNotIn('999', section)
        self.assertLess(bill.index('入款分类：'), bill.index('今日账单'))
        self.assertEqual(original, [tuple(row) for row in self.st.conn.execute('SELECT * FROM entries')])
        # The previous optional settlement section still works without being conflated.
        self.st.customer_options = {'categories': True}
        self.assertIn('备注分类结算：', format_bill(self.st, -1001))

    def test_income_categories_follow_bill_period_and_escape_labels(self):
        self.st.set_fee_percent(-1001, '0')
        self.send('+80 昨日备注')
        self.st.conn.execute('UPDATE entries SET accounting_date=?',
                             (self.st.previous_accounting_date(-1001),))
        self.st.conn.commit()
        self.send('+100 <b>备注&</b>')
        self.st.set_ledger_view_mode(-1001, 'detailed')
        today = format_bill(self.st, -1001)
        yesterday = format_bill(self.st, -1001, scope='yesterday')
        full = format_bill(self.st, -1001, scope='full')
        self.assertIn('&lt;b&gt;备注&amp;&lt;/b&gt;(1笔) 100 | 10U', today)
        self.assertNotIn('昨日备注', today)
        self.assertIn('昨日备注(1笔) 80 | 8U', yesterday)
        self.assertNotIn('备注&amp;', yesterday)
        self.assertNotIn('昨日备注', full)
        self.assertIn('&lt;b&gt;备注&amp;&lt;/b&gt;(1笔) 100 | 10U', full)
        archive = format_bill(self.st, -1001, scope='archive')
        self.assertIn('昨日备注(1笔) 80 | 8U', archive)
        self.st.void_last_entry(-1001)
        self.assertNotIn('入款分类：', format_bill(self.st, -1001))


if __name__ == '__main__':
    unittest.main()
