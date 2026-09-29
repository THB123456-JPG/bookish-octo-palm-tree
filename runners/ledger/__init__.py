# -*- coding: utf-8 -*-
"""记账机器人（`bot['type'] == 'ledger'`）

    runner.py   机器人本体（接线层：把 Telegram 消息翻译成 commands 调用）
    api.py      面板接口的实现（路由表在 panel.py）
    storage.py  sqlite 账本（9 张表）

★ 下面这些是从朋友的「记账机器人专业版」搬过来的（MIT 协议，已获授权）
  搬过来时只改了三样，业务逻辑一行没动：
    1. 导入路径：services.xxx / storage.xxx → 现在这个包
    2. 去掉 PTB 依赖：telegram.xxx / asyncio → 面板自己的 api.call（同步）
    3. 模块级全局状态 → 实例属性（多个记账机器人同时跑不能串味）

  模块对应关系：
    storage.py        ← storage/repositories/ledger_storage.py   （9 张表，纯 sqlite）
    commands.py       ← services/ledger/ledger_commands.py       （核心记账逻辑）
    calculator.py     ← services/calculator.py                   （算式求值）
    text_utils.py     ← utils/text_utils.py                      （HTML 长消息切分）
    identity.py       ← services/ledger/message_identity.py      （取发言人是谁）
    price.py          ← services/price/price_service.py          （OKX 报价）
    trc20.py          ← services/trc20/verify_service.py         （地址核对图）
    bill_messages.py  ← services/ledger/bill_messages.py         （账单发送/翻页）
    reminders.py      ← services/ledger/cutoff_reminders.py      （日切提醒）
    group_admin.py    ← （自己写的）批量清理 / @全体 / 广播
    positions.py      ← （自己写的）群消息位置记忆

  ⚠ identity.py 目前全项目没人 import（死代码），留着是为了对照上游；要清就清。
"""
from .runner import LedgerRunner

__all__ = ['LedgerRunner']
