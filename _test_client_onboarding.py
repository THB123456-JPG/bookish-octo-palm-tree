"""New clients use shared ledger features with isolated ownership and data."""
import copy
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import core
import customer_config as CC
import customer_ui
import miniapp
from manager import BotManager
from panel import PanelHandler
from runners.ledger import LedgerRunner
from _test_miniapp import signed


class ClientOnboardingTests(unittest.TestCase):
    def test_panel_add_activate_configure_and_account_for_two_new_clients(self):
        original_base = core.BASE_DIR
        with tempfile.TemporaryDirectory() as folder:
            core.set_base_dir(folder)
            Path(core.DATA_DIR).mkdir(exist_ok=True)
            mgr = BotManager({'miniapp_base_url': 'https://example.test'})
            class Handler(PanelHandler):
                password = 'FAKE_PANEL_PASSWORD'
                merch = None
                token_secret = 'FAKE_SIGNATURE_SECRET'
            Handler.mgr = mgr
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = 'http://127.0.0.1:%s' % server.server_port

            def post(path, body):
                req = Request(url + path, json.dumps(body).encode(),
                    {'Content-Type': 'application/json', 'X-Panel-Pass': Handler.password})
                with urlopen(req, timeout=5) as response:
                    return json.load(response)

            def overview(bot, uid):
                return post('/api/miniapp/%s/overview' % bot['id'],
                            {'init_data': signed(bot['token'], uid), 'payload': {}})

            try:
                clients = []
                with patch.object(LedgerRunner, 'start') as start, patch('manager.make_runner', side_effect=LedgerRunner), patch('core.TgAPI.call',
                        side_effect=[{'id': i, 'username': 'mock_client_%s_bot' % i, 'first_name': 'Client'} for i in (1, 2)]):
                    for i, uid in enumerate((1111, 2222), 1):
                        added = post('/api/bots', {'token': '%s:FAKE_CLIENT' % i,
                            'type': 'ledger', 'note': 'Client %s' % i, 'duration': 'forever'})
                        self.assertTrue(added['ok'])
                        bot = mgr.find(added['bot']['id'])
                        self.assertFalse(bot['remote'])
                        self.assertTrue(mgr.should_run(bot))
                        runner = mgr.runners[bot['id']]
                        self.assertIsInstance(runner, LedgerRunner)
                        calls = []
                        runner.api = SimpleNamespace(call=lambda method, _calls=calls, **kw:
                            _calls.append((method, kw)) or {'message_id': len(_calls)})
                        with self.assertRaises(HTTPError) as exc:
                            overview(bot, uid)
                        self.assertEqual(exc.exception.code, 403)
                        runner.handle({'message': {'message_id': 1,
                            'chat': {'id': uid, 'type': 'private'},
                            'from': {'id': uid, 'first_name': 'Client'},
                            'text': '/admin ' + added['bot']['bind_code']}})
                        self.assertEqual(bot['owner_id'], uid)
                        self.assertTrue(any('web_app' in str(kw) for _, kw in calls))
                        self.assertEqual(sum(method == 'setChatMenuButton' for method, _ in calls), 1)
                        view = overview(bot, uid)['data']
                        self.assertTrue(view['is_owner'])
                        self.assertTrue(all(view['permissions'][k] for k in ('manage', 'global_ops', 'broadcast')))
                        self.assertEqual(view['feature_fields'], [list(row) for row in CC.FEATURES])
                        self.assertEqual(view['customer'], CC.settings(bot))
                        calls.clear()
                        runner.handle({'message': {'message_id': 2,
                            'chat': {'id': uid, 'type': 'private'},
                            'from': {'id': uid}, 'text': '帮助'}})
                        sent = [kw for method, kw in calls if method == 'sendMessage']
                        self.assertEqual(len(sent), 1)
                        self.assertEqual([len(row) for row in sent[0]['reply_markup']['inline_keyboard']], [3, 3, 3, 1])
                        self.assertLess(len(sent[0]['text']), 200)
                        self.assertIn('/miniapp/' + bot['id'], customer_ui.app_url(runner))
                        clients.append((bot, runner, uid))
                    self.assertEqual(start.call_count, 2)

                first, second = clients
                self.assertEqual(CC.settings(first[0]), CC.settings(second[0]))
                untouched = copy.deepcopy(second[0])
                cfg = CC.settings(first[0])
                cfg['features']['join_notice'] = True
                miniapp.action(mgr, first[0], first[2], 'customer',
                    {'section': 'features', 'value': cfg['features'], 'revision': cfg['revision']})
                self.assertEqual(second[0], untouched)
                guest = overview(first[0], second[2])['data']
                self.assertEqual(guest['groups'], [])
                self.assertEqual(guest['staff'], [])
                self.assertNotIn('features', guest['customer'])
                for bot, runner, uid in clients:
                    cid = -uid
                    runner.handle({'message': {'message_id': 3,
                        'chat': {'id': cid, 'type': 'supergroup', 'title': 'Mock group'},
                        'from': {'id': uid, 'first_name': 'Client'}, 'text': '+100u'}})
                    self.assertEqual(len(runner.store.entries(cid)), 1)
                self.assertNotEqual(first[1].db_path, second[1].db_path)
                self.assertEqual(second[1].store.entries(-first[2]), [])
                self.assertEqual(first[1].store.entries(-second[2]), [])
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
                for runner in mgr.runners.values():
                    runner.close_db()
                core.set_base_dir(original_base)


if __name__ == '__main__':
    unittest.main()
