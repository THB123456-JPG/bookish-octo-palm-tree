"""Download is read-only; run-location switches preserve owner and money."""
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
import zipfile
from unittest.mock import Mock

import core
from _test_miniapp import setup_fixture, signed
from manager import BotManager
from panel import PanelHandler


class HostedModeTests(unittest.TestCase):
    def setUp(self):
        self.base = core.BASE_DIR
        self.tmp = tempfile.TemporaryDirectory()
        fixture = setup_fixture(self.tmp.name)
        self.manager = BotManager(fixture.cfg)
        self.manager.bots = copy.deepcopy(fixture.bots)
        self.manager.start_bot = Mock()
        self.manager.stop_bot = Mock(return_value=object())
        self.manager.wait_runner = Mock()
        self.manager.save()
        PanelHandler.mgr = self.manager
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

    def test_download_keeps_hosted_bot_and_owner_portal_and_has_runtime_dependencies(self):
        before = copy.deepcopy(self.manager.find('ledger1'))
        with urlopen(self.url + '/api/bots/ledger1/solozip?url=https://example.test') as response:
            self.assertEqual(response.status, 200)
            body = response.read()
        self.assertEqual(self.manager.find('ledger1'), before)
        self.manager.start_bot.assert_not_called()
        self.manager.stop_bot.assert_not_called()
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            for name in ('customer_config.py', 'customer_ui.py', 'customer_images.py'):
                self.assertIn('记账独立版/' + name, archive.namelist())
            with tempfile.TemporaryDirectory() as folder:
                self.assertTrue(all(not Path(n).is_absolute() and '..' not in Path(n).parts
                                    for n in archive.namelist()))
                archive.extractall(folder)
                result = subprocess.run([sys.executable, '-c',
                    'from runners.ledger import LedgerRunner; import customer_images'],
                    cwd=Path(folder) / '记账独立版', capture_output=True, text=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
        payload = {'init_data': signed(before['token'], 111), 'payload': {}}
        with urlopen(Request(self.url + '/api/miniapp/ledger1/overview',
                     json.dumps(payload).encode(), {'Content-Type': 'application/json'})) as response:
            self.assertTrue(json.load(response)['ok'])

    def test_switch_and_restore_preserve_owner_config_and_original_entries(self):
        bot = self.manager.find('ledger1')
        before = copy.deepcopy(bot)
        db = Path(core.DATA_DIR) / 'ledger1.sqlite3'
        money = db.read_bytes()
        for on in (True, False):
            request = Request(self.url + '/api/bots/ledger1/remote',
                              json.dumps({'on': on}).encode(),
                              {'Content-Type': 'application/json'})
            with urlopen(request) as response:
                self.assertTrue(json.load(response)['ok'])
            self.assertEqual(bot.get('remote'), on)
            for key in ('token', 'owner_id', 'admin_ids', 'expire_at', 'ledger', 'bind_code'):
                self.assertEqual(bot.get(key), before.get(key))
        self.assertEqual(db.read_bytes(), money)
        self.manager.start_bot.assert_called_once_with('ledger1')
        self.manager.stop_bot.assert_called_once_with('ledger1')


if __name__ == '__main__':
    unittest.main()
