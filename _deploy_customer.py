# -*- coding: utf-8 -*-
"""给**客户自己的服务器**装「记账独立版」

用法：
    # ★ 最推荐：先跟客户换公钥（**密码不用给任何人**）
    python _deploy_customer.py --pubkey
    #   ↑ 把打印出来的那一段发给客户，让他在服务器上跑一下，然后：

    # ① 先体检，什么都不改（装之前一定先跑这个）
    python _deploy_customer.py --host 1.2.3.4 --user ubuntu \
        --key ~/.ssh/tg_server \
        --panel https://tgbotbot.duckdns.org --pw 面板密码 --bot 1923be77 --check

    # ② 确认没问题再真装（去掉 --check）
    # ③ 卸掉（加 --uninstall）

    # 客户给的是**密码**的话，把 --key ... 换成 --pass 密码

    # 客户给的是**他自己的私钥文件**：--key 那个文件的路径
    # 私钥本身还带密码（passphrase）：再加 --keypass

★ 它从**面板**直接下那个填好的安装包（就是「⬇️ 安装包」按钮下的那个），
  所以不用手动打包、不用粘任何配置。

★★ 安全约定（改这个脚本的时候别破坏）：
  1. **只碰两个地方**：装它的那个目录（默认 ~/tgledger）+ 一个 systemd 服务。
     不装系统包（除了 venv 模块，那是 install.sh 自己按需装的）、
     不动防火墙、不动别的服务、不碰 /etc 里其它东西。
  2. **先体检再动手**：--check 会把系统版本、Python 版本、磁盘、sudo、
     同名服务都查一遍。装到一半发现 Python 太老最费时间。
  3. **卸载只删自己的东西**，客户的 data 目录留着（除非显式 --purge 再说）。
"""
import argparse
import io
import os
import posixpath
import sys

import paramiko

sys.stdout.reconfigure(encoding='utf-8')

DEFAULT_DIR = '~/tgledger'
DEFAULT_SERVICE = 'tgledger'
REMOTE_ZIP = '/tmp/tgledger_install.zip'


def say(msg=''):
    print(msg)


def connect(a):
    """连客户的服务器。**密码和密钥都支持**。

    ★ 密钥有两种来路，都走 --key：
      ① 客户自己的私钥文件（他给你）
      ② **把你的公钥发给客户**，让他加到 authorized_keys ——
         推荐这个：**密码根本不用经过你手**。
         跑 `python _deploy_customer.py --pubkey` 会把该发给客户的东西打出来。
    """
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kw = {'port': int(a.port or 22), 'timeout': 25,
          'username': a.user, 'allow_agent': False, 'look_for_keys': False}
    if a.key:
        p = os.path.expanduser(a.key)
        if not os.path.isfile(p):
            raise SystemExit('!! 找不到私钥文件：%s' % p)
        # ★ Windows 上私钥文件的权限不用管；Linux/Mac 上 ssh 会要求 600，
        #   但 paramiko 不检查，所以这儿也不用管
        kw['key_filename'] = p
        if a.keypass:
            # 带密码的私钥（生成时设了 passphrase）
            kw['passphrase'] = a.keypass
    else:
        kw['password'] = a.password
    try:
        c.connect(a.host, **kw)
    except paramiko.AuthenticationException:
        raise SystemExit(
            '!! 认证失败 —— 对一下：\n'
            '   · 账号对不对（root / ubuntu / 客户给你的那个）\n'
            '   · 如果是密钥：客户有没有把你的公钥加进去？\n'
            '     （跑 python _deploy_customer.py --pubkey 看该发什么给他）\n'
            '   · 如果私钥有密码，得加 --keypass\n'
            '   · 有些服务器**禁用了密码登录**，只能用密钥')
    except paramiko.SSHException as e:
        raise SystemExit('!! 连不上：%s' % e)
    return c


