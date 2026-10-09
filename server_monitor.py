"""Read-only Linux host samples; bounded history, no business database queries."""
from collections import deque
import copy
import math
import os
from pathlib import Path
import shutil
import threading
import time

import core

MIB = 1024**2
GIB = 1024**3
INTERVAL = 10


def process_sample(pid):
    path = Path('/proc')/str(pid)
    fields = (path/'stat').read_text().split(') ', 1)[1].split()
    values = dict(line.split(':', 1) for line in (path/'status').read_text().splitlines())
    return dict(pid=pid, start=int(fields[19]), ticks=int(fields[11])+int(fields[12]),
                rss=int(values.get('VmRSS','0 kB').split()[0])*1024,
                peak=int(values.get('VmHWM','0 kB').split()[0])*1024)


def host_sample(root):
    memory = {line.split(':')[0]:int(line.split()[1])*1024
              for line in Path('/proc/meminfo').read_text().splitlines() if len(line.split()) >= 2}
    cpu = list(map(int,Path('/proc/stat').read_text().splitlines()[0].split()[1:9]))
    disk = shutil.disk_usage(root)
    network = [line.split(':',1)[1].split() for line in Path('/proc/net/dev').read_text().splitlines()[2:]
               if line.split(':',1)[0].strip() != 'lo']
    cores = len(os.sched_getaffinity(0)) if hasattr(os,'sched_getaffinity') else os.cpu_count() or 1
    return dict(cpu_ticks=sum(cpu), idle_ticks=cpu[3]+cpu[4], cores=cores,
                memory_total=memory['MemTotal'], memory_available=memory['MemAvailable'],
                swap_used=memory.get('SwapTotal',0)-memory.get('SwapFree',0),
                disk_total=disk.total, disk_free=disk.free,
                uptime=float(Path('/proc/uptime').read_text().split()[0]),
                rx=sum(int(v[0]) for v in network), tx=sum(int(v[8]) for v in network))


def storage_usage(paths):
    result = dict(archive=0, media=0, other=0, by_bot={})
    seen = set()
    for bid, path in paths:
        path = Path(path)
        if path.resolve() in seen or path.is_symlink():
            continue
        seen.add(path.resolve())
        total = 0
        for directory, folders, files in os.walk(path, followlinks=False):
            folders[:] = [name for name in folders if not (Path(directory)/name).is_symlink()]
            for name in files:
                file = Path(directory)/name
                try:
                    if file.is_symlink():
                        continue
                    size = file.stat().st_size
                except OSError:
                    continue
                category = 'media' if any(p.endswith('.archive-media') for p in file.parts) else (
                    'archive' if '.archive.sqlite3' in name else 'other')
                result[category] += size
                total += size
        if bid:
            result['by_bot'][bid] = total
    result['updated_at'] = time.time()
    return result


def percentile(values, fraction=.95):
    values = sorted(values)
    return values[min(len(values)-1, math.ceil(len(values)*fraction)-1)] if values else 0


