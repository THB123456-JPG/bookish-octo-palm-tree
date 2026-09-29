# -*- coding: utf-8 -*-
"""USDT 助手（`bot['type'] == 'usdt'`）

★ 链上查询、地址编解码、汇率这些**通用工具**不在这里，在根目录的 `tron.py` ——
  商城机器人也要用（它靠那些收 USDT 付款），所以不能锁在某个类型里。
"""
from .runner import UsdtRunner

__all__ = ['UsdtRunner']
