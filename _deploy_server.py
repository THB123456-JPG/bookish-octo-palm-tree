# -*- coding: utf-8 -*-
"""把本地代码传到服务器并重启面板

用法：
    python _deploy_server.py --dry-run     # 只看看会动哪些文件，什么都不改
    python _deploy_server.py               # 真传 + 重启

★ 三条安全规则（改这个脚本时别破坏）：
  1. **绝不碰服务器上的数据** —— config.json / bots.json / merchants.json /
     data/ / 运行日志.txt 只读不写。它们和本地的不一样（服务器监听 0.0.0.0）。
  2. **传之前先备份** —— 服务器代码整份打包到 /home/ubuntu/ 下的备份目录，
     出问题能一条命令回滚（脚本最后会打印回滚命令）。
  3. **先传完再删旧的** —— 顺序反了的话，中途失败会留下一个起不来的服务。

连的是 ~/.ssh/tg_server 私钥（密码登录服务器已经关了）。
"""
import argparse
import os
import posixpath
import sys
import time

import paramiko

sys.stdout.reconfigure(encoding='utf-8')

HOST = '152.32.225.245'
USER = 'ubuntu'
KEY = os.path.expanduser('~/.ssh/tg_server')
APP_DIR = '/home/ubuntu/tgpanel'
SERVICE = 'tgpanel'

HERE = os.path.dirname(os.path.abspath(__file__))

# 只读，绝不覆盖（服务器上的和本地不一样）
KEEP_ON_SERVER = {'config.json', 'bots.json', 'merchants.json', '运行日志.txt'}
# 不用传
SKIP_FILES = KEEP_ON_SERVER | {
    'TG客服多开版.exe',          # Windows 的，服务器上用不着（38MB）
    'merchants.json.tmp',
}
SKIP_DIRS = {'data', '__pycache__', 'build', 'dist', '.git', '.venv',
             '_github_upload', '_apidoc', '_bak_panel_page.html',
             # ★ 从客户服务器拉下来的「完整版」参考源码（只在本地对比用，
             #   234KB，没必要传上去）
             '_full_ledger', '_srv_tron'}
# ★ 探测/对比用的一次性脚本，别往服务器上搬
SKIP_PREFIX = ('_tmp_', '_probe_', '_pull_', '_cmp_', '_dbg_', '_shot_')

# 本地已经没有、服务器上要删掉的（2026-09-29 按机器人类型分目录时淘汰的）
STALE_FILES = ['kefu.py', 'usdt.py', 'shop.py', 'shop_store.py', 'shop_pay.py',
               'shop_providers.py', 'ledger_bot.py']
STALE_DIRS = ['ledger']


def local_files():
    """要传的本地文件：相对路径 -> 绝对路径"""
    out = {}
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs
                   if d not in SKIP_DIRS and not d.startswith(SKIP_PREFIX)]
        rel = os.path.relpath(root, HERE)
        for fn in files:
            if fn in SKIP_FILES or fn.startswith(SKIP_PREFIX):
                continue
            r = fn if rel == '.' else posixpath.join(rel.replace(os.sep, '/'), fn)
            out[r] = os.path.join(root, fn)
    return out


