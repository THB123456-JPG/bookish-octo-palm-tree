"""Immutable per-bot source copies, scoped state merging and process supervision."""
import copy
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
from urllib.request import Request, urlopen

import core


def folder(bot):
    name = bot.get('instance_folder', '')
    if not re.fullmatch(r'[A-Za-z0-9_]{5,32}', name):
        raise ValueError('机器人用户名不是安全的实例目录名')
    parent = Path(core.BASE_DIR) / 'instances'
    target = parent / name
    if parent.is_symlink() or target.is_symlink() or target.resolve().parent != parent.resolve():
        raise ValueError('实例目录不在指定范围内')
    return target


def _stored(bot):
    return {k: copy.deepcopy(v) for k, v in bot.items() if k not in ('status', 'error')}


@contextmanager
def state_lock(path):
    with path.with_name('instance.lock').open('a+b') as lock:
        if os.name == 'nt':
            import msvcrt
            lock.seek(0)
            lock.write(b'0')
            lock.flush()
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == 'nt':
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def merge(current, before, after):
    """Apply only locally changed fields, retaining concurrent runtime changes."""
    result = copy.deepcopy(current)
    for key in before.keys() | after.keys():
        if key not in after:
            result.pop(key, None)
        elif key not in before or before[key] != after[key]:
            if all(isinstance(value, dict) for value in (current.get(key), before.get(key), after[key])):
                result[key] = merge(current[key], before[key], after[key])
            else:
                result[key] = copy.deepcopy(after[key])
    return result


def sync(bot, baseline, commit=False, child=False):
    path = Path(core.BOTS_FILE) if child else folder(bot) / 'bots.json'
    with state_lock(path):
        current = core.load_json(str(path), {})['bots'][0]
        if current['id'] != bot['id']:
            raise ValueError('实例身份与面板记录不一致')
        result = merge(current, baseline if baseline is not None else current, _stored(bot)) if baseline is not None else current
        if commit and result != current:
            core.save_json(str(path), {'bots': [result]})
        transient = {k: bot[k] for k in ('status', 'error') if k in bot}
        bot.update(result, **transient)
        for key in list(bot):
            if key not in result and key not in transient:
                bot.pop(key, None)
        return copy.deepcopy(result if commit else current)


