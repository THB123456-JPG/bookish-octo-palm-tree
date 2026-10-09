"""Local fake-data preview only. Never starts a Telegram poller."""
import json
import pathlib
import sys
import tempfile
from http.server import ThreadingHTTPServer

source = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(source))
from _test_miniapp import setup_fixture, signed
from panel import PanelHandler

with tempfile.TemporaryDirectory(prefix='miniapp-preview-') as folder:
    manager = setup_fixture(folder)
    auth = {bid: signed(manager.find(bid)['token'], 111)
            for bid in ('ledger1', 'shop1')}
    auth['operator'] = signed(manager.find('ledger1')['token'], 222)
    pathlib.Path(__file__).with_name('preview_auth.json').write_text(json.dumps(auth), encoding='utf-8')
    server = ThreadingHTTPServer(('127.0.0.1', 8765), PanelHandler)
    print('FAKE DATA preview http://127.0.0.1:8765/miniapp/ledger1', flush=True)
    server.serve_forever()
