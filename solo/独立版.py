# -*- coding: utf-8 -*-
"""记账机器人 · 独立版（跑在客户自己的服务器上）

跟面板版的区别：**只搬了记账功能 + 一个消息转发**。
没有面板、没有商城、没有 USDT、没有客服、没有商户后台、没有消息记录界面
（消息记录在服务商的面板上看）。

    python 独立版.py            正常跑
    python 独立版.py --check    只检查配置，不连 Telegram（装机时先跑这个）

配置在 config.json，格式见 config.example.json。
"""
import argparse
import os
import sys
import threading
import time
import traceback

import core
from runners.ledger import LedgerRunner
import relay

CONFIG_FILE = os.path.join(core.BASE_DIR, 'config.json')


class MiniMgr:
    """面板版里那些东西（增删机器人、启停线程、到期检查、多租户）
    这边**统统没有** —— 就一台机器人。

    但 `core.BaseRunner` 要用到 mgr 的三样东西，所以给个最小的替身：

        lock          ★★ 必须给！`set_status()` 每几秒就要
                      `with self.mgr.lock`。缺了不是「可能出问题」，
                      是**启动几秒内线程就垮**。
        save()        绑定关系（谁是管理员）变了要落盘
        is_expired()  到期由服务商那边管，本地一律不算过期
                      （到期后服务端会拒收回传，但记账本身照常）
    """

    def __init__(self, state_file):
        self.lock = threading.RLock()
        self.state_file = state_file
        self.bot = None          # 由 main 填进来，save() 时要写它

    def save(self):
        with self.lock:
            if self.bot is not None:
                core.save_json(self.state_file, self.bot)

    def is_expired(self, bot):
        return False


def load_bot(cfg):
    """把 config.json + 上次存下的绑定关系，拼成一个 bot dict。

    ★ 分两个文件：config.json 是**装机时填的**（token、回传地址…），
      data/<id>.state.json 是**跑起来之后自己攒的**（谁绑定了、绑定码）。
      混在一起的话，改配置容易把绑定关系覆盖掉。
    """
    bid = str(cfg.get('id') or '').strip()
    token = str(cfg.get('token') or '').strip()
    if not bid:
        raise SystemExit('config.json 里没填 id')
    if not token or ':' not in token:
        raise SystemExit('config.json 里 token 没填或格式不对'
                         '（应该是 数字:字母数字）')

    state_file = os.path.join(core.DATA_DIR, '%s.state.json' % bid)
    saved = core.load_json(state_file, {}) or {}

    bot = {
        'id': bid,
        'token': token,
        # ★★ type 是**数据不是代码**，必须是 'ledger' ——
        #    改了的话记账自己的分支会认不出来（而且归档那套也靠它判断）
        'type': 'ledger',
        'note': cfg.get('note') or bid,
        'username': saved.get('username') or '',
        # ★★ 优先用 config.json 里带的那个 —— 它跟**服务商面板上显示的
        #    是同一个**。不这样的话这边会自己再生成一个，跟面板对不上，
        #    客户照面板上的码去 /admin 绑定会被拒绝（两个码「看起来都对」，
        #    极难查）。只有配置里没有时才自己生成一个。
        'bind_code': (cfg.get('bind_code') or saved.get('bind_code')
                      or core.gen_code(8)),
        'admin_ids': saved.get('admin_ids') or [],
        'admin_name': saved.get('admin_name') or '',
        'owner_id': saved.get('owner_id') or '',
        'enabled': True,
        # ★★ 客户版**不存消息**（消息记录在服务商那边）。
        #    这里**必须显式写 False** —— 记账机器人的归档默认是**开**的，
        #    不关的话会在本地建 .archive.sqlite3、把群里的图下下来、
        #    每条发出的账单也存一遍，白白占客户的地方。
        'archive': {'enabled': False},
        'ledger': {'welcome_text': cfg.get('welcome_text') or ''},
        'created': saved.get('created') or time.strftime('%Y-%m-%d %H:%M'),
    }

    # 首次运行时，允许 config.json 里预填管理员（不然群管理命令全是废的，
    # 得让客户自己在群里发一遍 /admin 绑定码）
    if not bot['admin_ids']:
        ids = cfg.get('admin_ids') or []
        if isinstance(ids, (int, str)):
            ids = [ids]
        clean = []
        for x in ids:
            try:
                clean.append(int(str(x).strip()))
            except (TypeError, ValueError):
                pass
        if clean:
            bot['admin_ids'] = clean
            bot['owner_id'] = clean[0]
    return bot, state_file