def show_pubkey():
    """把自己的公钥打出来，连同「让客户跑的那一条命令」。

    ★ 为什么推荐这条路：**密码不用给任何人**。
      客户跑一条命令把它加进去，之后你就能直接连了，装完让他删掉即可。
    """
    home = os.path.expanduser('~')
    pub = ''
    for name in ('tg_server.pub', 'id_ed25519.pub', 'id_rsa.pub'):
        p = os.path.join(home, '.ssh', name)
        if os.path.isfile(p):
            pub = io.open(p, encoding='utf-8').read().strip()
            break
    say('=' * 56)
    say(' 把你的公钥发给客户（这样密码不用给任何人）')
    say('=' * 56)
    if not pub:
        say('  本机没找到公钥。先生成一个：')
        say('    ssh-keygen -t ed25519 -f ~/.ssh/tg_server -N ""')
        say('  然后再跑一遍这条命令。')
        return
    say()
    say('  把下面这一段发给客户，让他**在服务器上跑这一条**：')
    say()
    say('    mkdir -p ~/.ssh && chmod 700 ~/.ssh && \\')
    say('    echo "%s" >> ~/.ssh/authorized_keys && \\' % pub)
    say('    chmod 600 ~/.ssh/authorized_keys')
    say()
    say('  然后你就能这样连（不用密码）：')
    say('    python _deploy_customer.py --host 客户IP --user 客户账号 \\')
    say('        --key ~/.ssh/%s ...' % os.path.basename(
            os.path.join(home, '.ssh', 'tg_server')))
    say()
    say('  装完之后，让客户把这一行从 authorized_keys 里删掉就行。')
    say()
    say('  你的公钥：')
    say('    %s' % pub)


