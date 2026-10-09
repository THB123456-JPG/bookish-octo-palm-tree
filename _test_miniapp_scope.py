"""Role-scoped responses, write boundaries and lightweight overview with fake data."""
from contextlib import closing
import tempfile
import unittest
from unittest.mock import patch

import core
import customer_config as CC
import miniapp
from _test_miniapp import setup_fixture
from runners.ledger.storage import LedgerStore


class ScopeTests(unittest.TestCase):
    def setUp(self):
        self.previous = core.BASE_DIR
        self.tmp = tempfile.TemporaryDirectory(prefix='miniapp-scope-')
        self.mgr = setup_fixture(self.tmp.name)
        self.bot = self.mgr.find('ledger1')
        self.bot['ledger'] = {'staff_grants': {
            str(uid): dict(manage=manage, global_ops=ops, broadcast=broadcast, groups=groups)
            for uid, manage, ops, broadcast, groups in [
                (555, True, False, False, []), (556, False, True, False, []),
                (557, False, False, False, [-10011]), (558, False, False, True, []),
                (559, True, False, True, [-10011])]}}
        with closing(LedgerStore(miniapp.ledger_path(self.bot), initialize=False)) as store:
            store.set_user_setting(-10011, 557, 'rate', '7')
            store.set_user_setting(-10022, 557, 'rate', '9')
            self.before = [tuple(r) for r in store.conn.execute('SELECT * FROM entries')]

    def tearDown(self):
        with closing(LedgerStore(miniapp.ledger_path(self.bot), initialize=False)) as store:
            self.assertEqual([tuple(r) for r in store.conn.execute('SELECT * FROM entries')], self.before)
        self.tmp.cleanup()
        core.set_base_dir(self.previous)

    def action(self, uid, name, **payload):
        return miniapp.action(self.mgr, self.bot, uid, name, payload)

    def test_role_matrix_filters_config_groups_and_global_identity(self):
        for uid, groups, manage, broadcast, owner in [
            (111, 2, True, True, True), (555, 0, True, False, False),
            (556, 2, False, False, False), (557, 1, False, False, False),
            (558, 0, False, True, False), (559, 1, True, True, False)]:
            with self.subTest(uid=uid):
                data = self.action(uid, 'overview')
                self.assertEqual(len(data['groups']), groups)
                self.assertEqual('basics' in data['customer'], manage)
                self.assertEqual('ads' in data['customer'], manage or broadcast)
                self.assertEqual(bool(data['staff']), owner)
                self.assertEqual('enabled_ad_count' in data, manage or broadcast)
                self.assertEqual('enabled_reply_count' in data, manage or broadcast)
                self.assertEqual(data['tron']['can_bind'], owner)
                self.assertNotIn('entries', data)
                self.assertNotIn('stats', data)
                for person in data['permission_users']:
                    if not owner and person['user_id'] != uid:
                        self.assertFalse(person['owner'])
                        self.assertFalse(any(person['grants'][key] for key in ('manage', 'global_ops', 'broadcast')))
                        self.assertTrue(set(person['group_ids']) <= {g['chat_id'] for g in data['groups']})
        data = self.action(557, 'overview')
        self.assertEqual([u['user_id'] for u in data['permission_users']], [557])
        self.assertEqual([p['chat_id'] for p in data['permission_users'][0]['pricing']], [-10011])
        self.assertFalse(data['groups'][0]['can_manage_operators'])
        self.assertEqual(data['customer'], {'revision': 0})
        self.assertEqual(data['message_groups'], [])

    def test_single_group_cannot_bypass_ui_by_calling_write_apis(self):
        for name, payload in [
            ('staff', dict(user_id=999, add=True)), ('welcome', dict(newbie_welcome='bad')),
            ('tronkeys', dict(keys=[])), ('welcomephoto', dict(mode='remove', revision=0)),
            ('customer', dict(section='features', revision=0, value={})),
            ('customer', dict(section='ads', revision=0, value=[])),
            ('operator', dict(chat_id=-10011, user_id=999, add=True)),
            ('group', dict(chat_id=-10011, settings={'all_members_can_record': True})),
            ('group', dict(chat_id=-10022, settings={'rate': '10'})),
            ('pricing', dict(chat_id=-10011, user_id=111, settings={'rate': '10', 'fee_percent': ''})),
            ('bills', dict(chat_id=-10022)), ('statistics', dict(chat_id=-10022))]:
            with self.subTest(name=name, payload=payload), self.assertRaises(PermissionError):
                self.action(557, name, **payload)
        self.action(557, 'pricing', chat_id=-10011, user_id=557, settings={'rate': '8', 'fee_percent': '1'})
        self.action(557, 'group', chat_id=-10011, settings={'rate': '2'})
        with closing(LedgerStore(miniapp.ledger_path(self.bot), initialize=False)) as store:
            self.assertEqual(str(store.get_user_settings(-10011, 557)[0]), '8.0000')
        with self.assertRaises(PermissionError):
            self.action(111, 'pricing', chat_id=-10011, user_id=999999, settings={'rate': '8', 'fee_percent': ''})

    def test_legacy_group_binder_has_preview_only_and_cannot_modify(self):
        with closing(LedgerStore(miniapp.ledger_path(self.bot), initialize=False)) as store:
            store.set_chat_owner(-10011, 777, replace=True)
        data = self.action(777, 'overview')
        self.assertFalse(data['is_owner'])
        self.assertEqual(data['groups'], [])
        self.assertEqual(data['staff'], [])
        self.assertEqual(data['permission_users'], [])
        self.assertEqual(data['customer'], {'revision': 0})
        self.assertEqual(data['feature_fields'], CC.FEATURES)
        self.assertEqual(data['preview_basics']['payout_address'], '')
        self.assertEqual(self.action(777, 'bills')['bills'], [])
        self.assertEqual(self.action(777, 'statistics')['groups'], [])
        for name, payload in [('operator', dict(chat_id=-10011, user_id=999, add=True)),
                              ('group', dict(chat_id=-10011, settings={'rate': '2'})),
                              ('pricing', dict(chat_id=-10011, user_id=777, settings={'rate': '2', 'fee_percent': ''})),
                              ('customer', dict(section='basics', revision=0, value={})),
                              ('customer', dict(section='ads', revision=0, value=[])),
                              ('staff', dict(user_id=999, add=True))]:
            with self.subTest(name=name), self.assertRaises(PermissionError):
                self.action(777, name, **payload)

    def test_no_database_visitor_can_view_catalog_and_empty_lists(self):
        bot = self.mgr.find('ledger2')
        data = miniapp.action(self.mgr, bot, 999, 'overview', {})
        self.assertEqual(data['groups'], [])
        self.assertEqual(data['feature_fields'], CC.FEATURES)
        self.assertEqual(miniapp.action(self.mgr, bot, 999, 'bills', {})['bills'], [])
        self.assertEqual(miniapp.action(self.mgr, bot, 999, 'statistics', {})['groups'], [])
        with self.assertRaises(ValueError):
            self.action(111, 'group', chat_id=-10011, settings={'all_members_can_record': True})

    def test_overview_does_not_load_financial_history_and_batches_pricing(self):
        queries = []
        original = miniapp.connect

        def tracked(bot):
            connection = original(bot)
            connection.set_trace_callback(queries.append)
            return connection

        with patch('miniapp.connect', side_effect=tracked):
            data = self.action(111, 'overview')
        self.assertTrue(data['permission_users'])
        self.assertFalse(any('SELECT * FROM entries' in q for q in queries), queries)
        self.assertEqual(sum('FROM user_pricing' in q for q in queries), 1)
        self.assertEqual(sum(q == 'SELECT 1 FROM entries LIMIT 1' for q in queries), 1)

    def test_feature_checks_do_not_copy_rules_and_keep_live_config_changes(self):
        self.bot['ledger']['customer'] = {'features': {'phone': False}, 'ads': [{'image': 'fake-image'*1000}]}
        with patch('customer_config.deepcopy', side_effect=AssertionError('No full settings copy needed')):
            self.assertFalse(CC.feature(self.bot, 'phone'))
            self.assertTrue(CC.feature(self.bot, 'bill_switch'))
            self.bot['ledger']['customer']['features']['phone'] = True
            self.assertTrue(CC.feature(self.bot, 'phone'))
            with self.assertRaises(KeyError):
                CC.feature(self.bot, 'invalid-feature')

    def test_group_refresh_does_not_make_unused_permission_requests(self):
        from types import SimpleNamespace
        from runners.ledger import LedgerRunner, group_admin
        runner = LedgerRunner(self.mgr, self.bot)
        calls = []
        runner.api = SimpleNamespace(call=lambda method, **kw: calls.append(method) or
                                     dict(id=kw['chat_id'], type='supergroup', title='Fake group'))
        try:
            with patch.object(group_admin, 'check_permission', side_effect=AssertionError('Unused request')):
                runner.refresh_groups()
            self.assertEqual(calls, ['getChat', 'getChat'])
            self.assertEqual(len(runner.store.list_active_bot_groups()), 2)
        finally:
            runner.close_db()

    def test_u_details_can_be_aggregated_without_recomputing_snapshots(self):
        with closing(LedgerStore(miniapp.ledger_path(self.bot), initialize=False)) as store:
            store.add_entry(-10011, 'income', '100', 'U', '', 111, 'Owner', rate='7')
            self.before = [tuple(r) for r in store.conn.execute('SELECT * FROM entries')]
        month, today = miniapp.month_bounds()
        data = self.action(111, 'statistics', chat_id=-10011, start=month, end=today)
        self.assertEqual(miniapp.monetary(data['entries']), data['summary'])


if __name__ == '__main__':
    unittest.main()
