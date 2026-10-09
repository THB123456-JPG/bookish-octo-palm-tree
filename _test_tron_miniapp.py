"""Fake-network address queries, owner bindings and shared-key isolation."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import core
from _test_miniapp import setup_fixture, signed
from panel import PanelHandler
from runners.ledger.runner import LedgerRunner
from runners.ledger import tron_chain as tc

DEFAULTS = ['FAKE_PLATFORM_KEY_A', 'FAKE_PLATFORM_KEY_B', 'FAKE_PLATFORM_KEY_C']
ADDRESS = 'T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb'


class Tests(unittest.TestCase):
    def setUp(self):
        self.previous = core.BASE_DIR
        self.tmp = tempfile.TemporaryDirectory()
        self.mgr = setup_fixture(self.tmp.name)
        self.mgr.cfg['tron_api_keys'] = list(DEFAULTS)
        for bid in ('ledger1', 'ledger2'):
            self.mgr.runners[bid] = LedgerRunner(self.mgr, self.mgr.find(bid))
        self.runner = self.mgr.runners['ledger1']
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), PanelHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()
        core.set_base_dir(self.previous)

    def call(self, action, payload=None, uid=111, bid='ledger1'):
        bot = self.mgr.find(bid)
        req = Request('http://127.0.0.1:%s/api/miniapp/%s/%s' % (self.server.server_port, bid, action),
                      json.dumps(dict(init_data=signed(bot['token'], uid), payload=payload or {})).encode(),
                      {'Content-Type': 'application/json'})
        try:
            response = urlopen(req)
        except HTTPError as exc:
            response = exc
        with response:
            return response.status, json.load(response)

    def test_platform_rotation_cooldown_and_private_hint(self):
        watcher = self.runner.tronw
        self.assertEqual(set(watcher.next_key() for _ in range(6)), set(DEFAULTS))
        watcher.cool_key(DEFAULTS[0])
        self.assertEqual(set(watcher.next_key() for _ in range(6)), set(DEFAULTS[1:]))
        hint = ''.join(watcher.key_hint())
        expected = ('\n<b>💡 温馨提示</b>\n'
                    '为保证查询速度及稳定性，可以自行申请免费的 API Key，'
                    '在 TG 小程序「地址查询与密钥」中单独绑定使用。\n'
                    '机器人拥有者可在配置中心绑定，申请入口：'
                    '<a href="https://trongrid.io/">https://trongrid.io/</a>')
        self.assertEqual(hint, expected)
        self.assertEqual(''.join(watcher.key_hint(urgent=True)), expected)
        self.assertNotIn('还没绑定', hint)
        self.assertTrue(all(k not in hint for k in DEFAULTS))

    def test_owner_binding_is_persisted_and_other_bot_uses_defaults(self):
        own = ['FAKE_CUSTOM_KEY_A', 'FAKE_CUSTOM_KEY_B']
        self.runner.data['existing'] = 'retain'
        with patch.object(tc, 'probe', return_value=(True, 'fake validated')) as probe:
            status, result = self.call('tronkeys', dict(keys=own))
        self.assertEqual(status, 200)
        self.assertEqual(probe.call_count, 2)
        self.assertEqual(self.runner.tronw.keys(), own)
        self.assertEqual(self.mgr.runners['ledger2'].tronw.keys(), DEFAULTS)
        stored = json.loads(Path(self.runner.data_file).read_text(encoding='utf-8'))
        self.assertEqual(stored['tron_keys'], own)
        self.assertEqual(stored['existing'], 'retain')
        restarted = LedgerRunner(self.mgr, self.mgr.find('ledger1'))
        self.assertEqual(restarted.tronw.keys(), own)
        self.assertEqual(result['data']['source'], 'custom')
        self.assertTrue(all(k not in json.dumps(result) for k in own + DEFAULTS))
        self.assertEqual(self.call('overview')[1]['data']['tron']['custom_count'], 2)
        self.assertEqual(self.call('tronkeys', dict(keys=[]))[0], 200)
        self.assertEqual(self.runner.tronw.keys(), DEFAULTS)
        self.assertNotIn('tron_keys', self.runner.data)

    def test_non_owner_cannot_bind_or_clear(self):
        for uid in (222, 444, 333):
            with patch.object(tc, 'probe') as probe:
                for keys in (['FAKE_CUSTOM_KEY'], []):
                    self.assertEqual(self.call('tronkeys', dict(keys=keys), uid=uid)[0], 403)
                probe.assert_not_called()
        self.assertEqual(self.runner.tronw.keys(), DEFAULTS)

    def test_separate_bindings_append_deduplicate_and_survive_restart(self):
        own = ['FAKE_CUSTOM_KEY_%s' % i for i in range(5)]
        with patch.object(tc, 'probe', return_value=(True, 'fake validated')) as probe:
            for index, key in enumerate(own):
                status, result = self.call('tronkeys', dict(keys=[key]))
                self.assertEqual(status, 200)
                self.assertEqual(result['data']['custom_count'], index + 1)
                self.assertEqual(len(result['data']['masked_keys']), index + 1)
            self.assertEqual(self.call('tronkeys', dict(keys=[own[0], own[0]]))[0], 200)
            self.assertEqual(probe.call_count, 5)
            before = Path(self.runner.data_file).read_bytes()
            self.assertEqual(self.call('tronkeys', dict(keys=['FAKE_SIXTH_KEY']))[0], 400)
            self.assertEqual(probe.call_count, 5)
        self.assertEqual(Path(self.runner.data_file).read_bytes(), before)
        self.assertEqual(LedgerRunner(self.mgr, self.mgr.find('ledger1')).tronw.keys(), own)
        self.assertEqual(self.call('overview')[1]['data']['tron']['custom_count'], 5)
        self.assertEqual(self.mgr.runners['ledger2'].tronw.keys(), DEFAULTS)

    def test_concurrent_bindings_keep_both_additions_and_enforce_total_limit(self):
        barrier = threading.Barrier(2)
        def probe(key):
            barrier.wait(timeout=10)
            return True, 'fake validated'
        def bind(key, results):
            results.append(self.call('tronkeys', dict(keys=[key]))[0])
        for initial_count, expected in ((0, [200, 200]), (4, [200, 400])):
            self.runner.data['tron_keys'] = ['FAKE_OLD_KEY_%s' % i for i in range(initial_count)]
            results = []
            threads = [threading.Thread(target=bind, args=(key, results))
                       for key in ('FAKE_ADDED_KEY_A', 'FAKE_ADDED_KEY_B')]
            with patch.object(tc, 'probe', side_effect=probe):
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=15)
                    self.assertFalse(thread.is_alive())
            self.assertEqual(sorted(results), expected)
            stored = json.loads(Path(self.runner.data_file).read_text(encoding='utf-8'))['tron_keys']
            self.assertEqual(len(stored), initial_count + expected.count(200))
            self.assertEqual(stored, self.runner.tronw.custom_keys())

    def test_invalid_binding_retains_old_keys_without_echoing_secrets(self):
        self.runner.data['tron_keys'] = ['FAKE_OLD_CUSTOM_KEY']
        self.runner.save_data()
        original = Path(self.runner.data_file).read_bytes()
        with patch.object(tc, 'probe', side_effect=[(True, ''), (False, 'FAKE_BAD_KEY')]):
            status, result = self.call('tronkeys', dict(keys=['FAKE_NEW_KEY', 'FAKE_BAD_KEY']))
        self.assertEqual(status, 400)
        self.assertEqual(Path(self.runner.data_file).read_bytes(), original)
        self.assertEqual(self.runner.tronw.keys(), ['FAKE_OLD_CUSTOM_KEY'])
        self.assertNotIn('FAKE_BAD_KEY', json.dumps(result))
        with patch.object(tc, 'probe') as probe:
            for keys in ('secret', ['invalid key'], ['short'], ['FAKE_CUSTOM_KEY'] * 6):
                self.assertEqual(self.call('tronkeys', dict(keys=keys))[0], 400)
            probe.assert_not_called()

    def test_private_query_matches_bot_card_and_keeps_group_switches(self):
        self.mgr.find('ledger1')['ledger'] = {'customer': {'features': {
            'tron_balance': False, 'tron_details': False}}}
        used = []
        with ExitStack() as stack:
            stack.enter_context(patch.object(tc, 'chain_get', side_effect=lambda a, p, k: used.append(k) or []))
            stack.enter_context(patch.object(tc, 'recent', side_effect=lambda a, c, n, k: used.append(k) or []))
            stack.enter_context(patch.object(tc, 'balances', return_value=('TRX：1\nUSDT：2', {})))
            status, result = self.call('tronquery', dict(address=ADDRESS))
            self.assertEqual(status, 200)
            self.assertIn('USDT 余额：2', result['data']['html'])
            self.assertNotIn('温馨提示', result['data']['html'])
            self.assertEqual(len(used), 2)
            self.assertTrue(set(used) <= set(DEFAULTS))
            self.assertIn('温馨提示', self.runner.tronw.card(111, ADDRESS)[0])
            used.clear()
            self.runner.tronw.card(-10011, ADDRESS)
            self.assertEqual(used, [])
            self.assertEqual(self.call('tronquery', dict(address=ADDRESS), uid=222)[0], 200)
            self.assertEqual(self.call('tronquery', dict(address='T-invalid'))[0], 400)
            self.assertEqual(self.call('tronquery', dict(address=ADDRESS), uid=999)[0], 200)

    def test_alert_layout_sign_addresses_balances_and_buttons(self):
        watcher = self.runner.tronw
        other = 'TJJ1o4LztfS5appqHMUJtQeoVASiQr3ABS'
        watch = dict(buyer=222, address=ADDRESS, note='<客户地址>', detailed=0)
        tx = dict(coin='USDT', amount=300000000, timestamp=1791449415000,
                  **{'from': ADDRESS, 'to': other, 'hash': 'a' * 64})
        with patch.object(tc, 'chain_get', return_value=[{}]), \
                patch.object(tc, 'balances', return_value=('TRX：0\nUSDT：0', {})), \
                patch.object(self.runner, 'send_html', return_value={'message_id': 1}) as send:
            self.assertTrue(watcher.alert(watch, tx))
            text = send.call_args.args[1]
            self.assertTrue(text.startswith('📣 <b>&lt;客户地址&gt;</b>\n\n'))
            self.assertIn('交易金额：<b>- 300 USDT</b>\n交易类型：支出 ⬆️', text)
            self.assertIn('支付地址：<code>%s</code> ← 监控地址' % ADDRESS, text)
            self.assertIn('收款地址：<code>%s</code>\n' % other, text)
            self.assertIn('USDT余额：0\nTRX余额：0\n转账时间：' + tc.when(tx['timestamp']), text)
            buttons = send.call_args.kwargs['kb']['inline_keyboard']
            self.assertEqual([[b['text'] for b in row] for row in buttons], [['⚙️ 管理地址', '📊 账单统计']])
            self.assertEqual(buttons[0][0]['callback_data'], 'tw:d:' + ADDRESS)
            self.assertEqual(buttons[0][1]['callback_data'], 'tw:stats:' + ADDRESS)
            tx.update(coin='TRX', **{'from': other, 'to': ADDRESS})
            self.assertTrue(watcher.alert(watch, tx))
            text = send.call_args.args[1]
            self.assertIn('交易金额：<b>+ 300 TRX</b>\n交易类型：收入 ⬇️', text)
            self.assertIn('收款地址：<code>%s</code> ← 监控地址' % ADDRESS, text)

    def test_alert_failed_balance_is_unknown_and_send_failure_is_retryable(self):
        watch = dict(buyer=222, address=ADDRESS, note='', detailed=1)
        tx = dict(coin='USDT', amount=1, timestamp=1,
                  **{'from': ADDRESS, 'to': 'other', 'hash': 'b' * 64})
        with patch.object(tc, 'chain_get', side_effect=tc.ChainError('net', 'fake failure')), \
                patch.object(self.runner, 'send_html', return_value=None) as send:
            self.assertFalse(self.runner.tronw.alert(watch, tx))
            text = send.call_args.args[1]
            self.assertIn('USDT余额：暂未获取（不代表0）', text)
            self.assertIn('TRX余额：暂未获取（不代表0）', text)
            self.assertNotIn('USDT余额：0', text)

    def test_empty_account_is_not_reported_as_a_query_failure_or_zero_balance(self):
        self.assertEqual(tc.balances([], ADDRESS), ('未查询到已激活账户；请核对地址。', {}))
        with patch.object(tc, 'chain_get', return_value=[]), patch.object(tc, 'recent', return_value=[]):
            text, _ = self.runner.tronw.card(111, ADDRESS)
        self.assertIn('未查询到已激活账户', text)
        self.assertNotIn('余额暂时查不到', text)
        self.assertNotIn('USDT 余额：0', text)


if __name__ == '__main__':
    unittest.main()
