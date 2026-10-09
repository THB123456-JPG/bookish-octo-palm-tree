"""Execution-node service. Existing per-bot handlers remain the authorization boundary."""
import base64
import copy
import hmac
import http.client
import json
import os
from pathlib import Path
import re
import secrets
import signal
import ssl
import threading
from http.server import ThreadingHTTPServer
from urllib.parse import urlsplit

import core
from manager import BotManager
from nodes import MAX_BODY, MIRROR, PROTOCOL, bot_record
from panel import PanelHandler
from server_monitor import ServerMonitor


def create(manager, body):
    incoming = body.get('bot') or {}
    if not isinstance(incoming,dict):
        raise ValueError('机器人资料无效')
    bid = incoming.get('id','')
    if not re.fullmatch('[a-zA-Z0-9_-]{1,64}',bid) or incoming.get('type') not in ('ledger','kefu','usdt','shop'):
        raise ValueError('机器人资料无效')
    with manager.lock:
        existing = manager.find(bid)
        if existing:
            if any(existing.get(k)!=incoming.get(k) for k in ('token','type','username')):
                raise ValueError('实例编号已被不同机器人使用')
            return bot_record(manager,bid)  # Lost create response: return it without resetting data or polling twice.
        if manager.token_used(incoming.get('token')):
            raise ValueError('此 Token 已经在该服务器运行')
        try:
            identity = core.TgAPI(incoming.get('token','')).call('getMe')
        except Exception:
            raise ValueError('运行服务器无法验证机器人身份') from None
        if identity.get('username') != incoming.get('username'):
            raise ValueError('机器人身份与创建请求不符')
        shared = body.get('shared') or {}
        for key in ('miniapp_base_url','welcome','tron_api_keys','miniapp_footer_text'):
            if key in shared:
                manager.cfg[key] = shared[key]
        core.save_json(core.CONFIG_FILE,manager.cfg)
        bot = {k:copy.deepcopy(incoming[k]) for k in MIRROR if k in incoming}
        bot.update(remote=False,status='starting',error='')
        from bot_instances import provision
        provision(bot,manager.cfg)
        manager._instance_seen[bid] = copy.deepcopy({k:v for k,v in bot.items() if k not in ('status','error')})
        manager.bots.append(bot)
        manager.save()
        manager.start_bot(bid)
        return bot_record(manager,bid)


