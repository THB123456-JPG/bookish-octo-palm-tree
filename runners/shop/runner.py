# -*- coding: utf-8 -*-
"""商城机器人 —— 能量自助充值

★ 核心：**不依赖机器人交互**
   客户只要知道你的收款地址，从自己钱包转 TRX 过来，
   系统就把能量发给「转钱的那个地址」。机器人这里主要是：
     · 告诉客户收款地址和价格
     · 客户查自己的充值记录
     · 给运营者发通知、看流水统计
"""
import time
from datetime import datetime

import requests

from core import BaseRunner, TgError, log
from tron import Tron, addr_to_hex, fmt_amount, is_address, short, usdt_to_trx

from . import store as S
from .pay import PayWatcher
from .providers import get_provider, norm_username

# 常见操作要多少能量（给「查手续费」用）
ENERGY_COST = {
    '普通 TRC20 转账': 65000,
    '首次转给没有 USDT 的地址': 131000,
    'TRC20 授权（approve）': 15000,
    'USDT 转账 + 授权（一次搞定）': 80000,
}


def esc(s):
    return (str(s or '').replace('&', '&amp;')
            .replace('<', '&lt;').replace('>', '&gt;'))


class ShopRunner(BaseRunner):
    kind = 'shop'
    poll_timeout = 10          # 短一点，好穿插扫收款

    BTN_BUY = '⚡ 充能量'
    BTN_REC = '📊 我的记录'
    BTN_FEE = '💲 查手续费'
    BTN_RATE = '📈 实时U价'
    BTN_HELP = '☎️ 联系客服'
    BTN_PREM = '🎁 开通会员'
    BTN_AUTO = '🔥 笔数套餐'
    BTN_REMAIN = '🔢 剩余笔数'
    BUTTONS = [BTN_BUY, BTN_REC, BTN_FEE, BTN_RATE,
               BTN_PREM, BTN_AUTO, BTN_REMAIN, BTN_HELP]

    FAIL_NOTICE_GAP = 1800     # 同一笔发货失败，最多半小时提醒一次（防刷屏）

    def __init__(self, manager, bot):
        super().__init__(manager, bot)
        self.tron = Tron()
        self.store = S.ShopStore(self)
        self.pay = PayWatcher(self, self.store, tron=self.tron)
        self._sess = {}
        self._rate = None
        self._rate_at = 0.0
        self._bal_at = 0.0          # 上次查上游余额的时间（告警限流用）

    # ================= 基础 =================
    def provider(self):
        cfg = self.store.cfg
        return get_provider(cfg.get('provider'), cfg.get('provider_key'),
                            cfg.get('premium_key'), cfg)

    def menu(self):
        cfg = self.store.cfg
        rows = [[{'text': self.BTN_BUY}, {'text': self.BTN_REC}]]
        # 没开的功能就别显示按钮，免得客户点了扑空
        if cfg.get('auto_enabled'):
            rows.append([{'text': self.BTN_AUTO}, {'text': self.BTN_REMAIN}])
        rows.append([{'text': self.BTN_FEE}, {'text': self.BTN_RATE}])
        if cfg.get('premium_enabled'):
            rows.append([{'text': self.BTN_PREM}])
        rows.append([{'text': self.BTN_HELP}])
        return {'keyboard': rows, 'resize_keyboard': True,
                'is_persistent': True}

    def send_menu(self, chat_id, text):
        markup = self.menu()
        try:
            import customer_ui
        except ImportError:
            customer_ui = None
        if customer_ui and customer_ui.supported(self):
            markup = customer_ui.keyboard(self, chat_id)
        try:
            return self.api.call('sendMessage', chat_id=chat_id, text=text,
                                 disable_web_page_preview=True,
                                 reply_markup=markup)
        except TgError as e:
            log('[%s] 发菜单失败：%s' % (self.note(), e))
            return self.send(chat_id, text)

    def send_html(self, chat_id, text, kb=None):
        params = dict(chat_id=chat_id, text=text, parse_mode='HTML',
                      disable_web_page_preview=True)
        if kb:
            params['reply_markup'] = kb
        try:
            return self.api.call('sendMessage', **params)
        except TgError as e:
            log('[%s] 发 HTML 失败，退回纯文本：%s' % (self.note(), e))
            return self.send(chat_id, text)

    def match_button(self, text):
        t = (text or '').strip()
        for b in self.BUTTONS:
            if t == b or t == b.split(' ', 1)[-1]:
                return b
        return None

    def allow_callback(self, cid):
        """面向公众的服务机器人：谁点按钮都要响应（基类默认只放管理员）"""
        return True

    def welcome_owner(self):
        lines = ['这是个自助充能量的机器人。', '',
                 '⚡ 充能量 —— 看收款地址和价格',
                 '📊 我的记录 —— 查某个地址充了多少',
                 '💲 查手续费 —— 转账要多少能量',
                 '📈 实时U价 —— 当前 USDT 行情']
        if self.store.cfg.get('auto_enabled'):
            lines.append('🔥 笔数套餐 —— 买 N 笔，用一次扣一次')
            lines.append('🔢 剩余笔数 —— 查某个地址还剩几笔')
        if self.store.cfg.get('premium_enabled'):
            lines.append('🎁 开通会员 —— 自助开 Telegram Premium')
        lines.append('☎️ 联系客服')
        return '\n'.join(lines)

    def help_text(self, is_admin=False):
        """客户只看得到用法，运营指令只给管理员看"""
        lines = [
            '📖 能量自助充值 · 说明',
            '',
            '【怎么用】',
            '从你自己的钱包，往我们的收款地址转 TRX 即可，',
            '系统会自动把能量发到【你转账用的那个地址】。',
            '不用在机器人里登记地址，转了就发货。',
            '',
            '⚠️ 必须从自己的钱包转。',
            '从交易所直接转的话，能量会发到交易所地址上，白送。',
        ]
        if self.store.cfg.get('auto_enabled'):
            lines += [
                '',
                '【笔数套餐】',
                '点「🔥 笔数套餐」→ 选档位 → 填你的地址，',
                '按订单金额转 TRX，笔数就买到你那个地址上了。',
                '之后你用它转账，系统自动扣笔数，不用再操作。',
                '点「🔢 剩余笔数」发地址，可以随时查还剩几笔。',
            ]
        if self.store.cfg.get('premium_enabled'):
            lines += [
                '',
                '【开通 TG 会员】',
                '点「🎁 开通会员」→ 选时长 → 填用户名和你的付款地址，',
                '然后按订单上显示的金额转 USDT。',
                '⚠️ 会员要转订单上那个精确金额，少一分都对不上。',
                '用 /会员 可以查你自己下过的单。',
            ]
        if is_admin:
            lines += [
                '',
                '【运营指令】（仅管理员）',
                '/收款      看收款配置和扫描状态',
                '/流水      看最近 10 笔',
                '/统计      今日汇总',
                '/余额      查上游账户余额（余额见底就发不出货）',
            ]
        return '\n'.join(lines)

    # ================= 生命周期 =================
    def on_start(self):
        cfg = self.store.cfg
        if self.store.ready():
            log('[%s] 收款地址 %s，单价 %s TRX = %s 能量，上游 %s'
                % (self.note(), short(cfg['trx_own'], 8, 8),
                   fmt_amount(cfg.get('price') or 0),
                   '{:,}'.format(int(cfg.get('energy') or 0)),
                   self.provider().label))
        else:
            log('[%s] ⚠️ 还没配置收款地址（trx_own），客户充不了值' % self.note())

    def tick_interval(self):
        return 30.0

    def on_tick(self):
        if not self.store.cfg.get('enabled', True):
            return
        try:
            self.pay.scan()
        except TgError as e:
            log('[%s] 扫收款失败：%s' % (self.note(), e))
        except Exception as e:
            log('[%s] 扫收款异常：%s' % (self.note(), e))
        # 补发卡住的（程序上一轮崩在发货中间）
        try:
            self.pay.retry_pending()
        except Exception as e:
            log('[%s] 补发异常：%s' % (self.note(), e))
        # 上游余额见底就喊人 —— 不然要等客户付了钱才发现发不出货
        try:
            self.check_low_balance()
        except Exception as e:
            log('[%s] 查上游余额异常：%s' % (self.note(), e))
        # 会员订单：补开卡住的 + 作废超时没付款的
        try:
            self.pay.retry_premiums()
        except Exception as e:
            log('[%s] 补开会员异常：%s' % (self.note(), e))
        try:
            self.store.premium_expire_stale()
        except Exception as e:
            log('[%s] 清理超时会员订单异常：%s' % (self.note(), e))

    def check_low_balance(self):
        """上游余额不够发几单就告警。半小时最多喊一次，别刷屏。"""
        now = time.time()
        if now - self._bal_at < 1800:
            return
        cfg = self.store.cfg
        prov = self.provider()
        if not prov.configured():
            return
        self._bal_at = now          # 先记时间：就算查失败也别每轮重试
        try:
            bal = prov.balance()
        except Exception:
            return
        if bal is None:
            return
        per = prov.quote(int(cfg.get('energy') or 0), 1)[0]
        # 官方公示价会滞后（实测就比公示的高），有实际扣费记录就以实测为准，
        # 两个取大的 —— 宁可把余额估得紧一点，也别以为够用结果发不出去
        hist = [p.get('cost') for p in self.store.recent(20) if p.get('cost')]
        if hist:
            per = max(per, max(hist))
        if per <= 0 or bal >= per * 3:
            return                  # 还够发 3 单以上，不用吵

        can = int(bal / per)
        head = ('🚨 <b>上游余额告急</b>' if can <= 0
                else '⚠️ <b>上游余额偏低</b>')
        body = ('%s\n上游 <b>%s</b> 只剩 <b>%s TRX</b>，'
                '每单成本约 %s TRX。'
                % (head, esc(prov.label), fmt_amount(bal), fmt_amount(per)))
        body += ('\n\n<b>现在一单都发不出去了，客户付了钱会卡住，赶紧充值！</b>'
                 if can <= 0 else '\n\n只够再发 %d 单，抽空充一下。' % can)
        self.notify_group(body)
        oid = self.owner_id()
        if oid:
            self.send_html(oid, body)

    # ================= 消息分发 =================
    def on_owner(self, msg):
        self.handle_user(msg, is_admin=True)

    def on_guest(self, msg):
        self.handle_user(msg, is_admin=False)

    def handle_user(self, msg, is_admin):
        frm = msg.get('from') or {}
        cid = frm.get('id')
        text = (msg.get('text') or '').strip()

        # ★ /start 和 /help 必须对所有人响应，而且要放在管理员判断**之前**。
        #   客户第一次进来发的就是 /start，回「不认识的指令」这机器人就没法用了。
        if text.startswith('/start') or text.startswith('/help'):
            self.send_menu(cid, self.help_text(is_admin))
            return
        # 客户查自己下过的会员单
        if text.startswith('/会员') or text.startswith('/premium'):
            self.show_my_premiums(cid)
            return

        if is_admin and text.startswith('/'):
            if text.startswith('/收款') or text.startswith('/shop'):
                self.cmd_status(cid)
                return
            if text.startswith('/流水') or text.startswith('/payments'):
                self.cmd_payments(cid)
                return
            if text.startswith('/统计') or text.startswith('/stat'):
                self.cmd_stat(cid)
                return
            if text.startswith('/余额') or text.startswith('/balance'):
                self.cmd_balance(cid)
                return
            if text.startswith('/取消') or text.startswith('/cancel'):
                if self._await_op.pop(cid, None) is not None:
                    self.send_menu(cid, '已取消。')
                    return
            if text.startswith('/ops') or text.startswith('/操作人'):
                self.send_ops_view(cid)
                return

        if self.handle_op_input(cid, text):
            return
        if self.handle_session(cid, text, frm):
            return

        btn = self.match_button(text)
        if btn:
            self.on_button(cid, btn)
            return

        # 用户直接发了个地址 → 当成「查这个地址的记录」
        if is_address(text):
            self.show_addr_record(cid, text)
            return

        if text.startswith('/'):
            self.send_menu(cid, '不认识的指令。' + ('输入 /help 看说明。'
                                                  if is_admin else ''))
            return

        self.send_menu(cid, '点下面的菜单 👇')

    def on_button(self, cid, btn):
        if btn == self.BTN_BUY:
            self.flow_buy(cid)
        elif btn == self.BTN_REC:
            self._set_sess(cid, 'addr')
            self.send_menu(cid, '📊 把你的 TRC20 地址发给我，\n'
                                '我查查这个地址充过多少。')
        elif btn == self.BTN_FEE:
            self._set_sess(cid, 'fee')
            self.send_menu(cid, '💲 把你要转账的 TRC20 地址发给我，\n'
                                '我看看要烧多少 TRX。')
        elif btn == self.BTN_RATE:
            self.cmd_rate(cid)
        elif btn == self.BTN_PREM:
            self.flow_premium(cid)
        elif btn == self.BTN_AUTO:
            self.flow_auto(cid)
        elif btn == self.BTN_REMAIN:
            self.flow_remain(cid)
        elif btn == self.BTN_HELP:
            self.flow_contact(cid)

    # ================= 会话 =================
    def _set_sess(self, cid, flow, **kw):
        d = {'flow': flow, 'at': time.time()}
        d.update(kw)
        self._sess[cid] = d

    def handle_session(self, cid, text, frm):
        s = self._sess.get(cid)
        if not s:
            return False
        if time.time() - s.get('at', 0) > 600:
            self._sess.pop(cid, None)
            return False

        # ★★ 用户在等输入的时候点了别的菜单 —— 放行，别把按钮文字当地址
        #
        #   场景：点「🔢 剩余笔数」（它在等地址），发现点错了，
        #   又去点「📞 联系客服」→ 按钮文字「📞 联系客服」被当成地址，
        #   回一句「这不是有效的 TRC20 地址」，用户一脸懵。
        #
        #   所以：等待期间只要来的是**菜单按钮**或 **/ 命令**，
        #   就取消这次等待、交回给下面正常处理（点哪个就执行哪个）。
        #   只有**真的一段文字**才继续当输入。
        #
        #   ★ 跟 usdt.py 的 `_handle_note()` 是同一类坑，那边早修过了。
        text = text or ''
        if self.match_button(text) or text.startswith('/'):
            self._sess.pop(cid, None)
            return False

        flow = s.get('flow')
        self._sess.pop(cid, None)
        if flow == 'addr':
            self.show_addr_record(cid, text)
            return True
        if flow == 'fee':
            self.show_fee(cid, text)
            return True
        if flow == 'puser':
            u = norm_username(text)
            if not u or len(u) < 4 or ' ' in u:
                self.send_menu(cid, '❌ 用户名看着不对，'
                                    '重新点「🎁 开通会员」来一次。')
                return True
            self._set_sess(cid, 'paddr', month=s.get('month'), username=u)
            self.send_menu(cid, '还差最后一步：\n\n'
                                '你打算用哪个地址付款？\n'
                                '把地址发给我 —— 我靠它认这笔单子是你付的，\n'
                                '金额就不用加零头了。\n\n'
                                '（就是你转账用的那个钱包地址）\n\n'
                                '⚠️ 只收 <b>TRC20 网络</b>的地址：'
                                '<b>T 开头、34 位</b>。\n'
                                '以太坊、BSC、<b>TON</b> 等其它链的地址收不了，'
                                '发错了钱找不回来。')
            return True
        if flow == 'paddr':
            self.start_premium_order(cid, s.get('username'), s.get('month'),
                                     text)
            return True
        if flow == 'acaddr':
            self.start_auto_order(cid, text, s.get('count'))
            return True
        if flow == 'remain':
            self.show_remain(cid, text)
            return True
        return False

    # ================= ⚡ 充能量 =================
    def flow_buy(self, cid):
        cfg = self.store.cfg
        if not self.store.ready():
            self.send_menu(cid, '⚠️ 商户还没配置收款地址，暂时不能充值。\n请联系管理员。')
            return
        if not cfg.get('enabled', True):
            self.send_menu(cid, '⚠️ 充值功能维护中，请稍后再试。')
            return

        price = float(cfg.get('price') or 0)
        per = int(cfg.get('energy') or 0)
        mx = int(cfg.get('max') or 0)

        lines = ['⚡ <b>能量自助充值</b>', '']
        if cfg.get('notice'):
            lines += [esc(cfg['notice']), '']
        lines += [
            '从<b>你自己的钱包</b>往下面这个地址转 TRX，',
            '系统会自动把能量发到<b>你转账用的那个地址</b>。',
            '不用登记地址，转了就发货。', '',
            '📍 <b>收款地址</b>（点一下复制）：',
            '<code>%s</code>' % esc(cfg.get('trx_own')),
            '',
            '💱 <b>价格</b>',
            '%s TRX = %s 能量' % (fmt_amount(price), '{:,}'.format(per)),
        ]
        if mx > 0:
            lines.append('（最多一次 %s 倍，即 %s TRX = %s 能量）'
                         % (mx, fmt_amount(price * mx),
                            '{:,}'.format(per * mx)))
        lines += ['', '举个例子：']
        for n in (1, 2, 5):
            if mx and n > mx:
                break
            lines.append('  转 %s TRX → 发 %s 能量'
                         % (fmt_amount(price * n), '{:,}'.format(per * n)))
        lines += [
            '',
            '⏱ 到账后 3~10 秒自动发货',
            '',
            '⚠️ <b>必须从自己的钱包转</b>。',
            '从交易所直接转的话，能量会发到交易所地址上（白送）。',
        ]
        if cfg.get('contact'):
            lines += ['', '有疑问联系：%s' % esc(cfg['contact'])]

        kb = {'inline_keyboard': [[
            {'text': '📋 复制收款地址', 'callback_data': 'copy'},
        ]]} if False else None      # Telegram 不能程序化写剪贴板，靠 <code> 点击复制
        self.send_html(cid, '\n'.join(lines), kb)

    # ================= 📊 地址记录 =================
    def addr_energy(self, addr):
        """查地址当前可用能量（链上实时）。查不到返回 None。

        客户问「能量到底到没到」看的就是这个数 —— 比让我们自己记账可信得多。
        """
        try:
            r = requests.post(
                'https://api.trongrid.io/wallet/getaccountresource',
                json={'address': addr_to_hex(addr)}, timeout=15)
            j = r.json()
            return max(0, int(j.get('EnergyLimit') or 0)
                       - int(j.get('EnergyUsed') or 0))
        except Exception as e:
            log('[%s] 查能量失败（%s）：%s' % (self.note(), addr[:12], e))
            return None

    def show_addr_record(self, cid, addr):
        addr = (addr or '').strip()
        if not is_address(addr):
            self.send_menu(cid, '❌ 这不是有效的 TRC20 地址（T 开头，34 位）。')
            return
        a = (self.store.addrs or {}).get(addr)
        rows = [p for p in self.store.payments if p.get('from') == addr]

        lines = ['📊 <b>充值记录</b>', '',
                 '<code>%s</code>' % esc(addr), '']
        # 链上实时能量 —— 客户问「能量到没到」看的就是这一行
        e = self.addr_energy(addr)
        if e is not None:
            lines += ['⚡ 当前可用能量：<b>%s</b>' % '{:,}'.format(e), '']
        if not rows:
            lines.append('这个地址还没有充值记录。')
            lines.append('')
            lines.append('要充值点「⚡ 充能量」看收款地址。')
        else:
            lines += [
                '累计充值：<b>%s TRX</b>' % fmt_amount((a or {}).get('trx') or 0),
                '累计能量：<b>%s</b>' % '{:,}'.format(int((a or {}).get('energy') or 0)),
                '笔数：<b>%d</b>' % int((a or {}).get('count') or 0),
                '',
                '最近几笔：',
            ]
            for p in list(reversed(rows))[:6]:
                lines.append('· %s  +%s TRX → %s 能量  %s'
                             % (S.at_str(p), fmt_amount(p.get('amount') or 0),
                                '{:,}'.format(int(p.get('energy') or 0)),
                                S.pay_label(p)))
        self.send_html(cid, '\n'.join(lines))

    # ================= 💲 查手续费 =================
    def show_fee(self, cid, addr):
        addr = (addr or '').strip()
        if not is_address(addr):
            self.send_menu(cid, '❌ 不是有效的 TRC20 地址，重新点「💲 查手续费」')
            return
        lines = ['💲 <b>手续费估算</b>', '',
                 '地址：<code>%s</code>' % esc(addr), '']
        usdt = 0.0
        try:
            usdt, trx = self.tron.balance(addr)
            lines.append('当前余额：%s USDT / %s TRX'
                         % (fmt_amount(usdt), fmt_amount(trx)))
            lines.append('')
        except TgError as e:
            lines.append('（余额查不到：%s）' % esc(e))
            lines.append('')

        need = 131000 if usdt <= 0 else 65000
        burn = need * 100 / 1e6
        lines += [
            '<b>转一次 USDT 要多少</b>',
            '· 你的地址：%s' % ('USDT 余额为 0，属于首次转账，要 %s 能量'
                             % '{:,}'.format(need) if usdt <= 0
                             else '已有 USDT，普通转账要 %s 能量'
                                  % '{:,}'.format(need)),
            '· 直接烧 TRX：约 <b>%s TRX</b>' % fmt_amount(burn),
        ]
        cfg = self.store.cfg
        try:
            price = float(cfg.get('price') or 0)
            per = int(cfg.get('energy') or 0)
            if per > 0:
                units = -(-need // per)      # 向上取整
                cost = units * price
                lines.append('· 充值能量：约 <b>%s TRX</b>（%d 个单位）'
                             % (fmt_amount(cost), units))
                if burn > 0:
                    lines.append('  → 省 <b>%d%%</b>'
                                 % int((1 - cost / burn) * 100))
        except Exception:
            pass
        lines += ['', '其他操作参考：']
        for k, v in ENERGY_COST.items():
            lines.append('· %s：%s' % (k, '{:,}'.format(v)))
        self.send_html(cid, '\n'.join(lines))

    # ================= 📈 实时U价 =================
    def cmd_rate(self, cid):
        now = time.time()
        if not self._rate or now - self._rate_at > 60:
            try:
                from tron import Rate
                self._rate = Rate().get()
                self._rate_at = now
            except Exception as e:
                log('[%s] 取汇率失败：%s' % (self.note(), e))
                self._rate = None
        r = self._rate
        rows = (r or {}).get('rows') or []
        if not rows:
            self.send_menu(cid, '❌ 汇率暂时取不到，稍后再试。')
            return
        lines = ['📈 U 买入价 TOP10 · 欧易商家实时', '']
        for i, x in enumerate(rows, 1):
            nm = x['name']
            if len(nm) > 12:
                nm = nm[:11] + '…'
            lines.append('%2d. %.2f  %s' % (i, x['price'], nm))
        best = min(x['price'] for x in rows)
        lines += ['', '📍 最低价：%.2f CNY' % best]
        if len(rows) >= 3:
            lines.append('📌 第三档汇率：%.2f CNY' % rows[2]['price'])
        lines.append('🕐 %s' % datetime.fromtimestamp(
            (r or {}).get('at') or now).strftime('%H:%M:%S'))
        self.send_menu(cid, '\n'.join(lines))

    # ================= ☎️ 联系客服 =================
    def flow_contact(self, cid):
        """点「联系客服」直接跳到客服的 TG 聊天，不打印联系方式"""
        cfg = self.store.cfg
        c = (cfg.get('contact') or '').strip()
        if not c:
            self.send_menu(cid, '☎️ 管理员还没填客服用户名。')
            return
        # 填的是 @用户名 → 生成 t.me 跳转；填了完整链接也认
        u = c.lstrip('@').strip()
        url = c if c.startswith('http') else 'https://t.me/%s' % u
        kb = {'inline_keyboard': [[{'text': '💬 点这里联系客服', 'url': url}]]}
        self.send_html(cid, '☎️ <b>联系客服</b>\n\n点下面的按钮直接开聊 👇', kb)

    # ================= 🔥 笔数套餐 =================
    def flow_auto(self, cid):
        """列出笔数档位"""
        cfg = self.store.cfg
        if not cfg.get('auto_enabled'):
            # ★ 这条既是给客户的、也是给管理员看的 —— 管理员看到这句才知道
            #   该去哪儿开（用户就卡在这儿：机器人说没开，他不知道在哪开）
            self.send_menu(cid, '🔥 笔数套餐暂未开放。\n\n'
                                '（管理员：面板「机器人列表 → 商城机器人 → '
                                '⚙️ 配置」里有「笔数套餐」开关和「笔数档位」）')
            return
        if not self.store.ready():
            self.send_menu(cid, '⚠️ 商户还没配置收款地址，暂时不能下单。')
            return
        rows = []
        lines = ['🔥 <b>智能笔数套餐</b>', '',
                 '买一次，用一次，用完再买 —— 常转账的话比现租能量划算。', '']
        for c, p in self.store.auto_tiers():
            lines.append('· <b>%d 笔</b> —— %s TRX' % (c, fmt_amount(p)))
            rows.append([{'text': '%d 笔 · %s TRX' % (c, fmt_amount(p)),
                          'callback_data': 'ac:%d' % c}])
        if not rows:
            self.send_menu(cid, '🔥 笔数套餐开了，但还没定价，暂时买不了。\n\n'
                                '（管理员：面板「机器人列表 → 商城机器人 → '
                                '⚙️ 配置 → 笔数档位」里填，每行一档，\n'
                                '比如「10 30」就是 10 笔卖 30 TRX）')
            return
        lines += ['', '要买哪个档？点下面的按钮 👇']
        self.send_html(cid, '\n'.join(lines), {'inline_keyboard': rows})

    def flow_remain(self, cid):
        self._set_sess(cid, 'remain')
        self.send_menu(cid, '🔢 把地址发给我，我查查还剩几笔。')

    def show_remain(self, cid, address):
        addr = (address or '').strip()
        if not is_address(addr):
            self.send_menu(cid, '❌ 这不是有效的 TRC20 地址（T 开头，34 位）。')
            return
        try:
            info = self.provider().auto_remain(addr)
        except TgError as e:
            self.send_menu(cid, '❌ 查询失败：%s' % e)
            return
        except Exception as e:
            log('[%s] 查笔数异常：%s' % (self.note(), e))
            self.send_menu(cid, '❌ 查询失败，稍后再试。')
            return
        if info is None:
            self.send_menu(cid, '❌ 查询失败，稍后再试。')
            return
        lines = ['🔢 <b>笔数余额</b>', '',
                 '<code>%s</code>' % esc(addr), '',
                 '剩余：<b>%d 笔</b>' % info['remain'],
                 '累计买过 %d 笔，已用 %d 笔' % (info['bought'], info['used'])]
        if info['bought'] <= 0:
            lines += ['', '💡 这个地址还没买过笔数。点「🔥 笔数套餐」可以买。']
        elif info['remain'] <= 0:
            lines += ['', '💡 笔数用完了，点「🔥 笔数套餐」再买一点。']
        lines += ['',
                  '📋 每一笔的消费明细到 '
                  '<a href="https://jifei.io">jifei.io</a> 查。']
        self.send_html(cid, '\n'.join(lines))

    def start_auto_order(self, cid, address, count):
        """验证地址 + 生成笔数订单"""
        addr = (address or '').strip()
        if not is_address(addr):
            self.send_menu(cid, '❌ 这不是有效的 TRC20 地址（T 开头，34 位）。\n'
                                '重新点「🔥 笔数套餐」再来一次。')
            return
        try:
            count = int(count)
        except (TypeError, ValueError):
            self.send_menu(cid, '档位不对，重新点「🔥 笔数套餐」。')
            return
        price = self.store.auto_price(count)
        if price <= 0:
            self.send_menu(cid, '这个档还没定价，请联系管理员。')
            return
        amount = self.store.alloc_premium_amount(price)
        # 笔数订单复用会员订单那套存储：username 字段放地址，month 字段放笔数
        o = self.store.add_premium(cid, addr, count, amount,
                                   coin='TRX', pay_addr=addr, kind='auto')
        self.show_auto_order(cid, o)

    def show_auto_order(self, cid, o):
        cfg = self.store.cfg
        lines = ['🔥 <b>笔数订单已生成</b>', '',
                 '购买地址：<code>%s</code>' % esc(o['username']),
                 '笔数：<b>%s 笔</b>' % o['month'],
                 '订单号：<code>%s</code>' % o['oid'],
                 '',
                 '💰 <b>请转这个金额，一分都不能差：</b>',
                 '<code>%s</code> TRX' % fmt_amount(o['amount']),
                 '',
                 '📍 收款地址（点一下复制）：',
                 '<code>%s</code>' % esc(cfg.get('trx_own')),
                 '',
                 '⏰ 订单 %d 分钟内有效，超时要重新下单。'
                 % S.PREMIUM_EXPIRE_MIN,
                 '',
                 '⚠️ 必须<b>用上面登记的那个地址</b>转，不然认不出是你。',
                 '买好之后，你这个地址转账时会自动从笔数里扣，不用再操作。']
        self.send_html(cid, '\n'.join(lines))

    # ================= 🎁 开通会员 =================
    def premium_quote(self, month):
        """某档套餐：标价多少、客户该付多少、付什么币。

        返回 (标价USDT, 应付金额, 币种)

        ★ 填了会员专用 key（@GiftAPIBot，USDT 计价）→ **客户也付 USDT**。
          同一个 TRON 地址本来就既能收 TRX 也能收 USDT-TRC20，
          收 USDT、付 USDT，币种直接对齐 —— 不用换汇，也不担汇率风险。
          没填则和能量共用 web 域名（TRX 计价），客户就付 TRX。
        """
        price = self.store.premium_price(month)
        if price <= 0:
            return 0.0, 0.0, ''
        if (self.store.cfg.get('premium_key') or '').strip():
            return price, price, 'USDT'
        return 0.0, price, 'TRX'

    def flow_premium(self, cid):
        """列出会员套餐，让客户选一个"""
        cfg = self.store.cfg
        if not cfg.get('premium_enabled'):
            self.send_menu(cid, '🎁 会员开通暂未开放，看看别的吧。')
            return
        if not self.store.ready():
            self.send_menu(cid, '⚠️ 商户还没配置收款地址，暂时不能下单。')
            return

        rows = []
        lines = ['🎁 <b>Telegram 会员自助开通</b>', '']
        for m in S.PREMIUM_MONTHS:
            _u, pay, coin = self.premium_quote(m)
            if pay <= 0:
                continue
            lines.append('· <b>%d 个月</b> —— %s %s'
                         % (m, fmt_amount(pay), coin))
            rows.append([{'text': '%d 个月 · %s %s'
                                  % (m, fmt_amount(pay), coin),
                          'callback_data': 'pm:%d' % m}])
        if not rows:
            self.send_menu(cid, '🎁 会员套餐还没定价，请联系管理员。')
            return
        lines += ['', '要开哪一个？点下面的按钮 👇']
        self.send_html(cid, '\n'.join(lines), {'inline_keyboard': rows})

    def on_callback(self, cq):
        """客户点了套餐按钮"""
        data = cq.get('data') or ''
        cb_id = cq.get('id')
        cid = (cq.get('from') or {}).get('id')
        mid = (cq.get('message') or {}).get('message_id')

        if self.op_callback(cq, cid):
            return

        if data.startswith('pm:'):
            if not self.store.cfg.get('premium_enabled'):
                self._answer_cb(cb_id, '会员功能没开')
                return
            try:
                month = int(data[3:])
            except ValueError:
                self._answer_cb(cb_id, '参数不对')
                return
            price = self.store.premium_price(month)
            if price <= 0:
                self._answer_cb(cb_id, '这个套餐没定价')
                return
            self._answer_cb(cb_id, '%d 个月 · %s TRX'
                            % (month, fmt_amount(price)))
            self._clear_buttons(cid, mid, '🎁 开通 %d 个月会员' % month)
            self._set_sess(cid, 'puser', month=month)
            self.send_menu(cid, '要开通的是哪个账号？\n\n'
                                '把 Telegram 用户名发给我，\n'
                                '格式随意：@xxx、xxx、t.me/xxx 都行。\n\n'
                                '⚠️ 用户名一定要填对，开通后改不了。')
            return

        if data.startswith('ac:'):
            if not self.store.cfg.get('auto_enabled'):
                self._answer_cb(cb_id, '笔数套餐没开')
                return
            cnt = data[3:]
            price = self.store.auto_price(cnt)
            if price <= 0:
                self._answer_cb(cb_id, '这个档没定价')
                return
            self._answer_cb(cb_id, '%s 笔 · %s TRX' % (cnt, fmt_amount(price)))
            self._clear_buttons(cid, mid, '🔥 买 %s 笔' % cnt)
            self._set_sess(cid, 'acaddr', count=cnt)
            self.send_menu(cid, '把你要买笔数的地址发给我。\n\n'
                                '⚠️ 就是你以后转账用的那个地址 ——\n'
                                '笔数买到它头上，之后转账会自动从笔数里扣。\n\n'
                                '⚠️ 只收 <b>TRC20 网络</b>的地址：'
                                '<b>T 开头、34 位</b>。别的链（TON 等）收不了。')
            return

        self._answer_cb(cb_id, '不支持的操作')

    def _clear_buttons(self, cid, mid, text):
        """把带按钮的消息改成纯文字，免得客户重复点"""
        if not mid:
            return
        try:
            self.api.call('editMessageText', chat_id=cid, message_id=mid,
                          text=text, disable_web_page_preview=True)
        except TgError as e:
            log('[%s] 改消息失败：%s' % (self.note(), e))

    def start_premium_order(self, cid, username, month, pay_addr=''):
        """验证用户名 + 登记付款地址 + 生成订单"""
        month = int(month or 0)
        if month not in S.PREMIUM_MONTHS:
            self.send_menu(cid, '套餐不对，重新点「🎁 开通会员」。')
            return
        # 客户得先登记自己从哪个地址付款 —— 金额不带尾数了，
        # 同价订单全靠这个地址分清楚是谁的
        pay_addr = (pay_addr or '').strip()
        if not is_address(pay_addr):
            self.send_menu(cid, '❌ 这不是有效的 TRC20 地址（T 开头，34 位）。\n'
                                '重新点「🎁 开通会员」再来一次。')
            return
        price_usdt, pay, coin = self.premium_quote(month)
        if pay <= 0:
            self.send_menu(cid, '这个套餐还没定价，请联系管理员。')
            return
        u = norm_username(username)
        if not u or len(u) < 4 or ' ' in u:
            self.send_menu(cid, '❌ 用户名看着不对。\n'
                                '重新点「🎁 开通会员」再来一次。')
            return

        # 先问上游这个用户名能不能开（免费接口，不扣钱），
        # 顺便拿到昵称让客户确认没填错人
        try:
            info = self.provider().premium_query(u)
        except TgError as e:
            self.send_menu(cid, '❌ 这个用户名开不了：\n%s\n\n'
                                '检查一下有没有写错，或者稍后再试。' % e)
            return
        except Exception as e:
            log('[%s] 查用户名异常：%s' % (self.note(), e))
            self.send_menu(cid, '❌ 查询失败，稍后再试。')
            return

        amount = self.store.alloc_premium_amount(pay)
        o = self.store.add_premium(cid, u, month, amount,
                                   usdt=price_usdt, coin=coin,
                                   pay_addr=pay_addr)
        self.show_premium_order(cid, o, info)

    def show_premium_order(self, cid, o, info=None):
        cfg = self.store.cfg
        coin = (o.get('coin') or 'TRX').upper()
        nick = (info or {}).get('nickname') or ''
        lines = ['🎁 <b>订单已生成</b>', '',
                 '开通账号：<b>@%s</b>%s'
                 % (esc(o['username']), ('（%s）' % esc(nick)) if nick else ''),
                 '会员时长：<b>%d 个月</b>' % o['month'],
                 '订单号：<code>%s</code>' % o['oid'],
                 '',
                 '💰 <b>请转这个金额，一分都不能差：</b>',
                 '<code>%s</code> %s' % (fmt_amount(o['amount']), coin),
                 '',
                 '📍 收款地址（点一下复制）：',
                 '<code>%s</code>' % esc(cfg.get('trx_own')),
                 '']
        if o.get('pay_addr'):
            lines += ['⚠️ <b>必须用你登记的地址转</b>（不然我认不出是你）：',
                      '<code>%s</code>' % esc(o['pay_addr']),
                      '']
        if coin == 'USDT':
            lines += ['⚠️ 一定要选 <b>USDT-TRC20（波场链）</b>，',
                      '     选错链（ERC20/BEP20）转过来收不到，钱会丢！',
                      '']
        lines += ['⏰ 订单 %d 分钟内有效，超时要重新下单。'
                  % S.PREMIUM_EXPIRE_MIN,
                  '',
                  '💰 金额要<b>一分不差</b> —— 少一分系统都对不上。',
                  '（从交易所提币可能被扣手续费，尽量用钱包直接转）']
        self.send_html(cid, '\n'.join(lines))

    def show_my_premiums(self, cid):
        """客户查自己下过的会员订单"""
        rows = [o for o in self.store.premium_recent(50)
                if int(o.get('tg_id') or 0) == int(cid)]
        if not rows:
            self.send_menu(cid, '你还没有会员订单。')
            return
        lines = ['🎁 <b>我的会员订单</b>', '']
        for o in rows[:10]:
            lines.append('#%s  @%s · %d 个月\n   %s TRX · %s · %s'
                         % (o.get('oid'), esc(o.get('username')),
                            int(o.get('month') or 0),
                            fmt_amount(o.get('amount') or 0),
                            S.PR_LABEL.get(o.get('status'), o.get('status')),
                            S.at_str(o)))
        self.send_html(cid, '\n'.join(lines))

    def on_premium_paid(self, o):
        self.notify_group('🎁 收到会员付款 <b>%s TRX</b>\n'
                          '开通 @%s · %d 个月\n订单 %s，正在开通…'
                          % (fmt_amount(o.get('amount') or 0),
                             esc(o.get('username')), int(o.get('month') or 0),
                             o.get('oid')))

    def on_auto_result(self, o):
        """笔数订单的结果通知"""
        st = o.get('status')
        addr = esc(o.get('username'))
        cnt = o.get('month')
        if st == S.PR_DONE:
            head = ('🔥 <b>笔数到账</b>\n地址 <code>%s</code>\n'
                    '%s 笔已入账（订单 %s）' % (addr, cnt, o.get('oid')))
        elif st == S.PR_FAILED:
            head = ('⚠️⚠️ <b>笔数购买失败，需要人工处理</b>\n'
                    '地址 <code>%s</code> · %s 笔\n订单 %s\n原因：%s\n'
                    '客户已经付了 %s TRX，别忘了他'
                    % (addr, cnt, o.get('oid'), esc(o.get('note')),
                       fmt_amount(o.get('amount') or 0)))
        else:
            head = ('⏳ <b>笔数已提交，上游处理中</b>\n'
                    '地址 <code>%s</code> · %s 笔（订单 %s）'
                    % (addr, cnt, o.get('oid')))
        self.notify_group(head)
        oid = self.owner_id()
        if oid:
            self.send_html(oid, head)
        tg = int(o.get('tg_id') or 0)
        if tg and st == S.PR_DONE:
            self.send_menu(tg, '✅ %s 笔已经到账了！\n\n'
                                '地址：%s\n\n'
                                '以后你用这个地址转账，系统会自动从笔数里扣，'
                                '不用再操作。\n'
                                '发「🔢 剩余笔数」可以随时查还剩几笔。'
                           % (cnt, o.get('username')))

    def on_premium_result(self, o):
        if (o.get('kind') or 'premium') == 'auto':
            return self.on_auto_result(o)
        st = o.get('status')
        uname = esc(o.get('username'))
        mon = int(o.get('month') or 0)
        if st == S.PR_DONE:
            head = ('✅ <b>会员开通成功</b>\n'
                    '账号 @%s · %d 个月\n订单 %s'
                    % (uname, mon, o.get('oid')))
        elif st == S.PR_FAILED:
            head = ('⚠️⚠️ <b>会员开通失败，需要人工处理</b>\n'
                    '账号 @%s · %d 个月\n订单 %s\n原因：%s\n'
                    '客户已经付了 %s TRX，别忘了他'
                    % (uname, mon, o.get('oid'), esc(o.get('note')),
                       fmt_amount(o.get('amount') or 0)))
        else:
            head = ('⏳ <b>会员已提交，上游还在处理</b>\n'
                    '账号 @%s · %d 个月\n订单 %s\n'
                    '开通结果要盯一下，成了再跟客户确认'
                    % (uname, mon, o.get('oid')))
        self.notify_group(head)
        oid = self.owner_id()
        if oid:
            self.send_html(oid, head)

        # 客户那边：成功和「处理中」都告诉他一声；失败先不惊动他，等运营者处理
        tg = int(o.get('tg_id') or 0)
        if not tg:
            return
        if st == S.PR_DONE:
            self.send_menu(tg, '✅ 你的会员已经开通好了！\n\n'
                                '账号：@%s\n时长：%d 个月\n\n'
                                '去 Telegram 设置里就能看到 Premium 标识了。'
                           % (o.get('username'), mon))
        elif st == S.PR_PAID:
            self.send_menu(tg, '⏳ 你的会员申请已经提交，正在处理中。\n'
                                '开通好了我会通知你。')

    # ================= 通知（给运营者/群） =================
    def notify_group(self, text):
        cfg = self.store.cfg
        gid = (cfg.get('group_ids') or '').strip()
        if not gid:
            return
        for one in [x.strip() for x in gid.replace('，', ',').split(',') if x.strip()]:
            try:
                gid_i = int(one)
            except ValueError:
                continue
            try:
                self.api.call('sendMessage', chat_id=gid_i, text=text,
                              parse_mode='HTML', disable_web_page_preview=True)
            except TgError as e:
                log('[%s] 群通知失败（%s）：%s' % (self.note(), one, e))

    def on_payment_seen(self, p):
        self.notify_group('💰 收到 <b>%s TRX</b>\n来自 <code>%s</code>\n折合 %d 个单位，'
                          '正在发能量…'
                          % (fmt_amount(p['amount']), esc(p['from']), p['units']))

    def on_payment_done(self, p):
        self.notify_group('✅ <b>已发货</b>\n'
                          '地址 <code>%s</code>\n'
                          '收到 %s TRX → 发放 %s 能量\n'
                          'txid <code>%s</code>'
                          % (esc(p['from']), fmt_amount(p['amount']),
                             '{:,}'.format(int(p['energy'])), esc(p['txid'])))

    def on_payment_failed(self, p, reason):
        """发货失败时告警。

        ★ 只发给运营者（通知群 + 绑定者私聊），**不会发给客户**。
        ★ 同一笔单子 30 分钟内只提醒一次：自动补发每 5 分钟试一次，
          每次失败都推一条的话能把人刷疯，而原因通常压根没变。
        """
        now = int(time.time())
        quiet = (now - int(p.get('last_notice') or 0)) < self.FAIL_NOTICE_GAP
        p['last_notice'] = now
        self.store.save()
        if quiet:
            return

        self.notify_group('⚠️⚠️ <b>发货失败，需要人工处理</b>\n'
                          '地址 <code>%s</code>\n'
                          '收到 %s TRX（txid <code>%s</code>）\n'
                          '原因：%s'
                          % (esc(p['from']), fmt_amount(p['amount']),
                             esc(p['txid']), esc(reason)))
        oid = self.owner_id()
        if oid:
            self.send_html(oid, '⚠️ 有笔收款发货失败，客户的钱已经收到了，'
                                '请尽快手动处理：\n\n%s\n客户地址：<code>%s</code>\n'
                                '（同一笔单子半小时内只提醒一次；'
                                '余额不足的话充了值会自动补发）'
                           % (esc(reason), esc(p['from'])))

    def on_payment_skipped(self, p):
        self.notify_group('ℹ️ 收到 <b>%s TRX</b>（来自 <code>%s</code>），'
                          '不够一个单位，已忽略'
                          % (fmt_amount(p['amount']), esc(p['from'])))

    # ================= 运营指令 =================
    def cmd_status(self, cid):
        cfg = self.store.cfg
        st = self.pay.status()
        prov = self.provider()
        t = self.store.stats(1)
        lines = ['🏪 <b>收款状态</b>', '',
                 '总开关：%s' % ('✅ 开' if cfg.get('enabled', True) else '❌ 关'),
                 '收款地址：%s' % (('<code>%s</code>' % esc(st['pay_address']))
                                   if st['pay_address'] else '❌ 未配置'),
                 '单价：%s TRX = %s 能量' % (fmt_amount(cfg.get('price') or 0),
                                          '{:,}'.format(int(cfg.get('energy') or 0))),
                 '最高倍数：%s' % cfg.get('max'),
                 '上游：%s %s' % (prov.label, '✅' if prov.configured() else '❌'),
                 '扫描基线：%s' % ('已建立' if st['baseline'] else '未建立'),
                 '',
                 '今日：%d 笔 / %s TRX / %s 能量（失败 %d 笔）'
                 % (t['count'], fmt_amount(t['trx']),
                    '{:,}'.format(t['energy']), t['failed'])]
        self.send_html(cid, '\n'.join(lines))

    def cmd_payments(self, cid):
        rows = self.store.recent(10)
        if not rows:
            self.send_menu(cid, '还没有流水。')
            return
        lines = ['📋 <b>最近 10 笔</b>', '']
        for p in rows:
            lines.append('%s  +%s TRX → %s 能量  %s\n   <code>%s</code>'
                         % (S.at_str(p), fmt_amount(p.get('amount') or 0),
                            '{:,}'.format(int(p.get('energy') or 0)),
                            S.pay_label(p), esc(p.get('from') or '')))
        self.send_html(cid, '\n'.join(lines))

    def cmd_stat(self, cid):
        t = self.store.stats(1)
        a = self.store.stats(7)
        need = self.store.need_attention()
        lines = ['📊 <b>汇总</b>', '',
                 '<b>今日</b>：%d 笔 / %s TRX / %s 能量'
                 % (t['count'], fmt_amount(t['trx']), '{:,}'.format(t['energy'])),
                 '<b>近 7 天</b>：%d 笔 / %s TRX / %s 能量'
                 % (a['count'], fmt_amount(a['trx']), '{:,}'.format(a['energy'])),
                 '发货失败：%d 笔' % t['failed'],
                 '']
        if need:
            lines.append('⚠️ 有 %d 笔需要人工处理，发 /流水 看详情' % len(need))
        else:
            lines.append('✅ 没有需要人工处理的')
        self.send_html(cid, '\n'.join(lines))

    def cmd_balance(self, cid):
        """查上游余额 —— 余额见底就一单都发不出去，这是最要紧的运营指标"""
        cfg = self.store.cfg
        prov = self.provider()
        lines = ['💳 <b>上游账户</b>', '',
                 '供应商：%s' % esc(prov.label)]
        if not prov.configured():
            lines.append('❌ 还没填 API Key')
            self.send_html(cid, '\n'.join(lines))
            return
        try:
            bal = prov.balance()
        except Exception as e:
            bal = None
            lines.append('查询失败：%s' % esc(str(e)))
        if bal is None:
            if not any('查询失败' in x for x in lines):
                lines.append('没读到余额，检查 API Key 或网络')
        else:
            lines.append('余额：<b>%s TRX</b>' % fmt_amount(bal))
            # 按当前配置估算还能发几单
            need = prov.quote(int(cfg.get('energy') or 0), 1)[0]
            if need > 0:
                can = int(bal / need)
                lines.append('每单成本约 %s TRX（%s 能量 / 1 小时）'
                             % (fmt_amount(need),
                                '{:,}'.format(int(cfg.get('energy') or 0))))
                if can <= 0:
                    lines.append('')
                    lines.append('🚨 <b>余额不够发一单了，赶紧充值！</b>')
                elif can <= 3:
                    lines.append('')
                    lines.append('⚠️ 只够再发 %d 单，尽快充值' % can)
                else:
                    lines.append('大约还能发 %d 单' % can)
        self.send_html(cid, '\n'.join(lines))
