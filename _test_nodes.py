"""Real isolated node and bot processes, mocked Telegram and no external traffic."""
import base64
import copy
import hashlib
from http.server import ThreadingHTTPServer
import io
import json
import os
import shutil
import ssl
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request,urlopen
import unittest
from unittest.mock import patch,MagicMock

import core
import bot_instances
import nodes
from manager import BotManager
from panel import PanelHandler
import node_worker
from _test_bot_instances import BOOTSTRAP
from _test_miniapp import signed

REAL_POPEN = subprocess.Popen
BLOCK = r'''
import socket
_connect=socket.socket.connect
def _local(sock,address):
 if address[0] not in ('127.0.0.1','::1'):
  raise AssertionError('External network disabled')
 return _connect(sock,address)
socket.socket.connect=_local
'''
CHILD = BOOTSTRAP.replace('import instance_worker',BLOCK+'\nimport instance_worker')
NODE = r'''
import subprocess,core
real=subprocess.Popen
import bot_instances
child=CHILD
bot_instances.subprocess.Popen=lambda args,**kw:real([args[0],'-u','-c',child],**kw)
def api(self,method,**kwargs):
 if method=='getMe':
  number=int(self.token.split(':')[0])
  return dict(id=number,username='client_%s_bot'%number,first_name='Fake')
 raise AssertionError('No Telegram call may leave the node')
core.TgAPI.call=api
BLOCK
import node_worker
node_worker.main(tls=False)
'''.replace('CHILD',repr(CHILD)).replace('\nBLOCK\n','\n'+BLOCK+'\n')


