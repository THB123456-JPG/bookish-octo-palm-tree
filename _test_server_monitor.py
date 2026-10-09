"""Capacity must stay conservative, tenant-private and independent of business data."""
import hashlib
from http.server import ThreadingHTTPServer
import json
import mmap
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import core
from panel import PanelHandler
from runtime_cleanup import cleanup
import server_monitor as monitor


class Tests(unittest.TestCase):
    def test_sampling_handles_restarts_missing_processes_and_stale_data(self):
        bot = dict(id='one',type='ledger',username='sample_bot',instance_folder='sample_bot')
        info = {'pid':10,'received_updates':2}
        runner = SimpleNamespace(is_alive=lambda:True,info=lambda:info)
        manager = SimpleNamespace(lock=threading.RLock(),bots=[bot,dict(bot,id='remote',remote=True)],runners={'one':runner})
        host = dict(cpu_ticks=100,idle_ticks=80,cores=4,memory_total=8*monitor.GIB,
                    memory_available=7*monitor.GIB,swap_used=0,disk_total=120*monitor.GIB,
                    disk_free=110*monitor.GIB,uptime=100,rx=100,tx=100)
        proc = dict(pid=10,start=1,ticks=10,rss=40*monitor.MIB,peak=45*monitor.MIB)
        with tempfile.TemporaryDirectory() as folder, patch.object(core,'BASE_DIR',folder), \
             patch.object(monitor,'host_sample',side_effect=lambda _:dict(host)), \
             patch.object(monitor,'process_sample',side_effect=lambda _:dict(proc)) as sample, \
             patch.object(monitor.os,'sysconf',return_value=100,create=True), \
             patch.object(monitor.time,'monotonic',side_effect=[10,20,30,40]), \
             patch.object(monitor.time,'time',return_value=1000):
            service = monitor.ServerMonitor(manager)
            service.collect()
            self.assertIsNone(service.snapshot()['capacity']['additional'])
            host.update(cpu_ticks=200,idle_ticks=130)
            info['received_updates']=8;proc['ticks']=20
            service.collect()
            result=service.snapshot()
            self.assertEqual(result['cpu_percent'],50)
            self.assertEqual(result['updates_per_minute'],36)
            self.assertEqual((result['local_running'],result['remote_count']),(1,1))
            self.assertIsNotNone(result['capacity']['additional'])
            proc.update(pid=11,start=2,ticks=1);info.update(pid=11,received_updates=1)
            service.collect()
            self.assertIsNone(service.snapshot()['workers'][0]['updates_per_minute'])
            sample.side_effect=FileNotFoundError()
            service.collect()
            self.assertIsNone(service.snapshot()['capacity']['additional'])
            with patch.object(monitor.time,'time',return_value=1040):
                self.assertTrue(service.snapshot()['stale'])

    def test_phone_map_matches_bytes_without_mutating_database(self):
        from runners.ledger import lookup
        lookup._load_phone_db()
        mapping = lookup._phone_buf
        self.assertIsInstance(mapping,mmap.mmap)
        with self.assertRaises(TypeError):
            mapping[0] = 0
        samples = ['13800138000','15012345678','16512345678','19900001111']
        expected = [lookup.phone_info(n) for n in samples]
        try:
            lookup._phone_buf = Path(lookup.PHONE_DAT).read_bytes()
            self.assertEqual([lookup.phone_info(n) for n in samples],expected)
        finally:
            lookup._phone_buf = mapping
    def test_capacity_decreases_with_workload_and_pressure(self):
        host = dict(memory_total=8*monitor.GIB,cores=4,disk_total=120*monitor.GIB,disk_free=110*monitor.GIB)
        history = [dict(ts=10*i,cpu=2,available=7*monitor.GIB,bot_cpu=.005,updates=0) for i in range(60)]
        workers = [dict(id='one',type='ledger',rss=43*monitor.MIB,peak=45*monitor.MIB)]
        result = monitor.estimate(host,history,workers,{})
        self.assertLessEqual(result['additional'],56)
        self.assertIn('低负载',result['confidence'])
        history[-1]['available'] = monitor.GIB
        self.assertEqual(monitor.estimate(host,history,workers,{})['additional'],0)
        history[-1]['available'] = 7*monitor.GIB
        for row in history:
            row.update(bot_cpu=.3,updates=3)
        heavy = monitor.estimate(host,history,workers,{})
        self.assertLess(heavy['additional'],result['additional'])
        self.assertEqual(heavy['confidence'],'近期业务参考')
        self.assertIsNone(monitor.estimate(host,history,workers,{},False)['additional'])
        self.assertIsNone(monitor.estimate(host,history,[],{})['additional'])

    @patch('panel.login_log.add')
    def test_admin_only_and_cached_response(self, _log):
        snapshot = {'ready':True,'workers':[],'capacity':{'additional':30}}
        class Handler(PanelHandler):
            password = 'FAKE_MONITOR'
            merch = None
            mgr = SimpleNamespace(server_monitor=SimpleNamespace(snapshot=lambda:snapshot))
        server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        def call(auth=True):
            req = Request('http://127.0.0.1:%s/api/server'%server.server_port,
                          headers={'X-Panel-Pass':Handler.password} if auth else {})
            try:
                response = urlopen(req,timeout=5)
            except HTTPError as error:
                response = error
            with response:
                return response.status,json.load(response)
        try:
            self.assertEqual(call(False)[0],401)
            with patch.object(Handler,'_gate',return_value={'role':'merchant'}):
                self.assertEqual(call()[0],403)
            with patch.object(monitor,'host_sample',side_effect=AssertionError('HTTP must not collect')):
                self.assertEqual(call()[1]['server'],snapshot)
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_cleanup_preserves_bills_current_images_and_new_drafts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder);data = root/'data';data.mkdir()
            bot = {'id':'one','ledger':{'customer':{'ads':[{'image':'a'*64,'enabled':False}]},
                                        'newbie_welcome_photo':'local-welcome'}}
            names = ['one.sqlite3','one.sqlite3-wal','one.sqlite3-shm','one.positions.sqlite3',
                     'one.json','one.welcome.jpg','one.ad-'+('a'*64)+'.jpg','foreign.tmp',
                     'one.ad-'+('c'*64)+'.jpg']
            obsolete = ['one.ad-'+('b'*64)+'.jpg','one.json.ABCD1234.tmp','one.welcome.jpg.tmp']
            for name in names+obsolete:
                p = data/name;p.write_bytes(b'keep data')
                os.utime(p,(time.time()-3*86400,)*2)
            os.utime(data/names[-1],None)
            (root/'process.log').write_bytes((b'old log\n'*400000)+b'last line\n')
            before = {n:hashlib.sha256((data/n).read_bytes()).hexdigest() for n in names}
            cleanup(SimpleNamespace(lock=threading.RLock()),bot,root)
            self.assertTrue(all(not (data/n).exists() for n in obsolete))
            self.assertEqual(before,{n:hashlib.sha256((data/n).read_bytes()).hexdigest() for n in names})
            self.assertLessEqual((root/'process.log').stat().st_size,1024*1024)
            self.assertTrue((root/'process.log').read_bytes().endswith(b'last line\n'))

    def test_storage_reports_disk_bytes_without_reading_message_content(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path/'one.archive.sqlite3').write_bytes(b'x'*40)
            (path/'one.archive-media').mkdir()
            (path/'one.archive-media/a.jpg').write_bytes(b'y'*20)
            (path/'one.sqlite3').write_bytes(b'z'*30)
            usage = monitor.storage_usage([('one',path),('duplicate',path)])
            self.assertEqual((usage['archive'],usage['media'],usage['other']),(40,20,30))
            self.assertEqual(usage['by_bot'],{'one':90})

    def test_each_runner_imports_only_its_own_service(self):
        script = r'''
import os,sys,tempfile,json
from pathlib import Path
import core,manager,panel
assert not any(n.startswith(('runners.ledger','runners.shop','runners.kefu','runners.usdt')) for n in sys.modules)
with tempfile.TemporaryDirectory() as folder:
 core.set_base_dir(folder);Path(core.DATA_DIR).mkdir()
 bot={'id':'fake','type':sys.argv[1],'token':'123:FAKE','username':'fake_bot','enabled':True,'admin_ids':[]}
 mgr=manager.BotManager({});mgr.bots=[bot]
 runner=manager.make_runner(mgr,bot)
 assert runner.kind==sys.argv[1]
 foreign=[n for n in sys.modules if any(n=='runners.'+k or n.startswith('runners.'+k+'.') for k in manager.RUNNERS if k!=sys.argv[1])]
 assert not foreign,foreign
 print('isolated',sys.argv[1])
'''
        for kind in ('ledger','shop','kefu','usdt'):
            result = subprocess.run([sys.executable,'-c',script,kind],capture_output=True,
                                    encoding='utf-8',timeout=20,env=dict(os.environ,PYTHONUTF8='1'))
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)


if __name__ == '__main__':
    unittest.main()
