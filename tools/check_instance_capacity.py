"""Local isolated 100-instance exercise; fake Telegram, no production traffic."""
import argparse,json,os,sqlite3,subprocess,sys,tempfile,time
from pathlib import Path
from contextlib import closing
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import core,bot_instances
from manager import BotManager
from _test_bot_instances import BOOTSTRAP

MEMORY = r'''
import sys,ctypes,socket
from ctypes import wintypes
class Counters(ctypes.Structure):
 _fields_=[('cb',wintypes.DWORD),('PageFaultCount',wintypes.DWORD)]+[(name,ctypes.c_size_t) for name in ('PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage','QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage')]
def rss():
 if sys.platform!='win32':
  return int(next(l for l in Path('/proc/self/status').read_text().splitlines() if l.startswith('VmRSS:')).split()[1])*1024
 value=Counters();value.cb=ctypes.sizeof(value)
 api=ctypes.WinDLL('psapi').GetProcessMemoryInfo
 api.argtypes=[wintypes.HANDLE,ctypes.POINTER(Counters),wintypes.DWORD]
 if not api(wintypes.HANDLE(-1),ctypes.byref(value),value.cb):raise OSError('memory sample failed')
 return value.WorkingSetSize
original_save=core.save_json
def measured_save(path,value):
 if str(path).endswith('runtime.json'):
  value=dict(value,test_rss=rss(),test_services=sorted({n.split('.')[1] for n in sys.modules if n.startswith('runners.') and len(n.split('.'))>1}))
 return original_save(path,value)
core.save_json=measured_save
import requests
requests.Session.request=lambda *a,**kw: (_ for _ in ()).throw(AssertionError('External HTTP forbidden'))
original_connect=socket.socket.connect
def local_connect(sock,address):
 if address[0] not in ('127.0.0.1','::1'):
  Path('external_attempts.txt').write_text('blocked',encoding='utf-8')
  raise AssertionError('External network forbidden')
 return original_connect(sock,address)
socket.socket.connect=local_connect
'''

def wait(predicate,timeout=90):
 end=time.monotonic()+timeout
 while time.monotonic()<end:
  if predicate():return
  time.sleep(.1)
 raise TimeoutError('Isolated instance did not become ready')

def run(count):
 real_popen=subprocess.Popen
 bootstrap=BOOTSTRAP.replace('import instance_worker',MEMORY+'\nimport instance_worker')
 original=core.BASE_DIR
 with tempfile.TemporaryDirectory(prefix='tgpanel-capacity-') as directory:
  root=Path(directory).resolve()
  assert root.parent==Path(tempfile.gettempdir()).resolve() and root.name.startswith('tgpanel-capacity-')
  core.set_base_dir(str(root));Path(core.DATA_DIR).mkdir()
  manager=BotManager({'miniapp_base_url':'https://example.test'})
  try:
   with patch('bot_instances.subprocess.Popen',side_effect=lambda args,**kw:real_popen([args[0],'-u','-c',bootstrap],**kw)):
    for i in range(count):
     with patch('manager.TgAPI.call',return_value={'id':1000+i,'username':'capacity_%03d_bot'%i,'first_name':'Capacity'}):
      bot,error=manager.add('Capacity','%s:FAKE_CAPACITY'%(1000+i),'ledger')
      assert not error,error
     if (i+1)%20==0:print('started',i+1,flush=True)
    wait(lambda:all(r.info().get('status')=='running' for r in manager.runners.values()))
    for bot in manager.bots:
     core.save_json(str(bot_instances.folder(bot)/'incoming.json'),[{'update_id':1,'message':{'message_id':1,'chat':{'id':111,'type':'private'},'from':{'id':111,'first_name':'Fake'},'text':'/admin '+bot['bind_code']}}])
    wait(lambda:all(manager.find(b['id']).get('owner_id')==111 for b in manager.bots))
    sent=time.perf_counter();latencies={}
    for i,bot in enumerate(manager.bots):
     core.save_json(str(bot_instances.folder(bot)/'incoming.json'),[{'update_id':2,'message':{'message_id':2,'chat':{'id':-10000-i,'type':'supergroup','title':'Capacity'},'from':{'id':111,'first_name':'Fake'},'text':'+100'}}])
    def completed():
     for bot in manager.bots:
      if bot['id'] in latencies:continue
      file=bot_instances.folder(bot)/'outgoing.jsonl'
      text=file.read_text(encoding='utf-8') if file.exists() else ''
      if '100' in text and '入款' in text:latencies[bot['id']]=round((time.perf_counter()-sent)*1000,1)
     return len(latencies)==count
    wait(completed)
    for bot in manager.bots:
     path=bot_instances.folder(bot)/'data'/(bot['id']+'.sqlite3')
     with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
      assert db.execute('select count(*) from entries').fetchone()[0]==1
    infos=[r.info() for r in manager.runners.values()]
    assert len({i['pid'] for i in infos})==count
    assert all(i['test_services']==['ledger'] for i in infos)
    assert not any(root.rglob('external_attempts.txt'))
    values=sorted(latencies.values())
    return dict(instances=count,independent_pids=count,bills_written=count,only_ledger_loaded=True,external_calls=0,
                rss_total_mib=round(sum(i['test_rss'] for i in infos)/1024**2,1),
                rss_mean_mib=round(sum(i['test_rss'] for i in infos)/count/1024**2,1),
                batch_completion_ms=dict(p50=values[count//2],p95=values[min(count-1,int(count*.95))],max=values[-1]))
  finally:
   manager.stop_all();core.set_base_dir(original)

if __name__=='__main__':
 parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--count',type=int,default=100)
 parser.add_argument('--output',type=Path,default=Path('output/instance_capacity_100.json'))
 args=parser.parse_args();assert 1<=args.count<=100
 result=run(args.count);args.output.parent.mkdir(parents=True,exist_ok=True)
 args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
 print(json.dumps(result,ensure_ascii=False,indent=2))
