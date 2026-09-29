# -*- coding: utf-8 -*-
"""记每个群「机器人见过的最新消息 id」

为什么需要：TG 没有「拉群历史消息」的接口，删除只能**按 message_id 猜**。
所以得知道每个群当前大概到多少号了，才能从那儿往回删。

原版是挂了个自定义 HTTPXRequest 拦截**所有** API 响应来记；
面板这边更简单 —— `getUpdates` 回来的每条群消息顺手记一下就够了。

★ 只存一个数字，**不存消息内容**（隐私 + 体积）。
"""
from __future__ import annotations

import sqlite3
import threading


class GroupPositions:
    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.execute(
            'CREATE TABLE IF NOT EXISTS positions ('
            ' chat_id INTEGER PRIMARY KEY,'
            ' message_id INTEGER NOT NULL,'
            ' title TEXT,'
            ' last_seen TEXT)')
        self._db.commit()

    def record(self, chat_id, message_id, title=None):
        """记下这个群见过的最新 id（只增不减）"""
        try:
            cid, mid = int(chat_id), int(message_id or 0)
        except (TypeError, ValueError):
            return
        if cid >= 0 or mid <= 0:      # 只管群（group/supergroup 的 id 是负的）
            return
        with self._lock:
            try:
                self._db.execute(
                    'INSERT INTO positions (chat_id, message_id, title, last_seen)'
                    ' VALUES (?,?,?,datetime("now"))'
                    ' ON CONFLICT(chat_id) DO UPDATE SET'
                    '  message_id = MAX(message_id, excluded.message_id),'
                    '  title = COALESCE(excluded.title, positions.title),'
                    '  last_seen = excluded.last_seen',
                    (cid, mid, title))
                self._db.commit()
            except sqlite3.Error:
                pass

    def latest(self, chat_id):
        """这个群机器人见过的最新消息 id，没见过返回 0"""
        with self._lock:
            try:
                row = self._db.execute(
                    'SELECT message_id FROM positions WHERE chat_id=?',
                    (int(chat_id),)).fetchone()
            except (sqlite3.Error, TypeError, ValueError):
                return 0
        return int(row[0]) if row else 0

    def forget(self, chat_id):
        with self._lock:
            try:
                self._db.execute('DELETE FROM positions WHERE chat_id=?',
                                 (int(chat_id),))
                self._db.commit()
            except (sqlite3.Error, TypeError, ValueError):
                pass

    def close(self):
        try:
            self._db.close()
        except Exception:
            pass
