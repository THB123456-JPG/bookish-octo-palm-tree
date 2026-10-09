"""Extracted customer runtime -> signed HTTPS bridge -> original Mini App actions."""
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import zipfile

import core
import miniapp
import solo_pack
from _test_miniapp import setup_fixture, signed
from panel import PanelHandler


CLIENT = r'''
import importlib.util, json, sys
import core, relay, customer_config as CC
from runners.ledger import LedgerRunner
from runners.ledger.storage import LedgerStore
from runners.ledger.commands import Actor, handle_text
spec = importlib.util.spec_from_file_location('solo_main', '独立版.py')
entry = importlib.util.module_from_spec(spec); spec.loader.exec_module(entry)
cfg = core.load_json('config.json', {})
bot, state = entry.load_bot(cfg)
mgr = entry.MiniMgr(state, cfg); mgr.bot = bot
with __import__('contextlib').closing(LedgerStore(core.DATA_DIR + '/ledger1.sqlite3')) as st:
    for cid in (-10011, -10022):
        st.ensure_chat(cid); st.remember_bot_chat(cid, '客户服务器群', 'supergroup')
        st.set_chat_owner(cid, 111)
    st.add_operator(-10011, 222, 'operator', '单群操作员', 111)
    outcome = handle_text(st, -10011, Actor(111, 'owner', '拥有者'), '+321', {111})
    assert outcome.changed
runner = LedgerRunner(mgr, bot); mgr.runners[bot['id']] = runner
from runners.ledger import tron_chain
tron_chain.probe = lambda key: (True, '')  # No external query or credentials.
runner.tronw.card = lambda *args, **kw: ('客户服务器余额查询', None)
reporter = relay.Reporter(cfg['report'], bot['id'], bot['token'])
reporter.attach(runner)  # Configuration worker only; no Telegram poller.
print('READY', flush=True)
sys.stdin.readline()
reporter.stop()
if reporter.config_thread: reporter.config_thread.join(17)
loaded, _ = entry.load_bot(cfg)
assert loaded['ledger'] == bot['ledger']
assert loaded['admin_ids'] == bot['admin_ids']
assert loaded['admin_names'] == bot['admin_names']
assert loaded['owner_id'] == bot['owner_id']
fresh = LedgerRunner(entry.MiniMgr(state, cfg), loaded)
assert fresh.tronw.custom_keys() == ['CUSTOM_FAKE_QUERY_KEY']
assert fresh.tronw.keys() == ['CUSTOM_FAKE_QUERY_KEY']
assert fresh.mgr.cfg['miniapp_base_url'] == 'https://example.test'
assert fresh.mgr.find('unknown') is None
print('PERSISTED', flush=True)
'''