def main():
    ap = argparse.ArgumentParser(description='记账机器人 · 独立版')
    ap.add_argument('--check', action='store_true',
                    help='只检查配置，不连 Telegram')
    args = ap.parse_args()

    print('=' * 52)
    print(' 记账机器人 · 独立版')
    print('=' * 52)

    os.makedirs(core.DATA_DIR, exist_ok=True)
    cfg = core.load_json(CONFIG_FILE, None)
    if not isinstance(cfg, dict):
        raise SystemExit('找不到 %s（照 config.example.json 填一个）'
                         % CONFIG_FILE)

    bot, state_file = load_bot(cfg)
    rep = cfg.get('report') if isinstance(cfg.get('report'), dict) else {}

    print('  机器人 id ：%s' % bot['id'])
    print('  备注      ：%s' % bot['note'])
    print('  数据目录  ：%s' % core.DATA_DIR)
    print('  消息记录  ：不存（在服务商的面板上看）')
    # ★ 判断条件必须跟 relay.Reporter.ready() 一致：
    #   暗号是从 token 现算的，**没有 report.key 这个东西了**。
    #   （2026-09-29 真机装了一遍才发现：这里还在检查 report.key，
    #     结果转发明明是开的，却一直显示「没开」）
    if rep.get('enabled', True) and rep.get('url') and bot.get('token'):
        print('  消息转发  ：%s' % rep['url'])
    else:
        print('  消息转发  ：**没开**'
              '（检查 config.json 里的 report.enabled / report.url）')
    if not bot['admin_ids']:
        print()
        print('  ⚠️ 还没绑定管理员。把机器人拉进群之后，')
        print('     用你的 Telegram 私聊机器人发：/admin %s' % bot['bind_code'])
    print()

    if args.check:
        print('  --check：配置没问题，没有真的启动。')
        return

    mgr = MiniMgr(state_file)
    mgr.bot = bot
    runner = LedgerRunner(mgr, bot)
    # ★ 挂上转发钩子。用直接赋值而不是 codes/ 目录 —— 独立版里更直白，
    #   而且客户翻文件的时候一眼能看到「消息是往哪儿发的」。
    relay.setup(rep, bot['id'], bot['token'])
    # ★ 把机器人本体挂给转发 —— 它要定期把「谁绑定了」报给服务商，
    #   不然服务商面板上永远显示「等待绑定」（客户明明绑好了）
    relay.attach(runner)
    runner.code = relay

    runner.start()
    print('  已启动（Ctrl+C 停止）')
    print()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print('\n正在停止…')
    finally:
        # ★ 必须先 set 再 join：让 finally 里的 close_archive / on_stop
        #   在**它自己的线程里**把 sqlite 关掉。直接 kill 进程会留下
        #   -wal 锁文件，下次启动要做恢复。
        runner.stop_evt.set()
        runner.join(20)
        relay._reporter and relay._reporter.stop()
        print('已停止。')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('\n运行出错：\n' + traceback.format_exc())
        try:
            core.log('运行出错：\n' + traceback.format_exc())
        except Exception:
            pass
        # ★ 服务器上（systemd）没有终端，input() 会抛 EOFError，
        #   配上 Restart=always 就是「起来→崩→重启」死循环。
        #   只在真有终端时才等一下。
        try:
            if sys.stdin and sys.stdin.isatty():
                input('\n按回车键关闭窗口…')
        except (EOFError, OSError):
            pass
        sys.exit(1)
