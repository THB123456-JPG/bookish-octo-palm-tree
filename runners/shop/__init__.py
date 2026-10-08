# -*- coding: utf-8 -*-
"""商城机器人（`bot['type'] == 'shop'`）

    runner.py     机器人本体（菜单、下单、发货）
    store.py      商品/订单存储（sqlite）
    pay.py        收款监听
    providers.py  上游供应商对接
    api.py        面板接口的实现（路由表在 panel.py）
"""
from .runner import ShopRunner

__all__ = ['ShopRunner']
