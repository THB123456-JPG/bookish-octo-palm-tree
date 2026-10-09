# -*- coding: utf-8 -*-
"""TRC20 地址监听：查询卡片 + 地址簿 + 扫链提醒

私聊发一个 TRON 地址 → 回一张卡片（余额 + 最近 20 笔 USDT 交易 +
加入监听/刷新查询/链上详情/地址簿/首页），点「加入监听」之后
链上一有动静就推给他。

★ 面板和交互**照搬服务器上那套**（`features/shop/watch.py`），
  连文案和按钮排布都尽量一致 —— 你在服务器上用的是那套，
  两边不一样的话你自己都要重新适应。

★★★ 四条铁律，动这个文件之前先读一遍：

  1. **链上调用绝不能在收消息的主线程里做。**
     收消息和 on_tick 是同一个线程（core 的单线程循环）。
     一次查询要打 2～3 个接口，扫链要打 2×地址数 个 ——
     放主线程里就是「群里记账跟着卡」。这里所有联网动作
     都由 `send_card()` / `scan()` 在**后台线程**里跑。

  2. **查不到就说查不到，绝不显示 0。**
     卡片上那句「余额暂不可用，不代表余额为零」不是客套话 ——
     客户看见 0 会以为钱没了。接口坏了、超时了，一律照实说。

  3. **只提醒「订阅之后」的已确认转账。**
     靠 `added`（订阅那一刻的时间戳）卡住，不然一加监听就把
     几百条历史全推一遍，客户手机当场被淹没。

  4. **sqlite 只在**每次操作**时开一个连接、用完立刻关**
     （`_db()`，照抄 archive.py）。连接不能跨线程共用。

按钮的 callback_data 前缀是 `tw:`（runner.on_callback 的白名单里加了它）。
地址 34 字符，`tw:stats:<addr>` = 43 字节，没超 callback_data 的 64 上限。
"""
import contextlib
import os
import sqlite3
import threading
import time
from html import escape

import core
import customer_config as CC
from core import log
from . import tron_chain as tc

PREFIX = 'tw:'
CARD_TX = 20              # 卡片上列几笔（跟服务器那套一致）
SCAN_INTERVAL = 30        # 扫链间隔（秒）
SCAN_GAP = 0.6            # 两个地址之间歇一下，别把接口打急
MAX_WATCHES = 10          # 每人最多监听几个地址
EVENT_KEEP_MS = 2 * 86400 * 1000    # 转账明细保留两天
INPUT_TTL = 600           # 等输入（备注/最低金额）多久作废
# ★ 一条提醒最多重试几次。超过就认了（多半是被屏蔽/删号），
#   但**不标「已通知」也没关系** —— 事件还在 watch_events 里，
#   「账单统计」照样看得到这笔，不会凭空消失。
ALERT_MAX_TRIES = 6
# ★ 一把 Key 被限流之后冷多久。实测 TronGrid 限流约 6 秒恢复，
#   这里给 60 秒更保险 —— 反正多把 Key 会顶上，没必要抢那几秒。
KEY_COOLDOWN = 60
MAX_KEYS = 5                      # 最多存几把（够用了，多了也管不过来）
KEY_RETRY = 3                     # 一次查询最多换几把 Key 试
COINS = ('USDT', 'TRX')
# ★ 能改的字段白名单 —— set_field 要往 SQL 里拼列名，不白名单就是注入
FIELDS = ('note', 'coins', 'direction', 'minimum', 'detailed')

TXT_HOME = '🏠 首页'
TXT_BOOK = '📒 地址簿'
# ★ 卡片里两个查询线程最多等多久。链上接口自己带 (5,15) 超时，
#   再叠上重试最长也就 20 秒左右；这里是**最后一道保险** ——
#   万一底层卡住，卡片也得回来（回来时报「没查到」而不是永远转圈）。
CARD_JOIN = 45


def _reason(e):
    """把异常翻译成给用户看的一句话。认得出类型就说类型，认不出就照原话说"""
    why = tc._why(e) if isinstance(e, tc.ChainError) else None
    return why or (str(e)[:60] or '未知原因')


def _is_rate(e):
    """是不是被限流了（限流要把「去申请 Key」的引导顶出来）"""
    return isinstance(e, tc.ChainError) and e.kind == 'rate'


@contextlib.contextmanager
def _db(path, timeout=10):
    """开一个 sqlite 连接，用完**一定关掉**

    ★ 别写成 `with sqlite3.connect(...) as db:` —— 那个只管事务、
      **不关连接**，Windows 上文件会一直被占着（archive.py 踩过）
    """
    conn = sqlite3.connect(path, timeout=timeout)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def tx_url(txid):
    return 'https://tronscan.org/#/transaction/' + (txid or '')


