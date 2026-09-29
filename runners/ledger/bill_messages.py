# -*- coding: utf-8 -*-
"""账单消息的发送与翻页

原版是 PTB 的 async 写法，这里改成面板自己的 `api.call`（同步）。

保留了原作者的几个容错细节：
  · 第一块带按钮，后面的块不带（否则每块都有按钮）
  · 编辑时忽略「内容没变」的报错
  · 旧页删不掉时（超出 TG 删除时限）把内容改成「—」占位
"""
from __future__ import annotations

from core import TgError


def _mid(resp):
    """从 sendMessage 的返回里取 message_id

    ★ 面板的 api.call() 返回的**已经是 result 本身**（不是整个响应），
      所以直接取 message_id，别再套一层 'result'。
    """
    try:
        return (resp or {}).get('message_id')
    except Exception:
        return None


def _edit(api, chat_id, message_id, text, reply_markup):
    try:
        api.call('editMessageText', chat_id=chat_id, message_id=message_id,
                 text=text, parse_mode='HTML', disable_web_page_preview=True,
                 reply_markup=reply_markup)
    except TgError as e:
        # 「message is not modified」是正常的（点的还是当前页），别当错
        if 'not modified' not in str(e).lower():
            pass


def send_bill(api, chat_id, chunks, reply_markup, store):
    """发账单（可能分多块）。返回第一块的消息 id —— 翻页时要靠它定位"""
    root_id = None
    for i, chunk in enumerate(chunks):
        try:
            r = api.call('sendMessage', chat_id=chat_id, text=chunk,
                         parse_mode='HTML', disable_web_page_preview=True,
                         reply_markup=(reply_markup if i == 0 else None))
        except TgError:
            continue
        mid = _mid(r)
        if i == 0:
            root_id = mid
        elif reply_markup is not None and mid:
            store.remember_bill_page(chat_id, root_id, mid)
    return root_id


def replace_bill(api, chat_id, root_id, chunks, reply_markup, store):
    """点了翻页/切模式：改第一条，把之前多出来的页清掉"""
    try:
        pages = store.bill_pages(chat_id, root_id)
    except Exception:
        pages = []

    _edit(api, chat_id, root_id, chunks[0], reply_markup)

    for pid in pages:
        try:
            api.call('deleteMessage', chat_id=chat_id, message_id=pid)
        except TgError as e:
            # 删不掉（多半是超出时限了）→ 把内容改成「—」，别留一堆旧账单
            if 'not found' not in str(e).lower():
                _edit(api, chat_id, pid, '—', None)
        try:
            store.forget_bill_page(chat_id, root_id, pid)
        except Exception:
            pass

    for chunk in chunks[1:]:
        try:
            r = api.call('sendMessage', chat_id=chat_id, text=chunk,
                         parse_mode='HTML', disable_web_page_preview=True)
        except TgError:
            continue
        mid = _mid(r)
        if mid:
            store.remember_bill_page(chat_id, root_id, mid)
