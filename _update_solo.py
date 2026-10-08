# -*- coding: utf-8 -*-
"""给**已经装好**的「记账独立版」原地升级 —— 只换代码，不碰配置和数据

和 `_deploy_customer.py` 的区别：
    _deploy_customer.py   第一次装（会 rm -rf 整个目录，配置和数据都没了）
    _update_solo.py       已经装好了，要升级（**保住 config.json 和 data/**）

★★ 两条铁律：
  1. **绝不覆盖 config.json** —— 那里面是这台机器人的 id / token / 回传地址，
     覆盖成全套模板的话，机器人当场就废了（token 不对，连不上 Telegram）
  2. **绝不动 data/** —— 绑定关系（谁是管理员）在里面，
     没了的话客户得重新发一遍 /admin 绑定码
  备份在升级前做，出事能回滚（脚本最后会打印回滚命令）。

用法：
    python _update_solo.py --dry-run                 # 只看看会动什么
    python _update_solo.py --host 47.236.8.82 --key "桌面/aliyun秘钥.pem"
"""
import argparse
import io
import os
import posixpath
import shlex
import sys
import time
import zipfile

sys.stdout.reconfigure(encoding='utf-8')

import paramiko

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ZIP = os.path.join(os.path.dirname(HERE), '_solo_build',
                           '记账独立版.zip')

# ★★ 这两个**永远不传**：配置是这台机器专属的，data 是跑出来的
NEVER = ('config.json',)
NEVER_DIRS = ('data/',)


def say(msg=''):
    print(msg)


def connect(a):
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kw = {'timeout': 25, 'banner_timeout': 30}
    if a.key:
        kw['key_filename'] = os.path.expanduser(a.key)
        if a.keypass:
            kw['passphrase'] = a.keypass
    else:
        kw['password'] = a.password
    c.connect(a.host, username=a.user, port=a.port, **kw)
    return c


def main():
    ap = argparse.ArgumentParser(description='记账独立版 · 原地升级')
    ap.add_argument('--host', required=True)
    ap.add_argument('--user', default='root')
    ap.add_argument('--port', type=int, default=22)
    ap.add_argument('--key', help='私钥文件（推荐）')
    ap.add_argument('--keypass', help='私钥密码（一般不用）')
    ap.add_argument('--password', help='密码登录（能用密钥就别用它）')
    ap.add_argument('--zip', dest='zipfile', default=DEFAULT_ZIP)
    ap.add_argument('--dir', default='/root/tgledger',
                    help='安装目录（默认 /root/tgledger）')
    ap.add_argument('--name', default='tgledger', help='systemd 服务名')
    ap.add_argument('--dry-run', action='store_true', help='只看不动')
    a = ap.parse_args()

    zp = os.path.expanduser(a.zipfile)
    if not os.path.isfile(zp):
        raise SystemExit('找不到安装包：%s\n'
                         '（先跑 python _build_solo.py 生成）' % zp)
    z = zipfile.ZipFile(zp)
    app = posixpath.join(a.dir, '记账独立版')

    # ★ 包里套了一层「记账独立版/」，传的时候要去掉这层
    plan = []
    for n in z.namelist():
        if n.endswith('/') or n == '记账独立版/':
            continue
        rel = n.split('/', 1)[1] if '/' in n else n
        if not rel or rel in NEVER or rel.startswith(NEVER_DIRS):
            continue
        plan.append((n, rel))
    skipped = [n for n in z.namelist()
               if n.endswith('config.json') or '/data/' in n]

    say('=' * 56)
    say(' 记账独立版 · 原地升级')
    say('=' * 56)
    say('  服务器  ：%s@%s' % (a.user, a.host))
    say('  安装目录：%s' % app)
    say('  安装包  ：%s（%d KB）' % (os.path.basename(zp),
                                    os.path.getsize(zp) // 1024))
    say('  要传    ：%d 个文件' % len(plan))
    say('  ★ 不传  ：config.json / data/（%d 项，那种东西一覆盖机器人就废了）'
        % len(skipped))
    if a.dry_run:
        say()
        for _, rel in plan:
            say('    %s' % rel)
        say()
        say('（--dry-run，什么都没改）')
        return

    c = connect(a)

    def run(cmd, t=300):
        _i, o, e = c.exec_command(cmd, timeout=t)
        return (o.read().decode('utf-8', 'replace')
                + e.read().decode('utf-8', 'replace')).strip()

    say()
    say('--- ① 先看看现场 ---')
    say(run('ls -d %s 2>/dev/null && systemctl is-active %s 2>/dev/null'
            % (shlex.quote(app), shlex.quote(a.name))))
    has_cfg = run('test -f %s/config.json && echo yes || echo no'
                  % shlex.quote(app)).strip()
    if has_cfg != 'yes':
        say('  ⚠️ %s/config.json 不存在 —— 这更像是一次全新安装，' % app)
        say('     用 _deploy_customer.py 装，别用这个。')
        c.close()
        raise SystemExit(1)

    say()
    say('--- ② 备份（出事能回滚）---')
    bak = '/root/solo_backup_%s' % time.strftime('%Y%m%d_%H%M%S')
    say(run('mkdir -p %s && cp -a %s %s/ 2>&1; ls %s'
            % (shlex.quote(bak), shlex.quote(app), shlex.quote(bak),
               shlex.quote(bak))))
    say('  备份在 %s' % bak)

    say()
    say('--- ③ 传文件（先传完再重启，中途断了不会留下起不来的服务）---')
    sftp = c.open_sftp()
    n = 0
    for name, rel in plan:
        remote = posixpath.join(app, rel)
        d = posixpath.dirname(remote)
        run('mkdir -p %s' % shlex.quote(d))
        sftp.putfo(io.BytesIO(z.read(name)), remote)
        n += 1
    sftp.close()
    say('  传了 %d 个文件' % n)

    say()
    say('--- ④ 确认配置没被动过 ---')
    say('  config.json 还在：%s' % run(
        'test -f %s/config.json && echo 是 || echo 没了！'
        % shlex.quote(app)))
    say('  机器人 id：%s' % run(
        "python3 -c \"import json;print(json.load(open('%s/config.json'))"
        ".get('id'))\" 2>/dev/null" % app))
    say('  绑定数据：%s' % run(
        'ls %s/data/ 2>/dev/null | tr "\\n" " " || echo （还没有）'
        % shlex.quote(app)))

    say()
    say('--- ⑤ 重启 ---')
    say(run('sudo systemctl restart %s; sleep 3; systemctl is-active %s'
            % (shlex.quote(a.name), shlex.quote(a.name))))
    say()
    say('  最后几行日志：')
    say(run('sudo journalctl -u %s -n 12 --no-pager 2>&1 | tail -12'
            % shlex.quote(a.name)))

    say()
    say('=' * 56)
    say(' ★ 回滚命令（万一有问题，整段复制走）：')
    say('   sudo systemctl stop %s && rm -rf %s && '
        'cp -a %s/记账独立版 %s && sudo systemctl start %s'
        % (a.name, shlex.quote(app), bak, shlex.quote(a.dir), a.name))
    say('=' * 56)
    c.close()


if __name__ == '__main__':
    main()
