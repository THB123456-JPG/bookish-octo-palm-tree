"""Managed execution servers; credentials stay on the panel, data stays on its node."""
import base64
import copy
import hashlib
import hmac
import http.client
import io
import json
import os
from pathlib import Path
import re
import secrets
import ssl
import tarfile
import threading
import time
from urllib.parse import urlsplit

import core

PROTOCOL = 1
MAX_BODY = 24*1024*1024
MIRROR = ('id type token note username name bind_code admin_ids owner_id admin_name '
          'admin_username bound_at enabled created created_ts duration expire_at expired_at mid archive').split()


class NodeError(OSError):
    pass


def endpoint(value):
    parsed = urlsplit(str(value or '').strip())
    if (parsed.scheme not in ('https','http') or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in ('','/') or parsed.query or parsed.fragment
            or any(c.isspace() for c in value)):
        raise ValueError('请填写完整服务器地址，例如 https://服务器IP:9443')
    if parsed.scheme == 'http' and parsed.hostname not in ('127.0.0.1','::1'):
        raise ValueError('远程服务器必须使用 HTTPS')
    if parsed.port is not None and not 1 <= parsed.port <= 65535:
        raise ValueError('服务器端口无效')
    return parsed


def request(node, controller, action, data=None, timeout=25):
    parsed = endpoint(node['url'])
    connection = None
    try:
        if parsed.scheme == 'https':
            context = ssl.create_default_context()
            if node.get('fingerprint'):
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE  # Identity is pinned before sending any credential.
            connection = http.client.HTTPSConnection(parsed.hostname,parsed.port,timeout=4,context=context)
        else:
            connection = http.client.HTTPConnection(parsed.hostname,parsed.port,timeout=4)
        connection.connect()
        if parsed.scheme == 'https' and node.get('fingerprint'):
            digest = hashlib.sha256(connection.sock.getpeercert(binary_form=True)).hexdigest()
            if not hmac.compare_digest(digest,node['fingerprint']):
                raise NodeError('服务器证书指纹不符，未发送连接密钥')
        connection.sock.settimeout(timeout)
        payload = json.dumps(data or {},ensure_ascii=False).encode()
        if len(payload)>MAX_BODY:
            raise ValueError('服务器请求过大')
        connection.request('POST','/node/v1/'+action,payload,
                           {'Content-Type':'application/json','Authorization':'Bearer '+node['key'],
                            'X-TGPanel-Controller':controller})
        response = connection.getresponse()
        raw = response.read(MAX_BODY+1)
        if len(raw)>MAX_BODY:
            raise NodeError('服务器响应过大')
        result = json.loads(raw)
        if not isinstance(result,dict):
            raise NodeError('服务器响应格式无效')
        if response.status != 200 or not result.get('ok'):
            raise NodeError(result.get('error') or '服务器拒绝请求')
        if node.get('id') and result.get('node_id') != node['id']:
            raise NodeError('运行服务器身份发生变化，请检查节点目录')
        return result
    except NodeError:
        raise
    except (OSError,ValueError,http.client.HTTPException):
        raise NodeError('服务器连接失败，请检查地址、证书、密钥及端口；未自动重试操作') from None
    finally:
        if connection:
            connection.close()


def bot_record(manager, bid, snapshot=None):
    bot = manager.find(bid)
    if not bot:
        return None
    item = snapshot if snapshot is not None else next(row for row in manager.snapshot() if row['id']==bid)
    return dict(values={k:copy.deepcopy(bot[k]) for k in MIRROR if k in bot},
                status=item['status'],error=item['error'],stat=item['stat'],stat_label=item['stat_label'],
                folder=bot.get('instance_folder',''),has_secrets=item.get('has_secrets',False))


def bundle():
    from tools.source_release import node_sources
    root = Path(core.resource_path(''))
    names = node_sources(root)
    output = io.BytesIO()
    with tarfile.open(fileobj=output,mode='w:gz') as archive:
        for name in sorted(set(names)):
            path = root/name
            if not path.is_file() or path.is_symlink():
                raise ValueError('运行节点安装文件缺失')
            info = tarfile.TarInfo(name)
            content = path.read_bytes()
            info.size,info.mode = len(content),0o644
            archive.addfile(info,io.BytesIO(content))
    return output.getvalue()


