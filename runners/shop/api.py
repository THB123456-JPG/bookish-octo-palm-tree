# -*- coding: utf-8 -*-
"""商城机器人的面板接口 —— 实现部分

★ 路由的判断条件（哪段路径归商城）在 panel.py 里，
  这里只放**实现**。分开放是为了：改商城的接口，一个字都不会碰到
  记账 / 客服 / USDT 的代码。

★ 约定：每个 handle_xxx(h, path[, me]) 返回 **True = 这个请求我处理了**
  （不管成功还是报错），返回 False = 没匹配上，panel.py 继续往下找。
  h 就是 panel.PanelHandler 实例，借它的 _gate / _json / _shop_of / mgr 用。

★★ 安全：`config` 接口返回的内容带明文 `provider_key` / `premium_key`，
   **归属校验（h._shop_of）必须走在最前面** —— 否则等于把别人的上游账号送出去。
   改这里的顺序之前先想清楚这一点。
"""
from core import log

from . import store as SS


def pay_status(st):
    """从数据文件里读扫描状态（机器人没在跑时也能显示）"""
    sc = st.data.get('scan') or {}
    return {
        'pay_address': (st.cfg.get('trx_own') or ''),
        'baseline': bool(sc.get('baseline')),
        'seen_count': len(sc.get('seen') or []),
    }


def handle_get(h, path):
    """GET /api/shop/..."""

    # ---------------- 流水 ----------------
    if path.endswith('/payments'):
        me = h._gate()
        if me is None:
            return True
        bid = path[len('/api/shop/'):-len('/payments')].strip('/')
        st = h._shop_of(me, bid)
        if st is None:
            return True
        rows = []
        for p in st.recent(100):
            rows.append({
                'txid': p.get('txid'),
                'from': p.get('from'),
                'amount': p.get('amount'),
                'units': p.get('units'),
                'energy': p.get('energy'),
                'status': p.get('status'),
                'status_label': SS.pay_label(p),
                'note': p.get('note'),
                'at_str': SS.at_str(p),
                'paid_str': SS.paid_str(p),
            })
        h._json({'ok': True, 'payments': rows,
                 'attention': len(st.need_attention()),
                 'today': st.stats(1),
                 'scan': pay_status(st)})
        return True

    # ---------------- 配置 ----------------
    if path.endswith('/config'):
        me = h._gate()
        if me is None:
            return True
        bid = path[len('/api/shop/'):-len('/config')].strip('/')
        # ★ 这里返回的 config 带明文 provider_key / premium_key，
        #   归属校验必须走在前面，否则等于把别人的上游账号送出去
        st = h._shop_of(me, bid)
        if st is None:
            return True
        from .providers import provider_list
        h._json({'ok': True, 'config': st.cfg,
                 'providers': provider_list(),
                 'ready': st.ready()})
        return True

    return False