def server(manager, settings, address):
    internal_key = secrets.token_urlsafe(32)
    class Internal(PanelHandler):
        mgr = manager
        merch = None
        password = internal_key
        token_secret = secrets.token_urlsafe(32)

        def _auth(self):
            if not hmac.compare_digest(self.headers.get('X-Panel-Pass',''),internal_key):
                return None
            try:
                principal = json.loads(self.headers.get('X-Node-Principal','{}'))
            except ValueError:
                return None
            return principal if principal.get('role') in ('admin','merchant') else None

    inner = ThreadingHTTPServer(('127.0.0.1',0),Internal)
    threading.Thread(target=inner.serve_forever,daemon=True,name='node-local-api').start()
    enrollment = threading.Lock()

    class Handler(PanelHandler):
        mgr = manager

        def setup(self):
            self.request.settimeout(10)
            super().setup()

        def do_GET(self):
            self._json({'ok':False,'error':'找不到接口'},404)

        def do_POST(self):
            self.connection.settimeout(30)
            expected = 'Bearer '+settings['key']
            if not hmac.compare_digest(self.headers.get('Authorization',''),expected):
                self._json({'ok':False,'error':'节点连接密钥无效'},401)
                return
            try:
                size = int(self.headers.get('Content-Length','0'))
                if not 0<size<=MAX_BODY:
                    raise ValueError('节点请求大小无效')
                body = json.loads(self.rfile.read(size))
                self._body_read = True
                if not isinstance(body,dict):
                    raise ValueError('节点请求格式无效')
                controller = self.headers.get('X-TGPanel-Controller','')
                if not re.fullmatch('[a-f0-9]{32}',controller):
                    raise ValueError('主面板身份无效')
                action = self.path.removeprefix('/node/v1/')
                if not self.path.startswith('/node/v1/'):
                    raise ValueError('节点接口无效')
                with enrollment:
                    owner = settings.get('controller')
                    if owner and owner != controller:
                        raise ValueError('该节点已连接其他主面板')
                    if action=='enroll':
                        settings['controller'] = controller
                        core.save_json(str(Path(core.BASE_DIR)/'node.json'),settings)
                        if os.name!='nt':
                            (Path(core.BASE_DIR)/'node.json').chmod(0o600)
                    elif action!='info' and not owner:
                        raise ValueError('请先在主面板连接该服务器')
                result = {'ok':True,'node_id':settings['id'],'protocol':PROTOCOL}
                if action in ('info','enroll'):
                    pass
                elif action=='status':
                    rows = {r['id']:r for r in manager.snapshot()}
                    result.update(bots=[bot_record(manager,bid,rows[bid]) for bid in rows],
                                  server=manager.server_monitor.snapshot())
                elif action=='create':
                    result['bot'] = create(manager,body)
                elif action=='assign':
                    moved,taken,cleared = manager.assign_bots(str(body.get('mid') or ''),body.get('bids') or [],bool(body.get('clear',True)))
                    manager.reset_shop_scans(cleared)
                    result.update(moved=moved,taken=taken,cleared=cleared)
                elif action=='unread':
                    result.update(self.forward('/api/archive/unread_all','POST',{'role':'admin'},
                                               json.dumps(body).encode()))
                elif action=='rpc':
                    bid = body.get('bid','')
                    parsed = urlsplit(body.get('path',''))
                    path = parsed.path
                    method = body.get('method')
                    principal = body.get('principal') or {}
                    if not isinstance(principal,dict):
                        raise ValueError('节点身份格式无效')
                    if (parsed.scheme or parsed.netloc or parsed.fragment or method not in ('GET','POST')
                            or principal.get('role') not in ('admin','merchant')
                            or not re.fullmatch(r'/api/(?:bots|shop|archive|ledger|miniapp)/'+re.escape(bid)+r'/[a-zA-Z0-9_./-]+',path)
                            or not re.fullmatch('[a-zA-Z0-9_-]{1,64}',bid)
                            or any(part in ('.','..') for part in path.split('/'))):
                        raise ValueError('节点转发路径无效')
                    if path.endswith('/remote'):
                        raise ValueError('受管节点不允许变更运行位置')
                    bot = manager.find(bid)
                    if method=='POST' and path=='/api/bots/'+bid+'/delete' and not bot and principal['role']=='admin':
                        result['response'] = dict(status=200,body=base64.b64encode(b'{"ok":true}').decode(),content_type='application/json')
                    else:
                        if not bot:
                            raise ValueError('服务器未找到此机器人')
                        payload = base64.b64decode(body.get('body',''),validate=True)
                        if len(payload)>12*1024*1024:
                            raise ValueError('转发内容过大')
                        # Mini App requests validate Telegram signatures inside the normal handler.
                        result.update(self.forward(body['path'],method,principal,payload))
                    result['bot'] = bot_record(manager,bid)
                else:
                    raise ValueError('节点接口无效')
                self._json(result)
            except (ValueError,KeyError,TypeError) as error:
                self._json({'ok':False,'error':str(error) if isinstance(error,ValueError) and not isinstance(error,json.JSONDecodeError) else '节点请求格式无效'},400)
            except (OSError,http.client.HTTPException):
                self._json({'ok':False,'error':'节点操作未确认，请检查状态后重试'},503)

        def forward(self,path,method,principal,payload):
            connection = http.client.HTTPConnection('127.0.0.1',inner.server_port,timeout=23)
            try:
                connection.request(method,path,payload if method=='POST' else None,
                    {'Content-Type':'application/json','X-Panel-Pass':internal_key,
                     'X-Node-Principal':json.dumps(principal,ensure_ascii=True)})
                response = connection.getresponse()
                data = response.read(MAX_BODY+1)
                if len(data)>MAX_BODY:
                    raise ValueError('节点响应过大')
                return dict(response=dict(status=response.status,body=base64.b64encode(data).decode(),
                                          content_type=response.getheader('Content-Type','application/json')))
            finally:
                connection.close()

    outer = ThreadingHTTPServer(address,Handler)
    outer.inner = inner
    return outer


def main(tls=True):
    root = Path(__file__).resolve().parent
    core.set_base_dir(str(root))
    settings = core.load_json(str(root/'node.json'),{})
    if not re.fullmatch('[a-f0-9]{32}',settings.get('id','')) or len(settings.get('key',''))<32:
        raise ValueError('请先运行节点安装脚本')
    manager = BotManager(core.load_json(core.CONFIG_FILE,{}))
    manager.server_monitor = ServerMonitor(manager).start()
    httpd = server(manager,settings,(settings.get('host','0.0.0.0'),int(settings.get('port',9443))))
    if tls:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(str(root/'node-cert.pem'),str(root/'node-key.pem'))
        httpd.socket = context.wrap_socket(httpd.socket,server_side=True,do_handshake_on_connect=False)
    elif settings.get('host')!='127.0.0.1':
        raise ValueError('隔离测试只允许监听本机')
    stop = threading.Event()
    for signum in (signal.SIGTERM,signal.SIGINT):
        signal.signal(signum,lambda *args:stop.set())
    threading.Thread(target=httpd.serve_forever,daemon=True,name='node-api').start()
    manager.start_all()
    manager.start_watchdog()
    core.save_json(str(root/'node-runtime.json'),dict(pid=os.getpid(),port=httpd.server_port))
    try:
        stop.wait()
    finally:
        manager.stop_watchdog();manager.stop_all();manager.server_monitor.close()
        httpd.shutdown();httpd.server_close();httpd.inner.shutdown();httpd.inner.server_close()


if __name__=='__main__':
    main()
