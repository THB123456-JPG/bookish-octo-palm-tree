# -*- coding: utf-8 -*-
"""USDT-TRC20 助手（机器人本体）

功能：
    1. 查询   —— 给机器人发一个 TRC20 地址，返回最近的 USDT 转账
    2. 监听   —— /watch 添加地址，有转入或转出立刻推送提醒（谁、多少、什么时候、交易哈希）

★ 链上查询 / 地址编解码 / 汇率这些**通用工具**已经搬到根目录的 tron.py ——
  那些商城机器人也在用，不属于这个机器人。
"""
import time
from datetime import datetime

from core import BaseRunner, TgError, log

# 工具来自共享库 tron.py（不只是本文件在用，见那边的说明）
from tron import (Rate, TRONSCAN_TX, Tron, fmt_amount, fmt_time,
                  is_address, short)

class UsdtRunner(BaseRunner):
    kind = 'usdt'
    # 客服机器人是 20 秒长轮询；这里短一点，好穿插扫链
    poll_timeout = 5

    # 菜单按钮（改这里就改了底部菜单）
    BTN_WATCH = '📡 地址监听'
    BTN_QUERY = '🔍 查U交易'
    BTN_RATE = '💱 查U汇率'
    BTN_MANAGE = '🗂 地址管理'
    BTN_OPS = '👥 操作人'
    BUTTONS = [BTN_WATCH, BTN_QUERY, BTN_RATE, BTN_MANAGE, BTN_OPS]
    # 旧的按钮文字，兼容还在用老菜单的人
    OLD_LABELS = {'余额监听': BTN_WATCH}

    NOTE_TTL = 300        # 等备注的超时时间（秒），过了就当没这回事

    def __init__(self, manager, bot):
        super().__init__(manager, bot)
        self.tron = Tron()
        self.rate = Rate()
        # 每个管理员当前在什么模式：{'watch'|'query'}
        # 点按钮切换，内存里存着，程序重启就回到「没模式」状态
        self._mode = {}
        # 正在等谁补备注：aid -> {'address':..., 'at': 时间戳}
        self._pending = {}

    # -------- 底部菜单 --------
    def menu(self):
        return {
            'keyboard': [
                [{'text': self.BTN_WATCH}, {'text': self.BTN_QUERY}, {'text': self.BTN_RATE}],
                [{'text': self.BTN_MANAGE}, {'text': self.BTN_OPS}],
            ],
            'resize_keyboard': True,
            'is_persistent': True,
        }

    def send_menu(self, chat_id, text):
        """带底部菜单发一条消息"""
        try:
            return self.api.call('sendMessage', chat_id=chat_id, text=text,
                                 disable_web_page_preview=True,
                                 reply_markup=self.menu())
        except TgError as e:
            log('[%s] 发菜单失败，退回普通消息：%s' % (self.note(), e))
            return self.send(chat_id, text)

    # -------- 数据 --------
    @property
    def watches(self):
        return self.data.setdefault('watches', [])

    def find_watch(self, addr):
        for w in self.watches:
            if w.get('address') == addr:
                return w
        return None

    # -------- 启动 --------
    def on_start(self):
        n = len(self.watches)
        if n:
            log('[%s] 正在监听 %d 个地址' % (self.note(), n))

    # -------- 周期：扫链上有没有新交易 --------
    def tick_interval(self):
        return 45.0

    def on_tick(self):
        if not self.watches or not self.admins():
            return
        for w in list(self.watches):
            if self.stop_evt.is_set():
                return
            try:
                self._scan_one(w)
            except TgError as e:
                log('[%s] 扫 %s 失败：%s' % (self.note(), short(w.get('address')), e))
            except Exception as e:
                log('[%s] 扫 %s 异常：%s' % (self.note(), short(w.get('address')), e))
            self.stop_evt.wait(1.2)      # 别把接口打太急
        self.save_data()

    def _scan_one(self, w):
        addr = w.get('address') or ''
        last_ts = int(w.get('last_ts') or 0)
        # 往前多取 2 分钟，防止同一时间戳的交易被漏掉（重复的靠 txid 去重）
        min_ts = (last_ts - 120000) if last_ts else None
        txs = self.tron.transfers(addr, limit=50, min_ts=min_ts)
        if not txs:
            return

        # 第一次扫：只记基线，不报警（否则会把历史交易全推一遍）
        if not w.get('baseline'):
            w['baseline'] = True
            w['seen'] = [t['txid'] for t in txs][:300]
            w['last_ts'] = max(t['ts'] for t in txs)
            log('[%s] %s 已建立基线（%d 条历史记录，不推送）'
                % (self.note(), short(addr), len(txs)))
            return

        seen = set(w.get('seen') or [])
        fresh = [t for t in txs if t['txid'] not in seen]
        if not fresh:
            return

        fresh.sort(key=lambda t: t['ts'])      # 老的先报，顺序自然
        for t in fresh:
            self._alert(w, t)
            seen.add(t['txid'])

        w['seen'] = list(seen)[-300:]
        w['last_ts'] = max(max(t['ts'] for t in fresh), last_ts)

    def _alert(self, w, t):
        addr = w.get('address') or ''
        incoming = (t['to'] == addr)
        amt = fmt_amount(t['amount'])
        other = t['from'] if incoming else t['to']

        lines = [
            '🔔 %s提醒' % ('进账' if incoming else '转出'),
            '',
            '%s %s USDT' % ('📥 转入 +' if incoming else '📤 转出 -', amt),
            '🏷 备注：%s' % (w.get('note') or '未命名'),
            '👤 对方：%s' % short(other, 8, 8),
            '📍 地址：%s' % short(addr, 8, 8),
            '🕐 时间：%s' % fmt_time(t['ts']),
            '🔗 哈希：%s' % t['txid'],
        ]

        # 顺便查一下最新余额，商家对账时最有用
        try:
            usdt, _trx = self.tron.balance(addr)
            lines.append('')
            lines.append('💰 当前余额：%s USDT' % fmt_amount(usdt))
        except Exception as e:
            log('[%s] 查余额失败：%s' % (self.note(), e))

        lines.append('')
        lines.append(TRONSCAN_TX % t['txid'])
        self.send_to_admins('\n'.join(lines))
        log('[%s] %s %s USDT（%s）' % (self.note(), '进账' if incoming else '转出',
                                       amt, short(addr)))

    # -------- 其他人发消息：不对外开放 --------
    def on_guest(self, msg):
        frm = msg.get('from') or {}
        cid = frm.get('id')
        self.send(cid, '此机器人仅限授权用户使用。')

    # -------- 管理员发消息 --------
    def match_button(self, text):
        """按钮文本匹配（手打不带 emoji、或还在用旧按钮名的都能认）"""
        t = (text or '').strip()
        if t in self.OLD_LABELS:
            return self.OLD_LABELS[t]
        for b in self.BUTTONS:
            if t == b or t == b.split(' ', 1)[-1]:
                return b
        return None

    # -------- 模式 --------
    def set_mode(self, aid, mode):
        self._mode[aid] = mode

    def get_mode(self, aid):
        return self._mode.get(aid)

    # -------- 等待补备注 --------
    def _ask_note(self, aid, addr):
        self._pending[aid] = {'address': addr, 'at': time.time()}
        self.send_menu(aid,
                       '✅ 已加入监听\n%s\n\n'
                       '要不要加个备注，方便以后一眼认出是哪个客户？\n\n'
                       '· 想加 → 直接把昵称发给我（例：客户A付款）\n'
                       '· 不想加 → 回复「无」' % addr)

    def _handle_note(self, aid, text):
        """返回 True 表示这条消息被当成备注处理掉了"""
        p = self._pending.get(aid)
        if not p:
            return False
        # 超时了就当没这回事，走正常流程
        if time.time() - p.get('at', 0) > self.NOTE_TTL:
            self._pending.pop(aid, None)
            return False

        # 等备注的时候用户可能去点按钮 / 打指令 —— 那些不能被当成备注吃掉，
        # 取消等待，交给正常流程处理
        if text.startswith('/') or self.match_button(text):
            self._pending.pop(aid, None)
            return False

        addr = p.get('address') or ''
        self._pending.pop(aid, None)

        skip_words = ('无', '没有', '不加', '不用', '不需要', '跳过', 'no', 'skip', '-')
        if text.strip().lower() in skip_words or text.strip() in skip_words:
            self.set_mode(aid, 'watch')
            self.send_menu(aid, '好的，不加备注。')
            return True

        note = text.strip().replace('\n', ' ')[:40]
        if not note:
            self.set_mode(aid, 'watch')
            return True

        w = self.find_watch(addr)
        if not w:
            self.send_menu(aid, '这个地址已经不在监听列表里了，备注没加上。')
            return True
        w['note'] = note
        self.save_data()
        self.set_mode(aid, 'watch')
        self.send_menu(aid, '✅ 备注已保存：%s\n%s' % (note, addr))
        log('[%s] 备注更新 %s → %s' % (self.note(), short(addr), note))
        return True

    # -------- 按钮 --------
    def _on_button(self, aid, btn):
        if btn == self.BTN_RATE:
            self.cmd_rate(aid)
            return
        if btn == self.BTN_WATCH:
            self.set_mode(aid, 'watch')
            self.send_menu(aid,
                           '📡 地址监听\n\n'
                           '把 TRC20 地址发给我，我就帮你盯着它。\n'
                           '有转入或转出会立刻通知你。\n\n'
                           '例：TNXoiAJ3dct8Fjg4M9fkLFh9S2v9TXc32G\n\n'
                           '发过来我会问你要不要加备注。')
            return
        if btn == self.BTN_QUERY:
            self.set_mode(aid, 'query')
            self.send_menu(aid,
                           '🔍 查U交易\n\n'
                           '把 TRC20 地址发给我，我查它的余额和最近交易。\n\n'
                           '例：TNXoiAJ3dct8Fjg4M9fkLFh9S2v9TXc32G\n\n'
                           '（只查询，不会加入监听）')
            return
        if btn == self.BTN_MANAGE:
            self.cmd_address_manage(aid)
            return
        if btn == self.BTN_OPS:
            self.send_ops_view(aid)
            return

    def on_owner(self, msg):
        frm = msg.get('from') or {}
        aid = frm.get('id')
        text = (msg.get('text') or '').strip()

        # ---- 0. 正在等备注（优先级最高） ----
        if self._handle_note(aid, text):
            return
        # ---- 0b. 正在等操作人用户名 ----
        if text.startswith('/取消') or text.startswith('/cancel'):
            if self._await_op.pop(aid, None) is not None:
                self.send_menu(aid, '已取消。')
                return
        if self.handle_op_input(aid, text):
            return

        # ---- 1. 底部菜单按钮 ----
        btn = self.match_button(text)
        if btn:
            self._on_button(aid, btn)
            return

        # ---- 2. 指令 ----
        if text.startswith('/start') or text.startswith('/help'):
            self.send_menu(aid, self.help_text())
            return
        if text.startswith('/rate') or text.startswith('/汇率'):
            self.cmd_rate(aid)
            return
        if text.startswith('/watch'):
            self.cmd_watch(aid, text)
            return
        if text.startswith('/unwatch'):
            self.cmd_unwatch(aid, text)
            return
        if text.startswith('/list') or text.startswith('/地址管理'):
            self.cmd_address_manage(aid)
            return
        if text.startswith('/ops') or text.startswith('/操作人'):
            self.send_ops_view(aid)
            return
        if text.startswith('/query'):
            arg = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ''
            self.do_query(aid, arg)
            return
        if text.startswith('/'):
            self.send(aid, '不认识的指令。输入 /help 看说明。')
            return

        # ---- 3. 地址：按当前模式处理 ----
        if is_address(text):
            mode = self.get_mode(aid)
            if mode == 'watch':
                self.do_watch(aid, text)
            elif mode == 'query':
                self.do_query(aid, text)
            else:
                self._ask_mode(aid, text)
            return

        # ---- 4. 看着像想发地址、但格式不对 ----
        if text[:1].upper() == 'T' and 20 <= len(text) <= 50:
            self.send(aid, '❌ 这不是有效的 TRC20 地址。\n'
                           'TRX/USDT 地址是 T 开头、共 34 位的一串字符，例如：\n'
                           'TNXoiAJ3dct8Fjg4M9fkLFh9S2v9TXc32G')
            return

        self.send_menu(aid,
                       '先点下面的按钮选个功能 👇\n\n'
                       '📡 地址监听 —— 发地址加入监听，有进出账通知你\n'
                       '🔍 查U交易  —— 发地址查余额和交易\n'
                       '💱 查U汇率  —— 买U最划算的TOP10\n'
                       '🗂 地址管理 —— 看/删正在监听的地址')

    def _ask_mode(self, aid, addr):
        """没选模式就发了地址，问一下要干嘛"""
        try:
            self.api.call('sendMessage', chat_id=aid,
                          text='这个地址你想干什么？\n\n%s' % addr,
                          disable_web_page_preview=True,
                          reply_markup={'inline_keyboard': [[
                              {'text': '🔍 查询交易', 'callback_data': 'q:' + addr},
                              {'text': '📡 加入监听', 'callback_data': 'w:' + addr},
                          ]]})
        except TgError as e:
            log('[%s] 发模式询问失败：%s' % (self.note(), e))
            self.send_menu(aid, '请先点下面的「📡 地址监听」或「🔍 查U交易」，再发地址。')

    # -------- 📡 地址监听：只加监听，不查任何东西 --------
    def do_watch(self, aid, addr):
        addr = (addr or '').strip()
        if not is_address(addr):
            self.send(aid, '❌ 这不是有效的 TRC20 地址。\n'
                           'TRX/USDT 地址是 T 开头、共 34 位的一串字符。')
            return

        existed = self.find_watch(addr) is not None
        w = self._add_watch(addr, '')
        if existed:
            # 已经在监听里了，那就顺便问一下要不要改备注
            self.send_menu(aid, '这个地址已经在监听里了。\n%s\n'
                                '当前备注：%s' % (addr, w.get('note') or '未命名'))
            self.set_mode(aid, 'watch')
            return
        self._ask_note(aid, addr)

    # -------- 🔍 查U交易：只查询，不加监听 --------
    def do_query(self, aid, addr):
        addr = (addr or '').strip()
        if not is_address(addr):
            self.send(aid, '❌ 这不是有效的 TRC20 地址。\n'
                           'TRX/USDT 地址是 T 开头、共 34 位的一串字符。')
            return

        self.send(aid, '🔍 查询中…')

        usdt = trx = None
        try:
            usdt, trx = self.tron.balance(addr)
        except TgError as e:
            log('[%s] 查余额失败：%s' % (self.note(), e))

        try:
            txs = self.tron.transfers(addr, limit=10)
        except TgError as e:
            if usdt is None:
                self.send(aid, '❌ %s' % e)
                return
            txs = []

        lines = ['📋 %s' % short(addr, 8, 8), '']
        if usdt is not None:
            lines.append('💰 USDT 余额：%s' % fmt_amount(usdt))
            lines.append('⚡ TRX  余额：%s' % fmt_amount(trx))
        else:
            lines.append('（余额暂时取不到）')

        lines.append('')
        if not txs:
            lines.append('📭 没有 USDT 转账记录。')
        else:
            lines.append('最近 %d 条 USDT 转账：' % len(txs))
            lines.append('')
            for i, t in enumerate(txs, 1):
                incoming = (t['to'] == addr)
                lines.append('%d. %s %s USDT' % (
                    i, '📥 转入 +' if incoming else '📤 转出 -',
                    fmt_amount(t['amount'])))
                lines.append('   对方 %s' % short(t['from'] if incoming else t['to'], 6, 6))
                lines.append('   %s' % fmt_time(t['ts']))
                lines.append('   哈希 %s' % short(t['txid'], 10, 8))
                lines.append('')

        lines.append('🔗 https://tronscan.org/#/address/%s' % addr)
        self.send(aid, '\n'.join(lines))

    # -------- 功能 3：汇率（买U的 TOP10） --------
    def cmd_rate(self, aid):
        r = self.rate.get()
        rows = r.get('rows') or []
        if not rows:
            self.send_menu(aid, '❌ 汇率暂时取不到：%s\n稍后再试试。'
                           % (r.get('err') or '未知原因'))
            return

        lines = ['💱 U 买入价 TOP10 · 欧易商家实时', '']
        for i, x in enumerate(rows, 1):
            name = x['name']
            if len(name) > 12:
                name = name[:11] + '…'
            lines.append('%2d. %.2f  %s' % (i, x['price'], name))

        best = min(x['price'] for x in rows)
        lines.append('')
        lines.append('📍 最低价：%.2f CNY' % best)
        # 第三档 = 第 3 条的价格
        if len(rows) >= 3:
            lines.append('📌 第三档汇率：%.2f CNY' % rows[2]['price'])
        lines.append('🕐 %s' % datetime.fromtimestamp(r['at']).strftime('%H:%M:%S'))
        self.send_menu(aid, '\n'.join(lines))

    # -------- 🗂 地址管理（列表 + 删除按钮） --------
    def _manage_view(self):
        """返回 (文本, inline键盘)。没有地址时键盘是 None"""
        ws = self.watches
        if not ws:
            return ('🗂 地址管理\n\n'
                    '还没有监听任何地址。\n\n'
                    '直接把 TRC20 地址发给我，就会自动加入监听。', None)

        lines = ['🗂 地址管理', '',
                 '共 %d 个监听地址，点下面的按钮可以删除：' % len(ws), '']
        rows = []
        shown = ws[:30]
        for i, w in enumerate(shown, 1):
            note = w.get('note') or '未命名'
            addr = w.get('address') or ''
            lines.append('%d. %s' % (i, note))
            lines.append('   %s' % addr)
            lines.append('   加入 %s' % (w.get('added') or '?'))
            lines.append('')
            label = '🗑 %d. %s' % (i, note if len(note) <= 16 else note[:15] + '…')
            rows.append([{'text': label, 'callback_data': 'del:' + addr}])
        if len(ws) > 30:
            lines.append('…还有 %d 个没显示出来' % (len(ws) - 30))
            lines.append('（用 /unwatch 地址 可以删后面的）')
            lines.append('')
        lines.append('也可以直接发 /unwatch 地址 来取消监听')
        return '\n'.join(lines), {'inline_keyboard': rows}

    def cmd_address_manage(self, aid):
        text, kb = self._manage_view()
        if not kb:
            self.send_menu(aid, text)
            return
        try:
            self.api.call('sendMessage', chat_id=aid, text=text,
                          disable_web_page_preview=True, reply_markup=kb)
        except TgError as e:
            log('[%s] 发地址管理失败：%s' % (self.note(), e))
            self.send(aid, text)

    def _clear_buttons(self, aid, mid, text=None, kb=None):
        """把某条消息上的按钮去掉（或者换成新的）"""
        if not mid:
            return
        try:
            self.api.call('editMessageText', chat_id=aid, message_id=mid,
                          text=text or '（已处理）',
                          disable_web_page_preview=True,
                          reply_markup=kb or {'inline_keyboard': []})
        except TgError as e:
            log('[%s] 刷新消息失败：%s' % (self.note(), e))

    def on_callback(self, cq):
        """处理按钮：地址管理的删除、以及「查询/监听」二选一"""
        data = cq.get('data') or ''
        cb_id = cq.get('id')
        frm = cq.get('from') or {}
        aid = frm.get('id')
        msg = cq.get('message') or {}
        mid = msg.get('message_id')

        # ---- 操作人管理（基类处理） ----
        if self.op_callback(cq, aid):
            return

        # ---- 没选模式时发的地址，这里选 ----
        if data.startswith('q:') or data.startswith('w:'):
            addr = data[2:]
            if data.startswith('q:'):
                self._answer_cb(cb_id, '正在查询…')
                self.set_mode(aid, 'query')
                self._clear_buttons(aid, mid, '🔍 查询：%s' % addr)
                self.do_query(aid, addr)
            else:
                self._answer_cb(cb_id, '已加入监听')
                self.set_mode(aid, 'watch')
                self._clear_buttons(aid, mid, '📡 加入监听：%s' % addr)
                self.do_watch(aid, addr)
            return

        if not data.startswith('del:'):
            self._answer_cb(cb_id, '不支持的操作')
            return

        addr = data[4:]
        w = self.find_watch(addr)
        if not w:
            self._answer_cb(cb_id, '这个地址已经不在列表里了')
        else:
            note = w.get('note') or '未命名'
            self.watches.remove(w)
            self.save_data()
            self._answer_cb(cb_id, '已删除：%s' % note)
            log('[%s] 取消监听 %s（%s）' % (self.note(), short(addr), note))

        # 刷新这条消息，让删掉的按钮消失
        text, kb = self._manage_view()
        self._clear_buttons(aid, mid, text, kb)

    # -------- 功能 2：监听 --------
    def _add_watch(self, addr, note):
        """加入监听。已经在了就只更新备注"""
        w = self.find_watch(addr)
        if w:
            if note:
                w['note'] = note
                self.save_data()
            return w
        w = {
            'address': addr,
            'note': note or '未命名',
            'added': datetime.now().strftime('%Y-%m-%d %H:%M'),
            'baseline': False,
            'seen': [],
            'last_ts': 0,
        }
        self.watches.append(w)
        self.save_data()
        log('[%s] 新增监听 %s（%s）' % (self.note(), short(addr), note or '未命名'))
        return w

    def cmd_watch(self, aid, text):
        parts = text.split(None, 2)
        if len(parts) < 2 or not is_address(parts[1]):
            self.send(aid, '用法：/watch 地址 备注\n'
                           '例：/watch TNXoiAJ3dct8Fjg4M9fkLFh9S2v9TXc32G 客户A付款\n\n'
                           '★ 平时不用打指令 —— 点「📡 地址监听」后发地址就行')
            return
        addr = parts[1].strip()
        note = (parts[2].strip() if len(parts) > 2 else '')

        # 带备注的：直接加上
        if note:
            existed = self.find_watch(addr) is not None
            self._add_watch(addr, note)
            self.send(aid, '✅ 已加入监听\n🏷 备注：%s\n📍 %s\n\n'
                           '%s' % (note, addr,
                                   '（备注已更新）' if existed else
                                   '有转入或转出我会立刻通知你。'))
            self.set_mode(aid, 'watch')
            return

        # 没带备注的：走监听流程，顺便问备注
        self.do_watch(aid, addr)

    def cmd_unwatch(self, aid, text):
        parts = text.split()
        if len(parts) < 2:
            self.send(aid, '用法：/unwatch 地址\n（地址可以用 /list 查）')
            return
        addr = parts[1].strip()
        w = self.find_watch(addr)
        if not w:
            self.send(aid, '没有在监听这个地址。')
            return
        self.watches.remove(w)
        self.save_data()
        self.send(aid, '✅ 已取消监听 %s' % short(addr, 8, 8))
        log('[%s] 取消监听 %s' % (self.note(), short(addr)))

    # -------- 文案 --------
    def send_welcome(self, chat_id, text):
        # 绑定成功后顺便把底部菜单发出去
        self.send_menu(chat_id, text)

    def welcome_owner(self):
        return ('底部菜单五个功能，先点一个，再按提示发地址：\n\n'
                '📡 地址监听 —— 发地址加入监听，有进出账通知你\n'
                '🔍 查U交易  —— 发地址查余额和交易（不会监听）\n'
                '💱 查U汇率  —— 买U最划算的TOP10\n'
                '🗂 地址管理 —— 看/删正在监听的地址\n'
                '👥 操作人   —— 让别人也能用这个机器人')

    @staticmethod
    def help_text():
        return ('📖 USDT 助手 · 使用说明\n'
                '\n'
                '点底部菜单选功能，再按提示发地址：\n'
                '\n'
                '📡 地址监听\n'
                '   点它 → 发地址 → 加入监听\n'
                '   然后我会问你要不要加备注：\n'
                '     想加 → 直接回复昵称（例：客户A付款）\n'
                '     不加 → 回复「无」\n'
                '   之后这个地址有转入/转出会立刻通知你\n'
                '\n'
                '🔍 查U交易\n'
                '   点它 → 发地址 → 返回余额和最近交易\n'
                '   （只查询，不会加入监听）\n'
                '\n'
                '💱 查U汇率\n'
                '   买 U 最划算的 TOP10（欧易商家实时挂单价）\n'
                '\n'
                '🗂 地址管理\n'
                '   看所有正在监听的地址，点按钮就能删\n'
                '\n'
                '👥 操作人（只有绑定者能改）\n'
                '   一个机器人可以几个人共用。\n'
                '   点「➕ 添加操作人」→ 输入对方用户名 →\n'
                '   把生成的链接发给对方，对方点一下就能用。\n'
                '\n'
                '———— 常用指令 ————\n'
                '/watch 地址 备注    直接加监听（带备注）\n'
                '/unwatch 地址       取消监听\n'
                '/rate               查汇率\n'
                '/ops                操作人管理\n'
                '/id                 查看你的 ID\n'
                '/help               显示这段说明\n'
                '\n'
                '（第一次加监听只建基线，不会把历史交易推给你）')
