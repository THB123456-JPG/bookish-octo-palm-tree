"""No-network regressions for concurrent startup and unnecessary state writes."""
import copy
import importlib
import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import bot_instances
import core
from manager import BotManager


class AuditRuntimeTests(unittest.TestCase):
    def test_headless_start_does_not_log_password_or_open_browser(self):
        entry = importlib.import_module('主程序')
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as folder, patch.object(core, 'DATA_DIR', folder), \
             patch.object(entry, 'load_cfg', return_value={'password': 'FAKE_AUDIT_SECRET'}), \
             patch.object(entry, 'BotManager'), patch.object(entry, 'MerchantStore'), \
             patch.object(entry, 'ServerMonitor'), \
             patch.object(entry, 'PanelHandler', SimpleNamespace()), \
             patch.object(entry, 'ThreadingHTTPServer'), patch.object(entry.threading, 'Thread'), \
             patch.object(entry.time, 'sleep', side_effect=KeyboardInterrupt), \
             patch.object(entry.sys, 'stdout', output), patch.object(entry.sys, 'stdin', io.StringIO()), \
             patch.object(entry.webbrowser, 'open') as browser:
            entry.main()
            self.assertNotIn('FAKE_AUDIT_SECRET', output.getvalue())
            browser.assert_not_called()

    def test_concurrent_start_creates_one_runner(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(core, 'BOTS_FILE', str(Path(folder)/'bots.json')):
            mgr = BotManager({})
            mgr.bots = [dict(id='audit', type='kefu', enabled=True)]
            entered, release = threading.Event(), threading.Event()
            def make(*args):
                runner = SimpleNamespace(alive=False)
                runner.is_alive = lambda: runner.alive
                def start():
                    entered.set()
                    self.assertTrue(release.wait(3))
                    runner.alive = True
                runner.start = start
                return runner
            with patch('manager.make_runner', side_effect=make) as factory, ThreadPoolExecutor(2) as pool:
                a = pool.submit(mgr.start_bot, 'audit')
                self.assertTrue(entered.wait(3))
                b = pool.submit(mgr.start_bot, 'audit')
                time.sleep(.1)
                release.set()
                a.result(3); b.result(3)
                self.assertEqual(factory.call_count, 1)

    def test_unchanged_instance_configuration_is_not_rewritten(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'bots.json'
            bot = dict(id='audit', note='unchanged')
            core.save_json(str(path), {'bots': [bot]})
            with patch.object(core, 'BOTS_FILE', str(path)), patch('core.save_json', wraps=core.save_json) as save:
                baseline = bot_instances.sync(bot, copy.deepcopy(bot), commit=True, child=True)
                save.assert_not_called()
                bot['note'] = 'changed'
                bot_instances.sync(bot, baseline, commit=True, child=True)
                save.assert_called_once()
                self.assertEqual(core.load_json(str(path), {})['bots'][0]['note'], 'changed')


if __name__ == '__main__':
    unittest.main()
