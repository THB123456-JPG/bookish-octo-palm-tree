"""Project-scoped source plans, isolated rehearsal and data-preserving rollback."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen

ROOT = Path('/home/ubuntu/tgpanel')
ROOT_SOURCES = set(('主程序.py archive.py bot_instances.py core.py customer_config.py '
    'customer_images.py customer_ui.py instance_worker.py login_log.py manager.py '
    'merchants.py miniapp.py nodes.py node_worker.py panel.py runtime_cleanup.py server_monitor.py solo_pack.py totp.py tron.py '
    'messages_page.html miniapp_page.html panel_page.html requirements.txt').split())
PUBLIC_FILES = {'_deploy_server.py', 'tools/source_release.py', 'tools/check_isolated_load.py',
    'README.md', 'CUSTOMER_MINIAPP.md', 'LOCAL_WORKFLOW.md', '优化检查报告.md',
    '使用说明.txt', '记账机器人_客户使用说明.txt', '功能清单.txt', '独立实例运行.md', 'node_install.sh', '运行服务器接入.md'}
TESTS = ['_test_source_release', '_test_nodes', '_test_server_monitor', '_test_archive_replies', '_test_audit_runtime', '_test_bot_instances',
    '_test_client_onboarding', '_test_customer_config', '_test_miniapp', '_test_customer_ui',
    '_test_personnel', '_test_disable_cutoff', '_test_entry_formats', '_test_personal_pricing',
    '_test_owner_statistics', '_test_group_ready', '_test_hosted_mode', '_test_solo_config',
    '_test_miniapp_scope', '_test_tron_miniapp']


def allowed(name):
    if not isinstance(name, str) or '\\' in name or any(p in ('', '.', '..') for p in name.split('/')):
        return False
    p = PurePosixPath(name)
    return name in ROOT_SOURCES | PUBLIC_FILES or (
        p.parts[0] == 'runners' and p.suffix == '.py' and
        all(not part.startswith('_') or part == '__init__.py' for part in p.parts[1:])) or name in {
            'runners/__init__.py', 'runners/ledger/assets/idcode.txt', 'runners/ledger/assets/phone.dat',
            'static/manifest.webmanifest', 'static/icon-192.png', 'static/icon-512.png',
            'static/apple-touch-icon.png', 'solo/独立版.py', 'solo/relay.py',
            'solo/install.sh', 'solo/config.example.json'}


def shared(name):
    return name in ROOT_SOURCES or name.startswith(('runners/', 'static/'))


def node_sources(root):
    return [n for n in sources(root) if shared(n) or n.startswith('solo/') or n in
            ('tools/source_release.py','node_install.sh','运行服务器接入.md','记账机器人_客户使用说明.txt')]


def safe(root, name):
    path = root / name
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Path leaves source directory: '+name)
    if any(p.is_symlink() for p in [path, *path.parents] if p == root or root in p.parents):
        raise ValueError('Symlink is not a source target: '+name)
    return path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def sources(root):
    candidates = [root/n for n in ROOT_SOURCES | PUBLIC_FILES]
    candidates += [p for folder in ('runners', 'static', 'solo') for p in (root/folder).rglob('*') if p.is_file()]
    return {str(p.relative_to(root)).replace('\\', '/'): sha(safe(root, str(p.relative_to(root))))
            for p in candidates if p.is_file() and allowed(str(p.relative_to(root)).replace('\\', '/'))}


def directories(root):
    result = ['.']
    bots = json.loads((root/'bots.json').read_text(encoding='utf-8'))['bots']
    for bot in bots:
        name = bot.get('instance_folder')
        if not name:
            continue
        if not re.fullmatch(r'[A-Za-z0-9_]{5,32}', name):
            raise ValueError('Invalid instance folder')
        relative = 'instances/'+name
        path = safe(root, relative)
        manifest = json.loads((path/'INSTANCE.json').read_text(encoding='utf-8'))
        if manifest.get('id') != bot['id'] or manifest.get('username') != name or relative in result:
            raise ValueError('Instance identity mismatch: '+name)
        result.append(relative)
    return sorted(result)


def inventory(root, names):
    if not names or any(not allowed(n) for n in names):
        raise ValueError('Release contains non-source files')
    return {d: {n:sha(safe(root, n if d == '.' else d+'/'+n)) for n in sorted(names)
                if d == '.' or shared(n)} for d in directories(root)}


def plan(local, remote):
    changes = {}
    for directory, hashes in remote.items():
        for name, before in hashes.items():
            after = local[name]
            if directory != '.' and before not in (None, remote['.'][name], after):
                raise ValueError('Instance source differs from the template; preserve custom code: '+directory+'/'+name)
            if before != after:
                relative = name if directory == '.' else directory+'/'+name
                changes[relative] = {'source':name, 'before':before, 'after':after}
    return dict(sources=local, baseline=remote, changes=changes,
                restart=any(shared(row['source']) or row['source'].startswith('solo/') for row in changes.values()))


def validate(root, release):
    current = inventory(root, release['sources'])
    if current != release['baseline']:
        raise ValueError('Source or instance membership changed; generate a fresh plan')
    if plan(release['sources'], current) != release:
        raise ValueError('Invalid release plan')


def identity(root):
    info = dict(line.split('=', 1) for line in subprocess.check_output(
        ['systemctl', 'show', 'tgpanel', '-p', 'ActiveState', '-p', 'WorkingDirectory',
         '-p', 'ExecStart', '-p', 'KillMode'], text=True).splitlines())
    if info['ActiveState'] != 'active' or info['WorkingDirectory'] != str(root) or \
       str(root/'主程序.py') not in info['ExecStart'] or info['KillMode'] != 'control-group':
        raise ValueError('Unexpected production service identity')
    names = []
    for bot in json.loads((root/'bots.json').read_text())['bots']:
        try:
            with urlopen('https://api.telegram.org/bot'+bot['token']+'/getMe', timeout=20) as response:
                me = json.load(response)
            if not me.get('ok') or me['result']['username'] != bot['username'] or \
               str(me['result']['id']) != bot['token'].split(':', 1)[0]:
                raise ValueError()
        except Exception:
            raise RuntimeError('Read-only Telegram identity verification failed') from None
        names.append(bot['username'])
    if not names:
        raise ValueError('No registered bots to verify')
    return names


def protected(root):
    result = {}
    for directory in directories(root):
        d = root/directory
        paths = [d/n for n in ('config.json', 'bots.json', 'merchants.json', 'nodes.json', 'node.json', 'INSTANCE.json')]
        paths += [p for folder in ('data', 'codes') for p in (d/folder).rglob('*') if p.is_file()]
        result.update({str(p.relative_to(root)):sha(p) for p in paths if p.is_file()})
    return result


def install(src, dst):
    dst.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=dst.parent, delete=False) as handle:
        temporary = Path(handle.name)
    try:
        shutil.copy2(src, temporary)
        os.replace(temporary, dst)
    finally:
        temporary.unlink(missing_ok=True)


def backup(root, cp, release):
    validate(root, release)
    for relative, row in release['changes'].items():
        if row['before'] is not None:
            install(safe(root, relative), cp/'backup'/relative)


def restore(root, cp, release, changed=None):
    names = list(release['changes']) if changed is None else changed
    for relative in names:
        safe(root, relative)
        before = release['changes'][relative]['before']
        if before is not None and sha(cp/'backup'/relative) != before:
            raise ValueError('Rollback checkpoint corrupted: '+relative)
    for relative in names:
        dst = safe(root, relative)
        if release['changes'][relative]['before'] is None:
            dst.unlink(missing_ok=True)
        else:
            install(cp/'backup'/relative, dst)


def apply(root, cp, release, changed=None):
    validate(root, release)
    for name, digest in release['sources'].items():
        if sha(cp/'candidate'/name) != digest:
            raise ValueError('Candidate changed after verification: '+name)
    for relative, row in release['changes'].items():
        if changed is not None:
            changed.append(relative)
        install(cp/'candidate'/row['source'], safe(root, relative))


def service(action):
    subprocess.run(['sudo', '-n', 'systemctl', action, 'tgpanel'], check=True)


def verify(root, release, pids=None):
    current = inventory(root, release['sources'])
    if set(current) != set(release['baseline']) or any(
        digest != release['sources'][name] for hashes in current.values() for name, digest in hashes.items()):
        raise ValueError('Published source or instance set does not match release')
    if subprocess.check_output(['systemctl','is-active','tgpanel'],text=True).strip() != 'active':
        raise RuntimeError('Service is not active')
    for directory, old_pid in (pids or {}).items():
        end = time.monotonic()+35
        while time.monotonic() < end:
            runtime = json.loads((root/directory/'runtime.json').read_text())
            process = Path('/proc/%s' % runtime.get('pid'))
            if runtime.get('pid') != old_pid and runtime.get('status') == 'running' and process.exists():
                if str(root/directory/'instance_worker.py').encode() in (process/'cmdline').read_bytes():
                    break
            time.sleep(.5)
        else:
            raise RuntimeError('Instance did not resume: '+directory)
    cfg = json.loads((root/'config.json').read_text())
    host = cfg.get('host') or '127.0.0.1'
    if host == '0.0.0.0':
        host = '127.0.0.1'
    with urlopen('http://%s:%s/' % (host, cfg.get('port') or 8080), timeout=10) as response:
        if response.status != 200:
            raise RuntimeError('Panel health check failed')


def deploy(root, cp, release):
    identity(root)
    validate(root, release)
    if json.loads((cp/'ready.json').read_text()) != release['sources']:
        raise ValueError('Isolated checks have not passed for this source version')
    pids = {}
    if release['restart']:
        for d in directories(root):
            if d != '.':
                runtime = json.loads((root/d/'runtime.json').read_text())
                if runtime.get('status') == 'running':
                    pids[d] = runtime['pid']
        service('stop')
    changed = []
    try:
        before = protected(root) if release['restart'] else None
        apply(root, cp, release, changed)
        if before is not None and protected(root) != before:
            raise RuntimeError('Customer data changed during installation')
        if release['restart']:
            service('start')
        verify(root, release, pids)
    except Exception:
        if release['restart']:
            service('stop')
        try:
            restore(root, cp, release, changed)
        finally:
            if release['restart']:
                service('start')
        raise
    (cp/'deployed.json').write_text(json.dumps({'files':list(release['changes']),
        'instances':len(release['baseline'])-1, 'restart':release['restart'], 'verified':True}))


def prepare(root, cp, release):
    identity(root)
    backup(root, cp, release)
    rehearsal = cp/'rehearsal'
    rehearsal.mkdir()
    for name in release['sources']:
        path = root/name
        if path.is_file():
            install(path, rehearsal/name)
    # Rehearse replacing and restoring each distinct changed template file.
    for name in {row['source'] for row in release['changes'].values()}:
        original = sha(rehearsal/name)
        install(cp/'candidate'/name, rehearsal/name)
        if original is None:
            (rehearsal/name).unlink()
        else:
            install(root/name, rehearsal/name)
        if sha(rehearsal/name) != original:
            raise RuntimeError('Rollback rehearsal failed')
    for name, digest in release['sources'].items():
        if sha(cp/'candidate'/name) != digest:
            raise ValueError('Candidate hash mismatch: '+name)
        install(cp/'candidate'/name, rehearsal/name)
    for name in TESTS:
        install(cp/'tests'/(name+'.py'), rehearsal/(name+'.py'))
    with (cp/'regression.log').open('w',encoding='utf-8') as log:
        result = subprocess.run([str(root/'.venv/bin/python'),'-m','unittest',*TESTS,'-q'],
                                cwd=rehearsal,stdout=log,stderr=subprocess.STDOUT,timeout=180)
    if result.returncode:
        raise RuntimeError('Isolated regressions failed; see '+str(cp/'regression.log'))
    subprocess.run([str(root/'.venv/bin/python'),'tools/check_isolated_load.py',
                    '--output',str(cp/'isolated_load.json')],cwd=rehearsal,check=True,timeout=120)
    (cp/'ready.json').write_text(json.dumps(release['sources']))


def main():
    action, cp_text = sys.argv[1:3]
    cp = Path(cp_text)
    if cp.resolve() != cp or cp.parent != ROOT.parent/'tgpanel_checkpoints' or not re.fullmatch(r'release_[0-9_]+', cp.name):
        raise ValueError('Checkpoint outside designated directory')
    os.umask(0o077)
    # One release at a time; the OS also unlocks if the SSH process dies.
    import fcntl
    with (cp.parent/'release.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run_action(action, cp)


def run_action(action, cp):
    release = json.loads((cp/'plan.json').read_text())
    if action == 'prepare':
        prepare(ROOT, cp, release)
    elif action == 'deploy':
        deploy(ROOT, cp, release)
    elif action == 'verify':
        identity(ROOT)
        verify(ROOT, release)
    elif action == 'rollback':
        identity(ROOT)
        current = inventory(ROOT, release['sources'])
        if set(current) != set(release['baseline']):
            raise ValueError('Instance membership changed since release')
        for relative, row in release['changes'].items():
            if sha(safe(ROOT, relative)) not in (row['before'], row['after']):
                raise ValueError('Newer source would be overwritten: '+relative)
        if release['restart']:
            service('stop')
        try:
            before = protected(ROOT) if release['restart'] else None
            restore(ROOT, cp, release)
            if any(sha(safe(ROOT, name)) != row['before'] for name, row in release['changes'].items()):
                raise RuntimeError('Rollback source verification failed')
            if before is not None and protected(ROOT) != before:
                raise RuntimeError('Customer data changed during rollback')
        finally:
            if release['restart']:
                service('start')
    else:
        raise ValueError('Use prepare, deploy, verify or rollback')
    print(action+': completed', flush=True)


if __name__ == '__main__':
    main()
