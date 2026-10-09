"""Concurrent local Mini App reads with temporary bills and no external network."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
from unittest.mock import patch
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _test_miniapp import setup_fixture, signed
import core
from panel import PanelHandler
from runners.ledger.storage import LedgerStore


def run(requests=240, workers=8, entries=1000):
    original_connect = socket.socket.connect
    external_attempts = []

    def local_only(sock, address):
        if address[0] not in ('127.0.0.1', '::1'):
            external_attempts.append(address[0])
            raise AssertionError('External network disabled in isolated check')
        return original_connect(sock, address)

    old_base, old_manager = core.BASE_DIR, getattr(PanelHandler, 'mgr', None)
    try:
        with tempfile.TemporaryDirectory(prefix='miniapp-load-') as folder, \
             patch.object(socket.socket, 'connect', local_only):
            mgr = setup_fixture(folder)
            mgr.bots = [mgr.find('ledger1')]
            db = Path(core.DATA_DIR)/'ledger1.sqlite3'
            with closing(LedgerStore(db)) as store:
                for i in range(entries-1):
                    store.add_entry(-10011, 'income', '100', 'CNY', '并发测试',
                                    111, '测试用户', source_message_id=10000+i)
            before = hashlib.sha256(db.read_bytes()).hexdigest()
            server = ThreadingHTTPServer(('127.0.0.1', 0), PanelHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            auth = signed(mgr.bots[0]['token'], 111)
            body = json.dumps({'init_data':auth,'payload':{'chat_id':-10011}}).encode()

            def request(index):
                action = ('overview','bills','statistics')[index % 3]
                req = Request('http://127.0.0.1:%s/api/miniapp/ledger1/%s' %
                              (server.server_port, action), body, {'Content-Type':'application/json'})
                started = time.perf_counter()
                with urlopen(req, timeout=30) as response:
                    result = json.load(response)
                    assert response.status == 200 and result['ok'], action
                    if action == 'bills':
                        assert sum(len(b['entries']) for b in result['data']['bills']) == entries
                return (time.perf_counter()-started)*1000

            start = time.perf_counter()
            try:
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    times = sorted(pool.map(request, range(requests)))
                duration = time.perf_counter()-start
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
            assert hashlib.sha256(db.read_bytes()).hexdigest() == before, 'Read queries changed bills'
            assert not external_attempts
            return {'requests':requests, 'workers':workers, 'bill_entries':entries,
                    'errors':0, 'external_requests':0, 'database_unchanged':True,
                    'seconds':round(duration,2), 'requests_per_second':round(requests/duration,2),
                    'latency_ms':{'p50':round(times[len(times)//2],1),
                                  'p95':round(times[min(len(times)-1,int(len(times)*.95))],1),
                                  'max':round(times[-1],1)}}
    finally:
        core.set_base_dir(old_base)
        PanelHandler.mgr = old_manager


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--requests',type=int,default=240)
    parser.add_argument('--workers',type=int,default=8)
    parser.add_argument('--entries',type=int,default=1000)
    parser.add_argument('--output',type=Path)
    args = parser.parse_args()
    if min(args.requests,args.workers,args.entries) < 1:
        parser.error('Counts must be positive')
    result = json.dumps(run(args.requests,args.workers,args.entries),ensure_ascii=False,indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(result,encoding='utf-8')
    print(result)
