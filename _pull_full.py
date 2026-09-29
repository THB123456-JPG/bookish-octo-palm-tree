# -*- coding: utf-8 -*-
"""把完整版记账机器人的源码拉回本地，好跟面板里那份逐行对比

只拉「功能代码」：handlers / services/ledger / services/price / services/broadcast
/ services/forward / config / docs，**不拉 .venv**（几百 MB 的第三方库）。
"""
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
import paramiko

HOST, USER = '47.236.8.82', 'root'
KEY = os.path.join(os.path.expanduser('~'), 'Desktop', 'aliyun秘钥.pem')
TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_full_ledger')

WANT = [
    'handlers', 'services/ledger', 'services/price', 'services/broadcast',
    'services/forward', 'config', 'docs',
]
WANT_FILES = ['bot.py', 'README.md', 'CHANGELOG.md', 'requirements.txt',
              'ARCHITECTURE.md', 'services/runtime.py', 'services/support_relay.py']

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(HOST, username=USER, key_filename=KEY, timeout=25)
sftp = cli.open_sftp()

os.makedirs(TMP, exist_ok=True)
n = 0
total = 0


def grab(remote, local):
    global n, total
    os.makedirs(os.path.dirname(local), exist_ok=True)
    sftp.get(remote, local)
    size = os.path.getsize(local)
    n += 1
    total += size
    print('  %7d  %s' % (size, remote))


def walk(remote_dir, local_dir):
    try:
        entries = sftp.listdir_attr(remote_dir)
    except IOError:
        return
    for e in entries:
        if e.filename in ('__pycache__', '.venv', '.git', '.pytest_cache',
                          'outputs', 'feature_backups'):
            continue
        r = remote_dir + '/' + e.filename
        l = os.path.join(local_dir, e.filename)
        import stat
        if stat.S_ISDIR(e.st_mode):
            walk(r, l)
        elif e.filename.endswith(('.py', '.md', '.txt', '.json', '.ini',
                                  '.example', '.cfg')):
            try:
                grab(r, l)
            except Exception as ex:
                print('  跳过 %s（%s）' % (r, ex))


print('拉取中…')
for w in WANT:
    walk('/project/' + w, os.path.join(TMP, w))
for f in WANT_FILES:
    try:
        grab('/project/' + f, os.path.join(TMP, f))
    except Exception as ex:
        print('  跳过 %s（%s）' % (f, ex))

sftp.close()
cli.close()
print()
print('共 %d 个文件，%.1f KB → %s' % (n, total / 1024.0, TMP))