class TronWatch:
    """一个机器人一个。runner 在 __init__ 里建，挂成 self.tronw"""

    def __init__(self, runner):
        self.r = runner
        self.path = os.path.join(core.bot_data_dir(runner.bot),
                                 '%s.watches.sqlite3' % runner.bid)
        self._lock = threading.Lock()
        self._busy = False            # 扫链线程在跑（防 on_tick 反复起线程）
        self._next_scan = 0.0
        self._await = {}              # uid -> 等输入的状态
        self._alert_fail = {}         # txid -> 提醒连续失败了几次
        self._key_i = -1              # 多把 Key 轮询到第几把
        self._key_cool = {}           # key -> 冷却到什么时候（被限流了就换下一把）
        self._init_db()

    def _init_db(self):
        with _db(self.path) as db:
            db.execute(
                'CREATE TABLE IF NOT EXISTS watches('
                ' buyer INTEGER, address TEXT, since INTEGER, added INTEGER,'
                ' note TEXT NOT NULL DEFAULT "",'
                ' coins TEXT NOT NULL DEFAULT "USDT,TRX",'
                ' direction TEXT NOT NULL DEFAULT "both",'
                ' minimum INTEGER NOT NULL DEFAULT 0,'
                ' detailed INTEGER NOT NULL DEFAULT 0,'
                ' synced INTEGER NOT NULL DEFAULT 0,'
                ' error TEXT NOT NULL DEFAULT "",'
                ' revision TEXT NOT NULL DEFAULT "",'
                ' PRIMARY KEY(buyer, address))')
            db.execute(
                'CREATE TABLE IF NOT EXISTS watch_events('
                ' buyer INTEGER, address TEXT, txid TEXT, timestamp INTEGER,'
                ' coin TEXT, direction TEXT, amount INTEGER,'
                ' notified INTEGER NOT NULL DEFAULT 0,'
                ' PRIMARY KEY(buyer, address, txid))')

    # ================= 存储 =================
    # ★★ 链上 Key 支持**多把轮询**（2026-09-29 加）
    #
    #    为什么要多把：公共接口的免费额度很低，一个 Key 背靠背发几个请求就 429
    #    （实测第 2 个就限流）。多存几把就能轮着用，一把被限流就换下一把 ——
    #    比等 6 秒恢复快得多，也不会「一个地址都查不出来」。
    #
    #    ★★★ 密钥**只存在 `data/<机器人id>.json` 里，绝不写进代码**。
    #    代码是要传到**公开仓库**的；写死在代码里就等于把密钥公开，
    #    脱敏脚本会把副本里的替换成占位符（别人克隆下来是坏的），
    #    而真密钥会留在公开仓库的历史里 —— 这就是 `totp_secret` 刚出事的方式。
    #    `data/` 目录**不推送、不进日志、不进归档**，所以放这儿是安全的。
    def custom_keys(self):
        """当前配了哪几把 Key（去重、去空）。旧的单把 `tron_key` 也认"""
        out = []
        raw = self.r.data.get('tron_keys')
        if isinstance(raw, list):
            out = [str(x).strip() for x in raw]
        single = str(self.r.data.get('tron_key') or '').strip()
        if single:
            out.append(single)
        seen, uniq = set(), []
        for k in out:
            if k and k not in seen:
                seen.add(k)
                uniq.append(k)
        return uniq

    def keys(self):
        own = self.custom_keys()
        shared = self.r.mgr.cfg.get('tron_api_keys') or []
        return own or list(dict.fromkeys(str(k).strip() for k in shared if str(k).strip()))

    def _key_cooling(self, key):
        """这把 Key 现在是不是在「刚被限流，先别用」的冷却期"""
        until = self._key_cool.get(key, 0)
        return until > time.time()

    def next_key(self):
        """★ 轮询取一把 Key：跳过正在冷却的，全在冷却就挑最早恢复的那把。

        ★ 线程安全：查询线程和扫链线程会同时要 Key，所以用锁护住那把计数器。
        """
        keys = self.keys()
        if not keys:
            return ''
        with self._lock:
            self._key_i = (self._key_i + 1) % len(keys)
            start = self._key_i
        for off in range(len(keys)):
            k = keys[(start + off) % len(keys)]
            if not self._key_cooling(k):
                return k
        # 全在冷却：返回最早能用的那把（总比不发请求强）
        return min(keys, key=lambda k: self._key_cool.get(k, 0))

    def cool_key(self, key, seconds=KEY_COOLDOWN):
        """★ 这把 Key 刚被限流 → 冷一会儿，期间的请求自动用别的 Key"""
        if key:
            self._key_cool[key] = time.time() + seconds

    def api_key(self):
        """兼容旧调用：拿一把现在能用的 Key"""
        return self.next_key()

    def call_with_key(self, fn):
        """用「当前能用的那把 Key」调 `fn(key)`；**被限流就换下一把再试**。

        ★ 为什么要换 Key 重试：限流是**按 Key 算的**。一把被限流了，
          换另一把立刻就能查 —— 让用户干等 6 秒纯属浪费。
        ★ 只对「限流」换 Key：Key 无效、网络断了，换几把都一样，白折腾。
        """
        keys = self.keys()
        rounds = min(len(keys), KEY_RETRY) if keys else 1
        last = None
        for _ in range(max(1, rounds)):
            key = self.next_key()
            try:
                return fn(key)
            except tc.ChainError as e:
                if e.kind != 'rate' or len(keys) < 2:
                    raise
                self.cool_key(key)      # 把这把晾一会儿，下一轮自动用别的
                last = e
        raise last

    def all(self):
        with _db(self.path) as db:
            return [dict(x) for x in db.execute('SELECT * FROM watches')]

    def mine(self, buyer):
        with _db(self.path) as db:
            return [dict(x) for x in db.execute(
                'SELECT * FROM watches WHERE buyer=? ORDER BY added',
                (buyer,))]

    def one(self, buyer, address):
        with _db(self.path) as db:
            row = db.execute('SELECT * FROM watches WHERE buyer=? AND address=?',
                             (buyer, address)).fetchone()
            return dict(row) if row else None

    def add(self, buyer, address):
        """加监听。重复加 / 超上限都抛 ValueError（文案跟服务器那套一致）"""
        now = int(time.time() * 1000)
        with _db(self.path) as db:
            # ★ 先锁住再查：不然两个人同时点「加入监听」会一起通过检查，
            #   最后一个地址被挤掉或超出上限
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM watches WHERE buyer=? AND address=?',
                          (buyer, address)).fetchone():
                raise ValueError('此地址已在你的地址簿中，不会重复添加。')
            count = db.execute('SELECT COUNT(*) FROM watches WHERE buyer=?',
                               (buyer,)).fetchone()[0]
            if count >= MAX_WATCHES:
                raise ValueError('每人最多监听 %d 个地址。' % MAX_WATCHES)
            db.execute(
                'INSERT INTO watches(buyer,address,since,added,revision)'
                ' VALUES(?,?,?,?,?)',
                (buyer, address, now, now, os.urandom(8).hex()))

    def remove(self, buyer, address):
        with _db(self.path) as db:
            db.execute('DELETE FROM watches WHERE buyer=? AND address=?',
                       (buyer, address))
            db.execute('DELETE FROM watch_events WHERE buyer=? AND address=?',
                       (buyer, address))

    def set_field(self, buyer, address, field, value):
        if field not in FIELDS:          # ★ 列名要拼进 SQL，必须白名单
            raise ValueError('不能改这个字段')
        with _db(self.path) as db:
            db.execute('UPDATE watches SET %s=? WHERE buyer=? AND address=?'
                       % field, (value, buyer, address))

    def seen(self, buyer, address, txid):
        with _db(self.path) as db:
            row = db.execute(
                'SELECT notified FROM watch_events'
                ' WHERE buyer=? AND address=? AND txid=?',
                (buyer, address, txid)).fetchone()
            return bool(row and row['notified'])

    def record(self, buyer, address, tx, direction):
        with _db(self.path) as db:
            db.execute(
                'INSERT OR IGNORE INTO watch_events'
                '(buyer,address,txid,timestamp,coin,direction,amount)'
                ' VALUES(?,?,?,?,?,?,?)',
                (buyer, address, tx['id'], tx['timestamp'], tx['coin'],
                 direction, tx['amount']))

    def mark_notified(self, buyer, address, txid):
        with _db(self.path) as db:
            db.execute('UPDATE watch_events SET notified=1'
                       ' WHERE buyer=? AND address=? AND txid=?',
                       (buyer, address, txid))

    def totals(self, buyer, address, start, end):
        with _db(self.path) as db:
            rows = db.execute(
                'SELECT coin,direction,SUM(amount) AS s FROM watch_events'
                ' WHERE buyer=? AND address=? AND timestamp>=? AND timestamp<=?'
                ' GROUP BY coin,direction',
                (buyer, address, start, end)).fetchall()
        return {(r['coin'], r['direction']): r['s'] for r in rows}

    # ================= 查询卡片 =================
    def card(self, buyer, address, *, include_hint=True):
        """查一次，返回 (文本, 键盘)。★ 要联网 —— 调用方负责放进线程

        ★★ 余额和交易记录**并行查**（原来是先查完余额再查记录，白白多等一轮）。
          两个接口互不依赖，串起来等就是纯浪费用户的等待时间。
        ★ 一个失败**不影响**另一个：能显示的先显示，失败的那块单独说明原因。
        """
        watch = self.one(buyer, address)
        title = (watch or {}).get('note') or '地址查询'
        box = {}
        features = CC.settings(self.r.bot)['features']
        if buyer > 0:
            features.update(tron_balance=True, tron_details=True, balance_trx=True, balance_usdt=True)

        def _balance():
            try:
                # ★ 走 call_with_key：一把 Key 被限流会自动换下一把
                rows = self.call_with_key(
                    lambda k: tc.chain_get(address, {'only_confirmed': 'true'}, k))
                box['bal'], box['account'] = tc.balances(rows, address)
            except Exception as e:                      # noqa: BLE001
                box['bal_err'] = e

        def _records():
            try:
                box['tx'] = self.call_with_key(
                    lambda k: tc.recent(address, 'USDT', CARD_TX, k))
            except Exception as e:                      # noqa: BLE001
                box['tx_err'] = e

        def _count():
            try:
                import requests
                response = requests.get('https://apilist.tronscanapi.com/api/accountv2',
                                        params={'address': address}, timeout=(5, 15))
                response.raise_for_status()
                value = response.json().get('totalTransactionCount')
                if type(value) is not int or value < 0:
                    raise ValueError('invalid count')
                box['count'] = value
            except Exception:
                box['count'] = None

        # ★ 两个都放后台线程，然后**一起等** —— 总耗时 ≈ 慢的那个，不是两个相加
        queries = []
        if features['tron_balance']:
            queries.append(_balance)
        if features['tron_details']:
            queries.append(_records)
        if features['tron_count']:
            queries.append(_count)
        workers = [threading.Thread(target=f, daemon=True, name='troncard') for f in queries]
        for w in workers:
            w.start()
        for w in workers:
            # ★ join 要带超时：万一某个请求卡死（底层没超时），
            #   也不能把这张卡片永远挂着不回来
            w.join(CARD_JOIN)

        account = box.get('account') or {}
        records = box.get('tx') or []
        rate_limited = False

        if not features['tron_balance']:
            balance_text = ''
        elif 'bal' in box:
            balance_text = (box['bal'].replace('TRX：', '💎 TRX 余额：')
                                    .replace('USDT：', '💰 USDT 余额：'))
            balance_text = '\n'.join(line for line in balance_text.splitlines()
                                     if not ('USDT' in line and not features['balance_usdt'])
                                     and not ('TRX' in line and not features['balance_trx']))
        else:
            e = box.get('bal_err')
            # ★★ 查不到就明说查不到，**绝不显示 0** —— 客户看见 0 会以为钱没了
            balance_text = ('⚠️ 余额暂时查不到（%s）。'
                            '这**不代表余额为零**，稍后再试。' % _reason(e))
            log('[%s] 查余额失败 %s：%s' % (self.r.note(), address[:10], e))
            rate_limited = rate_limited or _is_rate(e)

        entries = ['%s | %s | %s USDT' % (
            tc.when(tx['timestamp'])[5:],
            '转入' if tx['to'] == address else '转出',
            tc.amount(tx['amount'])) for tx in records[:CARD_TX]]
        tx_note = ''
        if not entries and features['tron_details']:
            if 'tx_err' in box:
                e = box['tx_err']
                # ★ 分三种情况说清楚：网络问题 / 没有记录 / 没查到
                tx_note = ('⚠️ 交易记录没查到（%s），不是「没有交易」。'
                           % _reason(e))
                rate_limited = rate_limited or _is_rate(e)
                log('[%s] 查转账失败 %s：%s' % (self.r.note(), address[:10], e))
            else:
                tx_note = '当前返回范围内暂无 USDT 记录。'

        operations = [tx['timestamp'] for tx in records]
        if type(account.get('latest_opration_time')) is int:
            operations.append(account['latest_opration_time'])
        lines = [
            '<blockquote><b>%s</b>\n<code>%s</code></blockquote>'
            % (escape(title), address),
        ]
        if features['tron_details']:
            lines.append('最近 %d 笔 USDT 交易（已确认）：' % CARD_TX)
        if entries:
            lines.append('<pre>' + escape('\n'.join(entries)) + '</pre>')
        if tx_note:
            lines.append(escape(tx_note))
        if balance_text:
            lines.append(escape(balance_text))
        if features['tron_count']:
            count = box.get('count')
            lines.append('链上累计交易次数：' + (str(count) if count is not None else '暂未获取，不代表0'))
        if features['tron_balance'] or features['tron_details']:
            lines += [
            '⏰ 创建时间：' + tc.when(account.get('create_time')),
            '🕒 最近操作：' + tc.when(max(operations, default=0)),
            '时间：UTC+8 · 仅 TRX/USDT',
        ]
        # ★ 没配 Key 就顺势把「怎么配」讲清楚（尤其刚被限流过的时候）
        if buyer > 0 and include_hint:
            lines += self.key_hint(rate_limited)
        return '\n'.join(lines), self.card_kb(address, watch) if buyer > 0 else None

    def key_hint(self, urgent=False):
        """未绑定独立查询 Key 时显示申请和小程序绑定提示。"""
        if self.custom_keys():
            return []
        return ['\n<b>💡 温馨提示</b>\n'
                '为保证查询速度及稳定性，可以自行申请免费的 API Key，'
                '在 TG 小程序「地址查询与密钥」中单独绑定使用。\n'
                '机器人拥有者可在配置中心绑定，申请入口：'
                '<a href="https://trongrid.io/">https://trongrid.io/</a>']

    def card_kb(self, address, watch):
        head = ([{'text': '⚙️ 管理此地址', 'callback_data': PREFIX + 'd:' + address}]
                if watch else
                [{'text': '📡 加入监听', 'callback_data': PREFIX + 'add:' + address}])
        return {'inline_keyboard': [
            head,
            [{'text': '🔄 刷新查询', 'callback_data': PREFIX + 'q:' + address},
             {'text': '📊 账单统计', 'callback_data': PREFIX + 'stats:' + address}],
            [{'text': TXT_BOOK, 'callback_data': PREFIX + 'book'},
             {'text': TXT_HOME, 'callback_data': PREFIX + 'home'}],
        ]}

    def send_card(self, chat_id, address, mid=None):
        """★ 自己起线程去查 —— 链上调用绝不能卡在收消息的主线程里

        ★★ 先**立刻**回一句「正在查询…」，查完再把它**改成**卡片。
          为什么非要多这一下：查一次要打两个接口，慢的时候好几秒，
          用户发完地址一点动静都没有，会以为机器人死了，然后狂发。
          ★ 用「改消息」而不是「再发一条」：免得聊天记录里堆一句废话。
        """
        address = (address or '').strip()

        def work():
            if self.r.expired():
                return
            placeholder = None
            if not mid:
                # ★ send_html 返回的是 TG 的整个结果，要取 message_id 才能改它
                res = self.r.send_html(
                    chat_id, '⏳ 正在查询余额和最近交易，请稍候…')
                placeholder = (res or {}).get('message_id')
            try:
                text, kb = self.card(chat_id, address)
            except Exception as e:
                log('[%s] 查询出错 %s：%s' % (self.r.note(), address[:10], e))
                if placeholder:
                    self.r.edit_html(chat_id, placeholder, '❌ 查询失败，请稍后再试。')
                elif mid:
                    self.r.edit_html(chat_id, mid, '❌ 查询失败，请稍后再试。')
                else:
                    self.r.send_html(chat_id, '❌ 查询失败，请稍后再试。')
                return
            target = mid or placeholder
            if self.r.expired() or (chat_id < 0 and not CC.feature(self.r.bot, 'tron_verify')):
                return
            if target:
                self.r.edit_html(chat_id, target, text, kb=kb)
            else:
                self.r.send_html(chat_id, text, kb=kb)

        threading.Thread(target=work, daemon=True, name='tronq').start()

    # ★★ 原来这里有个 `cooling()`「同一个人 3 秒内不许再查」的拦截，
    #    2026-09-29 按用户要求**整个删掉了**：
    #      「我不怕额度刷光，你让他别提示查询太频繁了就行，
    #        我要发一个它查询一个，发一个查询一个，
    #        额度怕刷光我多申请几个 api 的 key 就行了」
    #    → 额度问题改用**多把 Key 轮询 + 一把被限流就换下一把**来解决
    #      （见 keys() / next_key() / cool_key()），
    #      不再靠「少让用户查几次」来省额度。
    #    ★ 删干净了，没留半截死代码：常量、状态、调用点一起没了。

    # ================= 地址簿 / 详情 / 统计 =================
    def book_view(self, buyer):
        rows = self.mine(buyer)
        if not rows:
            return ('📒 <b>我的地址簿</b>\n\n'
                    '还没有监听任何地址。\n\n'
                    '把 TRC20 地址发给我就能查，查完点「📡 加入监听」。\n'
                    '（每人最多 %d 个）' % MAX_WATCHES,
                    {'inline_keyboard': [[{'text': TXT_HOME,
                                           'callback_data': PREFIX + 'home'}]]})
        lines = ['📒 <b>我的地址簿</b> · %d/%d 个' % (len(rows), MAX_WATCHES), '']
        btns = []
        for i, w in enumerate(rows, 1):
            label = w['note'] or (w['address'][:8] + '…')
            lines.append('%d. %s' % (i, escape(label)))
            lines.append('<code>%s</code>' % w['address'])
            if w['error']:
                lines.append('⚠️ 上次同步失败，会重试')
            lines.append('')
            btns.append([{'text': '%d. %s' % (i, label),
                          'callback_data': PREFIX + 'd:' + w['address']}])
        btns.append([{'text': TXT_HOME, 'callback_data': PREFIX + 'home'}])
        return '\n'.join(lines), {'inline_keyboard': btns}

    def detail_view(self, w):
        state = ('尚未完成首次同步' if not w['synced']
                 else '最近同步：' + tc.when(w['synced']))
        if w['error']:
            state += '\n本次同步失败，会重试；余额和统计不代表零。'
        lines = [
            '<b>%s</b>' % escape(w['note'] or '未备注'),
            '<code>%s</code>' % w['address'], '',
            '币种：' + w['coins'],
            '方向：' + {'both': '转入和转出', 'in': '仅转入',
                        'out': '仅转出'}.get(w['direction'], w['direction']),
            '最低金额：' + tc.amount(w['minimum']),
            '格式：' + ('详细' if w['detailed'] else '简洁'),
            state,
        ]
        a = w['address']
        return '\n'.join(lines), {'inline_keyboard': [
            [{'text': '查询余额/记录', 'callback_data': PREFIX + 'q:' + a},
             {'text': '账单统计', 'callback_data': PREFIX + 'stats:' + a}],
            [{'text': '设置备注', 'callback_data': PREFIX + 'note:' + a},
             {'text': '通知设置', 'callback_data': PREFIX + 'prefs:' + a}],
            [{'text': '停止监听', 'callback_data': PREFIX + 'del:' + a}],
            [{'text': TXT_BOOK, 'callback_data': PREFIX + 'book'},
             {'text': TXT_HOME, 'callback_data': PREFIX + 'home'}],
        ]}

    def prefs_view(self, w):
        a = w['address']
        return ('点一下切换（统计照常记录，只影响推不推给你）',
                {'inline_keyboard': [
                    [{'text': '币种：' + w['coins'],
                      'callback_data': PREFIX + 'coins:' + a}],
                    [{'text': '方向：' + {'both': '全部', 'in': '转入',
                                          'out': '转出'}.get(w['direction'],
                                                             w['direction']),
                      'callback_data': PREFIX + 'dir:' + a}],
                    [{'text': '格式：' + ('详细' if w['detailed'] else '简洁'),
                      'callback_data': PREFIX + 'fmt:' + a}],
                    [{'text': '最低金额：' + tc.amount(w['minimum']),
                      'callback_data': PREFIX + 'min:' + a}],
                    [{'text': '返回地址', 'callback_data': PREFIX + 'd:' + a}],
                ]})

    def stats_view(self, w):
        now = int(time.time() * 1000)
        # 北京时间今天 00:00 的毫秒
        day = time.time() + 8 * 3600
        today = int((day - day % 86400 - 8 * 3600) * 1000)
        lines = ['📊 <b>收支统计</b> · UTC+8',
                 escape(w['note'] or w['address'])]
        if not w['synced']:
            lines.append('')
            lines.append('尚未完成首次同步，暂无统计。')
        else:
            lines.append('同步至：' + tc.when(w['synced']))
            if w['error'] or now - w['synced'] > 2 * SCAN_INTERVAL * 1000:
                lines.append('⚠️ 数据同步延迟或失败，以下可能不完整。')
            for label, start in (('近 24 小时', now - 86400000), ('今日', today)):
                lines.append('')
                lines.append('<b>%s</b>' % label)
                totals = self.totals(w['buyer'], w['address'],
                                     max(start, w['added']), w['synced'])
                for coin in COINS:
                    lines.append('%s 转入 %s / 转出 %s' % (
                        coin, tc.amount(totals.get((coin, 'in'), 0)),
                        tc.amount(totals.get((coin, 'out'), 0))))
        return '\n'.join(lines), {'inline_keyboard': [
            [{'text': '返回地址', 'callback_data': PREFIX + 'd:' + w['address']}],
            [{'text': TXT_BOOK, 'callback_data': PREFIX + 'book'},
             {'text': TXT_HOME, 'callback_data': PREFIX + 'home'}],
        ]}

    # ================= 等输入（备注 / 最低金额）=================
    def wait_input(self, uid, flow, address):
        self._await[uid] = {'flow': flow, 'address': address, 'at': time.time()}

    def take_input(self, uid, text):
        """这个人正在等输入吗？是就吃掉这条消息并返回 True。

        ★★ 必须在记账/算式**之前**判断 —— 不然用户输入的「备注」
          会被当成记账或算式吃掉（比如备注写「+100」）。
        ★ 过期的**不吃**：直接放行走正常流程，别把人家的记账吞了。
        """
        d = self._await.get(uid)
        if not d:
            return False
        del self._await[uid]
        if time.time() - d['at'] > INPUT_TTL:
            return False
        w = self.one(uid, d['address'])
        if not w:
            self.r.send_html(uid, '这个地址已经不在你的地址簿里了。')
            return True
        text = (text or '').strip()
        try:
            if d['flow'] == 'note':
                value = '' if text == '清空' else text
                if text != '清空' and (not value or len(value) > 24 or '\n' in value):
                    raise ValueError('备注要 1～24 个字、不能换行；发「清空」可以去掉备注。')
                self.set_field(uid, w['address'], 'note', value)
            else:
                if text == '0':
                    value = 0
                else:
                    value = tc.units(text)
                self.set_field(uid, w['address'], 'minimum', value)
        except ValueError as e:
            self.r.send_html(uid, escape(str(e)))
            return True
        text, kb = self.detail_view(self.one(uid, w['address']))
        self.r.send_html(uid, text, kb=kb)
        return True

    # ================= 按钮 =================
    def on_callback(self, chat_id, uid, mid, data):
        """处理 tw: 的按钮。返回 True = 我处理了"""
        verb, _, address = (data or '')[len(PREFIX):].partition(':')

        if verb == 'home':
            self.r.send_html(chat_id, self.r.help_text(False))
            return True
        if verb == 'book':
            text, kb = self.book_view(uid)
            self.r.edit_html(chat_id, mid, text, kb=kb)
            return True

        # 下面这些都要带地址
        if not tc.address_valid(address):
            self.r.send_html(chat_id, '地址按钮无效，请重新打开地址簿。')
            return True
        w = self.one(uid, address)

        if verb == 'q':
            # 刷新查询（就地改掉原卡片）
            # ★ 原来这里有个冷却拦截，已按用户要求去掉 —— 点一下就查一下。
            #   额度靠多把 Key 轮询顶上，不靠拦用户。
            self.send_card(chat_id, address, mid=mid)
            return True

        if verb == 'add':
            try:
                self.add(uid, address)
            except ValueError as e:
                self.r.send_html(chat_id, escape(str(e)))
                return True
            self.r.send_html(chat_id, '✅ 已加入监听，有转入转出就通知你。')
            text, kb = self.detail_view(self.one(uid, address))
            self.r.send_html(chat_id, text, kb=kb)
            return True

        if not w:
            # ★ 卡片上的「账单统计」在没加监听时也会点到 —— 说清楚该干嘛，
            #   别甩一句「不在地址簿里」让人莫名其妙
            self.r.send_html(chat_id, '先点上面的「📡 加入监听」，'
                                      '才能看这个地址的账单统计。')
            return True

        if verb == 'd':
            text, kb = self.detail_view(w)
            self.r.edit_html(chat_id, mid, text, kb=kb)
            return True
        if verb == 'stats':
            text, kb = self.stats_view(w)
            self.r.edit_html(chat_id, mid, text, kb=kb)
            return True
        if verb == 'prefs':
            text, kb = self.prefs_view(w)
            self.r.edit_html(chat_id, mid, text, kb=kb)
            return True
        if verb in ('note', 'min'):
            self.wait_input(uid, 'note' if verb == 'note' else 'minimum',
                            address)
            hint = ('发备注（1～24 字，发「清空」去掉）'
                    if verb == 'note' else '发最低金额（0 = 不过滤）')
            self.r.send_html(chat_id, hint)
            return True
        if verb in ('coins', 'dir', 'fmt'):
            field, options = {
                'coins': ('coins', ['USDT,TRX', 'USDT', 'TRX']),
                'dir': ('direction', ['both', 'in', 'out']),
                'fmt': ('detailed', [0, 1]),
            }[verb]
            value = options[(options.index(w[field]) + 1) % len(options)]
            self.set_field(uid, address, field, value)
            text, kb = self.prefs_view(self.one(uid, address))
            self.r.edit_html(chat_id, mid, text, kb=kb)
            return True
        if verb == 'del':
            self.r.send_html(
                chat_id,
                escape('确认停止监听？\n%s\n%s\n\n'
                       '只清掉这个订阅和它的本地统计，机器人/账本都不受影响。'
                       % (w['note'] or '未备注', address)),
                kb={'inline_keyboard': [[
                    {'text': '确认停止', 'callback_data': PREFIX + 'ok:' + address},
                    {'text': '取消', 'callback_data': PREFIX + 'd:' + address}]]})
            return True
        if verb == 'ok':
            self.remove(uid, address)
            self.r.send_html(chat_id, '已停止监听，本地统计也清掉了。')
            text, kb = self.book_view(uid)
            self.r.send_html(chat_id, text, kb=kb)
            return True
        return True

    # ================= 扫链（后台线程）=================
    def should_scan(self):
        """到点了吗。★ 只有主线程（on_tick）调"""
        if not self.all():
            return False
        return time.time() >= self._next_scan and not self._busy

    def scan(self):
        """扫一遍所有监听地址。★ 后台线程跑"""
        with self._lock:
            if self._busy:
                return
            self._busy = True
        try:
            self._next_scan = time.time() + SCAN_INTERVAL
            for w in self.all():
                if self.r.stop_evt.is_set():
                    return
                try:
                    self._scan_one(w)
                except Exception as e:
                    self._mark_error(w, e)
                self.r.stop_evt.wait(SCAN_GAP)
        finally:
            self._busy = False

    def _mark_error(self, w, e):
        log('[%s] 扫 %s 失败：%s' % (self.r.note(), w['address'][:10], e))
        try:
            with _db(self.path) as db:
                db.execute('UPDATE watches SET error=?'
                           ' WHERE buyer=? AND address=? AND revision=?',
                           ('同步暂不可用', w['buyer'], w['address'],
                            w['revision']))
        except Exception:
            pass

    def _scan_one(self, w):
        started = int(time.time() * 1000)
        # 往前多取 10 分钟，防止同一时间戳的被漏掉（重复的靠 txid 去重）
        floor = max(int(w['added'] or 0), int(w['since'] or 0) - 600000)
        pages = []
        for coin in COINS:
            # ★ 每种币单独走 call_with_key：一个币被限流了换把 Key，
            #   不至于把整轮扫描拖垮
            pages.append(self.call_with_key(
                lambda k, c=coin: tc.transfers(w['address'], c, floor, k)))
        rows = sorted([tx for recs, _ in pages for tx in recs],
                      key=lambda t: t['timestamp'])

        for tx in rows:
            current = self.one(w['buyer'], w['address'])
            if not current:
                return                      # 用户把它删了，别再造事件
            if tx['timestamp'] < current['added']:
                continue                    # ★ 订阅之前的历史，一条都不推
            direction = 'in' if tx['to'] == current['address'] else 'out'
            already = self.seen(w['buyer'], w['address'], tx['id'])
            self.record(w['buyer'], w['address'], tx, direction)
            if already:
                continue
            if (tx['coin'] in current['coins'].split(',')
                    and tx['amount'] >= current['minimum']
                    and current['direction'] in ('both', direction)):
                # ★★ 发不出去就**绝不能标「已通知」**（2026-09-29 修）。
                #    原来不管发没发成功都标掉 —— 发送失败（网络抖、被限流、
                #    用户刚好屏蔽了机器人）就等于**这笔钱的提醒永远丢了**，
                #    用户还以为没人给他转过账。钱的事，宁可重试。
                #    ★ `seen()` 只认 notified=1，所以没标掉的下一轮扫描会重推。
                if not self.alert(current, tx):
                    n = self._alert_fail.get(tx['id'], 0) + 1
                    self._alert_fail[tx['id']] = n
                    if n < ALERT_MAX_TRIES:
                        continue        # 留着，下一轮再试
                    # ★ 试够次数还不行（多半是被屏蔽/删号了）→ 放弃重试，
                    #   但**事件本身还在 watch_events 里**，
                    #   用户点「账单统计」照样看得到这笔，不会凭空消失。
                    log('[%s] 提醒 %s 连着 %d 次没发出去，先不再重试'
                        % (self.r.note(), tx['hash'][:16], n))
            self._alert_fail.pop(tx['id'], None)
            self.mark_notified(w['buyer'], w['address'], tx['id'])

        until = max([u for _, u in pages] or [int(w['since'] or 0)])
        with _db(self.path) as db:
            db.execute('UPDATE watches SET since=MAX(since,?),synced=?,error=""'
                       ' WHERE buyer=? AND address=? AND revision=?',
                       (until, started, w['buyer'], w['address'], w['revision']))
            db.execute('DELETE FROM watch_events'
                       ' WHERE buyer=? AND address=? AND timestamp<?'
                       ' AND notified=1',
                       (w['buyer'], w['address'],
                        min(started - EVENT_KEEP_MS, until - 600000)))

    def alert(self, w, tx):
        """★ 谁加的监听就发给谁（私聊）"""
        incoming = tx['to'] == w['address']
        balances = {'USDT': '暂未获取（不代表0）', 'TRX': '暂未获取（不代表0）'}
        try:
            rows = self.call_with_key(lambda k: tc.chain_get(w['address'], {'only_confirmed': 'true'}, k))
            value, _ = tc.balances(rows, w['address'])
            balances = dict(line.split('：', 1) for line in value.splitlines())
            assert set(balances) == {'TRX', 'USDT'}
        except Exception:
            balances = {'USDT': '暂未获取（不代表0）', 'TRX': '暂未获取（不代表0）'}
        lines = ['📣 <b>%s</b>' % escape(w['note'] or '地址监听'), '',
                 '交易金额：<b>%s %s %s</b>' % ('+' if incoming else '-', tc.amount(tx['amount']), tx['coin']),
                 '交易类型：' + ('收入 ⬇️' if incoming else '支出 ⬆️'),
                 '收款地址：<code>%s</code>%s' % (escape(tx['to']), ' ← 监控地址' if tx['to'] == w['address'] else ''),
                 '支付地址：<code>%s</code>%s' % (escape(tx['from']), ' ← 监控地址' if tx['from'] == w['address'] else ''), '',
                 'USDT余额：' + balances['USDT'],
                 'TRX余额：' + balances['TRX'],
                 '转账时间：' + tc.when(tx['timestamp'])]
        kb = {'inline_keyboard': [[
            {'text': '⚙️ 管理地址', 'callback_data': PREFIX + 'd:' + w['address']},
            {'text': '📊 账单统计', 'callback_data': PREFIX + 'stats:' + w['address']}]]}
        try:
            got = self.r.send_html(w['buyer'], '\n'.join(lines), kb=kb)
        except Exception as e:
            log('[%s] 发监听提醒失败（%s）：%s' % (self.r.note(), w['buyer'], e))
            return False        # ★ 调用方靠这个决定「别标已通知」
        # ★★ 注意：`send_html` **失败时不抛异常，返回 None**
        #    （它内部把 TgError 吞了，退回纯文本，纯文本再失败就 return None）。
        #    所以这里必须**看返回值** —— 只写 try/except 的话，
        #    「用户屏蔽了机器人」这种失败会被当成发送成功，
        #    然后标成「已通知」= 这笔钱的提醒永远丢了。
        #    这是 2026-09-29 修的真 bug，别改回去。
        if not got:
            log('[%s] 监听提醒没发出去（%s）：发送接口返回空'
                % (self.r.note(), w['buyer']))
            return False
        return True