class Nodes:
    def __init__(self, manager):
        self.manager = manager
        self.path = Path(core.BASE_DIR)/'nodes.json'
        self.state = core.load_json(str(self.path),{}) or {}
        self.state.setdefault('controller',secrets.token_hex(16))
        self.state.setdefault('default','')
        self.state.setdefault('nodes',[])
        self.state.setdefault('local',{'name':'本机服务器'})
        self.lock = threading.RLock()
        self.cache = {}
        self.stopped = threading.Event()
        self.thread = None

    def save(self):
        core.save_json(str(self.path),self.state)
        if os.name != 'nt':
            self.path.chmod(0o600)

    def get(self, nid):
        with self.lock:
            node = next((n for n in self.state['nodes'] if n['id']==nid),None)
            if not node:
                raise NodeError('未找到运行服务器')
            return dict(node)

    def call(self, nid, action, data=None, timeout=25):
        return request(self.get(nid),self.state['controller'],action,data,timeout)

    def register(self, body):
        previous = self.get(body['id']) if body.get('id') else {}
        url = str(body.get('url') or '').strip().rstrip('/')
        endpoint(url)
        fingerprint = str(body.get('fingerprint') or '').replace(':','').strip().lower()
        if fingerprint and not re.fullmatch('[a-f0-9]{64}',fingerprint):
            raise ValueError('请填写完整 SHA256 证书指纹')
        key = str(body.get('key') or previous.get('key') or '').strip()
        name = str(body.get('name') or '').strip()
        if not name or len(name)>40 or len(key)<32 or len(key)>256 or any(c.isspace() for c in key):
            raise ValueError('请填写服务器名称及安装时生成的连接密钥')
        node = dict(previous,name=name,url=url,key=key,fingerprint=fingerprint)
        info = request(node,self.state['controller'],'info')
        if info.get('protocol') != PROTOCOL or not re.fullmatch('[a-f0-9]{32}',info.get('node_id','')):
            raise ValueError('运行节点版本不兼容，请使用当前面板的节点安装包')
        node['id'] = info['node_id']
        if previous and previous['id'] != node['id']:
            raise ValueError('不能用其他服务器替换已有节点，避免机器人错配')
        with self.lock:
            if not previous and any(n['id']==node['id'] for n in self.state['nodes']):
                raise ValueError('该服务器已经添加')
            self.save()  # Persist the controller identity before claiming a node.
        request(node,self.state['controller'],'enroll')
        with self.lock:
            self.state['nodes'] = [n for n in self.state['nodes'] if n['id']!=node['id']]+[node]
            self.save()
        self.refresh(node['id'])
        return node['id']

    def set_default(self, nid):
        if nid:
            self.call(nid,'info',timeout=5)
        with self.lock:
            self.state['default'] = nid
            self.save()

    def settings(self, body):
        name = str(body.get('name') or '').strip()
        if not name or len(name)>40:
            raise ValueError('服务器名称须为 1–40 字')
        try:
            renew,remind = (int(body.get(k) or 0) for k in ('renew_at','remind_at'))
        except (TypeError,ValueError):
            raise ValueError('续费时间无效') from None
        if min(renew,remind)<0 or max(renew,remind)>4102444800 or (remind and (not renew or remind>renew)):
            raise ValueError('提醒时间不能晚于到期时间，请先填写到期时间')
        nid = str(body.get('id') or '')
        with self.lock:
            if nid:
                self.get(nid)
                target = next(n for n in self.state['nodes'] if n['id']==nid)
            else:
                target = self.state['local']
            target.update(name=name,renew_at=renew,remind_at=remind)
            self.save()

    def remove(self, nid):
        with self.manager.lock:
            if any(b.get('node_id')==nid for b in self.manager.bots):
                raise ValueError('此服务器仍有机器人，不能移除连接')
            with self.lock:
                self.get(nid)
                self.state['nodes'] = [n for n in self.state['nodes'] if n['id']!=nid]
                if self.state['default']==nid:
                    self.state['default']=''
                self.cache.pop(nid,None)
                self.save()

    def merge(self, nid, record):
        if not record:
            return
        values = record['values']
        with self.manager.lock:
            bot = self.manager.find(values['id'])
            if not bot or bot.get('node_id') != nid:
                return
            before = copy.deepcopy(bot)
            bot.update({k:copy.deepcopy(values[k]) for k in MIRROR if k in values})
            bot.update(status=record['status'],error=record['error'],node_pending=False,
                       node_folder=record['folder'],node_stat=record['stat'],
                       node_stat_label=record['stat_label'],node_has_secrets=record['has_secrets'])
            if bot != before:
                self.manager.save()

    def refresh(self, nid):
        try:
            value = self.call(nid,'status',timeout=5)
            records = value.pop('bots',[])
            for record in records:
                self.merge(nid,record)
            found = {r['values']['id'] for r in records}
            with self.manager.lock:
                for bot in self.manager.bots:
                    if bot.get('node_id')==nid and bot['id'] not in found:
                        bot.update(status='error',error='服务器未找到此实例，请重试部署或检查节点目录')
            snapshot = dict(online=True,updated_at=time.time(),server=value.get('server'),error='')
        except NodeError as error:
            snapshot = dict(online=False,updated_at=time.time(),server=None,error=str(error))
        with self.lock:
            self.cache[nid] = snapshot
        return snapshot

    def listing(self):
        with self.lock:
            now = time.time()
            def renewal(n):
                renew,remind = n.get('renew_at',0),n.get('remind_at',0)
                return dict(renew_at=renew,remind_at=remind,
                            renewal_soon=bool(renew and now>=renew-3*86400),
                            renewal_status='overdue' if renew and now>=renew else
                            'due' if remind and now>=remind else '')
            return dict(default=self.state['default'],local=dict(self.state['local'],**renewal(self.state['local'])),nodes=[dict(
                **{k:n[k] for k in ('id','name','url','fingerprint')},
                **renewal(n),
                **copy.deepcopy(self.cache.get(n['id'],dict(online=False,updated_at=0,error='等待连接验证',server=None))))
                for n in self.state['nodes']])

    def provision(self, bot):
        shared = {k:self.manager.cfg[k] for k in ('miniapp_base_url','welcome','tron_api_keys','miniapp_footer_text') if k in self.manager.cfg}
        value = self.call(bot['node_id'],'create',dict(bot={k:bot[k] for k in MIRROR if k in bot},shared=shared))
        self.merge(bot['node_id'],value['bot'])
        return value

    def rpc(self, bot, path, method, principal, body=b''):
        data = dict(bid=bot['id'],path=path,method=method,principal=principal,
                    body=base64.b64encode(body).decode())
        result = self.call(bot['node_id'],'rpc',data)
        self.merge(bot['node_id'],result.get('bot'))
        response = result['response']
        if method=='POST' and path=='/api/bots/'+bot['id']+'/delete' and response['status']==200:
            answer = json.loads(base64.b64decode(response['body']))
            if answer.get('ok'):
                with self.manager.lock:
                    self.manager.bots = [b for b in self.manager.bots if b['id']!=bot['id']]
                    self.manager.save()
        return response

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run,daemon=True,name='node-status')
            self.thread.start()
        return self

    def _run(self):
        while not self.stopped.is_set():
            with self.lock:
                ids = [n['id'] for n in self.state['nodes']]
            for nid in ids:
                if self.stopped.is_set():
                    break
                self.refresh(nid)
            self.stopped.wait(10)

    def close(self):
        self.stopped.set()