def handle_post(h, path, me):
    """POST /api/shop/...（me 是 panel.py 已经鉴权过的身份）"""

    # ---------------- 改配置 ----------------
    if (path.endswith('/config') and len(path.strip('/').split('/')) == 4):
        bid = path[len('/api/shop/'):-len('/config')].strip('/')
        st = h._shop_of(me, bid)
        if st is None:
            return True
        patch = h._body() or {}
        # 只允许改认识的键，防止面板塞垃圾进来
        allow = {'enabled', 'trx_own', 'price', 'energy', 'max',
                 'min_units', 'provider', 'provider_key',
                 'group_ids', 'contact', 'notice',
                 'premium_enabled', 'premium_prices',
                 'premium_key', 'premium_callback',
                 'auto_enabled', 'auto_prices', 'auto_counts',
                 'auto_type',
                 # 自定义上游（客户照自己文档填）
                 'custom_url', 'custom_method', 'custom_ok_path',
                 'custom_ok_value', 'custom_order_path'}
        clean = {k: v for k, v in patch.items() if k in allow}
        if not clean:
            h._json({'ok': False, 'error': '没有可改的字段'})
            return True
        # ★★ 自定义上游的接口地址：**存的时候就先验一道**。
        #    这是为了「填错了立刻告诉他」，**不是**安全上的那道门 ——
        #    安全那道门在 providers.CustomProvider.order() 里，
        #    每次真发请求前都会按**当时的**解析结果再验一次
        #    （域名可以事后改解析，只存的时候验一次等于没验）。
        if clean.get('custom_url'):
            from .providers import check_public_url
            _ok, _why = check_public_url(clean['custom_url'])
            if not _ok:
                h._json({'ok': False, 'error': '接口地址不能用：%s' % _why})
                return True
        # 改了收款地址必须重建扫描基线，
        # 否则新地址历史上的 TRX 转入会被当成新收款，白送能量
        addr_changed = ('trx_own' in clean
                        and clean['trx_own'] != st.cfg.get('trx_own'))
        st.set_cfg(clean)
        if addr_changed:
            st.data['scan'] = {'baseline': False, 'seen': [], 'last_ts': 0}
            st.save()
            log('收款地址变了，商城机器人 %s 的扫描基线已重置' % bid)
        h._json({'ok': True, 'baseline_reset': addr_changed})
        return True

    # ---------------- 上游自检 ----------------
    # 可以用 body 里的临时值测（改完 key 先测再存），不传就用已保存的
    if (path.endswith('/providertest')
            and len(path.strip('/').split('/')) == 4):
        bid = path[len('/api/shop/'):-len('/providertest')].strip('/')
        st = h._shop_of(me, bid)
        if st is None:
            return True
        from .providers import get_provider
        b = h._body() or {}
        cfg = dict(st.cfg)
        # 用输入框里的临时值测（改完还没保存也能先测）
        for k in ('provider', 'provider_key', 'custom_url', 'custom_method',
                  'custom_ok_path', 'custom_ok_value', 'custom_order_path'):
            if k in b:
                cfg[k] = b[k]
        name = cfg.get('provider')
        try:
            prov = get_provider(name, cfg.get('provider_key'),
                                cfg.get('premium_key'), cfg)
            passed, msg = prov.self_test()
        except Exception as e:
            passed, msg = False, str(e)
        log('上游自检（%s / %s）：%s' % (bid, name, msg))
        h._json({'ok': True, 'pass': passed, 'msg': msg})
        return True

    # ---------------- 重发 ----------------
    if (path.endswith('/retry') and len(path.strip('/').split('/')) == 6):
        parts = path.strip('/').split('/')
        bid, txid = parts[2], parts[4]
        st = h._shop_of(me, bid)
        if st is None:
            return True
        p = st.by_txid(txid)
        if not p:
            h._json({'ok': False, 'error': '找不到这笔流水'})
            return True
        r = h.mgr.runners.get(bid)
        if r is None or not hasattr(r, 'pay'):
            h._json({'ok': False,
                     'error': '机器人没在运行，重发需要先把机器人启用'})
            return True
        try:
            # manual=True：这是人在面板上点的，表示已经确认过了，
            # 允许越过「结果不明」的保护强制重发
            r.pay.deliver(p, p.get('from'), int(p.get('energy') or 0),
                          manual=True)
            h._json({'ok': True, 'status': p.get('status'),
                     'note': p.get('note')})
        except Exception as e:
            h._json({'ok': False, 'error': str(e)})
        return True

    # ---------------- 标记已处理 ----------------
    if (path.endswith('/mark') and len(path.strip('/').split('/')) == 6):
        parts = path.strip('/').split('/')
        bid, txid = parts[2], parts[4]
        st = h._shop_of(me, bid)
        if st is None:
            return True
        p = st.by_txid(txid)
        if not p:
            h._json({'ok': False, 'error': '找不到这笔流水'})
            return True
        st.mark(p, SS.P_SKIPPED, '面板标记已处理')
        log('面板标记流水 %s 已处理' % txid[:16])
        h._json({'ok': True})
        return True

    return False