class SoloConfigTests(unittest.TestCase):
    def setUp(self):
        self.base = core.BASE_DIR
        self.tmp = tempfile.TemporaryDirectory(prefix='solo-config-')
        self.mgr = setup_fixture(self.tmp.name)
        self.bot = self.mgr.find('ledger1')
        self.bot['remote'] = True
        self.mgr.cfg.update(miniapp_footer_text='联系客户服务@Example',
                            tron_api_keys=['SHARED_FAKE_QUERY_KEY'], password='PANEL_SECRET_FAKE')
        PanelHandler.mgr = self.mgr
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), PanelHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:%s' % self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.tmp.cleanup()
        core.set_base_dir(self.base)

    def call(self, action='overview', payload=None, uid=111, raw=None):
        body = dict(init_data=raw or signed(self.bot['token'], uid), payload=payload or {})
        request = Request(self.url + '/api/miniapp/ledger1/' + action,
                          json.dumps(body).encode(), {'Content-Type': 'application/json'})
        try:
            response = urlopen(request, timeout=30)
        except HTTPError as response:
            return response.code, json.load(response)
        with response:
            return response.status, json.load(response)

    def test_extracted_package_config_permissions_images_keys_and_restart(self):
        with urlopen(self.url + '/api/bots/ledger1/solozip?url=' + self.url) as response:
            content = response.read()
        with tempfile.TemporaryDirectory(prefix='solo-client-') as folder:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                prefix = '记账独立版/'
                for filename in solo_pack.ROOT_FILES:
                    self.assertEqual(archive.read(prefix+filename).decode(), solo_pack._read(filename))
                cfg = json.loads(archive.read(prefix+'config.json'))
                self.assertEqual(cfg['owner_id'], 111)
                self.assertEqual(cfg['tron_api_keys'], ['SHARED_FAKE_QUERY_KEY'])
                self.assertNotIn('password', cfg)
                self.assertNotIn('instance_folder', cfg)
                self.assertFalse(any(n.endswith('.sqlite3') for n in archive.namelist()))
                archive.extractall(folder)
            cwd = Path(folder)/'记账独立版'
            checked = subprocess.run([sys.executable, '独立版.py', '--check'], cwd=cwd,
                                     capture_output=True, text=True, timeout=20)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            process = subprocess.Popen([sys.executable, '-u', '-c', CLIENT], cwd=cwd,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True, encoding='utf-8')
            try:
                while True:
                    line = process.stdout.readline()
                    if line.strip() == 'READY':
                        break
                    if not line:
                        self.fail(process.stderr.read())
                code, result = self.call()
                self.assertEqual(code, 200, result)
                view = result['data']
                self.assertEqual(view['footer_text'], self.mgr.cfg['miniapp_footer_text'])
                self.assertEqual(view['customer']['basics']['view_mode'], 'compact')
                self.assertEqual(view['tron']['default_count'], 1)
                code, result = self.call('bills', {'chat_id': -10011})
                self.assertEqual(code, 200, result)
                self.assertIn('321', json.dumps(result))  # Local customer ledger, not platform's 1000.
                self.assertNotIn('业务回归', json.dumps(result, ensure_ascii=False))
                code, result = self.call('overview', uid=222)
                self.assertEqual(code, 200, result)
                self.assertEqual([g['chat_id'] for g in result['data']['groups']], [-10011])
                self.assertEqual(self.call('group', {'chat_id': -10022, 'settings': {'rate': '2'}}, uid=222)[0], 403)
                self.assertEqual(self.call('staff', {'user_id': 555, 'add': True}, uid=222)[0], 403)
                code, result = self.call('overview', uid=999)
                self.assertEqual(code, 200, result)
                self.assertFalse(result['data']['groups'])
                self.assertFalse(result['data']['staff'])
                basics = dict(view['customer']['basics'], welcome_text='客户本机欢迎语')
                self.assertEqual(self.call('customer', {'section': 'basics', 'revision': view['customer']['revision'], 'value': basics})[0], 200)
                self.assertEqual(self.call('customer', {'section': 'basics', 'revision': view['customer']['revision'], 'value': basics})[0], 400)
                self.assertEqual(self.call('customer', {'section': 'basics', 'revision': 0, 'value': basics}, uid=999)[0], 403)
                self.assertEqual(self.call('group', {'chat_id': -10011, 'settings': {'rate': '2', 'ledger_view_mode': 'detailed'}})[0], 200)
                self.assertEqual(self.call('staff', {'user_id': 555, 'add': True, 'name': '客户内部人员'})[0], 200)
                self.assertEqual(self.call('tronkeys', {'keys': ['CUSTOM_FAKE_QUERY_KEY']})[0], 200)
                self.assertEqual(self.call('tronkeys', {'keys': []}, uid=999)[0], 403)
                code, result = self.call('tronquery', {'address': 'TWc69BK4pkadCYhWKVbZVgTPGsBHTK3NSx'})
                self.assertEqual(code, 200, result)
                self.assertEqual(result['data']['html'], '客户服务器余额查询')
                # Upload/preview also pass through the bridge, including base64 bodies.
                import base64
                from PIL import Image
                buffer = io.BytesIO(); Image.new('RGB', (16, 16), 'blue').save(buffer, format='PNG')
                image = 'data:image/png;base64,' + base64.b64encode(buffer.getvalue()).decode()
                latest = self.call()[1]['data']
                self.assertEqual(self.call('welcomephoto', {'mode': 'upload', 'revision': latest['customer']['revision'], 'image': image})[0], 200)
                code, result = self.call('welcomephoto', {'mode': 'preview'})
                self.assertEqual(code, 200, result)
                self.assertTrue(result['data']['image'].startswith('data:image/jpeg;base64,'))
                self.assertEqual(self.call(raw=signed('WRONG_FAKE_TOKEN', 111))[0], 400)
                self.assertEqual(self.call(raw=signed(self.bot['token'], 111, age=7200))[0], 400)
                # No customer writes or picture bytes landed in the platform database/config.
                self.assertNotIn('customer', self.bot.get('ledger', {}))
                self.assertNotIn(555, self.bot['admin_ids'])
                self.assertFalse((Path(core.DATA_DIR)/'ledger1.welcome.jpg').exists())
            finally:
                stdout, stderr = process.communicate('\n', timeout=22)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertIn('PERSISTED', stdout)

    def test_bridge_is_bounded_scoped_and_never_dispatches_twice(self):
        bridge = miniapp.RemoteRequests()
        results = []
        thread = threading.Thread(target=lambda: results.append(bridge.call('a', 'overview', {}, timeout=1)))
        thread.start()
        job = bridge.exchange('a', None, timeout=.1)
        self.assertIsNotNone(job)
        self.assertIsNone(bridge.exchange('a', None, timeout=.01))
        reply = dict(id=job['id'], status=200, result=dict(ok=True, data={'local': True}))
        self.assertIsNone(bridge.exchange('other', reply, timeout=.01))
        self.assertTrue(thread.is_alive())
        bridge.exchange('a', reply, timeout=.01)
        thread.join()
        self.assertEqual(results, [reply])
        self.assertFalse(bridge.jobs)
        with self.assertRaises(OSError):
            bridge.call('offline', 'customer', {}, timeout=.01)
        self.assertFalse(bridge.jobs)
        for i in range(4):
            bridge.jobs[str(i)] = dict(bid='full')
        with self.assertRaises(OSError):
            bridge.call('full', 'customer', {}, timeout=.01)

    def test_ingest_auth_and_run_location_are_required(self):
        headers = {'Content-Type': 'application/json', 'X-Ingest-Sig': 'WRONG_SIGNATURE_FAKE'}
        request = Request(self.url+'/api/ingest/config', b'{}', headers)
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        self.assertEqual(error.exception.code, 403)
        self.bot['remote'] = False
        headers['X-Ingest-Sig'] = core.ingest_sig(self.bot['id'], self.bot['token'])
        with self.assertRaises(HTTPError) as error:
            urlopen(Request(self.url+'/api/ingest/config', b'{}', headers))
        self.assertEqual(error.exception.code, 403)

    def test_package_uses_bot_source_and_only_referenced_customer_images(self):
        from bot_instances import provision
        from customer_images import image_path, ad_path, LOCAL_PHOTO
        folder = provision(self.bot, self.mgr.cfg)
        marker = '\n# isolated customer source\n'
        owned = folder/'customer_ui.py'
        owned.write_text(owned.read_text(encoding='utf-8') + marker, encoding='utf-8')
        image_path(self.bot).write_bytes(b'FAKE_WELCOME_IMAGE')
        ref = 'a' * 64
        ad_path(self.bot, ref).write_bytes(b'FAKE_AD_IMAGE')
        self.bot['ledger'] = dict(newbie_welcome_photo=LOCAL_PHOTO,
                                  customer={'ads': [dict(image=ref)]})
        unrelated = folder/'data'/'other-bot-secret.json'
        unrelated.write_text('FAKE_OTHER_BOT_DATA')
        buffer = io.BytesIO()
        solo_pack.make_zip(buffer, solo_pack.client_config(
            self.bot['id'], self.bot['token'], self.url, bot=self.bot, settings=self.mgr.cfg), bot=self.bot)
        with zipfile.ZipFile(buffer) as archive:
            self.assertTrue(archive.read('记账独立版/customer_ui.py').decode().endswith(marker))
            self.assertEqual(archive.read('记账独立版/data/'+image_path(self.bot).name), b'FAKE_WELCOME_IMAGE')
            self.assertEqual(archive.read('记账独立版/data/'+ad_path(self.bot, ref).name), b'FAKE_AD_IMAGE')
            self.assertFalse(any(n.endswith(unrelated.name) for n in archive.namelist()))
if __name__ == '__main__':
    unittest.main()
