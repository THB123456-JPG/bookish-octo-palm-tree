"""Isolated HTTP security, onboarding, config and original business regressions."""
import hashlib
import hmac
import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import core
import miniapp
from panel import PanelHandler
from runners.ledger.commands import Actor, handle_text
from runners.ledger.storage import LedgerStore


def signed(token, uid, age=0, **extra):
    fields = {'auth_date': str(int(time.time())-age),
              'user': json.dumps({'id': uid, 'first_name': 'Test'}, separators=(',', ':'))}
    fields.update(extra)
    check = '\n'.join('%s=%s' % x for x in sorted(fields.items()))
    secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
    fields['hash'] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


class Manager:
    def __init__(self):
        self.cfg = {'miniapp_base_url': 'https://example.test'}
        self.lock = threading.RLock()
        self.runners = {}
        self.bots = [
            {'id': 'ledger1', 'type': 'ledger', 'token': '111:FAKE_LEDGER', 'owner_id': 111,
             'admin_ids': [111, 444], 'username': 'example_ledger', 'bind_code': 'FAKECODE'},
            {'id': 'ledger2', 'type': 'ledger', 'token': '222:FAKE_OTHER', 'owner_id': 333,
             'admin_ids': [333]},
            {'id': 'shop1', 'type': 'shop', 'token': '333:FAKE_SHOP', 'owner_id': 111,
             'admin_ids': [111, 444], 'shop': {'provider_key': 'FAKE_UPSTREAM', 'premium_key': 'FAKE_PREMIUM'}},
            {'id': 'newbot', 'type': 'ledger', 'token': '444:FAKE_NEW', 'admin_ids': [],
             'bind_code': 'ACTIVATE_TEST'}]

    def find(self, bid):
        return next((b for b in self.bots if b['id']==bid), None)

    def save(self):
        core.save_json(core.BOTS_FILE, self.bots)

    def is_expired(self, bot):
        return bool(bot.get('expire_at') and bot['expire_at'] < time.time())


def setup_fixture(folder):
    core.set_base_dir(folder)
    Path(core.DATA_DIR).mkdir(exist_ok=True)
    mgr = Manager()
    with __import__('contextlib').closing(LedgerStore(Path(core.DATA_DIR)/'ledger1.sqlite3')) as st:
        for cid, title in [(-10011, '有权限测试群'), (-10022, '无权限测试群')]:
            st.ensure_chat(cid)
            st.remember_bot_chat(cid, title, 'supergroup')
            st.set_chat_owner(cid, 111)
            st.remember_user(cid, 222, 'operator', '<script>alert(1)</script>')
        st.add_operator(-10011, 222, 'operator', '测试操作员', 111)
        handle_text(st, -10011, Actor(111, 'boss', '所有者'), '+1000/9 业务回归', {111})
    PanelHandler.mgr = mgr
    return mgr


