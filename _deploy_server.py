"""Incremental source release for the designated tgpanel server."""
import argparse
from datetime import datetime
import getpass
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys

from tools import source_release as release

HERE = Path(__file__).resolve().parent
HOST, USER = '165.154.66.129', 'ubuntu'
APP_DIR = '/home/ubuntu/tgpanel'


def local_files():
    return {name:str(HERE/name) for name in release.sources(HERE)}


def command(client, cmd, script=None):
    _, out, err = client.exec_command(cmd, timeout=240)
    if script is not None:
        out.channel.sendall(script.encode('utf-8'))
        out.channel.shutdown_write()
    stdout, stderr = out.read().decode('utf-8'), err.read().decode('utf-8')
    if out.channel.recv_exit_status():
        raise RuntimeError(stderr[-4000:] or stdout[-4000:] or 'Remote release failed')
    return stdout


def inspect(client, hashes):
    source = (HERE/'tools/source_release.py').read_text(encoding='utf-8')
    source = source.split("\nif __name__ == '__main__':", 1)[0]
    source += '\nprint(json.dumps({"bots":identity(ROOT),"inventory":inventory(ROOT,%r)}))' % sorted(hashes)
    return json.loads(command(client, APP_DIR+'/.venv/bin/python -', source))


def upload(client, cp, names, folder):
    parents = {str(PurePosixPath(cp)/folder/PurePosixPath(n).parent) for n in names}
    command(client, 'mkdir -m 700 -p '+ ' '.join(shlex.quote(p) for p in sorted(parents)))
    with client.open_sftp() as sftp:
        for name in sorted(names):
            sftp.put(str(HERE/name), str(PurePosixPath(cp)/folder/name))


def checkpoint(value):
    if not re.fullmatch(r'/home/ubuntu/tgpanel_checkpoints/release_[0-9_]+', value):
        raise argparse.ArgumentTypeError('Invalid release checkpoint')
    return value


def run(client, args):
    for action in ('rollback', 'verify'):
        cp = getattr(args, action, None)
        if cp:
            checkpoint(cp)
            print(command(client, APP_DIR+'/.venv/bin/python '+shlex.quote(cp+'/release.py')+
                          ' '+action+' '+shlex.quote(cp)), end='')
            return
    hashes = release.sources(HERE)
    remote = inspect(client, hashes)
    result = release.plan(hashes, remote['inventory'])
    print(json.dumps({'bots':remote['bots'], 'instances':list(remote['inventory']),
                      'changed_files':list(result['changes']), 'restart':result['restart']}, ensure_ascii=False, indent=2))
    if args.dry_run or not result['changes']:
        return result
    subprocess.run([sys.executable,'-m','unittest',*release.TESTS,'-q'], cwd=HERE,
                   env=dict(os.environ,PYTHONUTF8='1'),check=True)
    if release.sources(HERE) != hashes:
        raise RuntimeError('Local source changed during tests; generate a fresh plan')
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    cp = '/home/ubuntu/tgpanel_checkpoints/release_'+stamp
    command(client, 'mkdir -m 700 -p '+shlex.quote(str(PurePosixPath(cp).parent)))
    command(client, 'mkdir -m 700 '+shlex.quote(cp))
    upload(client, cp, hashes, 'candidate')
    upload(client, cp, [name+'.py' for name in release.TESTS], 'tests')
    with client.open_sftp() as sftp:
        sftp.put(str(HERE/'tools/source_release.py'), cp+'/release.py')
        with sftp.file(cp+'/plan.json','w') as handle:
            handle.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
    state = {'checkpoint':cp,'plan':result,'prepared':False,'deployed':False}
    (HERE/'output').mkdir(exist_ok=True)
    state_path = HERE/'output'/('release_'+stamp+'.json')
    for action in ('prepare','deploy'):
        state_path.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')
        print(command(client, APP_DIR+'/.venv/bin/python '+shlex.quote(cp+'/release.py')+
                      ' '+action+' '+shlex.quote(cp)), end='')
        state['prepared' if action=='prepare' else 'deployed'] = True
    state_path.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')
    print('Rollback: python _deploy_server.py --rollback '+cp)
    return state


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run',action='store_true',help='Read-only identity and source comparison')
    mode.add_argument('--rollback',type=checkpoint,help='Restore changed source from this checkpoint')
    mode.add_argument('--verify',type=checkpoint,help='Verify the published source and instances')
    parser.add_argument('--key',type=Path,help='SSH private key; otherwise use agent or password')
    args = parser.parse_args()
    import paramiko
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    password = os.environ.get('TGPANEL_SSH_PASSWORD')
    if not args.key and not password and sys.stdin.isatty():
        password = getpass.getpass('SSH password (blank uses SSH agent): ') or None
    try:
        client.connect(HOST,username=USER,password=password,
                       key_filename=str(args.key) if args.key else None,timeout=20)
        run(client,args)
    finally:
        client.close()


if __name__ == '__main__':
    main()
