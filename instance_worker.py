"""Run one customer's source copy and configuration server in its own process."""
import os
from pathlib import Path
import secrets
import signal
import threading
import time
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse

os.environ['PANEL_INSTANCE_CHILD'] = '1'

import core
from manager import BotManager
from panel import PanelHandler


def main():
    root = Path(__file__).resolve().parent
    core.set_base_dir(str(root))
    manager = BotManager(core.load_json(core.CONFIG_FILE, {}))
    if len(manager.bots) != 1:
        raise ValueError('独立实例必须只有一个机器人')
    bot = manager.bots[0]
    manifest = core.load_json(str(root / 'INSTANCE.json'), {})
    if manifest.get('id') != bot['id'] or manifest.get('username') != root.name:
        raise ValueError('独立实例身份不符')
    class Handler(PanelHandler):
        mgr = manager
        password = secrets.token_urlsafe(32)
        merch = None
        token_secret = secrets.token_urlsafe(32)

        def do_GET(self):
            self._json({'ok':False,'error':'找不到接口'},404)

        def do_POST(self):
            if self.path == '/instance/panel':
                if self._gate() is None:
                    return
                body = self._body()
                path = urlparse(body.get('path','')).path
                principal = body.get('principal') or {}
                if bot['type'] != 'shop' or not path.startswith('/api/shop/'+bot['id']+'/') or principal.get('role') not in ('admin','merchant') or body.get('method') not in ('GET','POST'):
                    self._json({'ok':False,'error':'实例请求无效'},400)
                    return
                # The parent authenticates users; keep their role and ownership for every child route.
                original_path, original_gate, original_body = self.path,self._gate,self._body
                try:
                    self.path = body['path']
                    self._gate = lambda: principal
                    self._body = lambda: body.get('body') or {}
                    if body['method'] == 'GET':
                        return PanelHandler.do_GET(self)
                    return PanelHandler.do_POST(self)
                finally:
                    self.path,self._gate,self._body = original_path,original_gate,original_body
            if self.path == '/instance/shop-reset':
                if self._gate() is None:
                    return
                self._body()
                manager.find(bot['id'])
                runner = manager.runners.get(bot['id'])
                if bot['type'] != 'shop' or not runner:
                    self._json({'ok':False,'error':'实例类型无效'},400)
                    return
                runner.store.data['scan'] = {'baseline':False,'seen':[],'last_ts':0}
                runner.store.save()
                self._json({'ok':True})
                return
            if self.path != '/instance/action':
                self._json({'ok':False,'error':'找不到接口'},404)
                return
            if self._gate() is None:
                return
            try:
                import miniapp
                body = self._body()
                current = manager.find(bot['id'])
                if not current.get('owner_id') or manager.is_expired(current):
                    raise PermissionError('机器人尚未激活或已到期')
                result = miniapp.action(manager, current, body['user_id'], body['action'], body.get('payload', {}))
                self._json({'ok': True, 'data': result})
            except PermissionError as exc:
                self._json({'ok': False, 'error': str(exc)}, 403)
            except (ValueError, TypeError, KeyError) as exc:
                self._json({'ok': False, 'error': str(exc) if isinstance(exc, ValueError) else '请求格式无效'}, 400)
            except OSError:
                self._json({'ok': False, 'error': '实例数据暂不可用'}, 503)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    stopping = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *args: stopping.set())
    threading.Thread(target=server.serve_forever, daemon=True).start()
    manager.start_all()
    expiry = bot.get('expire_at')
    previous = None
    next_housekeeping = 0
    next_stat, stat = 0, 0
    try:
        while not stopping.is_set():
            manager.find(bot['id'])
            runner = manager.runners.get(bot['id'])
            if runner and expiry != bot.get('expire_at'):
                runner._expired_notice.clear()
                expiry = bot.get('expire_at')
            if time.monotonic()>=next_stat:
                stat = (runner.stat() if bot['type']=='ledger' else
                      len(runner.store.need_attention()) if bot['type']=='shop' else
                      len(getattr(runner,'watches',[]) or []) if bot['type']=='usdt' else
                      len(getattr(runner,'customers',{}) or {})) if runner else 0
                next_stat = time.monotonic()+5
            current = dict(pid=os.getpid(), port=server.server_port,
                auth=Handler.password, status=bot.get('status'), error=bot.get('error'),
                received_updates=getattr(runner, 'received_updates', 0),stat=stat)
            if current != previous:
                core.save_json(str(root / 'runtime.json'), current)
                previous = current
            if runner and not runner.is_alive():
                break
            if time.monotonic()>=next_housekeeping:
                from runtime_cleanup import cleanup
                threading.Thread(target=cleanup,args=(manager,bot,root),daemon=True,name='runtime-cleanup').start()
                next_housekeeping = time.monotonic()+3600
            stopping.wait(0.5)
    finally:
        manager.stop_all()
        server.shutdown()
        server.server_close()


if __name__ == '__main__':
    main()