def main():
    global HOST, USER, KEY, APP_DIR, SERVICE
    ap = argparse.ArgumentParser(description='把代码传到服务器并重启面板')
    ap.add_argument('--dry-run', action='store_true', help='只看看，不改任何东西')
    ap.add_argument('--no-restart', action='store_true', help='传完不重启')
    # ★★ 换服务器就靠这几个开关（以前 IP 是写死的，换机器得改代码）
    ap.add_argument('--host', default=HOST, help='服务器 IP（默认 %s）' % HOST)
    ap.add_argument('--user', default=USER, help='登录用户名')
    ap.add_argument('--key', default=KEY, help='SSH 私钥文件')
    ap.add_argument('--dir', default='',
                    help='代码放哪（默认 /home/<用户名>/tgpanel）')
    ap.add_argument('--service', default=SERVICE, help='systemd 服务名')
    args = ap.parse_args()

    HOST = args.host
    USER = args.user
    KEY = os.path.expanduser(args.key)
    SERVICE = args.service
    if args.dir:
        APP_DIR = args.dir
    else:
        APP_DIR = ('/root/tgpanel' if USER == 'root'
                   else '/home/%s/tgpanel' % USER)
    print('目标：%s@%s:%s（服务 %s）' % (USER, HOST, APP_DIR, SERVICE))
    if not os.path.exists(KEY):
        raise SystemExit('!! 找不到私钥 %s（用 --key 指一个）' % KEY)

    want = local_files()
    print('本地要传的文件：%d 个' % len(want))

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(HOST, username=USER, key_filename=KEY, timeout=25)

    def run(cmd, timeout=120):
        _i, o, e = c.exec_command(cmd, timeout=timeout)
        return (o.read().decode('utf-8', 'replace')
                + e.read().decode('utf-8', 'replace')).strip()

    raw = run('cd %s && find . -type f -printf "%%P\\n"' % APP_DIR).split('\n')
    # 只关心「代码文件」，.git/.venv/data/dist 那些不算差异
    have = set(x for x in raw
               if x and not x.startswith(('.git/', '.venv/', 'data/',
                                          'dist/', 'build/', '__pycache__/'))
               and '__pycache__' not in x)
    # 服务器上有、本地没有的（= 老的/淘汰的文件，删之前要人看一眼）
    # ★ 把「只读不碰」的数据文件排除掉 —— 它们本来就不在 want 里，
    #   列出来只会让人以为要被删
    extra = sorted(x for x in (set(have) - set(want))
                   if os.path.basename(x) not in KEEP_ON_SERVER)

    new = sorted(set(want) - have)
    changed = sorted(set(want) & have)
    print('  新增 %d，覆盖 %d' % (len(new), len(changed)))
    print('  服务器上有、本地没有的（不会自动删，下面单独列）：')
    for x in extra:
        print('     -', x)

    if args.dry_run:
        print('\n（--dry-run，什么都没改）')
        c.close()
        return

    # ---- ① 备份服务器代码 ----
    stamp = time.strftime('%Y%m%d_%H%M%S')
    home = '/root' if USER == 'root' else '/home/%s' % USER
    bk = '%s/tgpanel_backup_%s' % (home, stamp)
    print('\n① 备份服务器代码 → %s' % bk)
    print(run('mkdir -p %s && cd %s && tar czf %s/code.tgz '
              '--exclude=data --exclude=__pycache__ --exclude=.venv '
              '--exclude=dist --exclude=build . && ls -lh %s/code.tgz'
              % (bk, APP_DIR, bk, bk)))

    # ---- ② 传文件 ----
    print('\n② 上传 %d 个文件…' % len(want))
    sftp = c.open_sftp()
    made = set()
    fails = []
    for i, (rel, src) in enumerate(sorted(want.items()), 1):
        d = posixpath.dirname(rel)
        if d and d not in made:
            run('mkdir -p %s' % posixpath.join(APP_DIR, d))
            made.add(d)
        try:
            sftp.put(src, posixpath.join(APP_DIR, rel))
        except Exception as e:
            fails.append('%s: %s' % (rel, e))
        if i % 40 == 0:
            print('   … %d/%d' % (i, len(want)))
    sftp.close()
    print('   传完 %d 个，失败 %d 个' % (len(want) - len(fails), len(fails)))
    for f in fails:
        print('   !! ', f)
    if fails:
        print('★ 有文件没传上去，**不删旧文件也不重启**，请重跑')
        c.close()
        return

    # ---- ③ 删掉淘汰的旧文件 ----
    print('\n③ 删除已淘汰的旧模块')
    for d in STALE_DIRS:
        run('rm -rf %s/%s' % (APP_DIR, d))
    for f in STALE_FILES:
        run('rm -f %s/%s' % (APP_DIR, f))
    run('cd %s && find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null'
        % APP_DIR)
    print('   删了 %s + %s/，并清了 __pycache__'
          % (' '.join(STALE_FILES), STALE_DIRS[0]))

    # ---- ④ 重启 ----
    if args.no_restart:
        print('\n④ 按 --no-restart 要求，没有重启（新代码还没生效）')
    else:
        print('\n④ 重启 %s' % SERVICE)
        print(run('sudo systemctl restart %s && sleep 3 && '
                  'systemctl is-active %s' % (SERVICE, SERVICE)))

    # ---- ⑤ 功能自检 ----
    # ★★ 从「import 一下」升级成**逐项验功能**。
    #    换服务器最容易出的不是「起不来」，而是「起来了但某个功能悄悄瘸了」——
    #    少装一个包（Pillow）、少传一个文件（panel_page.html），
    #    表现都是「点那个功能没反应」，而进程活得好好的、日志干干净净。
    #    `--url` 不带值 = 按服务器上 config.json 的端口自己拼。
    print('\n⑤ 功能自检（逐项验，不是只 import）')
    out = run('cd %s && .venv/bin/python _selfcheck.py --url 2>&1 | tail -40'
              % APP_DIR, timeout=180)
    print(out)
    print('\n服务状态：')
    print(run('systemctl status %s --no-pager 2>&1 | head -6' % SERVICE))
    print('\n服务日志最后几行：')
    print(run('journalctl -u %s -n 12 --no-pager | tail -12' % SERVICE))
    print('\n★ 回滚命令（万一有问题）：')
    print('   ssh -i %s %s@%s "cd %s && tar xzf %s/code.tgz && '
          'sudo systemctl restart %s"'
          % (KEY, USER, HOST, APP_DIR, bk, SERVICE))
    c.close()


if __name__ == '__main__':
    main()
