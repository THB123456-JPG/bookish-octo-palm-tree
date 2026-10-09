"""Personal/group pricing, safe username parsing, actual dispatch and immutable history."""
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from runners.ledger.commands import Actor, handle_text, format_bill
from runners.ledger.storage import LedgerStore


class PersonalPricingTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name)/'ledger.sqlite3'
        self.st = LedgerStore(self.path)
        self.boss, self.aaa, self.bbb = Actor(111, 'boss', '老板'), Actor(222, 'aaaa', '甲'), Actor(333, 'bbb', '乙')
        for user in (self.aaa, self.bbb):
            self.st.remember_user(-1001, user.user_id, user.username, user.display_name)
        self.mid = 0

    def tearDown(self):
        self.st.close()
        self.folder.cleanup()

    def command(self, text, actor=None, reply=None, cid=-1001):
        self.mid += 1
        return handle_text(self.st, cid, actor or self.boss, text, {111}, reply_user=reply, message_id=self.mid)

    def rows(self):
        return [tuple(r) for r in self.st.conn.execute('SELECT * FROM entries ORDER BY id')]

    def test_compact_spaced_reply_and_global_configuration(self):
        self.assertTrue(self.command('设置汇率@aaaa7.3').changed)
        self.assertTrue(self.command('设置费率@aaaa3').changed)
        self.assertEqual(self.st.get_user_settings(-1001, 222), (Decimal('7.3'), Decimal('3')))
        self.assertEqual(self.st.get_settings(-1001), (Decimal('1'), Decimal('0')))
        self.assertTrue(self.command('设置汇率 @AAAA 8.1', reply=self.bbb).changed)
        self.assertEqual(self.st.get_user_settings(-1001, 333), self.st.get_settings(-1001))
        self.assertTrue(self.command('设置费率0.12', reply=self.bbb).changed)
        self.assertEqual(self.st.get_user_settings(-1001, 333)[1], Decimal('0.12'))
        self.assertTrue(self.command('设置汇率7.5').changed)
        self.assertTrue(self.command('设置费率10').changed)
        self.assertEqual(self.st.get_user_settings(-1001, 222), (Decimal('8.1'), Decimal('3')))
        self.assertEqual(self.st.get_user_settings(-1001, 333), (Decimal('7.5'), Decimal('0.12')))
        self.assertIn('@aaaa', self.command('查看费率@aaaa').text)
        self.assertIn('0.12%', self.command('查看费率', reply=self.bbb).text)
        self.assertIn('7.5', self.command('查看费率').text)
        self.assertTrue(self.command('设置费率 @aaaa 默认').changed)
        self.assertEqual(self.st.get_user_settings(-1001, 222), (Decimal('8.1'), Decimal('10')))
        self.assertTrue(self.command('设置汇率默认', reply=self.aaa).changed)
        self.assertEqual(self.st.get_user_settings(-1001, 222), self.st.get_settings(-1001))

    def test_removed_rate_aliases_do_not_change_group_or_personal_parameters(self):
        self.command('设置汇率 @aaaa 7.3')
        self.command('设置费率 @aaaa 3')
        before = self.st.get_settings(-1001), self.st.get_user_settings(-1001, 222), self.rows()
        for text in ('汇率7.3', '汇率 @aaaa 8', '费率3', '费率 @aaaa 9',
                     '/set_fee 8', '/set_fee @aaaa 9', '/fee_rate', '/fee_rate @aaaa'):
            self.assertIsNone(self.command(text), text)
        self.assertEqual(before, (self.st.get_settings(-1001), self.st.get_user_settings(-1001, 222), self.rows()))
        import customer_ui
        text = next(text for key, _, text in customer_ui.LEDGER_HELP if key == 'rates')
        self.assertNotIn('/set_fee', text)
        self.assertNotIn('/fee_rate', text)
        self.assertNotIn('<code>汇率7.3</code>', text)
        self.assertNotIn('<code>费率3</code>', text)

    def test_reply_direct_explicit_rate_payout_and_historical_snapshots(self):
        self.st.add_operator(-1001, 222, 'aaaa', '甲', 111)
        self.st.add_operator(-1001, 333, 'bbb', '乙', 111)
        self.st.add_operator(-1002, 222, 'aaaa', '甲', 111)
        self.command('设置汇率10')
        self.command('设置费率5')
        self.command('+100', actor=self.aaa)
        original = self.rows()
        self.command('设置汇率 @aaaa 5')
        self.command('设置费率 @aaaa 10')
        self.assertEqual(self.rows(), original)
        self.command('+100', actor=self.aaa)
        self.assertEqual(self.st.entries(-1001)[-1].net_amount, Decimal('18'))
        self.command('+100', actor=self.boss, reply=self.aaa)
        e = self.st.entries(-1001)[-1]
        self.assertEqual((e.operator_id, e.rate, e.fee_percent, e.net_amount), (111, Decimal('5'), Decimal('10'), Decimal('18')))
        self.command('+100/20', actor=self.boss, reply=self.aaa)
        self.assertEqual(self.st.entries(-1001)[-1].net_amount, Decimal('4.5'))
        self.command('-100', actor=self.boss, reply=self.aaa)
        self.assertEqual(self.st.entries(-1001)[-1].net_amount, Decimal('-18'))
        self.command('下发12', actor=self.boss, reply=self.aaa)
        self.assertEqual(self.st.entries(-1001)[-1].net_amount, Decimal('12'))
        self.command('下发-12', actor=self.aaa)
        self.assertEqual(self.st.entries(-1001)[-1].net_amount, Decimal('-12'))
        self.command('+100', actor=self.bbb)
        self.assertEqual(self.st.entries(-1001)[-1].net_amount, Decimal('9.5'))
        self.command('+100', actor=self.aaa, cid=-1002)
        self.assertEqual(self.st.entries(-1002)[-1].net_amount, Decimal('100'))
        before = self.rows()
        self.command('设置汇率9')
        self.command('设置费率2')
        self.assertEqual(self.rows(), before)
        self.assertIn('默认汇率', format_bill(self.st, -1001))
        self.assertEqual(self.st.summary(-1001).income_usdt, Decimal('41.5'))

    def test_unknown_ambiguous_numeric_usernames_invalid_values_and_permissions(self):
        self.st.remember_user(-1001, 444, 'aaaa7', '数字用户名')
        self.st.remember_user(-1002, 555, 'other', '别群用户')
        self.assertTrue(self.command('设置汇率@aaaa7.3').changed)
        self.assertTrue(self.command('设置汇率 @aaaa7 7.3').changed)
        self.assertFalse(self.command('设置汇率@aaaa73').changed)
        self.assertIn('歧义', self.command('设置汇率@aaaa73').text)
        before = self.st.get_settings(-1001)
        for text in ('设置费率@other3', '设置汇率 @missing 7.3', '设置费率 @aaaa -1',
                     '设置费率 @aaaa 100', '设置费率 @aaaa 99.99999',
                     '设置汇率 @aaaa 0', '设置汇率 @aaaa 0.00001',
                     '设置汇率 @aaaa NaN', '设置费率 @aaaa Infinity',
                     '设置汇率 @aaaa 7.3 @bbb', '设置汇率默认'):
            self.assertFalse(self.command(text).changed, text)
        self.assertEqual(self.st.get_settings(-1001), before)
        self.assertFalse(self.command('设置费率 @aaaa 3', actor=self.aaa).changed)
        self.assertFalse(self.command('设置汇率 @aaaa 7.3', cid=111).changed)
        self.st.add_operator(-1001, 333, 'bbb', '乙', 111)
        self.assertTrue(self.command('设置费率 @aaaa 0', actor=self.bbb).changed)
        self.assertEqual(self.st.get_user_settings(-1001, 222)[1], 0)
        self.command('下课')
        self.assertIsNone(self.command('设置费率 @aaaa 3'))
        self.command('上课')
        self.assertTrue(self.command('设置费率 @aaaa 3').changed)

    def test_persistence_zero_snapshot_and_legacy_backfill(self):
        self.st.add_operator(-1001, 222, 'aaaa', '甲', 111)
        self.command('设置费率 @aaaa 99.9')
        self.command('+0.01', actor=self.aaa)
        self.assertEqual(self.st.entries(-1001)[-1].net_amount, 0)
        original = self.rows()
        self.st.close()
        self.st = LedgerStore(self.path)
        self.assertEqual(self.rows(), original)
        self.assertEqual(self.st.get_user_settings(-1001, 222)[1], Decimal('99.9'))
        with tempfile.TemporaryDirectory() as folder:
            legacy = LedgerStore(Path(folder)/'legacy.sqlite3')
            legacy.add_entry(-1001, 'income', '100', 'CNY', '', 111, '老板')
            for col in ('fee_amount', 'payable_amount', 'payable_usdt'):
                legacy.conn.execute('ALTER TABLE entries DROP COLUMN '+col)
            legacy.conn.commit()
            legacy.close()
            legacy = LedgerStore(Path(folder)/'legacy.sqlite3')
            self.assertEqual(legacy.entries(-1001)[0].payable_usdt, 100)
            legacy.close()

    def test_real_update_dispatch_and_price_alias_help(self):
        import core
        from runners.ledger import LedgerRunner
        original_base = core.BASE_DIR
        try:
            core.set_base_dir(self.folder.name)
            Path(core.DATA_DIR).mkdir(exist_ok=True)
            runner = LedgerRunner(SimpleNamespace(cfg={}, save=lambda: None, is_expired=lambda b: False),
                {'id': 'pricing-test', 'type': 'ledger', 'token': 'FAKE', 'owner_id': 111,
                 'admin_ids': [111], 'archive': {'enabled': False}})
            calls = []
            runner.api = SimpleNamespace(call=lambda method, **kw: calls.append((method, kw)) or {'message_id': len(calls)})
            def send(text, uid, reply=None):
                msg = {'message_id': len(calls)+100, 'chat': {'id': -1001, 'type': 'supergroup', 'title': '群'},
                       'from': {'id': uid, 'first_name': '人员', 'username': 'aaaa' if uid==222 else 'boss'}, 'text': text}
                if reply:
                    msg['reply_to_message'] = {'from': {'id': reply, 'first_name': '甲', 'username': 'aaaa'}}
                runner.handle({'message': msg})
            try:
                send('hello', 222)
                send('设置汇率@aaaa7.3', 111)
                send('设置费率3', 111, reply=222)
                send('+730', 111, reply=222)
                e = runner.store.entries(-1001)[-1]
                self.assertEqual((e.net_amount, e.operator_id), (Decimal('97'), 111))
                self.assertEqual(runner.store.get_settings(-1001), (Decimal('1'), Decimal('0')))
                from runners.ledger import price
                self.assertFalse(price.is_price_command('/price'))
                import customer_ui
                text = next(text for key, _, text in customer_ui.LEDGER_HELP if key=='rates')
                for alias in ('币价', 'bj', 'z0', 'BJ', 'Z0'):
                    self.assertTrue(price.is_price_command(alias))
                    self.assertIn(alias, text)
            finally:
                runner.close_db()
        finally:
            core.set_base_dir(original_base)


if __name__ == '__main__':
    unittest.main()