def provision(bot, cfg, migrate=False):
    """Copy source once. Never overwrite an existing customer's customized code."""
    if bot.get('instance_folder'):
        target = folder(bot)
        manifest = core.load_json(str(target / 'INSTANCE.json'), {})
        if manifest.get('id') != bot['id']:
            raise ValueError('实例目录已存在或身份不符')
        return target
    candidate = dict(bot, instance_folder=bot.get('username', ''))
    target = folder(candidate)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise ValueError('该机器人用户名的实例目录已存在')
    legacy = [p for p in Path(core.DATA_DIR).iterdir() if p.name.startswith(bot['id'] + '.')] if migrate else []
    if any(p.is_symlink() or (p.is_dir() and any(c.is_symlink() for c in p.rglob('*'))) for p in legacy):
        raise ValueError('原数据目录包含符号链接，迁移已停止')
    stage = target.parent / ('.create-' + bot['id'])
    stage.mkdir()
    moved = []
    published = False
    try:
        source = Path(core.resource_path(''))
        for path in source.iterdir():
            if path.is_file() and not path.name.startswith('_') and (path.suffix in ('.py', '.html') or path.name == 'requirements.txt'):
                shutil.copy2(path, stage / path.name)
        for name in ('runners', 'static'):
            shutil.copytree(source / name, stage / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        (stage / 'data').mkdir()
        (stage / 'codes').mkdir()
        code = Path(core.BASE_DIR) / 'codes' / (bot['id'] + '.py')
        if code.is_file():
            shutil.copy2(code, stage / 'codes' / code.name)
        core.save_json(str(stage / 'bots.json'), {'bots': [_stored(candidate)]})
        core.save_json(str(stage / 'config.json'), {k: cfg[k] for k in ('welcome', 'miniapp_base_url', 'tron_api_keys') if k in cfg})
        hashes = {str(p.relative_to(stage)): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in stage.rglob('*.py')}
        core.save_json(str(stage / 'INSTANCE.json'), {'id': bot['id'], 'username': candidate['instance_folder'],
                       'type': bot['type'], 'created_at': int(time.time()), 'source_hashes': hashes})
        stage.rename(target)
        published = True
        # The caller must stop and join the old runner before moving its files.
        for path in legacy:
            destination = target / 'data' / path.name
            shutil.move(str(path), str(destination))
            moved.append((destination, path))
    except Exception:
        for destination, original in reversed(moved):
            shutil.move(str(destination), str(original))
        if published and target.exists():
            shutil.rmtree(target)
        if stage.exists() and stage.resolve().parent == target.parent.resolve():
            shutil.rmtree(stage)
        raise
    bot['instance_folder'] = candidate['instance_folder']
    return target


def remove(bot):
    target = folder(bot)
    manifest = core.load_json(str(target / 'INSTANCE.json'), {})
    if manifest.get('id') != bot['id']:
        raise ValueError('拒绝删除身份不符的实例目录')
    shutil.rmtree(target)


class _StopEvent(threading.Event):
    def __init__(self, runner):
        super().__init__()
        self.runner = runner

    def set(self):
        super().set()
        process = self.runner.process
        if process and process.poll() is None:
            process.terminate()


class InstanceRunner(threading.Thread):
    """Each child imports only its own source tree and owns its own poller."""
    def __init__(self, manager, bot):
        super().__init__(daemon=True, name='instance-' + bot['username'])
        self.mgr, self.bot, self.bid = manager, bot, bot['id']
        self.directory = folder(bot)
        self.process = None
        self.stop_evt = _StopEvent(self)
        self.api = core.TgAPI(bot['token'])
        self._expired_notice = {}

    def run(self):
        environment = dict(os.environ, PANEL_INSTANCE_CHILD='1')
        try:
            with (self.directory / 'process.log').open('ab') as output:
                self.process = subprocess.Popen([sys.executable, '-u', str(self.directory / 'instance_worker.py')],
                    cwd=self.directory, env=environment, stdout=output, stderr=output,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                if self.stop_evt.is_set():
                    self.process.terminate()
                code = self.process.wait()
        except OSError:
            self.bot.update(status='error', error='独立进程启动失败，请检查目录权限及 Python 环境')
            return
        self.bot['status'] = 'stopped' if self.stop_evt.is_set() else 'error'
        self.bot['error'] = '' if self.stop_evt.is_set() else '独立进程已退出（%s），请检查该机器人目录的 process.log' % code

    def join(self, timeout=None):
        super().join(timeout)
        if self.is_alive() and self.stop_evt.is_set() and self.process:
            self.process.kill()
            super().join(5)

    def info(self):
        data = core.load_json(str(self.directory / 'runtime.json'), {})
        return data if self.process and data.get('pid') == self.process.pid and self.process.poll() is None else {}

    def request(self, path, body=None):
        info = self.info()
        if not info.get('port'):
            raise OSError('机器人独立进程尚未就绪')
        request = Request('http://127.0.0.1:%d%s' % (info['port'], path),
                          None if body is None else json.dumps(body).encode(),
                          {'Content-Type': 'application/json', 'X-Panel-Pass': info['auth']})
        return urlopen(request, timeout=20)

    def send(self, uid, text, **kwargs):
        return self.api.call('sendMessage', chat_id=uid, text=text, **kwargs)

    def send_to_admins(self, text):
        for uid in self.bot.get('admin_ids') or []:
            self.send(uid, text)

    def stat(self):
        return self.info().get('stat', 0)

    def stat_label(self):
        return '记账群'