def fetch_package(a):
    """从面板下那个已经填好的安装包"""
    import requests
    url = '%s/api/bots/%s/solozip?url=%s' % (
        a.panel.rstrip('/'), a.bot,
        requests.utils.quote(a.panel.rstrip('/')))
    r = requests.get(url, headers={'X-Panel-Pass': a.pw}, timeout=120)
    if r.status_code != 200 or r.content[:2] != b'PK':
        raise SystemExit('!! 从面板拿安装包失败（HTTP %s）：%s'
                         % (r.status_code, r.text[:120]))
    say('  从面板拿到安装包：%d KB' % (len(r.content) // 1024))
    return r.content


def preflight(c, a):
    """★ 先体检，什么都不改。返回 True = 可以装"""
    def run(cmd):
        _i, o, e = c.exec_command(cmd, timeout=60)
        return (o.read().decode('utf-8', 'replace')
                + e.read().decode('utf-8', 'replace')).strip()

    ok = True
    osrel = run("cat /etc/os-release 2>/dev/null | grep PRETTY_NAME | cut -d= -f2")
    py = run("python3 --version 2>&1")
    sudo = run("sudo -n true 2>&1 && echo yes || echo no")
    home = run("echo $HOME")
    disk = run("df -h $HOME | tail -1 | awk '{print $4}'")
    svc = run("systemctl list-unit-files 2>/dev/null | grep -c '^%s\\.service' "
              % a.name)

    say('  系统      ：%s' % (osrel or '取不到'))
    say('  Python    ：%s' % (py or '没装 python3'))
    say('  家目录    ：%s（剩余 %s）' % (home, disk or '?'))
    say('  sudo      ：%s' % ('有' if sudo == 'yes' else '**没有**'))
    say('  同名服务  ：%s' % ('**已经有了**（换个 --name）' if svc.strip() != '0'
                              else '没有，可以装'))

    # Python 版本：install.sh 也会拦，但在这儿拦住能省一次上传
    v = run("python3 -c 'import sys;print(sys.version_info[0]*100+sys.version_info[1])' 2>&1")
    try:
        if int(v) < 310:
            say('  ✗ Python 太老（要 3.10 以上）')
            say('    → 换个系统镜像（Ubuntu 22.04/24.04），或者先装个新 Python')
            ok = False
    except ValueError:
        say('  ✗ 没装 python3')
        ok = False

    if sudo != 'yes':
        say('  ✗ 这个账号没有 sudo，装不了 systemd 服务')
        say('    → 向客户要个有 sudo 的账号（root 也行）')
        ok = False
    if svc.strip() != '0':
        ok = False
    say()
    say('  体检结果：%s' % ('可以装 ✓' if ok else '**先解决问题再装** ✗'))
    return ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--host', default='', help='客户的服务器 IP')
    ap.add_argument('--user', default='', help='SSH 账号')
    ap.add_argument('--pass', dest='password', default='', help='SSH 密码')
    ap.add_argument('--key', default='',
                    help='私钥文件路径（客户的，或者你自己那把）')
    ap.add_argument('--keypass', default='',
                    help='私钥本身带密码时才要（生成时设了 passphrase）')
    ap.add_argument('--pubkey', action='store_true',
                    help='★ 打印你的公钥 + 让客户跑的那条命令（推荐用这个，'
                         '密码不用给任何人）')
    ap.add_argument('--port', default='22')
    ap.add_argument('--panel', default='https://tgbotbot.duckdns.org',
                    help='你的面板地址')
    ap.add_argument('--pw', default='', help='你的面板密码（用来下安装包）')
    ap.add_argument('--bot', default='', help='面板上那个机器人的 id')
    ap.add_argument('--zip', dest='zipfile', default='',
                    help='★ 已经手动下好安装包了就给它的路径 '
                         '（给了这个就不用 --pw / --bot）')
    ap.add_argument('--dir', default=DEFAULT_DIR, help='装到哪（默认 ~/tgledger）')
    ap.add_argument('--name', default=DEFAULT_SERVICE, help='systemd 服务名')
    ap.add_argument('--check', action='store_true', help='只体检，什么都不改')
    ap.add_argument('--uninstall', action='store_true', help='卸掉')
    a = ap.parse_args()

    # ★ 光看公钥的模式，不用连任何服务器
    if a.pubkey:
        show_pubkey()
        return

    if not a.host or not a.user:
        raise SystemExit('!! 要给 --host（客户 IP）和 --user（账号）\n'
                         '   （只想打印公钥的话：--pubkey）')
    if not a.password and not a.key:
        raise SystemExit('!! 要么给 --pass（密码），要么给 --key（私钥文件）\n'
                         '   两个都不想给客户要？跑 --pubkey，'
                         '把你的公钥发给客户加一下就行')

    say('=' * 56)
    say(' 给客户的服务器装「记账独立版」')
    say('=' * 56)
    say('  目标：%s@%s:%s' % (a.user, a.host, a.port))
    say()

    c = connect(a)
    say('  SSH 连上了 ✓')
    say()

    def run(cmd, t=900):
        _i, o, e = c.exec_command(cmd, timeout=t)
        return (o.read().decode('utf-8', 'replace')
                + e.read().decode('utf-8', 'replace')).strip()

    # ---------------- 卸载 ----------------
    if a.uninstall:
        say('--- 卸载 ---')
        d = run('echo %s' % a.dir).strip()
        # ★ 程序实际在 <dir>/记账独立版/ 里（zip 里就是套了一层目录），
        #   以前直接拿 <dir> 去找 install.sh，找不到、什么也没卸成
        #   （2026-09-29 真机测出来的）
        app = posixpath.join(d, '记账独立版') if d.startswith('/') else ''
        out = run('cd %s 2>/dev/null && sudo bash install.sh --uninstall '
                  '--name %s 2>&1 | tail -3' % (app, a.name)) if app else ''
        if out:
            say('  %s' % out)
        # ★ 再兜一道：install.sh 可能已经不在了（目录被删过），
        #   但 systemd 的单元还留着 —— 不在客户机器上留残渣。
        #   这条是幂等的，多跑几遍也安全。
        run('sudo systemctl stop %s 2>/dev/null; '
            'sudo systemctl disable %s 2>/dev/null; '
            'sudo rm -f /etc/systemd/system/%s.service; '
            'sudo systemctl daemon-reload' % (a.name, a.name, a.name))
        left = run('systemctl list-unit-files --no-pager 2>/dev/null | '
                   'grep -c "^%s\\.service"' % a.name).strip()
        say('  服务已卸干净 ✓' if left == '0'
            else '  ⚠️ 服务单元还在，手动查一下：%s' % left)
        say()
        say('★ 安装目录 %s 和里面的 data/ **没动**（客户的数据留着）' % a.dir)
        say('  要一并删掉自己动手：rm -rf %s' % a.dir)
        c.close()
        return

    # ---------------- 体检 ----------------
    say('--- 体检（什么都不改）---')
    ok = preflight(c, a)
    if a.check:
        c.close()
        return
    if not ok:
        c.close()
        raise SystemExit('!! 体检没过，没动手')

    # ---------------- 拿到安装包 ----------------
    if a.zipfile:
        # ★ 已经手动下好了就直接用那个（不用再去面板拉一遍）
        p = os.path.expanduser(a.zipfile)
        if not os.path.isfile(p):
            c.close()
            raise SystemExit('!! 找不到安装包：%s' % p)
        data = io.open(p, 'rb').read()
        if data[:2] != b'PK':
            c.close()
            raise SystemExit('!! %s 不像个 zip' % p)
        say('--- 用本地的安装包 ---')
        say('  %s（%d KB）' % (os.path.basename(p), len(data) // 1024))
    else:
        if not a.pw or not a.bot:
            c.close()
            raise SystemExit('!! 要么给 --zip（本地的安装包），'
                             '要么给 --pw + --bot（从面板下）')
        say('--- 从面板下安装包 ---')
        data = fetch_package(a)

    # ---------------- 上传 ----------------
    say()
    say('--- 传到客户服务器 ---')
    sftp = c.open_sftp()
    sftp.putfo(io.BytesIO(data), REMOTE_ZIP)
    sftp.close()
    d = run('echo %s' % a.dir).strip()
    say('  传到 %s' % REMOTE_ZIP)

    # ★ 解到客户家目录下（/tmp 重启就没），先把上一次的换掉
    say(run('sudo rm -rf %s && mkdir -p %s && '
            'python3 -c "import zipfile;zipfile.ZipFile(\'%s\').extractall(\'%s\')"'
            ' && ls %s/记账独立版/ | tr "\\n" " "'
            % (d, d, REMOTE_ZIP, d, d)))
    say('  解到 %s/记账独立版/' % d)

    # ---------------- 真装 ----------------
    say()
    say('--- 跑 install.sh ---')
    out = run('cd %s/记账独立版 && sudo bash install.sh --name %s 2>&1 | '
              'grep -E "Python|机器人 id|消息转发|消息记录|数据目录|✗|跑起来了|没起来"'
              % (d, a.name))
    say(out)
    run('rm -f %s' % REMOTE_ZIP)

    # ---------------- 自检 ----------------
    say()
    say('--- 装完自检 ---')
    act = run('systemctl is-active %s' % a.name).strip()
    say('  服务状态  ：%s' % act)
    logtail = run('tail -12 %s/记账独立版/运行日志.txt 2>/dev/null' % d)
    say('  日志最后几行：')
    for line in logtail.split('\n')[-6:]:
        say('    %s' % line)
    if '转发已开' in logtail:
        say('  ★ 转发已开（消息会回传到你的面板）✓')
    if act == 'active':
        say()
        say('  ✅ 装好了')
        say('  客户那边的机器人在群里干活，消息会出现在你的面板「消息记录」里。')
        say()
        # ★ 卸载命令必须**带上 --dir / --name** —— 不然它按默认值去卸，
        #   卸的是另一台（2026-09-29 打印出来一眼看到才发现）
        say('  卸载：python _deploy_customer.py --host %s --user %s \\'
            % (a.host, a.user))
        say('            --dir %s --name %s --uninstall' % (a.dir, a.name))
    else:
        say()
        say('  ✗ 服务没起来。看完整日志：')
        say('    ssh %s@%s "tail -40 %s/记账独立版/运行日志.txt"'
            % (a.user, a.host, d))
    c.close()


if __name__ == '__main__':
    main()
