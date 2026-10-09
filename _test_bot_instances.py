"""Real child processes, isolated source edits and deletion with fake Telegram."""
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
import threading

import core
import bot_instances as instances
import customer_config as CC
import miniapp
from manager import BotManager
from urllib.error import HTTPError
from panel import PanelHandler

REAL_POPEN = subprocess.Popen
BOOTSTRAP = '''
import json,threading,time
from pathlib import Path
import core
guard=threading.Lock()
def fake(self,method,**kw):
    if method=='getMe':
        return {'id':int(self.token.split(':')[0]),'username':Path.cwd().name,'first_name':'Fake'}
    if method=='getUpdates':
        time.sleep(.03)
        path=Path('incoming.json')
        if path.exists():
            value=json.loads(path.read_text(encoding='utf-8'))
            path.unlink()
            return value
        return []
    if method=='getMyDescription': return {'description':''}
    with guard, Path('outgoing.jsonl').open('a',encoding='utf-8') as output:
        output.write(json.dumps({'method':method,**kw},ensure_ascii=False)+'\\n')
    return {'message_id':99,'status':'administrator'}
core.TgAPI.call=fake
core.TgAPI.call_file=lambda *a,**kw:{'message_id':99}
import instance_worker
instance_worker.main()
'''


class InstanceTests(unittest.TestCase):
    def setUp(self):
        self.original_base = core.BASE_DIR
        self.tmp = tempfile.TemporaryDirectory()
        core.set_base_dir(self.tmp.name)
        Path(core.DATA_DIR).mkdir()
        self.manager = BotManager({'miniapp_base_url': 'https://example.test',
                                   'tron_api_keys': ['FAKE_SHARED_A', 'FAKE_SHARED_B', 'FAKE_SHARED_C']})
        class Handler(PanelHandler):
            password = 'FAKE_ADMIN'
            merch = None
        Handler.mgr = self.manager
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.counter = 0
        self.popen = patch('bot_instances.subprocess.Popen', side_effect=lambda args, **kw:
                           REAL_POPEN([args[0], '-u', '-c', BOOTSTRAP], **kw))
        self.popen.start()

    def tearDown(self):
        self.manager.stop_all()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join()
        self.popen.stop()
        core.set_base_dir(self.original_base)
        self.tmp.cleanup()

    def wait(self, predicate):
        end = time.monotonic() + 15
        while time.monotonic() < end:
            if predicate():
                return
            time.sleep(.05)
        logs = [p.read_text(encoding='utf-8', errors='replace')[-2500:]
                for p in Path(self.tmp.name).glob('instances/*/process.log')]
        logs += [p.read_text(encoding='utf-8', errors='replace')[-1800:]
                 for p in Path(self.tmp.name).glob('instances/*/outgoing.jsonl')]
        self.fail('Child verification timed out: ' + '\n'.join(logs))

    def add(self, number, kind='ledger'):
        with patch('manager.TgAPI.call', return_value=dict(id=number,
                username='client_%s_bot' % number, first_name='Fake')):
            bot, error = self.manager.add('Client', '%s:FAKE_INSTANCE' % number, kind)
        self.assertIsNone(error)
        self.wait(lambda: self.manager.runners[bot['id']].info().get('status') == 'running')
        return bot

    def test_all_types_have_separate_processes_and_shop_config_stays_in_child(self):
        created = [self.add(200+i,kind) for i,kind in enumerate(('ledger','shop','kefu','usdt'))]
        infos = [self.manager.runners[b['id']].info() for b in created]
        self.assertEqual(len({r['pid'] for r in infos}),4)
        for bot in created:
            path = instances.folder(bot)
            self.assertEqual(path.name,bot['username'])
            self.assertTrue((path/'bots.json').exists())
            self.assertTrue((path/'data').is_dir())
        shop = created[1]
        runner = self.manager.runners[shop['id']]
        with self.assertRaises(HTTPError) as error:
            runner.request('/api/bots')
        self.assertEqual(error.exception.code,404)
        error.exception.close()
        with self.assertRaises(HTTPError) as error:
            runner.request('/instance/panel',dict(path='/api/shop/'+created[0]['id']+'/config',
                           method='GET',principal={'role':'admin'}))
        self.assertEqual(error.exception.code,400)
        error.exception.close()
        url = 'http://127.0.0.1:%s/api/shop/%s/config'%(self.server.server_port,shop['id'])
        body = json.dumps({'notice':'Isolated shop configuration'}).encode()
        request = Request(url,body,{'X-Panel-Pass':'FAKE_ADMIN','Content-Type':'application/json'})
        with urlopen(request,timeout=20) as response:
            self.assertTrue(json.load(response)['ok'])
        with urlopen(Request(url,headers={'X-Panel-Pass':'FAKE_ADMIN'}),timeout=20) as response:
            self.assertEqual(json.load(response)['config']['notice'],'Isolated shop configuration')
        with patch.object(PanelHandler,'_gate',return_value={'role':'merchant','mid':'someone-else'}):
            with self.assertRaises(HTTPError) as error:
                urlopen(request,timeout=20)
            self.assertEqual(error.exception.code,404)
            error.exception.close()
        child = core.load_json(str(instances.folder(shop)/'bots.json'),{})['bots'][0]
        self.assertEqual(child['shop']['notice'],'Isolated shop configuration')
        self.assertFalse((Path(core.DATA_DIR)/(shop['id']+'.json')).exists())
        self.control(shop,'disable')
        with urlopen(Request(url,headers={'X-Panel-Pass':'FAKE_ADMIN'}),timeout=20) as response:
            self.assertEqual(json.load(response)['config']['notice'],'Isolated shop configuration')
        with urlopen(request,timeout=20) as response:
            self.assertTrue(json.load(response)['ok'])

    def control(self, bot, action):
        request = Request('http://127.0.0.1:%s/api/bots/%s/%s' %
            (self.server.server_port, bot['id'], action), b'{}',
            {'Content-Type': 'application/json', 'X-Panel-Pass': 'FAKE_ADMIN'})
        with urlopen(request, timeout=20) as response:
            self.assertTrue(json.load(response)['ok'])

    def update(self, bot, text, uid, cid=None):
        path = instances.folder(bot) / 'incoming.json'
        self.wait(lambda: not path.exists())
        self.counter += 1
        core.save_json(str(path), [{'update_id': self.counter, 'message': {
            'message_id': self.counter, 'chat': {'id': cid or uid,
                'type': 'supergroup' if cid else 'private', 'title': 'Fake group'},
            'from': {'id': uid, 'first_name': 'Fake'}, 'text': text}}])
        self.wait(lambda: not path.exists())

    def messages(self, bot):
        path = instances.folder(bot) / 'outgoing.jsonl'
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()
                if line.strip()] if path.exists() else []

    def test_source_customization_restart_config_and_delete_are_per_username(self):
        a, b = self.add(101), self.add(102)
        for bot in (a, b):
            cfg = core.load_json(str(instances.folder(bot) / 'config.json'), {})
            self.assertEqual(cfg['tron_api_keys'], self.manager.cfg['tron_api_keys'])
        for bot, owner in ((a, 111), (b, 222)):
            self.update(bot, '/admin ' + bot['bind_code'], owner)
            self.wait(lambda: self.manager.find(bot['id']).get('owner_id') == owner)
        self.manager.cfg['miniapp_footer_text'] = '客户共同联系@ExampleDev'
        for bot, owner in ((a, 111), (b, 222)):
            self.assertEqual(miniapp.action(self.manager, bot, owner, 'overview', {})['footer_text'], '客户共同联系@ExampleDev')
        self.manager.cfg['miniapp_footer_text'] = miniapp.DEFAULT_FOOTER
        self.assertEqual(CC.settings(a), CC.settings(b))
        self.assertEqual(CC.settings(a)['basics']['currency'], 'CNY')
        self.assertEqual(CC.settings(a)['basics']['view_mode'], 'compact')
        cfg = CC.settings(a)
        cfg['features']['join_notice'] = True
        miniapp.action(self.manager, a, 111, 'customer',
            dict(section='features', value=cfg['features'], revision=cfg['revision']))
        self.manager.find(a['id'])
        self.assertTrue(CC.settings(a)['features']['join_notice'])
        self.assertFalse(CC.settings(b)['features']['join_notice'])
        with self.assertRaises(PermissionError):
            miniapp.action(self.manager, a, 222, 'customer',
                dict(section='features', value=cfg['features'], revision=1))
        for bot, owner in ((a, 111), (b, 222)):
            self.update(bot, '+100', owner, -owner)
            self.wait(lambda: len(miniapp.action(self.manager, bot, owner, 'bills', {})['bills']) == 1)
            self.wait(lambda: any('100/1=100U' in msg.get('text', '') for msg in self.messages(bot)))
            self.assertFalse(any('最近流水' in msg.get('text', '') or '折合' in msg.get('text', '') for msg in self.messages(bot)))
            self.update(bot, '显示日切', owner, -owner)
            self.wait(lambda: any('下次日切' in msg.get('text', '') for msg in self.messages(bot)))
        self.assertEqual(miniapp.overview(self.manager, a, 111)['groups'][0]['chat_id'], -111)
        self.assertEqual(miniapp.overview(self.manager, b, 222)['groups'][0]['chat_id'], -222)
        pid_a = self.manager.runners[a['id']].info()['pid']
        pid_b = self.manager.runners[b['id']].info()['pid']
        code = instances.folder(a) / 'customer_ui.py'
        code.write_text(code.read_text(encoding='utf-8').replace('📖 使用说明</b>', '📖 A定制说明</b>'), encoding='utf-8')
        self.control(a, 'restart')
        self.wait(lambda: self.manager.runners[a['id']].info().get('status') == 'running')
        self.assertNotEqual(self.manager.runners[a['id']].info()['pid'], pid_a)
        self.assertEqual(self.manager.runners[b['id']].info()['pid'], pid_b)
        self.update(a, '帮助', 111)
        self.wait(lambda: any('A定制说明' in msg.get('text', '') for msg in self.messages(a)))
        self.update(b, '帮助', 222)
        self.wait(lambda: any('📖 使用说明' in msg.get('text', '') for msg in self.messages(b)))
        self.assertFalse(any('A定制说明' in msg.get('text', '') for msg in self.messages(b)))
        directory = instances.folder(a)
        self.control(a, 'delete')
        self.assertFalse(directory.exists())
        self.assertIsNone(self.manager.find(a['id']))
        self.assertEqual(self.manager.runners[b['id']].info()['pid'], pid_b)
        self.assertTrue(instances.folder(b).exists())

    def test_scoped_state_merge_and_legacy_data_migration(self):
        bot = dict(id='old1', type='ledger', username='legacy_test_bot', token='1:FAKE',
                   enabled=True, admin_ids=[123], owner_id=123, bind_code='KEEP', expire_at=123456789)
        expected = b'legacy data unchanged'
        (Path(core.DATA_DIR) / 'old1.json').write_bytes(expected)
        before = copy.deepcopy(bot)
        instances.provision(bot, self.manager.cfg, migrate=True)
        self.assertEqual((instances.folder(bot) / 'data/old1.json').read_bytes(), expected)
        self.assertFalse((Path(core.DATA_DIR) / 'old1.json').exists())
        for key, value in before.items():
            self.assertEqual(bot[key], value)
        original = copy.deepcopy(bot)
        path = instances.folder(bot) / 'bots.json'
        current = core.load_json(str(path), {})['bots'][0]
        current['admin_ids'] = [123, 456]
        core.save_json(str(path), {'bots': [current]})
        bot['note'] = 'Panel changed note'
        instances.sync(bot, original, commit=True)
        self.assertEqual(bot['admin_ids'], [123, 456])
        self.assertEqual(bot['note'], 'Panel changed note')
        altered = dict(bot, instance_folder='../outside')
        with self.assertRaises(ValueError):
            instances.remove(altered)
        collision = dict(id='other', type='ledger', username=bot['username'])
        with self.assertRaises(ValueError):
            instances.provision(collision, self.manager.cfg)

    def test_idle_runtime_is_not_rewritten(self):
        bootstrap = BOOTSTRAP.replace('import instance_worker',
            'from runners.ledger import LedgerRunner\nLedgerRunner.tick_interval=lambda self: .1\nimport instance_worker')
        with patch(__name__ + '.BOOTSTRAP', bootstrap):
            bot = self.add(103)
        path = instances.folder(bot) / 'runtime.json'
        time.sleep(.6)
        modified = path.stat().st_mtime_ns
        time.sleep(1.2)
        self.assertEqual(path.stat().st_mtime_ns, modified)
        self.update(bot, '/admin ' + bot['bind_code'], 111)
        self.update(bot, '+100', 111, -111)
        self.wait(lambda: self.manager.runners[bot['id']].info().get('stat') == 1)

    def test_failed_data_move_restores_original_files_and_removes_partial_instance(self):
        bot = dict(id='failed1', type='ledger', username='failed_test_bot', token='1:FAKE')
        paths = [Path(core.DATA_DIR) / ('failed1.' + suffix) for suffix in ('json', 'sqlite3')]
        for path in paths:
            path.write_bytes(b'original')
        real_move = instances.shutil.move
        count = 0
        def move(source, destination):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError('Simulated migration failure')
            return real_move(source, destination)
        with patch('bot_instances.shutil.move', side_effect=move):
            with self.assertRaises(OSError):
                instances.provision(bot, self.manager.cfg, migrate=True)
        self.assertTrue(all(path.read_bytes() == b'original' for path in paths))
        self.assertNotIn('instance_folder', bot)
        self.assertFalse((Path(core.BASE_DIR) / 'instances/failed_test_bot').exists())


if __name__ == '__main__':
    unittest.main()