class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='miniapp-test-')
        cls.mgr = setup_fixture(cls.tmp.name)
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), PanelHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = 'http://127.0.0.1:%s' % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.tmp.cleanup()

    def call(self, bid='ledger1', uid=111, action='overview', payload=None, raw=None):
        b = self.mgr.find(bid)
        body = json.dumps({'init_data': signed(b['token'], uid) if raw is None else raw,
                           'payload': payload or {}}).encode()
        req = Request(self.url+'/api/miniapp/'+bid+'/'+action, body,
                      {'Content-Type': 'application/json'})
        try:
            response = urlopen(req)
        except HTTPError as e:
            response = e
        with response:
            return response.status, json.load(response)

    def test_authentication(self):
        self.assertEqual(self.call(raw='')[0], 400)
        raw = signed(self.mgr.find('ledger1')['token'], 111)
        self.assertEqual(self.call(raw=raw.replace('111', '999'))[0], 400)
        self.assertEqual(self.call(bid='ledger2', raw=raw)[0], 400)
        self.assertEqual(self.call(raw=signed('111:FAKE_LEDGER', 111, age=3602))[0], 400)
        self.assertEqual(self.call(raw=signed('111:FAKE_LEDGER', 111, age=-120))[0], 400)
        self.assertEqual(self.call(raw=raw+'&user=x')[0], 400)
        self.assertEqual(self.call(raw=signed('111:FAKE_LEDGER', True))[0], 400)
        # Telegram's optional signature is part of the token-based HMAC check string.
        self.assertEqual(self.call(raw=signed('111:FAKE_LEDGER', 111, signature='test-signature'))[0], 200)

    def test_owner_scope_and_read_only_overview(self):
        db = Path(core.DATA_DIR)/'ledger1.sqlite3'
        before = hashlib.sha256(db.read_bytes()).hexdigest()
        status, body = self.call()
        self.assertEqual(status, 200)
        self.assertEqual(len(body['data']['groups']), 2)
        self.assertEqual(hashlib.sha256(db.read_bytes()).hexdigest(), before)
        self.assertNotIn('token', body['data'])
        self.assertNotIn('bind_code', body['data'])
        self.assertEqual(self.call(uid=999)[0], 200)
        self.assertEqual(self.call(bid='shop1', uid=999)[0], 403)

    def test_group_permissions(self):
        status, body = self.call(uid=222)
        self.assertEqual(status, 200)
        self.assertEqual([g['chat_id'] for g in body['data']['groups']], [-10011])
        self.assertEqual(self.call(uid=222, action='group', payload={
            'chat_id': -10022, 'settings': {'rate': '2'}})[0], 403)
        self.assertEqual(self.call(uid=222, action='welcome', payload={'newbie_welcome': 'test'})[0], 403)
        self.assertEqual(self.call(uid=222, action='staff', payload={'user_id': 999, 'add': True})[0], 403)
        self.assertEqual(self.call(uid=222, action='operator', payload={
            'chat_id': -10011, 'user_id': 999, 'add': True})[0], 403)

    def test_overview_counts_enabled_groups_and_distinct_group_operators(self):
        from contextlib import closing
        with closing(LedgerStore(Path(core.DATA_DIR)/'ledger1.sqlite3', initialize=False)) as st:
            previous = st.is_ledger_enabled(-10022)
            st.set_ledger_enabled(-10022, False)
            st.add_operator(-10022, 222, '', '跨群操作员', 111)
            st.add_operator(-10022, 777, '', '另一个操作员', 111)
            st.ensure_chat(-10033)
            st.remember_bot_chat(-10033, '已退出测试群', 'supergroup')
            st.add_operator(-10033, 888, '', '已退出群的操作员', 111)
            st.deactivate_bot_chat(-10033)
        try:
            data = self.call()[1]['data']
            self.assertEqual(data['enabled_group_count'], 1)
            self.assertEqual(data['operator_count'], 2)
            operator = self.call(uid=777)[1]['data']
            self.assertEqual(operator['enabled_group_count'], 0)
            self.assertEqual(operator['operator_count'], 2)
            self.assertEqual(self.call(action='operator', payload={
                'chat_id': -10022, 'user_id': 777, 'add': False})[0], 200)
            self.assertEqual(self.call()[1]['data']['operator_count'], 1)
            self.assertEqual(self.call(uid=777)[0], 200)
        finally:
            with closing(LedgerStore(Path(core.DATA_DIR)/'ledger1.sqlite3', initialize=False)) as st:
                st.set_ledger_enabled(-10022, previous)
                st.remove_operator(-10022, 222)
                st.remove_operator(-10022, 777)

    def test_remaining_time_uses_server_expiry(self):
        from unittest.mock import patch
        bot = self.mgr.find('ledger1')
        previous = bot.get('expire_at', 0)
        try:
            with patch('miniapp.time.time', return_value=1800000000):
                for expiry, remaining in [(0, None), (1800090061, 90061), (1799999999, 0)]:
                    bot['expire_at'] = expiry
                    data = miniapp.overview(self.mgr, bot, 111)
                    self.assertEqual(data['server_time'], 1800000000)
                    self.assertEqual(data['expire_at'], expiry)
                    self.assertEqual(data['remaining_seconds'], remaining)
        finally:
            bot['expire_at'] = previous

    def test_configuration_preserves_money(self):
        from contextlib import closing
        with closing(LedgerStore(Path(core.DATA_DIR)/'ledger1.sqlite3', initialize=False)) as st:
            before = st.summary(-10011)
            original = [tuple(r) for r in st.conn.execute('SELECT * FROM entries')]
        status, _ = self.call(action='group', payload={'chat_id': -10011, 'settings': {
            'rate': '7.3', 'fee_percent': '3', 'ledger_reset_hour': 0,
            'ledger_view_mode': 'compact', 'ledger_enabled': True}})
        self.assertEqual(status, 200)
        with closing(LedgerStore(Path(core.DATA_DIR)/'ledger1.sqlite3', initialize=False)) as st:
            after = st.summary(-10011)
            self.assertEqual(after.balance_usdt, before.balance_usdt)
            self.assertEqual([tuple(r) for r in st.conn.execute('SELECT * FROM entries')], original)
            settings = st.get_settings(-10011)
        self.assertEqual(self.call(action='group', payload={'chat_id': -10011,
            'settings': {'rate': '8', 'fee_percent': '-1'}})[0], 400)
        self.assertEqual(self.call(action='group', payload={'chat_id': -10011,
            'settings': {'ledger_reset_hour': '1.5'}})[0], 400)
        with closing(LedgerStore(Path(core.DATA_DIR)/'ledger1.sqlite3', initialize=False)) as st:
            self.assertEqual(st.get_settings(-10011), settings)
        for val in ('NaN', 'Infinity', True, '0'):
            self.assertEqual(self.call(action='group', payload={'chat_id': -10011,
                'settings': {'rate': val}})[0], 400)

    def test_cutoff_switch_and_other_saves_preserve_disabled_period(self):
        from contextlib import closing
        def state():
            with closing(LedgerStore(Path(core.DATA_DIR)/'ledger1.sqlite3', initialize=False)) as st:
                return st.is_ledger_cutoff_enabled(-10011), st.current_accounting_date(-10011), st.entries(-10011)
        original = state()
        self.assertEqual(self.call(uid=333, action='group', payload={'chat_id': -10011,
            'settings': {'ledger_cutoff_enabled': False}})[0], 403)
        for settings in ({'ledger_cutoff_enabled': False},
                         {'ledger_cutoff_enabled': False, 'ledger_reset_hour': 7, 'rate': '7.3'},
                         {'ledger_reset_hour': 8}):
            self.assertEqual(self.call(action='group', payload={'chat_id': -10011, 'settings': settings})[0], 200)
            enabled, period, entries = state()
            self.assertFalse(enabled)
            self.assertEqual(period, original[1])
            self.assertEqual(entries, original[2])
        status, body = self.call()
        self.assertEqual(status, 200)
        self.assertFalse(next(g for g in body['data']['groups'] if g['chat_id']==-10011)['ledger_cutoff_enabled'])
        self.assertEqual(self.call(action='group', payload={'chat_id': -10011,
            'settings': {'ledger_cutoff_enabled': 'false'}})[0], 400)
        self.assertEqual(self.call(action='group', payload={'chat_id': -10011,
            'settings': {'ledger_cutoff_enabled': True, 'ledger_reset_hour': 3}})[0], 200)
        self.assertTrue(state()[0])
        self.assertEqual(state()[1:], original[1:])

    def test_personnel_and_welcome(self):
        self.assertEqual(self.call(action='staff', payload={'user_id': 555, 'name': '测试', 'add': True})[0], 200)
        self.assertIn(555, self.mgr.find('ledger1')['admin_ids'])
        self.assertEqual(self.call(action='staff', payload={'user_id': 555, 'add': False})[0], 200)
        self.assertEqual(self.call(action='staff', payload={'user_id': 111, 'add': False})[0], 400)
        self.assertEqual(self.call(action='operator', payload={'chat_id': -10022, 'user_id': 555, 'add': True})[0], 200)
        self.assertEqual(self.call(uid=555)[0], 200)
        self.assertEqual(self.call(action='operator', payload={'chat_id': -10022, 'user_id': 555, 'add': False})[0], 200)
        self.assertEqual(self.call(uid=555)[0], 200)
        for value in ('欢迎 {name}', '', None):
            self.assertEqual(self.call(action='welcome', payload={'newbie_welcome': value})[0], 200)
            self.assertEqual(self.mgr.find('ledger1')['ledger']['newbie_welcome'], value)

    def test_shop_configuration_and_secrets(self):
        status, body = self.call(bid='shop1')
        self.assertEqual(status, 200)
        self.assertNotIn('FAKE_UPSTREAM', json.dumps(body))
        self.assertNotIn('FAKE_PREMIUM', json.dumps(body))
        self.assertEqual(self.call(bid='shop1', uid=444, action='shop', payload={'price': 4})[0], 403)
        p = {'trx_own': 'TJRabPrwbZy45sbavfcjinPJC18kjpRTv8', 'price': 4,
             'provider_key': '', 'premium_key': '', 'auto_type': 0}
        status, body = self.call(bid='shop1', action='shop', payload=p)
        self.assertEqual(status, 200)
        self.assertTrue(body['data']['baseline_reset'])
        b = self.mgr.find('shop1')
        self.assertEqual(b['shop']['provider_key'], 'FAKE_UPSTREAM')
        self.assertEqual(b['shop']['premium_key'], 'FAKE_PREMIUM')
        runtime = core.load_json(Path(core.DATA_DIR)/'shop1.json', {})
        self.assertEqual(runtime['scan'], {'baseline': False, 'seen': [], 'last_ts': 0})
        for patch in ({'trx_own': 'bad'}, {'price': -1}, {'energy': 1.2},
                      {'custom_url': 'http://127.0.0.1/private'}, {'min_units': 11, 'max': 10},
                      {'provider': 'bad'}, {'premium_prices': {'1': 9}}, {'token': 'bad'}):
            self.assertEqual(self.call(bid='shop1', action='shop', payload=patch)[0], 400)

    def test_activation_and_navigation(self):
        self.assertEqual(self.call(bid='newbot')[0], 403)
        bot = self.mgr.find('newbot')
        r = core.BaseRunner(self.mgr, bot)
        sent = []
        calls = []
        r.send = lambda cid, message, **kw: sent.append((cid, message, kw))
        r.api.call = lambda method, **kw: calls.append((method, kw))
        r.cmd_admin(111, {'id': 111, 'first_name': 'Client'}, '/admin ACTIVATE_TEST')
        self.assertEqual(bot['owner_id'], 111)
        self.assertTrue(any('web_app' in str(m) for _, _, m in sent))
        self.assertEqual(self.call(bid='newbot')[0], 200)
        bot['expire_at'] = int(time.time())-10
        self.assertEqual(self.call(bid='newbot')[0], 403)
        bot.pop('expire_at')
        import customer_ui
        r.expired = lambda: False
        self.assertFalse(customer_ui.handle(r, {'message': {'chat': {'type': 'supergroup'},
            'from': {'id': 111}, 'text': '+1000/9'}}))
        r.bot['type'] = 'usdt'
        self.assertFalse(customer_ui.supported(r))

    def test_public_page_and_bad_requests(self):
        with urlopen(self.url+'/miniapp/ledger1') as response:
            self.assertEqual(response.status, 200)
            self.assertIn('no-store', response.headers['Cache-Control'])
            self.assertNotIn('FAKE_LEDGER', response.read().decode())
        req = Request(self.url+'/api/miniapp/ledger1/overview', b'[]', {'Content-Type': 'application/json'})
        with self.assertRaises(HTTPError) as ctx:
            urlopen(req)
        self.assertEqual(ctx.exception.code, 400)


if __name__ == '__main__':
    unittest.main(verbosity=2)
