# -*- coding: utf-8 -*-
"""收款监听 + 自动发货

★ 核心模型（照 AE86X/Tronbot）：**不依赖机器人交互**
   客户从自己钱包转 TRX 到收款地址 → 我们看转账记录的 from 是谁 →
   就把能量发给谁。配对信息就在转账记录里，不用客户登记地址，
   也就不存在「发错人」的问题。

   金额按倍数折算（3.5 TRX 一个单位 → 32000 能量）：
       转 3.5 → 1 单位；转 7 → 2 单位；转 35 → 10 单位

去重逻辑照搬 usdt.py 的 _scan_one（min_ts 回退 2 分钟、txid 集合去重、
seen 保留 300 条、首次只建基线）—— 那套是踩过坑的，别重写。
"""
import threading
import time

from core import TgError, log, save_json
from tron import Tron, fmt_amount
from .providers import UncertainError
from . import store as S


class PayWatcher:
    """收款地址的扫描和发货。一个商城机器人一个实例。"""

    SCAN_LIMIT = 30             # 每轮每种币各拉多少条转入
    SEEN_KEEP = 300             # 记住最近多少条 txid 去重
    MIN_TRX = 0.000001          # 小于这个数（1 SUN）的 TRX 转账直接忽略
    MIN_USDT = 0.01             # 小于这个数的 USDT 转账直接忽略

    def __init__(self, runner, store, tron=None):
        self.r = runner
        self.s = store
        self.tron = tron or Tron()
        # ★ 防重复扣费：一把锁 + 一个「正在发货」的进程内集合。
        #   见 _claim() 的注释 —— 上游调用**不在**这把锁里。
        self._ship_lock = threading.Lock()
        self._shipping = set()

    # -------- 扫描进度 --------
    @property
    def state(self):
        return self.s.data.setdefault(
            'scan', {'baseline': False, 'seen': [], 'last_ts': 0})

    def reset_baseline(self):
        """换了收款地址之后必须重建基线，否则新地址的历史转账会被当成本次收款"""
        self.s.data['scan'] = {'baseline': False, 'seen': [], 'last_ts': 0}
        self.s.save()

    def pay_addr(self):
        return (self.s.cfg.get('trx_own') or '').strip()

    # -------- 主流程 --------
    def scan(self):
        """扫一轮：识别新到的 TRX、折算能量、发货。返回本次处理掉的流水列表"""
        if not self.s.cfg.get('enabled', True):
            return []
        addr = self.pay_addr()
        if not addr:
            return []

        st = self.state
        last_ts = int(st.get('last_ts') or 0)
        min_ts = (last_ts - 120000) if last_ts else None   # 回退 2 分钟防漏

        # ★ 两种币一起扫：同一个 TRON 地址 TRX 和 USDT-TRC20 都能收。
        #   客户转 TRX = 买能量，转 USDT = 买会员，靠币种天然分开，不会打架。
        txs = []
        for coin, fetch in (('TRX', self.tron.trx_transfers),
                            ('USDT', self.tron.transfers)):
            try:
                for t in fetch(addr, limit=self.SCAN_LIMIT, min_ts=min_ts):
                    t['coin'] = coin
                    txs.append(t)
            except TgError as e:
                # 一种币扫失败不该拖累另一种
                log('[%s] 扫 %s 转入失败：%s' % (self.r.note(), coin, e))
        if not txs:
            return []

        # 第一次扫：只建基线（否则会把历史上收过的全部当成新收款发出去）
        if not st.get('baseline'):
            st['baseline'] = True
            st['seen'] = [t['txid'] for t in txs][:self.SEEN_KEEP]
            st['last_ts'] = max(t['ts'] for t in txs)
            self.s.save()
            log('[%s] 收款基线已建立（%d 条历史转入，不会补发）'
                % (self.r.note(), len(txs)))
            return []

        seen = set(st.get('seen') or [])
        fresh = [t for t in txs if t['txid'] not in seen]
        if not fresh:
            return []

        fresh.sort(key=lambda t: t['ts'])      # 老的先处理（按时间顺序发货）
        done = []
        for t in fresh:
            seen.add(t['txid'])
            amt = float(t.get('amount') or 0)
            floor = self.MIN_TRX if t.get('coin') == 'TRX' else self.MIN_USDT
            if amt < floor:
                continue                        # 尘埃转账，忽略
            p = self.handle_one(t)
            if p:
                done.append(p)

        st['seen'] = list(seen)[-self.SEEN_KEEP:]
        st['last_ts'] = max(max(t['ts'] for t in fresh), last_ts)
        self.s.save()
        return done

    # -------- 处理单笔 --------
    def handle_one(self, tx):
        """一笔转入 → 看币种分流：**USDT 是买会员，TRX 是买能量**"""
        coin = (tx.get('coin') or 'TRX').upper()
        txid = tx.get('txid') or ''
        frm = tx.get('from') or ''

        # 这个 txid 已经处理过就不重复（防程序重启后重复发货）
        if txid and self.s.by_txid(txid):
            return None
        # 自己转给自己 / 收款地址转出，跳过
        if not frm or frm == self.pay_addr():
            return None

        amount = float(tx.get('amount') or 0)

        # 收到 USDT → 只可能是买会员。能量只收 TRX，
        # 所以这一步不存在「误当成买能量白送」的风险。
        if coin == 'USDT':
            return self.handle_usdt_in(tx, frm, amount)

        # ---- 以下都是 TRX ----
        # ① 先看是不是在给会员订单付款（金额精确匹配，仅限 TRX 计价的会员单）
        po = self.s.premium_match_amount(amount, 'TRX', frm)
        if po:
            return self.handle_premium(tx, po, frm)

        # ★ ② 金额「贴着」某个待付款会员订单、但没精确对上
        #    （多半是客户从交易所转账被扣了手续费）。
        #    这种情况绝不能掉进能量逻辑 —— 那会按倍数白送一大把能量出去。
        near = self.s.premium_near_amount(amount, 'TRX')
        if near:
            p = self.s.add_payment(
                txid, frm, amount, 0, 0, S.P_PENDING,
                '金额接近会员订单 %s（%s TRX），可能是交易所扣了手续费，'
                '等人工确认' % (near['oid'], fmt_amount(near['amount'])),
                paid_at=tx.get('ts') or 0)
            p['poid'] = near['oid']
            self.s.save()
            log('[%s] ⚠️ 收到 %s TRX，贴着会员订单 %s（%s TRX），'
                '不发能量，等人工核对'
                % (self.r.note(), fmt_amount(amount), near['oid'],
                   fmt_amount(near['amount'])))
            self.r.on_payment_failed(
                p, '金额接近会员订单 %s 但没精确对上，可能是交易所扣了手续费。'
                   '核对后可在面板处理' % near['oid'])
            return p

        # ③ 都不是 → 正常的能量订单
        units, energy, note = self.s.calc(amount)

        # 不够一个单位 → 只记一笔，不发能量
        if units <= 0:
            p = self.s.add_payment(txid, frm, amount, 0, 0,
                                   S.P_SKIPPED, note or '金额不足',
                                   paid_at=tx.get('ts') or 0)
            log('[%s] 收到 %s TRX（来自 %s），不足一个单位，忽略：%s'
                % (self.r.note(), fmt_amount(amount), frm[:12], note))
            self.r.on_payment_skipped(p)
            return p

        # 先落一条 pending，防止发货过程中崩了之后重复发
        p = self.s.add_payment(txid, frm, amount, units, energy,
                               S.P_PENDING, note, paid_at=tx.get('ts') or 0)
        self.r.on_payment_seen(p)

        self.deliver(p, frm, energy)
        return p

    # -------- USDT 收款（会员）--------
    def handle_usdt_in(self, tx, frm, amount):
        """收到 USDT-TRC20 → 认会员订单

        客户付 USDT、上游也收 USDT，币种直接对齐，不用换汇。
        """
        txid = tx.get('txid') or ''
        po = self.s.premium_match_amount(amount, 'USDT', frm)
        if po:
            return self.handle_premium(tx, po, frm)

        near = self.s.premium_near_amount(amount, 'USDT')
        if near:
            note = ('金额接近会员订单 %s（%s USDT）但没精确对上，'
                    '可能被扣了手续费，等人工确认'
                    % (near['oid'], fmt_amount(near['amount'])))
        else:
            note = '收到 USDT 但没有对得上的会员订单（可能是误转，或客户转错了金额）'
        p = self.s.add_payment(txid, frm, amount, 0, 0, S.P_PENDING, note,
                               paid_at=tx.get('ts') or 0)
        p['coin'] = 'USDT'
        if near:
            p['poid'] = near['oid']
        self.s.save()
        log('[%s] ⚠️ 收到 %s USDT（来自 %s）：%s'
            % (self.r.note(), fmt_amount(amount), frm[:12], note))
        self.r.on_payment_failed(p, note)
        return p

    # -------- 会员订单 --------
    def handle_premium(self, tx, o, frm):
        """会员订单收到款 → 调上游开通"""
        txid = tx.get('txid') or ''
        amount = float(tx.get('amount') or 0)

        o['status'] = S.PR_PAID
        o['pay_txid'] = txid
        o['paid_at'] = int(tx.get('ts') or 0)
        o['paid_from'] = frm                 # 记下谁付的，要退款时用得上
        self.s.save()

        p = self.s.add_payment(
            txid, frm, amount, 0, 0, S.P_PENDING,
            '会员订单 %s（@%s %s 个月）' % (o['oid'], o['username'], o['month']),
            paid_at=tx.get('ts') or 0)
        p['poid'] = o['oid']
        p['coin'] = (tx.get('coin') or 'TRX').upper()
        self.s.save()
        log('[%s] 收到会员付款 %s %s（@%s %s 个月），开始开通'
            % (self.r.note(), fmt_amount(amount), p['coin'],
               o['username'], o['month']))
        self.r.on_premium_paid(o)
        self.deliver_premium(p, o)
        return p

    def deliver_premium(self, p, o, manual=False):
        """调上游给 @username 开通会员。

        防重复扣费的道理和能量一模一样：请求打出去了但没拿到答复时，
        上游可能**已经开通了** —— 重发就是再扣一次钱，还可能给客户多开一个月。
        ★ 跟 `deliver()` 一样，判定 + 落盘整段进锁（见 `_claim()`）。
        """
        k = self._key(o)
        try:
            ok, why = self._claim(k, o, S.PR_DONE, manual)
        except Exception as e:
            log('[%s] ⚠️ 开通前的状态没能存进磁盘（%s），这笔先不开 ——'
                '宁可不发也不能冒着重复扣钱的风险' % (self.r.note(), e))
            self.r.on_payment_failed(p, '保存状态失败，暂不开通：%s' % e)
            return False
        if not ok:
            return self._refuse_premium(p, o, k, why)
        try:
            return self._ship_premium(p, o)
        finally:
            self._release(k)

    def _refuse_premium(self, p, o, k, why):
        """会员订单没抢到处理权（文案和原来一字不差）"""
        if why == 'SHIPPING':
            log('[%s] 会员订单 %s 正在开通中，这次点击忽略（防重复扣费）'
                % (self.r.note(), o.get('oid')))
            return False
        if why == 'DONE':
            log('[%s] 会员订单 %s 已经开通过了，跳过'
                % (self.r.note(), o.get('oid')))
            return True
        msg = ('上一次开通请求结果不明（%s），上游可能已经开通了。'
               '请先到上游后台核对，确认没开再手动重开'
               % time.strftime('%m-%d %H:%M:%S',
                               time.localtime(int(o.get('dispatched') or 0))))
        o['status'] = S.PR_PAID
        o['note'] = msg
        self.s.save()
        self.s.mark(p, S.P_PENDING, msg)
        log('[%s] ⚠️ 会员订单 %s 上次结果不明，不自动重开'
            % (self.r.note(), o.get('oid')))
        self.r.on_payment_failed(p, msg)
        return False

    def _ship_premium(self, p, o):
        """已经拿到处理权之后，真正开通那一段。★ 不持锁（原因同 `_ship`）"""

        kind = o.get('kind') or 'premium'
        try:
            prov = self.r.provider()
            cb = (self.s.cfg.get('premium_callback') or '').strip()
            if kind == 'auto':
                # 笔数订单：username 字段存的是「地址」，month 字段存的是「笔数」
                oid, raw = prov.auto_buy(
                    o.get('username') or '',
                    int(o.get('month') or 0),
                    int(self.s.cfg.get('auto_type') or 1), cb)
            else:
                oid, raw = prov.premium_open(o.get('username'),
                                             int(o.get('month') or 0), cb)
        except UncertainError as e:
            # 结果不明 → 挂着等人核对，绝不自动重开
            o['status'] = S.PR_PAID
            o['note'] = '结果不明：%s' % e
            self.s.save()
            self.s.mark(p, S.P_PENDING, '结果不明，等人工核对：%s' % e)
            log('[%s] ⚠️⚠️ 会员开通结果不明 %s：%s'
                % (self.r.note(), o.get('oid'), e))
            self.r.on_payment_failed(p, str(e))
            return False
        except TgError as e:
            # 上游明确说没成 → 钱没扣，清掉标记以后还能重开
            o.pop('dispatched', None)
            o['status'] = S.PR_FAILED
            o['note'] = '开通失败：%s' % e
            self.s.save()
            self.s.mark(p, S.P_FAILED, '开通失败：%s' % e)
            log('[%s] ⚠️ 会员开通失败 %s：%s'
                % (self.r.note(), o.get('oid'), e))
            self.r.on_payment_failed(p, str(e))
            return False
        except Exception as e:
            o['status'] = S.PR_PAID
            o['note'] = '开通异常，结果不明：%s' % e
            self.s.save()
            self.s.mark(p, S.P_PENDING, '开通异常，结果不明：%s' % e)
            log('[%s] ⚠️⚠️ 会员开通异常 %s：%s'
                % (self.r.note(), o.get('oid'), e))
            self.r.on_payment_failed(p, '系统异常：%s' % e)
            return False

        o.pop('dispatched', None)
        o['provider_oid'] = oid
        if raw.get('amount') is not None:
            try:
                o['cost'] = round(float(raw['amount']), 6)
            except (TypeError, ValueError):
                pass

        if kind == 'auto':
            # 笔数下单是同步的：回了 orderId 就代表笔数已经进账了
            o['status'] = S.PR_DONE
            o['done_at'] = int(time.time())
            o['note'] = '已到账 %s 笔' % o.get('month')
            self.s.mark(p, S.P_DONE, '已买 %s 笔（上游单号 %s）'
                        % (o.get('month'), oid))
            log('[%s] ✅ 笔数到账：%s 买了 %s 笔（单号 %s）'
                % (self.r.note(), (o.get('username') or '')[:12],
                   o.get('month'), oid))
            self.s.save()
            self.r.on_premium_result(o)
            return True

        st = str(raw.get('status'))
        if st == '1':
            o['status'] = S.PR_DONE
            o['done_at'] = int(time.time())
            o['note'] = '开通成功'
            self.s.mark(p, S.P_DONE, '已开通（上游单号 %s）' % oid)
            self.s.credit('premium:%s' % o['username'], p['amount'], 0)
            log('[%s] ✅ 会员开通成功 @%s %s 个月（单号 %s）'
                % (self.r.note(), o['username'], o['month'], oid))
        elif st == '2':
            o['status'] = S.PR_FAILED
            o['note'] = str(raw.get('remark') or '上游开通失败')
            self.s.mark(p, S.P_FAILED, '开通失败：%s' % o['note'])
            log('[%s] ⚠️ 会员开通失败 @%s：%s'
                % (self.r.note(), o['username'], o['note']))
        else:
            # 0 = 上游还在开通中，先挂着，等回调或人工确认
            o['status'] = S.PR_PAID
            o['note'] = str(raw.get('remark') or '上游处理中')
            self.s.mark(p, S.P_PENDING, '上游处理中：%s' % o['note'])
            log('[%s] 会员 @%s 已提交，上游还在处理（单号 %s）'
                % (self.r.note(), o['username'], oid))
        self.s.save()
        self.r.on_premium_result(o)
        return True

    def deliver(self, p, to_addr, energy, manual=False):
        """调上游 API 把能量委托给 to_addr

        ★★ 防重复扣费（这是整个商城最容易丢钱的地方）★★
        上游下单是「按次扣钱」的。如果请求发出去了但没拿到答复
        （超时 / 断网 / 程序崩了），上游那边**可能已经扣费发货**。
        这时候自动重发 = 再扣一次钱 = 白亏一份。

        所以发货前先落一个 dispatched 标记：
          · 标记在 → 说明上一次请求已经打出去了，结果不明
                     → 一律不自动重发，挂「待处理」等人工核对
          · 标记不在 → 上一次连请求都没发出去，重发是安全的

        manual=True 表示人已经去上游后台核对过了、确认要重发。

        ★★ 2026-09-29 加的：上面这套判定以前是「裸奔」的 ★★
        `deliver()` 会被**多个线程**同时调到 —— 面板手动重发（HTTP 线程）
        和机器人轮询线程（扫链 / retry_pending）。原来的
        「读 dispatched → 判断 → 写 dispatched」**不是临界区**，
        两边能同时通过检查 → 同一笔单子调两次上游 = **扣两次钱**。
        现在「判定 + 落盘」整段进 `_ship_lock`，见 `_claim()`。
        """
        k = self._key(p)
        try:
            ok, why = self._claim(k, p, S.P_DONE, manual)
        except Exception as e:
            # 状态没写进磁盘 → 这笔先不发（下一次扫描/点击会再试，不会漏掉）
            log('[%s] ⚠️ 发货前的状态没能存进磁盘（%s），这笔先不发 ——'
                '宁可不发也不能冒着重复扣钱的风险' % (self.r.note(), e))
            self.r.on_payment_failed(p, '保存状态失败，暂不发货：%s' % e)
            return False
        if not ok:
            return self._refuse(p, k, why)
        try:
            return self._ship(p, to_addr, energy)
        finally:
            self._release(k)

    # -------- 防重复扣费的三块骨架（deliver / deliver_premium 共用）--------
    def _key(self, obj):
        """一笔流水/订单的唯一键。txid 优先，其次 oid，最后退回对象地址。"""
        return str(obj.get('txid') or obj.get('oid') or id(obj))

    def _claim(self, k, obj, done_status, manual):
        """★ 原子拿「这笔的处理权」。返回 (True, '') = 拿到了，可以去调上游。

        ★ 为什么必须加锁：「读标记 → 判断 → 写标记」这三步中间只要有一个
          别的线程挤进来，两边就都认为自己该发货 → **同一笔扣两次钱**。
          锁只圈这一小段（几个字典操作 + 一次落盘），**绝不圈上游调用** ——
          上游来回要好几秒，圈进去所有订单都得排队。
        ★ `_shipping` 是**进程内**的「正在发货」集合：同一笔在被一个线程
          处理期间，别的线程（哪怕是手动重发）一律拒绝。
          光靠 `dispatched` 标记不够 —— manual=True 会跳过那个检查。
        ★ 进程重启后 `_shipping` 是空的，但 `dispatched` 已经落盘了 →
          重启后走「结果不明 → 转人工核对」，**不会**被当成可重试。
          （这是故意的：崩在半路的那一笔，上游到底扣没扣钱只有人知道）
        """
        with self._ship_lock:
            if k in self._shipping:
                return False, 'SHIPPING'
            if obj.get('status') == done_status:
                return False, 'DONE'
            if obj.get('dispatched') and not manual:
                return False, 'UNCERTAIN'
            # 写前标记：先记下「我要发了」，**落盘之后**才去调上游
            obj['dispatched'] = int(time.time())
            obj['tries'] = int(obj.get('tries') or 0) + 1
            try:
                self._save_hard()
            except Exception:
                # ★ 落盘失败就别发货：写不进去 = 崩了以后这笔看起来「没发过」
                #   → 重启后会被重发 → **重复扣钱**。宁可不发，也不能冒这个险。
                obj.pop('dispatched', None)
                obj['tries'] = max(0, int(obj.get('tries') or 0) - 1)
                raise
            self._shipping.add(k)      # ★ 落盘成功之后才算「我拿着」
        return True, ''

    def _save_hard(self):
        """★ 发货前的落盘，**必须确认真的写进去了**。

        平时 `BaseRunner.save_data()` 写盘失败只记一条日志（这是对的 ——
        不该因为磁盘抽风就把整个机器人搞崩）。但**发货前不行**：
        `dispatched` 标记就是「上游可能已经扣钱了」的唯一证据，
        它没落盘的话，进程一崩这笔在盘上看起来「从没发过」→ 重启后重发
        → 上游再扣一次钱。所以这里直接调 `core.save_json`，让异常抛上去。
        """
        path = getattr(self.r, 'data_file', None)
        if not path:
            self.s.save()        # 测试桩没有 data_file：退回原来的路
            return
        save_json(path, self.r.data)

    def _release(self, k):
        """★ 必须在 finally 里调 —— 漏了这笔就永远卡在「正在发货」"""
        with self._ship_lock:
            self._shipping.discard(k)

    def _refuse(self, p, k, why):
        """没抢到处理权时的三种走法（文案和原来一字不差）"""
        tx = (p.get('txid') or '')[:16]
        if why == 'SHIPPING':
            log('[%s] 流水 %s 正在发货中，这次点击忽略（防重复扣费）'
                % (self.r.note(), tx))
            return False
        if why == 'DONE':
            # 已经发成功了就别再碰（面板手滑点重发也不会重复扣钱）
            log('[%s] 流水 %s 已经是已发货状态，跳过' % (self.r.note(), tx))
            return True
        msg = ('上一次发货请求结果不明（%s），上游可能已经扣费发货。'
               '请先到上游后台按 traceId=%s 核对，确认没发再手动重发'
               % (time.strftime('%m-%d %H:%M:%S',
                                time.localtime(int(p.get('dispatched') or 0))),
                  tx))
        self.s.mark(p, S.P_PENDING, msg)
        log('[%s] ⚠️ %s 上次结果不明，不自动重发，等人工核对'
            % (self.r.note(), tx))
        self.r.on_payment_failed(p, msg)
        return False

    def _ship(self, p, to_addr, energy):
        """已经拿到处理权之后，真正发货那一段。

        ★ 这一段**不持锁** —— 上游来回要好几秒，圈进锁里会把所有订单都卡住。
        """

        try:
            prov = self.r.provider()
            oid, _raw = prov.order(to_addr, int(energy), 1,
                                   trace=(p.get('txid') or ''))
        except UncertainError as e:
            # 结果不明：留在待处理，等人工。★ 绝不能标 failed 让重试流程捡走
            self.s.mark(p, S.P_PENDING, '结果不明，等人工核对：%s' % e)
            log('[%s] ⚠️⚠️ 发货结果不明 %s：%s'
                % (self.r.note(), (p.get('txid') or '')[:16], e))
            self.r.on_payment_failed(p, str(e))
            return False
        except TgError as e:
            # 上游明确说没成功 → 钱没扣，清掉标记，以后还能重发
            p.pop('dispatched', None)
            self.s.mark(p, S.P_FAILED, '发货失败：%s' % e)
            log('[%s] ⚠️ 发货失败 %s：%s'
                % (self.r.note(), (p.get('txid') or '')[:16], e))
            self.r.on_payment_failed(p, str(e))
            return False
        except Exception as e:
            # 本地异常（比如 provider 代码崩了）——也是结果不明，按最保守处理
            self.s.mark(p, S.P_PENDING, '发货异常，结果不明：%s' % e)
            log('[%s] ⚠️⚠️ 发货异常 %s：%s'
                % (self.r.note(), (p.get('txid') or '')[:16], e))
            self.r.on_payment_failed(p, '系统异常：%s' % e)
            return False

        # 到这儿上游明确成功了。
        # 记下上游实际扣了多少钱 —— 真实成本只有上游说了算，
        # 攒几笔就能看出官方公示的价准不准，定价心里才有底。
        if isinstance(_raw, dict) and _raw.get('amount') is not None:
            try:
                p['cost'] = round(float(_raw['amount']), 6)
            except (TypeError, ValueError):
                pass
        p.pop('dispatched', None)
        self.s.mark(p, S.P_DONE, (p.get('note') or '') or ('上游单号 %s' % oid))
        self.s.credit(to_addr, p['amount'], energy)
        log('[%s] ✅ 发货成功：给 %s 发 %s 能量（收款 %s，上游 %s）'
            % (self.r.note(), to_addr[:12], energy,
               (p.get('txid') or '')[:16], oid[:20]))
        self.r.on_payment_done(p)
        return True

    # -------- 会员订单补开 --------
    def retry_premiums(self, limit=3):
        """补开卡住的会员订单。规矩和能量一样：只补「确定没打给上游」的。"""
        now = int(time.time())
        n = 0
        for o in self.s.premiums:
            if o.get('status') not in (S.PR_FAILED, S.PR_PAID):
                continue
            if not o.get('pay_txid'):
                continue
            if o.get('dispatched'):
                continue        # 结果不明，等人工
            tries = int(o.get('tries') or 0)
            low_bal = '余额不足' in (o.get('note') or '')
            if tries >= (self.LOW_BAL_TRIES if low_bal else self.MAX_TRIES):
                continue
            delay = (self.LOW_BAL_DELAY if low_bal
                     else self.RETRY_DELAYS[min(tries,
                                                len(self.RETRY_DELAYS) - 1)])
            paid_s = int(o.get('paid_at') or 0) / 1000.0
            if paid_s and now - paid_s < delay:
                continue
            # 因为是「确定没打给上游」才走到这，重开是安全的
            p = self.s.by_txid(o.get('pay_txid'))
            if p is None:
                continue
            log('[%s] 补开会员订单 %s（第 %d 次）'
                % (self.r.note(), o.get('oid'), tries + 1))
            self.deliver_premium(p, o, manual=True)
            n += 1
            if n >= limit:
                break
        return n

    # -------- 成本估算（补发前判断余额够不够）--------
    def _unit_cost(self):
        """一单大概花多少 TRX：有实测扣费就用实测，否则退回上游报价。

        实测优先 —— 官方公示价会滞后（实测 1.56 vs 公示 1.1）。
        """
        hist = [p.get('cost') for p in self.s.recent(30) if p.get('cost')]
        if hist:
            return max(hist)
        try:
            return float(self.r.provider().quote(
                int(self.s.cfg.get('energy') or 0), 1)[0] or 0)
        except Exception:
            return 0.0

    def _upstream_balance(self):
        try:
            return self.r.provider().balance()
        except Exception:
            return None

    # -------- 补发 --------
    RETRY_DELAYS = (60, 300, 1800)      # 第1次等1分钟，第2次5分钟，第3次半小时
    MAX_TRIES = 3                       # 普通失败最多自动重试 3 次
    # 「上游余额不足」放宽上限：这种失败只要充了值就能成，
    # 而卡住的每一条都是已经收了客户钱、欠着客户的货，不能轻易放弃
    LOW_BAL_TRIES = 20
    LOW_BAL_DELAY = 300                 # 余额不足的每 5 分钟看一次

    def retry_pending(self, limit=5):
        """补发「确定没发出去」的流水。

        ★ 只补发没带 dispatched 标记的：
          · 带标记 = 上次请求已经打给上游了，结果不明 → 重发要再扣一次钱，只能人工核对
          · 没标记 = 上次要么没发（程序刚崩），要么上游明确说失败（钱没扣）→ 重发安全

        重试按 RETRY_DELAYS 退避，免得一直骚扰上游。
        典型场景：上游余额不足 → 失败 → 你充了值 → 这几笔自动补上。
        """
        now = int(time.time())
        n = 0
        todo = (self.s.recent(50, status=S.P_PENDING)
                + self.s.recent(50, status=S.P_FAILED))

        # 有因「余额不足」卡住的单子时，先看一眼上游余额 ——
        # 不够就先别动，免得每 5 分钟白打一次上游 API、白刷一条失败日志。
        # 等充了值余额够了，下一轮自然就会补发。
        def _stuck(x):
            return (not x.get('dispatched')
                    and '余额不足' in (x.get('note') or ''))

        stuck = [p for p in todo if _stuck(p)]
        if stuck:
            per = self._unit_cost()
            bal = self._upstream_balance()
            if per > 0 and bal is not None and bal < per:
                log('[%s] 上游余额 %s TRX，不够补发（每笔约 %s TRX），先等着'
                    % (self.r.note(), fmt_amount(bal), fmt_amount(per)))
                todo = [p for p in todo if not _stuck(p)]

        for p in todo:
            if p.get('dispatched'):
                continue        # 结果不明，等人工，绝不自动补发
            tries = int(p.get('tries') or 0)
            # 余额不足是可恢复的（充值即好），给它更宽容的额度
            low_bal = '余额不足' in (p.get('note') or '')
            if tries >= (self.LOW_BAL_TRIES if low_bal else self.MAX_TRIES):
                continue
            if low_bal:
                delay = self.LOW_BAL_DELAY
            else:
                delay = self.RETRY_DELAYS[min(tries,
                                              len(self.RETRY_DELAYS) - 1)]
            if now - int(p.get('at') or 0) < delay:
                continue
            log('[%s] 补发流水 %s（第 %d 次重试）'
                % (self.r.note(), p['txid'][:16], tries + 1))
            self.deliver(p, p.get('from'), p.get('energy') or 0)
            n += 1
            if n >= limit:
                break
        return n

    # -------- 给面板看的 --------
    def status(self):
        st = self.state
        return {
            'pay_address': self.pay_addr(),
            'baseline': bool(st.get('baseline')),
            'last_ts': int(st.get('last_ts') or 0),
            'seen_count': len(st.get('seen') or []),
        }