class NodesTests(unittest.TestCase):
    def test_three_day_badge_uses_expiry_for_all_servers_and_clears_after_renewal(self):
        before=core.BASE_DIR
        with tempfile.TemporaryDirectory() as directory:
            try:
                core.set_base_dir(directory);registry=nodes.for_manager(BotManager({}))
                registry.state['nodes']=[dict(id='a'*32,name='新节点',url='https://example.test',key='FAKE'*12,fingerprint='')]
                now=2000000000
                with patch('nodes.time.time',return_value=now):
                    self.assertFalse(registry.listing()['local']['renewal_soon'])
                    for remaining,expected in ((3*86400+1,False),(3*86400,True),(1,True),(0,True),(-1,True)):
                        for nid in ('','a'*32):
                            with self.subTest(remaining=remaining,nid=nid):
                                registry.settings(dict(id=nid,name='服务器',renew_at=now+remaining))
                                listing=registry.listing();row=listing['nodes'][0] if nid else listing['local']
                                self.assertEqual(row['renewal_soon'],expected)
                    registry.settings(dict(name='提前提醒',renew_at=now+7*86400,remind_at=now-1))
                    self.assertEqual(registry.listing()['local']['renewal_status'],'due')
                    self.assertFalse(registry.listing()['local']['renewal_soon'])
                    registry.settings(dict(id='a'*32,name='已续费',renew_at=now+30*86400))
                    self.assertFalse(registry.listing()['nodes'][0]['renewal_soon'])
                    registry.settings(dict(name='清除日期'))
                    self.assertFalse(registry.listing()['local']['renewal_soon'])
            finally:core.set_base_dir(before)

    def test_renewal_reminders_persist_without_network_or_affecting_bots(self):
        before=core.BASE_DIR
        with tempfile.TemporaryDirectory() as directory:
            try:
                core.set_base_dir(directory);manager=BotManager({});registry=nodes.for_manager(manager)
                registry.state['nodes']=[dict(id='a'*32,name='新节点',url='https://example.test',key='FAKE'*12,fingerprint='')]
                with patch('nodes.request',side_effect=AssertionError('Reminders never use network')):
                    registry.settings(dict(name='香港主机',renew_at=2000,remind_at=1000))
                    registry.settings(dict(id='a'*32,name='备用服务器',renew_at=3000,remind_at=2500))
                    for invalid in (dict(name=''),dict(name='bad',renew_at=2000,remind_at=2001),dict(name='bad',remind_at=1000),dict(name='bad',renew_at=-1)):
                        with self.assertRaises(ValueError):registry.settings(invalid)
                    restored=nodes.Nodes(manager)
                    with patch('nodes.time.time',return_value=1500):
                        listing=restored.listing();self.assertEqual(listing['local']['renewal_status'],'due')
                        self.assertEqual(listing['nodes'][0]['renewal_status'],'')
                    with patch('nodes.time.time',return_value=2000):self.assertEqual(restored.listing()['local']['renewal_status'],'overdue')
                    restored.settings(dict(name='已续费',renew_at=4000,remind_at=3500))
                    with patch('nodes.time.time',return_value=2000):self.assertEqual(restored.listing()['local']['renewal_status'],'')
                    restored.settings(dict(name='取消提醒'))
                    self.assertEqual(restored.listing()['local']['renew_at'],0)
                self.assertEqual(manager.runners,{})
            finally:core.set_base_dir(before)

    def test_real_https_self_signed_pin_and_access_key(self):
        executable=shutil.which('openssl')
        if not executable and os.name=='nt':
            executable=next((str(p) for p in (Path('C:/Program Files/Git/usr/bin/openssl.exe'),Path('C:/Program Files/Git/mingw64/bin/openssl.exe')) if p.exists()),None)
        if not executable:self.skipTest('OpenSSL required for isolated TLS verification')
        before=core.BASE_DIR;server=None
        with tempfile.TemporaryDirectory() as directory:
            try:
                root=Path(directory);core.set_base_dir(directory)
                subprocess.run([executable,'req','-x509','-newkey','rsa:2048','-nodes','-days','1',
                    '-keyout',str(root/'key.pem'),'-out',str(root/'cert.pem'),'-subj','/CN=localhost'],check=True,capture_output=True)
                config=dict(id='d'*32,key='FAKE_TLS_KEY_'*4)
                server=node_worker.server(BotManager({}),config,('127.0.0.1',0))
                context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(root/'cert.pem',root/'key.pem')
                server.socket=context.wrap_socket(server.socket,server_side=True,do_handshake_on_connect=False)
                threading.Thread(target=server.serve_forever,daemon=True).start()
                fingerprint=hashlib.sha256(ssl.PEM_cert_to_DER_cert((root/'cert.pem').read_text())).hexdigest()
                node=dict(id=config['id'],key=config['key'],url='https://127.0.0.1:%s'%server.server_port,fingerprint=fingerprint)
                self.assertTrue(nodes.request(node,'e'*32,'info')['ok'])
                self.assertTrue(nodes.request(node,'e'*32,'enroll')['ok'])
                with self.assertRaises(nodes.NodeError):nodes.request(dict(node,key='WRONG'*12),'e'*32,'info')
                with self.assertRaises(nodes.NodeError):nodes.request(dict(node,fingerprint='0'*64),'e'*32,'info')
            finally:
                if server:server.shutdown();server.server_close();server.inner.shutdown();server.inner.server_close()
                core.set_base_dir(before)

    def test_transport_pins_identity_before_credentials_and_rejects_plain_remote_http(self):
        with self.assertRaises(ValueError):nodes.endpoint('http://203.0.113.2:9443')
        for value in ('https://user:pass@host','https://host/path','https://host/#fragment'):
            with self.assertRaises(ValueError):nodes.endpoint(value)
        connection=MagicMock();connection.sock.getpeercert.return_value=b'wrong certificate'
        with patch('nodes.http.client.HTTPSConnection',return_value=connection):
            with self.assertRaises(nodes.NodeError):
                nodes.request(dict(url='https://example.test:9443',fingerprint='0'*64,key='FAKE'*12),'a'*32,'info')
        connection.request.assert_not_called()

    def test_bundle_has_only_sources_and_installer_never_overwrites_existing_node(self):
        with tarfile.open(fileobj=io.BytesIO(nodes.bundle()),mode='r:gz') as archive:
            names=archive.getnames()
            self.assertTrue({'node_worker.py','node_install.sh','manager.py','solo/relay.py'}<=set(names))
            self.assertFalse(any(n in names for n in ('nodes.json','node.json','bots.json','config.json')))
            self.assertFalse(any(n.startswith(('data/','instances/','output/')) for n in names))
            installer=archive.extractfile('node_install.sh').read()
            self.assertNotIn(b'\r',installer)
            self.assertIn(b'[ -e "$NODE_ROOT" ]',installer)

    def test_new_node_runs_all_types_old_process_stays_put_and_outages_never_fail_over(self):
        before=core.BASE_DIR
        process=None;api_server=None;manager=None;node_output=None
        package=nodes.bundle()
        with tempfile.TemporaryDirectory(prefix='tgpanel-nodes-') as directory:
            root=Path(directory).resolve()
            self.assertEqual(root.parent,Path(tempfile.gettempdir()).resolve())
            main=root/'main';main.mkdir();remote=root/'node';remote.mkdir()
            with tarfile.open(fileobj=io.BytesIO(package),mode='r:gz') as archive:
                for member in archive.getmembers():
                    self.assertTrue((remote/member.name).resolve().is_relative_to(remote))
                    path=remote/member.name;path.parent.mkdir(parents=True,exist_ok=True)
                    path.write_bytes(archive.extractfile(member).read())
            (remote/'data').mkdir();(remote/'codes').mkdir()
            core.save_json(str(remote/'node.json'),dict(id='b'*32,key='FAKE_NODE_KEY_'*4,host='127.0.0.1',port=0))
            core.save_json(str(remote/'config.json'),{})
            node_output=(remote/'test-process.log').open('ab')
            try:
                core.set_base_dir(str(main));Path(core.DATA_DIR).mkdir()
                manager=BotManager({'miniapp_base_url':'https://example.test','miniapp_footer_text':'Main footer'})
                registry=nodes.for_manager(manager)
                class Handler(PanelHandler):
                    mgr=manager;password='FAKE_ADMIN';merch=None
                api_server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
                threading.Thread(target=api_server.serve_forever,daemon=True).start()
                def call(path,body=None):
                    request=Request('http://127.0.0.1:%s%s'%(api_server.server_port,path),
                                    json.dumps(body).encode() if body is not None else None,
                                    {'X-Panel-Pass':'FAKE_ADMIN','Content-Type':'application/json'})
                    try:response=urlopen(request,timeout=30)
                    except HTTPError as error:response=error
                    with response:return response.status,json.load(response)
                def wait(condition):
                    end=time.monotonic()+20
                    while time.monotonic()<end:
                        if condition():return
                        time.sleep(.1)
                    self.fail('Isolated node check timed out')
                def add(number,kind='ledger',**extra):
                    with patch('manager.TgAPI.call',return_value={'id':number,'username':'client_%s_bot'%number,'first_name':'Fake'}):
                        status,value=call('/api/bots',dict(token='%s:FAKE_NODE'%number,type=kind,**extra))
                    self.assertTrue(value['ok'],value)
                    return manager.find(value['bot']['id'])
                with patch('bot_instances.subprocess.Popen',side_effect=lambda args,**kw:REAL_POPEN([args[0],'-u','-c',CHILD],**kw)):
                    old=add(701,node_id='')
                    wait(lambda:manager.runners[old['id']].info().get('status')=='running')
                    old_pid=manager.runners[old['id']].info()['pid']
                    process=REAL_POPEN([sys.executable,'-u','-c',NODE],cwd=remote,stdout=node_output,stderr=node_output,
                                      env=dict(os.environ,PYTHONUTF8='1'),creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
                    wait(lambda:(remote/'node-runtime.json').exists())
                    port=core.load_json(str(remote/'node-runtime.json'),{})['port']
                    node=dict(name='新服务器',url='http://127.0.0.1:%s'%port,key='FAKE_NODE_KEY_'*4,fingerprint='')
                    self.assertTrue(call('/api/nodes',node)[1]['ok'])
                    nid='b'*32
                    listing=call('/api/nodes')[1]
                    self.assertNotIn(node['key'],json.dumps(listing))
                    self.assertTrue(call('/api/nodes/settings',dict(name='主服务器 A',renew_at=2000,remind_at=1000))[1]['ok'])
                    self.assertTrue(call('/api/nodes/settings',dict(id=nid,name='新增服务器 B'))[1]['ok'])
                    self.assertEqual(manager.snapshot()[0]['server_name'],'主服务器 A')
                    self.assertTrue(call('/api/nodes/default',{'id':nid})[1]['ok'])
                    created=[add(702+i,kind) for i,kind in enumerate(('ledger','shop','kefu','usdt'))]
                    def ready():
                        registry.refresh(nid)
                        return all(b.get('status')=='running' for b in created)
                    wait(ready)
                    self.assertEqual(next(b for b in manager.snapshot() if b['id']==created[0]['id'])['server_name'],'新增服务器 B')
                    for bot in created:
                        self.assertEqual(bot['node_id'],nid)
                        self.assertNotIn(bot['id'],manager.runners)
                        self.assertFalse((main/'instances'/bot['username']).exists())
                        self.assertTrue((remote/'instances'/bot['username']/'data').is_dir())
                    ledger=created[0];folder=remote/'instances'/ledger['username']
                    def update(text,index,chat=None):
                        core.save_json(str(folder/'incoming.json'),[{'update_id':index,'message':dict(message_id=index,text=text,date=int(time.time()),
                            chat={'id':chat or 111,'type':'supergroup' if chat else 'private'},
                            **{'from':{'id':111,'first_name':'Fake'}})}])
                        wait(lambda:not (folder/'incoming.json').exists())
                    update('/admin '+ledger['bind_code'],1)
                    wait(lambda:core.load_json(str(folder/'bots.json'),{})['bots'][0].get('owner_id')==111)
                    for index,chat in ((2,-111),(3,-222)):update('+100',index,chat)
                    auth=signed(ledger['token'],111)
                    wait(lambda:len(call('/api/miniapp/'+ledger['id']+'/overview',dict(init_data=auth,payload={}))[1].get('data',{}).get('groups',[]))==2)
                    for action in ('overview','bills','statistics'):
                        result=call('/api/miniapp/'+ledger['id']+'/'+action,dict(init_data=auth,payload={'chat_id':-111}))[1]
                        self.assertTrue(result['ok'],result)
                    denied=call('/api/miniapp/'+ledger['id']+'/bills',dict(init_data=signed(ledger['token'],222),payload={'chat_id':-111}))
                    self.assertEqual(denied[0],403)
                    self.assertFalse((Path(core.DATA_DIR)/(ledger['id']+'.sqlite3')).exists())
                    self.assertTrue(call('/api/archive/'+ledger['id']+'/chats')[1]['ok'])
                    archive_url='/api/archive/'+ledger['id']
                    wait(lambda:len(call(archive_url+'/chats')[1].get('chats',[]))==2)
                    bills_before=call('/api/miniapp/'+ledger['id']+'/bills',dict(init_data=auth,payload={'chat_id':-111}))[1]
                    deleted=call(archive_url+'/clear',{'chat_id':-111})[1]
                    self.assertTrue(deleted['ok'])
                    self.assertGreater(deleted['deleted'],0)
                    self.assertEqual([c['chat_id'] for c in call(archive_url+'/chats')[1]['chats']],['-222'])
                    self.assertEqual(call('/api/miniapp/'+ledger['id']+'/bills',dict(init_data=auth,payload={'chat_id':-111}))[1],bills_before)
                    self.assertFalse((Path(core.DATA_DIR)/(ledger['id']+'.archive.sqlite3')).exists())
                    update('New archive message',4,-111)
                    wait(lambda:len(call(archive_url+'/chats')[1].get('chats',[]))==2)
                    self.assertEqual([m['text'] for m in call(archive_url+'/messages?chat=-111')[1]['messages']],['New archive message'])
                    self.assertTrue(call('/api/archive/unread_all',{'seen':{}})[1]['ok'])
                    shop=created[1]
                    self.assertTrue(call('/api/shop/'+shop['id']+'/config',{'notice':'Node only'})[1]['ok'])
                    self.assertEqual(call('/api/shop/'+shop['id']+'/config')[1]['config']['notice'],'Node only')
                    self.assertTrue(call('/api/shop/'+shop['id']+'/config',{'provider_key':'FAKE_OLD_OWNER'})[1]['ok'])
                    moved,taken,cleared=manager.assign_bots('customer_a',[shop['id']])
                    self.assertEqual((moved,taken,cleared),(1,0,[shop['id']]))
                    self.assertFalse(call('/api/shop/'+shop['id']+'/config')[1]['config'].get('provider_key'))
                    with patch.object(Handler,'_gate',return_value={'role':'merchant','mid':'customer_a'}):
                        self.assertTrue(call('/api/shop/'+shop['id']+'/config')[1]['ok'])
                        self.assertEqual(call('/api/archive/'+ledger['id']+'/chats')[0],404)
                    with patch.object(Handler,'_gate',return_value={'role':'merchant','mid':'wrong'}):
                        self.assertEqual(call('/api/shop/'+shop['id']+'/config')[0],404)
                        self.assertEqual(call('/api/nodes')[0],403)
                        self.assertEqual(call('/api/nodes/settings',dict(name='NO'))[0],403)
                    actual=nodes.request
                    def lost(node,controller,action,*args,**kw):
                        value=actual(node,controller,action,*args,**kw)
                        if action=='create':raise nodes.NodeError('Lost response')
                        return value
                    with patch('nodes.request',side_effect=lost):pending=add(706)
                    self.assertTrue(pending['node_pending'])
                    pending_dir=remote/'instances'/pending['username']
                    wait(lambda:(pending_dir/'runtime.json').exists())
                    pending_pid=core.load_json(str(pending_dir/'runtime.json'),{})['pid']
                    self.assertTrue(call('/api/bots/'+pending['id']+'/retrydeploy',{})[1]['ok'])
                    self.assertEqual(core.load_json(str(pending_dir/'runtime.json'),{})['pid'],pending_pid)
                    self.assertFalse(pending['node_pending'])
                    self.assertFalse(call('/api/nodes/'+nid+'/remove',{})[1]['ok'])
                    with self.assertRaises(nodes.NodeError):
                        nodes.request(registry.get(nid),'c'*32,'info')
                    self.assertEqual(manager.runners[old['id']].info()['pid'],old_pid)
                    with patch('nodes.request',side_effect=nodes.NodeError('Offline')):
                        registry.refresh(nid)
                        status,value=call('/api/bots',dict(token='707:FAKE_NODE',type='ledger'))
                        self.assertFalse(value['ok'])
                        self.assertEqual(call('/api/bots/'+ledger['id']+'/restart',{})[0],503)
                        manager.start_all()
                    self.assertEqual(manager.runners[old['id']].info()['pid'],old_pid)
                    self.assertNotIn(ledger['id'],manager.runners)
                    registry.refresh(nid)
                    remote_pid=core.load_json(str(folder/'runtime.json'),{})['pid']
                    self.assertTrue(call('/api/bots/'+ledger['id']+'/restart',{})[1]['ok'])
                    wait(lambda:core.load_json(str(folder/'runtime.json'),{}).get('pid')!=remote_pid)
                    self.assertEqual(manager.runners[old['id']].info()['pid'],old_pid)
                    self.assertTrue(call('/api/bots/'+pending['id']+'/delete',{})[1]['ok'])
                    self.assertIsNone(manager.find(pending['id']))
                    self.assertFalse(pending_dir.exists())
                    self.assertTrue((main/'instances'/old['username']).exists())
            finally:
                if process:
                    if os.name=='nt':
                        subprocess.run(['taskkill','/PID',str(process.pid),'/T','/F'],capture_output=True)
                    else:process.terminate()
                    try:process.wait(30)
                    except subprocess.TimeoutExpired:process.kill();process.wait(10)
                if manager:manager.stop_all()
                if api_server:api_server.shutdown();api_server.server_close()
                if node_output:node_output.close()
                core.set_base_dir(before)


if __name__=='__main__':unittest.main()
