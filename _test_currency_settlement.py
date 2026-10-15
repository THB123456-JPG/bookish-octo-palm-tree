"""RMB settlement uses original amounts/rates and never relabels stored U payments."""
from decimal import Decimal
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
import unittest

import miniapp
import _test_customer_config as fixtures
from runners.ledger import commands as C
from runners.ledger import reminders
from runners.ledger.storage import LedgerStore


class CurrencySettlementTests(unittest.TestCase):
    tearDown = fixtures.CustomerTests.tearDown
    call = fixtures.CustomerTests.call
    features = fixtures.CustomerTests.features
    save = fixtures.CustomerTests.save

    def setUp(self):
        fixtures.CustomerTests.setUp(self)
        self.cid = -10022
        self.bot.setdefault('ledger', {}).setdefault('group_currency', {})[str(self.cid)] = 'U'
        self.r.customer.sync()
        self.r.store.set_rate(self.cid, '11')
        self.r.store.set_fee_percent(self.cid, '0')

    def record(self, text, reply=None):
        self.mid += 1
        self.r.handle({'message': dict(message_id=self.mid, text=text,
            chat={'id': self.cid, 'type': 'supergroup', 'title': 'RMB结算测试'},
            **{'from': {'id': 111, 'first_name': '老板'}},
            **({'reply_to_message': reply} if reply else {}))})
        return self.mid

    def test_rmb_bill_payout_reverse_and_historical_rate(self):
        self.record('+2000/11')
        self.record('+2000/11')
        bill = C.format_bill(self.r.store, self.cid)
        self.assertEqual(bill.count('2000U × 11 = 22000RMB'), 2)
        self.assertIn('总入款金额：4000U', bill)
        self.assertIn('应下发：44000RMB', bill)
        self.record('下发1000')
        entry = self.r.store.entries(self.cid)[-1]
        self.assertEqual((entry.currency, entry.amount), ('CNY', Decimal('1000')))
        bill = C.format_bill(self.r.store, self.cid)
        self.assertIn('已下发：1000RMB', bill)
        self.assertIn('43000RMB', bill)
        self.record('下发-100')
        self.record('设置汇率20')
        bill = C.format_bill(self.r.store, self.cid)
        self.assertIn('应下发：44000RMB', bill)
        self.assertIn('已下发：900RMB', bill)
        self.assertIn('43100RMB', bill)

    def test_old_u_payment_remains_u_and_converts_at_its_own_rate(self):
        self.record('+2000/11')
        st = self.r.store
        old = st.add_entry(self.cid, 'payout', '100', 'U', '', 111, '老板')
        before = tuple(st.conn.execute('SELECT * FROM entries WHERE id=?', (old.id,)).fetchone())
        self.record('设置汇率20')
        bill = C.format_bill(st, self.cid)
        self.assertIn('已下发：1100RMB', bill)
        self.assertIn('20900RMB', bill)
        self.assertEqual(before, tuple(st.conn.execute('SELECT * FROM entries WHERE id=?', (old.id,)).fetchone()))

    def test_miniapp_bill_statistics_and_mixed_currency_totals(self):
        self.record('+2000/11')
        self.record('下发1000')
        result = miniapp.statistics(self.bot, 111, {'chat_id': self.cid})
        summary = result['summary']
        self.assertEqual((summary['unit'], summary['income_unit']), ('RMB', 'U'))
        self.assertEqual([Decimal(summary[k]) for k in ('income', 'payable', 'paid', 'balance')],
                         list(map(Decimal, ('2000', '22000', '1000', '21000'))))
        self.assertEqual(result['entries'][-1]['settlement_unit'], 'RMB')
        self.assertEqual(Decimal(result['entries'][-1]['settlement_amount']), 1000)
        bill = miniapp.bill_query(self.bot, 111, {'chat_id': self.cid})['bills'][0]
        self.assertEqual(bill['summary'], summary)
        mixed = miniapp.statistics(self.bot, 111, {})['summary']
        self.assertEqual({s['unit'] for s in mixed['by_currency']}, {'U', 'RMB'})
        self.assertNotIn('balance', mixed)

    def test_reply_edit_keeps_rmb_input_and_cny_group_stays_u(self):
        self.record('+2000/11')
        mid = self.record('下发1000')
        self.features(edit_confirm=False)
        self.record('下发500', {'message_id': mid, 'text': '下发1000', 'from': {'id': 111}})
        bill = C.format_bill(self.r.store, self.cid)
        self.assertIn('已下发：500RMB', bill)
        self.assertIn('21500RMB', bill)
        self.cid = -10011
        self.record('下发5')
        self.assertEqual(self.r.store.entries(self.cid)[-1].currency, 'U')
        self.assertIn('已下发：5U', C.format_bill(self.r.store, self.cid))

    def test_exact_rmb_small_payments_and_explicit_u_payment(self):
        self.record('+1/11')
        for _ in range(3):
            self.record('下发0.01')
        self.assertIn('10.97RMB', C.format_bill(self.r.store, self.cid))
        self.record('下发0.1u')
        self.assertIn('已下发：1.13RMB', C.format_bill(self.r.store, self.cid))
        self.assertIn('9.87RMB', C.format_bill(self.r.store, self.cid))

    def test_pure_u_dividend_mode_stays_u(self):
        self.bot['ledger'].setdefault('customer', {}).setdefault('features', {})['pure_u'] = True
        self.record('+2000/11')
        self.record('分红100')
        bill = C.format_bill(self.r.store, self.cid)
        self.assertNotIn('RMB', bill)
        self.assertIn('已分红：100U', bill)
        self.assertIn('1900U', bill)

    def test_month_prune_preserves_rmb_and_unknown_legacy_carry_is_not_repriced(self):
        self.record('+2000/11')
        self.record('下发1000')
        st = self.r.store
        period = st.current_accounting_date(self.cid)
        with st.conn:
            st.conn.execute("UPDATE entries SET created_at='2000-01-01T00:00:00+00:00' WHERE chat_id=?", (self.cid,))
        st.prune_month()
        self.record('设置汇率20')
        self.assertEqual(st.opening_balance(self.cid, period, 'RMB'), Decimal('21000'))
        self.assertIn('含结转未下发：21000RMB', C.format_bill(st, self.cid))
        with st.conn:
            st.conn.execute('UPDATE customer_opening_rmb SET balance=NULL WHERE chat_id=?', (self.cid,))
            st.conn.execute('DELETE FROM customer_opening_units WHERE chat_id=?', (self.cid,))
        before = st.opening_balance(self.cid, period)
        st.prune_month()
        self.assertEqual(st.opening_balance(self.cid, period), before)
        self.assertIn('金额待核对，未合并', C.format_bill(st, self.cid))

    def test_private_statistics_split_units_and_use_historical_amounts(self):
        self.record('+2000/11')
        self.record('下发1000')
        selected = [(self.cid, 'U入款群'), (-10011, 'RMB入款群')]
        report = C.format_owner_statistics(self.r.store, selected)
        self.assertIn('加分 +22000 RMB ｜ 下发 -1000 RMB', report)
        self.assertIn('加分 +111.11 U', report)
        mine = C.format_mine_multi(self.r.store, 111, selected)
        self.assertIn('2000U × 11 = 22000RMB', mine)
        self.assertIn('合计 2 笔 ｜ 加分 +22000 RMB ｜ 下发 -1000 RMB', mine)

    def test_rmb_carry_cleanup_and_legacy_writer_compatibility(self):
        st = self.r.store
        period = st.current_accounting_date(self.cid)
        previous = st.previous_accounting_date(self.cid)
        st.prune_month()
        with st.conn:
            for cid, key in ((self.cid, period), (self.cid, previous), (-10011, period)):
                st.conn.execute('INSERT OR REPLACE INTO customer_opening_balances VALUES(?,?,?)', (cid, key, '10'))
                st.conn.execute('INSERT OR REPLACE INTO customer_opening_rmb VALUES(?,?,?,?)', (cid, key, '110', '10'))
            # Old source can still write its three-column table after a source rollback.
            st.conn.execute('INSERT OR REPLACE INTO customer_opening_balances VALUES(?,?,?)', (self.cid, period, '20'))
        self.assertIsNone(st.opening_balance(self.cid, period, 'RMB'))
        miniapp.action(self.mgr, self.bot, 111, 'deletebill', dict(chat_id=self.cid, period=period, confirm=True))
        self.assertEqual(st.opening_balance(self.cid, period, 'RMB'), 0)
        self.assertIsNone(st.conn.execute('SELECT 1 FROM customer_opening_rmb WHERE chat_id=? AND period=?', (self.cid, period)).fetchone())
        self.assertEqual(st.opening_balance(self.cid, previous, 'RMB'), 110)
        st.clear_entries(self.cid)
        self.assertIsNone(st.conn.execute('SELECT 1 FROM customer_opening_rmb WHERE chat_id=?', (self.cid,)).fetchone())
        self.assertEqual(st.opening_balance(-10011, period, 'RMB'), 110)

    def test_deposit_uses_rmb_balance_and_group_rate_to_value_u_collateral(self):
        self.save('deposits', {str(self.cid): '100'})
        self.features(deposit_notice=True)
        self.record('+2000/11')
        self.record('下发21000')
        self.assertTrue(any('未下发 1000.00RMB' in kw.get('text', '') for _, kw in self.calls))

    def mode(self, currency):
        self.bot['ledger']['group_currency'][str(self.cid)] = currency
        self.r.customer.sync()

    def test_switch_mode_keeps_original_calculations_categories_and_miniapp_units(self):
        self.mode('CNY')
        self.record('+100/6.67')
        self.mode('U')
        self.record('+100/6.67')
        st = self.r.store
        original = [tuple(r) for r in st.conn.execute('SELECT * FROM entries WHERE chat_id=?', (self.cid,))]
        for mode in ('U', 'CNY'):
            self.mode(mode)
            for view in ('compact', 'detailed'):
                st.set_ledger_view_mode(self.cid, view)
                bill = C.format_bill(st, self.cid)
                self.assertIn('100/6.67=14.99U', bill)
                self.assertIn('100U × 6.67 = 667RMB', bill)
                self.assertIn('总入款金额：100RMB ＋ 100U', bill)
                self.assertIn('应下发：14.99U ＋ 667RMB', bill)
                self.assertNotIn('114.99', bill)
                self.assertNotIn('767RMB', bill)
                if view == 'detailed':
                    recent = bill.split('最近流水：', 1)[1]
                    self.assertIn('：+14.99 U 老板', recent)
                    self.assertIn('：+667 RMB 老板', recent)
                    self.assertNotIn('→', recent)
                    self.assertIn('老板(2笔) 100RMB ＋ 100U | 14.99U ＋ 667RMB', bill)
            stats = miniapp.statistics(self.bot, 111, {'chat_id': self.cid})
            self.assertEqual([(s['unit'], Decimal(s['payable'])) for s in stats['summary']['by_currency']], [('U', Decimal('14.99')), ('RMB', Decimal('667'))])
            self.assertEqual([e['settlement_unit'] for e in stats['entries']], ['U', 'RMB'])
            self.assertEqual(stats['summary'], miniapp.bill_query(self.bot, 111, {'chat_id': self.cid})['bills'][0]['summary'])
        self.assertEqual(original, [tuple(r) for r in st.conn.execute('SELECT * FROM entries WHERE chat_id=?', (self.cid,))])
        with __import__('contextlib').closing(LedgerStore(st.path)) as reopened:
            self.assertEqual([e.settlement_unit for e in reopened.entries(self.cid)], ['U', 'RMB'])

    def test_mixed_payouts_reversals_and_reply_edit_preserve_original_mode(self):
        self.mode('CNY')
        income = self.record('+100/6.67')
        self.record('下发5')
        self.mode('U')
        self.record('+100/6.67')
        self.record('下发100')
        self.record('下发-10')
        self.features(edit_confirm=False)
        self.record('设置汇率20')
        self.record('+200', {'message_id': income, 'text': '+100/6.67', 'from': {'id': 111}})
        old = self.r.store.entries(self.cid)[0]
        self.assertEqual((old.currency, old.rate, old.settlement_unit), ('CNY', Decimal('6.67'), 'U'))
        bill = C.format_bill(self.r.store, self.cid)
        self.assertIn('应下发：29.99U ＋ 667RMB', bill)
        self.assertIn('已下发：5U ＋ 90RMB', bill)
        self.assertIn('24.99U ＋ 577RMB', bill)
        report = C.format_owner_statistics(self.r.store, [(self.cid, '混合群')])
        self.assertIn('加分 +29.99 U ｜ 下发 -5 U', report)
        self.assertIn('加分 +667 RMB ｜ 下发 -90 RMB', report)

    def test_legacy_modes_recover_from_original_currency_in_retained_records(self):
        self.mode('CNY')
        self.record('+100/6.67')
        self.record('+100u/6.67')
        st = self.r.store
        with st.conn:
            st.conn.execute("UPDATE entries SET settlement_unit='' WHERE chat_id=?", (self.cid,))
        for mode in ('CNY', 'U'):
            self.mode(mode)
            self.assertIn('应下发：14.99U ＋ 667RMB', C.format_bill(st, self.cid))
        before = miniapp.statistics(self.bot, 111, {'chat_id': self.cid})['summary']
        st.clear_entries(self.cid)
        after = miniapp.statistics(self.bot, 111, {'chat_id': self.cid})
        self.assertEqual(after['summary'], before)
        self.assertTrue(all(e['cleared'] for e in after['entries']))
        # Upgrade both tables together so the old INSERT ... SELECT * clear path remains compatible.
        with st.conn:
            st.conn.execute('ALTER TABLE entries DROP COLUMN settlement_unit')
            st.conn.execute('ALTER TABLE customer_month_entries DROP COLUMN settlement_unit')
        st.init_schema()
        self.assertEqual(miniapp.statistics(self.bot, 111, {'chat_id': self.cid})['summary'], before)
        self.record('+1')
        st.clear_entries(self.cid)

    def test_mixed_month_carry_is_separate_and_deleted_with_its_period(self):
        self.mode('CNY')
        self.record('+100/6.67')
        self.mode('U')
        self.record('+100/6.67')
        st = self.r.store
        period = st.current_accounting_date(self.cid)
        with st.conn:
            st.conn.execute("UPDATE entries SET created_at='2000-01-01T00:00:00+00:00' WHERE chat_id=?", (self.cid,))
        st.prune_month()
        self.mode('CNY')
        st.prune_month()
        self.assertEqual(st.settlement_openings(self.cid, period), {'U': Decimal('14.99'), 'RMB': Decimal('667')})
        self.assertIn('月初结转余额：14.99U ＋ 667RMB', C.format_bill(st, self.cid))
        miniapp.action(self.mgr, self.bot, 111, 'deletebill', dict(chat_id=self.cid, period=period, confirm=True))
        self.assertEqual(st.settlement_openings(self.cid, period), {})
        self.assertIsNone(st.conn.execute('SELECT 1 FROM customer_opening_units WHERE chat_id=? AND period=?', (self.cid, period)).fetchone())

    def test_reply_payout_retains_four_decimal_rate_after_mode_change(self):
        self.record('设置汇率6.6789')
        self.record('+100')
        mid = self.record('下发1u')
        self.features(edit_confirm=False)
        self.mode('CNY')
        self.record('设置汇率20')
        self.record('下发2', {'message_id': mid, 'text': '下发1u', 'from': {'id': 111}})
        entry = self.r.store.entries(self.cid)[-1]
        self.assertEqual((entry.currency, entry.rate, entry.settlement_unit), ('U', Decimal('6.6789'), 'RMB'))
        self.assertIn('已下发：13.36RMB', C.format_bill(self.r.store, self.cid))

    def test_cutoff_does_not_offset_different_settlement_currencies(self):
        self.mode('CNY')
        self.record('下发5')
        self.mode('U')
        self.record('+100/6.67')
        st = self.r.store
        period = st.current_accounting_date(self.cid)
        now = datetime.now(reminders.LEDGER_TZ).replace(hour=10, minute=0, second=0)
        with st.conn:
            st.conn.execute('DELETE FROM ledger_reminder_schedule')
        reminders.initialize_schedule(st, now-timedelta(days=1))
        calls = []
        api = SimpleNamespace(call=lambda method, **kw: calls.append(kw) or {'message_id': 1})
        with patch.object(st, 'list_active_bot_groups', return_value=[{'chat_id': self.cid}]), \
             patch.object(st, 'latest_closed_period', return_value=(period, now)):
            self.assertEqual(reminders.send_due_reminders(api, st, now), 1)
            self.assertEqual(reminders.send_due_reminders(api, st, now), 0)
        self.assertIn('5.00U', calls[0]['text'])
        self.assertNotIn('RMB', calls[0]['text'])


if __name__ == '__main__':
    __import__('unittest').main()