def for_manager(manager):
    with manager.lock:
        if not hasattr(manager,'nodes'):
            manager.nodes = Nodes(manager)
        return manager.nodes


def proxy(handler, path, method, principal):
    """Only per-bot routes; the node repeats the existing tenant and role checks."""
    match = re.fullmatch(r'/api/(bots|shop|archive|ledger)/([a-zA-Z0-9_-]{1,64})/[^?]+',path)
    if not match:
        return False
    bot = handler.mgr.find(match[2])
    if not bot or not bot.get('node_id'):
        return False
    if principal is None:
        principal = handler._gate()
        if principal is None:
            return True
    if handler._bot_of(principal,bot['id']) is None:
        return True
    try:
        data = handler._raw_body(12*1024*1024) if method=='POST' else b''
        if path.endswith('/remote'):
            raise ValueError('运行服务器由创建时确定，不能切换客户自建模式')
        if method=='POST' and path.endswith('/token'):
            used = handler.mgr.token_used((json.loads(data or b'{}').get('token') or ''),bot['id'])
            if used:
                raise ValueError('此 Token 已属于其他机器人')
        if method=='POST' and path.endswith('/retrydeploy'):
            if not handler._admin_only(principal):
                return True
            if not bot.get('node_pending'):
                raise ValueError('此实例已完成部署，请刷新状态后检查')
            for_manager(handler.mgr).provision(bot)
            handler._json({'ok':True})
            return True
        response = for_manager(handler.mgr).rpc(bot,handler.path,method,principal,data)
        handler._send(response['status'],base64.b64decode(response['body']),response['content_type'])
    except ValueError as error:
        handler._json({'ok':False,'error':str(error)},400)
    except (NodeError,KeyError):
        handler._json({'ok':False,'error':'远程操作未确认，请刷新状态后检查；不会在本机接管或重复执行'},503)
    return True


def handle(handler, path, method, principal):
    if not path.startswith('/api/nodes'):
        return False
    if not handler._admin_only(principal):
        return True
    registry = for_manager(handler.mgr)
    try:
        if method=='GET' and path=='/api/nodes/package':
            handler._send(200,bundle(),'application/gzip')
        elif method=='GET' and path=='/api/nodes':
            handler._json({'ok':True,**registry.listing()})
        elif method=='POST' and path=='/api/nodes':
            nid = registry.register(handler._body())
            handler._json({'ok':True,'id':nid})
        elif method=='POST' and path=='/api/nodes/default':
            registry.set_default(str(handler._body().get('id') or ''))
            handler._json({'ok':True})
        elif method=='POST' and path=='/api/nodes/settings':
            registry.settings(handler._body())
            handler._json({'ok':True})
        elif method=='POST' and re.fullmatch('/api/nodes/[a-f0-9]{32}/(?:check|remove)',path):
            nid,action = path.split('/')[-2:]
            handler._body()
            value = registry.refresh(nid) if action=='check' else registry.remove(nid)
            handler._json({'ok':True,'result':value})
        else:
            handler._json({'ok':False,'error':'找不到接口'},404)
    except (NodeError,ValueError) as error:
        handler._json({'ok':False,'error':str(error)},400)
    return True
