"""Real-browser monitor layout, permissions and request lifecycle, with fake data."""
import copy,json,sys,tempfile,threading,time
from pathlib import Path
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import core
from _test_miniapp import setup_fixture
from manager import BotManager
from panel import PanelHandler
import nodes
from unittest.mock import patch
from playwright.sync_api import sync_playwright

output=Path(__file__).resolve().parents[1]/'output/playwright'
output.mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory() as folder:
 fixture=setup_fixture(folder);mgr=BotManager(fixture.cfg);mgr.bots=copy.deepcopy(fixture.bots)
 sample=dict(ready=True,updated_at=time.time(),interval=10,cores=4,uptime=86500,cpu_percent=2.3,
  memory=dict(total=8*1024**3,available=7*1024**3,percent=12.5),disk=dict(total=120*1024**3,free=110*1024**3,percent=8.3),
  network=dict(rx_per_second=1000,tx_per_second=600),local_running=2,remote_count=0,updates_per_minute=12,
  capacity=dict(additional=53,confidence='低负载样本，采用保守预算',limiting='cpu',memory_per_bot=96*1024**2,cpu_cores_per_bot=.05,memory_reserve=1.6*1024**3,sample_seconds=300,received_updates=20),
  workers=[dict(username='example_ledger',rss=43*1024**2,cpu=.004,updates_per_minute=12)],
  storage=dict(archive=300000,media=2000000,other=400000),alerts=[],history=[dict(ts=time.time()-600+i*10,cpu=2+i%5,memory=12.5+i%2) for i in range(60)])
 mgr.server_monitor=SimpleNamespace(snapshot=lambda:sample)
 registry=nodes.for_manager(mgr)
 registry.state['local']=dict(name='香港主服务器',renew_at=int(time.time())+86400,remind_at=int(time.time())-60)
 nid='b'*32
 remote=copy.deepcopy(sample);remote['capacity']['additional']=32;remote['local_running']=1
 remote['workers']=[dict(username='new_ledger_bot',rss=42*1024**2,cpu=.003,updates_per_minute=8)]
 registry.state['nodes']=[dict(id=nid,name='新加坡服务器',url='https://node.example.test:9443',key='FAKE_NODE_KEY_'*4,fingerprint='f'*64)]
 registry.cache[nid]=dict(online=True,updated_at=time.time(),server=remote,error='')
 registry.save()
 class Handler(PanelHandler):
  password='FAKE_MONITOR';merch=None;token_secret='FAKE_SECRET'
 Handler.mgr=mgr
 server=ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
 try:
  with sync_playwright() as p:
   browser=p.chromium.launch(headless=True)
   page=browser.new_page(viewport={'width':1280,'height':1000})
   errors=[];requests=[]
   page.on('pageerror',lambda error:errors.append(str(error)))
   page.on('request',lambda request:requests.append(request.url) if request.url.endswith('/api/server') else None)
   page.add_init_script("localStorage.setItem('panel_pw','FAKE_MONITOR');localStorage.setItem('panel_tab','server');")
   page.goto('http://127.0.0.1:%s/'%server.server_port)
   page.get_by_text('约 53 个',exact=True).wait_for()
   assert page.locator('#nodeMonitor_local .server-metric').count()==4
   assert page.locator('#nodeMonitor_'+nid+' .server-metric').count()==4
   page.locator('#nodeDetail_local summary').click()
   page.get_by_role('button',name='刷新',exact=True).click()
   page.wait_for_function('!SERVER_BUSY')
   assert page.locator('#nodeDetail_local').get_attribute('open') is not None
   page.locator('#nodeDetail_local summary').click()
   assert page.locator('#serverRenewBadge').is_visible()
   assert '1 台服务器' in page.locator('#serverRenewBadge').get_attribute('aria-label')
   page.locator('[data-pane=server]').screenshot(path=str(output/'server-renewal-red-dot.png'))
   assert '香港主服务器' in page.locator('#renewalAlerts').inner_text()
   assert '新加坡服务器' in page.locator('#serverWorkers').inner_text()
   assert 'new_ledger_bot' in page.locator('#serverWorkers').inner_text()
   page.get_by_text('约 32 个',exact=True).wait_for()
   page.locator('#nodeList .node-item').first.get_by_role('button',name='名称与续费',exact=True).click()
   page.locator('#nodeSettingsName').fill('香港一号')
   page.locator('#nodeRenewAt').fill('2027-01-01T12:00')
   page.locator('#nodeRemindAt').fill('2027-01-02T12:00')
   page.locator('#nodeSettingsSave').click()
   page.get_by_text('提醒时间不能晚于到期时间，请先填写到期时间',exact=True).wait_for()
   assert registry.state['local']['name']=='香港主服务器'
   page.locator('#nodeRemindAt').fill('2026-12-28T12:00')
   page.locator('#nodeSettingsSave').click()
   page.wait_for_function("document.getElementById('nodeSettingsForm').hidden")
   assert registry.state['local']['name']=='香港一号'
   assert registry.state['local']['renew_at']==1798776000
   assert not page.locator('#serverRenewBadge').is_visible()
   with patch.object(registry,'call',return_value={'ok':True}):
    page.locator('#nodeList .node-item').nth(1).get_by_role('button',name='设为新增默认',exact=True).click()
    page.wait_for_function("document.getElementById('addNode').value==='"+nid+"'")
   assert registry.state['default']==nid
   page.reload();page.get_by_text('约 53 个',exact=True).wait_for()
   assert page.locator('#addNode').input_value()==nid
   assert '香港一号' in page.locator('#nodeList').inner_text()
   page.get_by_role('button',name='添加服务器',exact=True).click()
   page.locator('#nodeName').fill('东京节点')
   page.locator('#nodeUrl').fill('https://new.example.test:9443')
   page.locator('#nodeKey').fill('FAKE_NEW_NODE_KEY_'*3)
   page.locator('#nodeFingerprint').fill('e'*64)
   with patch('nodes.request',return_value=dict(ok=True,protocol=1,node_id='c'*32,bots=[],server=remote)):
    page.locator('#nodeConnectionSave').click()
    page.wait_for_function("document.getElementById('nodeConnectionForm').hidden")
   assert registry.get('c'*32)['name']=='东京节点'
   assert page.locator('#nodeKey').input_value()==''
   registry.cache[nid].update(online=False,server=None)
   page.get_by_role('button',name='刷新',exact=True).click()
   page.get_by_text('服务器连接中断，实际运行状态待确认',exact=True).wait_for()
   assert page.locator('#nodeMonitor_'+nid+' .server-metric').count()==0
   assert page.locator('#nodeMonitor_local .server-metric').count()==4
   registry.cache[nid].update(online=True,server=remote,updated_at=time.time())
   page.get_by_role('button',name='刷新',exact=True).click()
   page.locator('#nodeMonitor_'+nid).get_by_text('约 32 个',exact=True).wait_for()
   page.get_by_text('约 53 个',exact=True).wait_for()
   page.screenshot(path=str(output/'server-monitor-desktop.png'),full_page=True)
   for width in (320,390):
    page.set_viewport_size({'width':width,'height':844})
    page.reload();page.get_by_text('约 53 个',exact=True).wait_for()
    assert page.locator('[data-pane=server]').bounding_box()['x']<width
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),width
    page.locator('#nodeList .node-item').first.get_by_role('button',name='名称与续费',exact=True).click()
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),('renewal',width)
    page.locator('#nodeSettingsForm').get_by_role('button',name='取消',exact=True).click()
    page.screenshot(path=str(output/('server-monitor-%s.png'%width)),full_page=True)
    if width==390:page.locator('#nodeList .node-item').first.screenshot(path=str(output/'server-monitor-card.png'))
   registry.settings(dict(id=nid,name='新加坡服务器',renew_at=int(time.time())+2*86400))
   page.evaluate("async () => {switchTab('bots');await loadNodes(true);loadServer(true);}")
   assert page.locator('#serverRenewBadge').is_visible()
   page.evaluate("switchTab('server')")
   page.wait_for_function('!SERVER_BUSY')
   assert page.locator('#serverRenewBadge').is_visible()
   button_box=page.locator('[data-pane=server]').bounding_box();dot_box=page.locator('#serverRenewBadge').bounding_box()
   assert dot_box['x']>button_box['x']+button_box['width']/2
   assert dot_box['y']<button_box['y']+button_box['height']/2
   page.locator('[data-pane=server]').screenshot(path=str(output/'server-renewal-red-dot-390.png'))
   registry.settings(dict(id=nid,name='新加坡服务器',renew_at=int(time.time())+30*86400))
   page.evaluate('async () => {await loadNodes(true);}')
   assert not page.locator('#serverRenewBadge').is_visible()
   page.evaluate("switchTab('bots');loadServer(true)")
   before=len(requests);page.wait_for_timeout(1200)
   assert len(requests)==before
   page.evaluate("ME={role:'merchant'};applyRoleTabs();switchTab('server')")
   assert page.evaluate('CUR_TAB')=='bots'
   assert not page.locator('[data-pane=server]').is_visible()
   assert not errors,errors
   browser.close()
  print(json.dumps({'desktop':True,'mobile':[320,390],'merchant_hidden':True,'polling_stops_when_hidden':True,'js_errors':errors}))
 finally:
  server.shutdown();server.server_close();thread.join()
