# -*- coding: utf-8 -*-
"""新服务器部署演练：在真机上完整走一遍 deploy.sh + 功能自检

    python _rehearsal.py --host X --user root --key 私钥 [--dir 目录] [--port 端口]

★ 为什么要有这个：换服务器的坑**不是「起不来」，是「起来了但某个功能悄悄瘸了」**，
  还有一类更阴的：脚本本身在某台机器上跑不通（比如 CRLF 换行、pip 源连不上）。
  这些只有**真在机器上跑一遍**才会暴露 —— 本机跑多少测试都测不出来。

★★ 演练会：传代码 → 跑 deploy.sh（独立服务名+端口，**不碰现有服务**）
  → 跑 _selfcheck.py → 报结果。用完把服务删掉，不留残渣。
"""
import argparse
import os
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')
import paramiko

HERE = os.path.dirname(os.path.abspath(__file__))
SKIP_DIRS = {'.git', '__pycache__', 'data', 'build', 'dist', '.venv',
             '_github_upload', '_apidoc', '_full_ledger', '_srv_tron'}
SKIP_EXT = {'.pyc', '.exe', '.png', '.db', '.sqlite3'}
# ★ config.json 也要跳过 —— 真实的新服务器（git clone）拿到的是
#   **空密码的模板**，deploy.sh 会生成一个新的。带上本地这份的话
#   会「沿用旧密码」，那条生成分支就测不到了（第一次演练就是这样）
SKIP_FILES = {'bots.json', 'merchants.json', '运行日志.txt', 'config.json'}


def say(msg=''):
    print(msg, flush=True)


def main():
    ap = argparse.ArgumentParser(description='新服务器部署演练')
    ap.add_argument('--host', required=True)
    ap.add_argument('--user', default='root')
    ap.add_argument('--key', required=True)
    ap.add_argument('--dir', default='')
    ap.add_argument('--port', type=int, default=8090)
    ap.add_argument('--name', default='tgpanel-rehearsal')
    ap.add_argument('--keep', action='store_true', help='跑完不清理（留着看）')
    a = ap.parse_args()

    d = a.dir or ('/root/%s' % a.name if a.user == 'root'
                  else '/home/%s/%s' % (a.user, a.name))
    key = os.path.expanduser(a.key)

    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(a.host, username=a.user, key_filename=key, timeout=60,
              banner_timeout=60, auth_timeout=60)

    def run(cmd, t=300):
        _i, o, e = c.exec_command(cmd, timeout=t)
        return (o.read().decode('utf-8', 'replace')
                + e.read().decode('utf-8', 'replace')).rstrip()

    def sudo(cmd, t=900):
        """★ 保持连接跑完 —— 后台 + nohup 会被 SSH 断连带走（踩过）"""
        return run('sudo bash -c %s' % (repr(cmd).replace("'", '"')
                                        if False else "'%s'" % cmd.replace("'", "'\\''")), t)

    say('=' * 62)
    say(' 新服务器部署演练')
    say('=' * 62)
    say('  目标：%s@%s' % (a.user, a.host))
    say('  目录：%s   端口：%s   服务名：%s' % (d, a.port, a.name))
    say('  ★ 用独立目录 + 独立服务名 + 独立端口，**不碰机器上现有的服务**')
    say()

    say('--- ① 传代码 ---')
    run('rm -rf %s && mkdir -p %s' % (d, d))
    sftp = c.open_sftp()
    n = 0
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS
                   and not x.startswith(('_tmp_', '_dbg'))]
        for fn in files:
            if os.path.splitext(fn)[1].lower() in SKIP_EXT or fn in SKIP_FILES:
                continue
            p = os.path.join(root, fn)
            rel = os.path.relpath(p, HERE).replace(os.sep, '/')
            run('mkdir -p %s' % os.path.dirname(d + '/' + rel), 60)
            sftp.put(p, d + '/' + rel)
            n += 1
    sftp.close()
    say('  传了 %d 个文件' % n)

    say()
    say('--- ② 跑 deploy.sh（这一步最慢，装依赖）---')
    t0 = time.time()
    # ★ deploy.sh 会检查「必须 root」—— 演练脚本得替它加 sudo
    out = run('cd %s && sudo bash deploy.sh --name %s --port %d 2>&1 | tail -40'
              % (d, a.name, a.port), 900)
    say(out)
    say('  （耗时 %.0f 秒）' % (time.time() - t0))
    # ★★ 光看「跑完了没报错」不够 —— `set -e` 撞上管道失败是**静默退出**：
    #    一个错都不打，就停在半路（2026-09-29 实测：卡在「虚拟环境：…」，
    #    什么提示都没有）。所以拿「成功标记」来判断，别信退出码。
    # 这几个标记是 deploy.sh 真的会打的（别凭空猜词 —— 猜错会误报「没跑完」，
    #  那就比不报还糟：你会以为脚本坏了）
    done_marks = ('依赖装好了', '面板密码', '看日志：journalctl')
    missing = [m for m in done_marks if m not in out]
    say('  ★ 脚本**完整跑完**了吗：%s'
        % ('是' if not missing else '❌ 没有！缺这些标记：%s（多半是静默退出了）'
           % missing))

    say()
    say('--- ③ 服务起来了吗 ---')
    say('  %s' % run('sudo systemctl is-active %s' % a.name))
    say('  重启次数：%s' % run('sudo systemctl show %s -p NRestarts --value'
                              % a.name))

    say()
    say('--- ④ 功能自检（逐项验，不是只 import）---')
    out = run('cd %s && .venv/bin/python _selfcheck.py --url 2>&1 | tail -45' % d,
              300)
    say(out)
    passed = '功能没有缺失' in out

    say()
    say('--- ⑤ 面板真的能打开吗（从机器外面看不看得到）---')
    say('  监听情况：%s' % run("ss -lntp 2>/dev/null | grep ':%d' || echo 没在听"
                              % a.port))
    say('  本机 HTTP：%s' % run(
        'curl -s -o /dev/null -w "%%{http_code} %%{size_download}字节" '
        'http://127.0.0.1:%d/ --max-time 10' % a.port))

    if not a.keep:
        say()
        say('--- ⑥ 清理（不留残渣）---')
        run('sudo systemctl stop %s 2>/dev/null; '
            'sudo systemctl disable %s 2>/dev/null; '
            'sudo rm -f /etc/systemd/system/%s.service; '
            'sudo systemctl daemon-reload'
            % (a.name, a.name, a.name), 300)
        run('sudo rm -rf %s' % d, 300)
        say('  服务已删、目录已清')
        say('  机器上现有服务：%s' % run(
            'systemctl list-units --type=service --state=running --no-pager '
            '--no-legend 2>/dev/null | awk \'{print $1}\' | tr "\\n" " "'))

    say()
    say('=' * 62)
    say(' 演练结果：%s' % ('✅ 全过，换新服务器没问题' if passed
                          else '❌ 有问题，看上面'))
    say('=' * 62)
    c.close()
    sys.exit(0 if passed else 1)


if __name__ == '__main__':
    main()