def estimate(host, history, workers, storage, complete=True):
    ledger = [r for r in workers if r['type']=='ledger' and r.get('rss')]
    if not ledger or not history or not complete:
        return dict(additional=None, reason='等待完整的记账实例采样')
    memory_budget = max(96*MIB, max(r['peak'] for r in ledger)*1.5)
    cpu_budget = max(.05, percentile([h['bot_cpu'] for h in history])*2)
    memory_reserve = max(GIB, host['memory_total']*.2)
    available = min(h['available'] for h in history)
    cpu_peak = percentile([h['cpu'] for h in history])*host['cores']/100
    disk_budget = max(256*MIB, max((storage.get('by_bot',{}).get(r['id'],0) for r in ledger),default=0)*3)
    limits = dict(memory=max(0,int((available-memory_reserve)//memory_budget)),
                  cpu=max(0,int((host['cores']*.7-cpu_peak)//cpu_budget)),
                  disk=max(0,int((host['disk_free']-max(2*GIB,host['disk_total']*.1))//disk_budget)))
    elapsed = history[-1]['ts']-history[0]['ts']
    received = sum(h['updates'] for h in history)
    return dict(additional=min(limits.values()), limits=limits,
                limiting=min(limits,key=limits.get), memory_per_bot=round(memory_budget),
                cpu_cores_per_bot=round(cpu_budget,3), memory_reserve=round(memory_reserve),
                sample_seconds=round(elapsed), received_updates=received,
                confidence='近期业务参考' if elapsed>=300 and received>=100 else '低负载样本，采用保守预算')


class ServerMonitor:
    def __init__(self, manager):
        self.manager = manager
        self.root = Path(core.BASE_DIR)
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.history = deque(maxlen=180)  # ponytail: 30-minute bounded aggregates, no per-message history.
        self.previous = None
        self.storage = dict(archive=0,media=0,other=0,by_bot={},updated_at=0)
        self.result = dict(ready=False, reason='正在采样，请稍候', updated_at=0)
        self.thread = None

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run,daemon=True,name='server-monitor')
            self.thread.start()
        return self

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(2)

    def snapshot(self):
        with self.lock:
            result = copy.deepcopy(self.result)
        result['stale'] = bool(result.get('updated_at') and time.time()-result['updated_at']>INTERVAL*3)
        return result

    def _run(self):
        if not Path('/proc/meminfo').exists():
            with self.lock:
                self.result = dict(ready=False,reason='服务器监控需运行在 Linux 主机',updated_at=time.time())
            return
        while not self.stop_event.is_set():
            try:
                self.collect()
            except (OSError,ValueError,KeyError,TypeError):
                with self.lock:
                    self.result = dict(self.result, error='本次采样失败，保留上次数据')
            self.stop_event.wait(INTERVAL)

    def collect(self):
        host = host_sample(self.root)
        now, monotonic = time.time(), time.monotonic()
        with self.manager.lock:
            bots = [dict(id=b['id'],username=b.get('username') or b['id'],type=b.get('type'),
                         remote=bool(b.get('remote') or b.get('node_id')),instance_folder=b.get('instance_folder'))
                    for b in self.manager.bots]
            runners = dict(self.manager.runners)
        paths, workers, raw, complete = [('',self.root/'data')], [], {}, True
        local_running, remote = 0, 0
        previous = self.previous
        elapsed = monotonic-previous['time'] if previous else 0
        received = 0
        for b in bots:
            if b['remote']:
                remote += 1
                continue
            if b['instance_folder']:
                # folder() also rejects unsafe names and symlink directories.
                from bot_instances import folder
                directory = folder(b)
                paths.append((b['id'],directory/'data'))
            runner = runners.get(b['id'])
            if not runner or not runner.is_alive():
                continue
            local_running += 1
            row = dict(id=b['id'],username=b['username'],type=b['type'],rss=None,peak=0,cpu=None,updates_per_minute=None)
            try:
                info = runner.info() if b['instance_folder'] else {}
                if not b['instance_folder']:
                    # Threads share the panel process: never invent a per-bot RSS.
                    complete = False
                    workers.append(row)
                    continue
                proc = process_sample(int(info['pid']))
                proc['received'] = int(info.get('received_updates',0))
                raw[b['id']] = proc
                row.update(rss=proc['rss'],peak=proc['peak'])
                before = previous['workers'].get(b['id']) if previous else None
                if before and (before['pid'],before['start']) == (proc['pid'],proc['start']) and elapsed>0:
                    row['cpu'] = max(0,(proc['ticks']-before['ticks'])/os.sysconf('SC_CLK_TCK')/elapsed)
                    updates = max(0,proc['received']-before['received'])
                    received += updates
                    row['updates_per_minute'] = round(updates*60/elapsed,1)
            except (OSError,ValueError,KeyError):
                complete = False
            workers.append(row)
        previous_host = previous['host'] if previous else None
        total = host['cpu_ticks']-previous_host['cpu_ticks'] if previous else 0
        cpu = max(0,min(100,(1-(host['idle_ticks']-previous_host['idle_ticks'])/total)*100)) if total>0 else None
        self.previous = dict(time=monotonic,host=host,workers=raw)
        if cpu is not None:
            self.history.append(dict(ts=now,cpu=cpu,available=host['memory_available'],
                                     memory=(1-host['memory_available']/host['memory_total'])*100,
                                     bot_cpu=max((r['cpu'] or 0 for r in workers if r['type']=='ledger'),default=0),
                                     updates=received))
        # Directory sizes are sampled in the background every five minutes, never per HTTP request.
        if now-self.storage['updated_at']>=300:
            self.storage = storage_usage(paths)
        capacity = estimate(host,list(self.history),workers,self.storage,complete)
        memory_pct = (1-host['memory_available']/host['memory_total'])*100
        disk_pct = (1-host['disk_free']/host['disk_total'])*100
        alerts = []
        if memory_pct>=80: alerts.append('内存余量不足，请准备扩容')
        if disk_pct>=85: alerts.append('磁盘余量不足，请检查数据与备份')
        if len(self.history)>=6 and all(h['cpu']>=70 for h in list(self.history)[-6:]):
            alerts.append('CPU 持续繁忙，请减少新增实例')
        if host['swap_used']>0: alerts.append('正在使用交换空间，请关注响应速度')
        if capacity.get('additional') is not None and capacity['additional']<5:
            alerts.append('预计可新增不足 5 个，请准备扩容')
        result = dict(ready=True,updated_at=now,interval=INTERVAL,cores=host['cores'],
                      uptime=host['uptime'],cpu_percent=round(cpu,1) if cpu is not None else None,
                      memory=dict(total=host['memory_total'],available=host['memory_available'],percent=round(memory_pct,1)),
                      disk=dict(total=host['disk_total'],free=host['disk_free'],percent=round(disk_pct,1)),
                      network=dict(rx_per_second=max(0,(host['rx']-previous_host['rx'])/elapsed) if elapsed>0 else None,
                                   tx_per_second=max(0,(host['tx']-previous_host['tx'])/elapsed) if elapsed>0 else None),
                      local_running=local_running,remote_count=remote,workers=workers,
                      updates_per_minute=round(received*60/elapsed,1) if elapsed>0 else None,
                      storage=self.storage,capacity=capacity,alerts=alerts,
                      history=[dict(ts=h['ts'],cpu=round(h['cpu'],1),memory=round(h['memory'],1)) for h in self.history])
        with self.lock:
            self.result = result
