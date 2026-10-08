# -*- coding: utf-8 -*-
"""TG 机器人管理面板 · 入口

双击运行后：
    1. 启动网页管理面板（默认 http://127.0.0.1:8080）
    2. 把 bots.json 里所有启用的机器人拉起来
    3. 自动打开浏览器

加新机器人类型见 README.md「如何扩展」。
"""
import os
import socket
import sys
import threading
import time
import traceback
import webbrowser
from http.server import ThreadingHTTPServer

import core
# ★ 路径常量必须走 core.xxx，**不能** `from core import DATA_DIR` ——
#   测试靠改写 core.DATA_DIR 来隔离数据目录，from-import 会把值钉死在导入那一刻，
#   改写就不生效了，测试会静默写进真实的 data/（manager.py 里也有同样的说明）
from core import gen_code, load_json, log, save_json
from manager import BotManager
from merchants import MerchantStore
from panel import PanelHandler

CONFIG_TEMPLATE = {
    "host": "127.0.0.1",
    "port": 8080,
    "password": "",
    "welcome": "您好！请直接留言，我们会尽快回复您。",
    # 给商户登录 token 签名用。丢了只会让商户重新登录一次，无害
    "token_secret": "",
}


def load_cfg():
    cfg = load_json(core.CONFIG_FILE, None)
    if not isinstance(cfg, dict):
        cfg = dict(CONFIG_TEMPLATE)
    changed = False
    for k, v in CONFIG_TEMPLATE.items():
        if k not in cfg:
            cfg[k] = v
            changed = True
    # 密码必须纯 ASCII：它要走 HTTP 头，中文会让请求发不出去
    pw = cfg.get('password') or ''
    if not pw or any(ord(c) > 127 for c in pw):
        cfg['password'] = gen_code(8)
        changed = True
    # 签名密钥：跟管理员密码分开，改密码不会把商户全踢下线
    if not (cfg.get('token_secret') or '').strip():
        cfg['token_secret'] = gen_code(32)
        changed = True
    if changed:
        save_json(core.CONFIG_FILE, cfg)
    return cfg


def pause():
    """出错时停一下等回车 —— 双击 exe 时这样才看得到报错。

    ★ 服务器上（systemd / docker，没有终端）**不能等**：
      没有 stdin 时 input() 会抛 EOFError，配上 Restart=always 就是
      「起来→崩→重启→再崩」的死循环，日志还看不出原因。
      所以只在真的有终端（人坐在电脑前）时才暂停。
    """
    try:
        if not (sys.stdin and sys.stdin.isatty()):
            return
        input('\n按回车键关闭窗口…')
    except (EOFError, OSError):
        pass


def lan_ip():
    """取本机局域网 IP（只为在控制台提示一句，取不到就返回空）"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # 不会真的发包，只是让系统选出出口网卡
        s.connect(('8.8.8.8', 80))
        return s.getsockname()[0]
    except Exception:
        return ''
    finally:
        s.close()


def main():
    print('=' * 56)
    print(' TG 机器人管理面板')
    print('=' * 56)

    os.makedirs(core.DATA_DIR, exist_ok=True)
    cfg = load_cfg()
    mgr = BotManager(cfg)

    host = cfg.get('host') or '127.0.0.1'
    port = int(cfg.get('port') or 8080)

    if host not in ('127.0.0.1', 'localhost'):
        print()
        print('  ！注意：面板绑定在 %s，外网可访问，请务必保管好密码。' % host)

    PanelHandler.mgr = mgr
    PanelHandler.password = cfg.get('password') or ''
    PanelHandler.merch = MerchantStore()
    PanelHandler.token_secret = cfg.get('token_secret') or ''

    fallback_note = ''
    try:
        httpd = ThreadingHTTPServer((host, port), PanelHandler)
    except OSError as e:
        # 绑的是某个具体网卡（比如 Tailscale 的 IP）但绑不上 ——
        # 最常见的原因是 Tailscale 没启动。退回只监听本机，别让程序起不来。
        if host not in ('127.0.0.1', '0.0.0.0'):
            print('\n⚠️ 绑定 %s 失败：%s' % (host, e))
            try:
                httpd = ThreadingHTTPServer(('127.0.0.1', port), PanelHandler)
                fallback_note = ('原本要监听 %s 但绑不上（Tailscale 没启动？），'
                                 '已退回只监听本机' % host)
                host = '127.0.0.1'
            except OSError as e2:
                print('退回本机也失败：%s' % e2)
                pause()
                return
        else:
            print('\n启动面板失败：%s' % e)
            print('端口 %s 可能被占用，请改 config.json 里的 port。' % port)
            pause()
            return

    url = 'http://%s:%s' % (host, port)
    print()
    if fallback_note:
        print('  ⚠️ %s' % fallback_note)
        print()
    print('  面板地址：%s' % url)
    print('  面板密码：%s' % cfg['password'])
    n_merch = PanelHandler.merch.count() if PanelHandler.merch else 0
    if n_merch:
        print('  商户账号：%d 个（他们自己登录用 %s/m，账号密码在面板里建）'
              % (n_merch, url))
    if host not in ('127.0.0.1', 'localhost'):
        lan = lan_ip()
        if lan:
            print('  （同一局域网也可以用：http://%s:%s）' % (lan, port))
    print()
    print('  打开面板 → 选类型 → 填备注和 token → 添加')
    print('  → 把绑定码发给使用者 → 他在 Telegram 里发 /admin 绑定码')
    print()
    print('  （关掉这个窗口，所有机器人和面板都会停止）')
    print('=' * 56)
    print()

    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    mgr.start_all()
    mgr.start_watchdog()      # 到期检查：到点停用，停用满 7 天自动删除

    try:
        webbrowser.open(url)
    except Exception:
        pass

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print('\n正在停止…')
        mgr.stop_all()
        httpd.shutdown()
        print('已停止。')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('\n运行出错：\n' + traceback.format_exc())
        log('运行出错：\n' + traceback.format_exc())
        pause()
