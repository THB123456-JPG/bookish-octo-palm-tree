# -*- coding: utf-8 -*-
"""群消息记录（本地版）

来源：朋友那套「审计」里的 `services/message_archive.py`。
★★ **只搬了本地归档，同步代理（`deploy/audit_agent.py`）整个不要** ——
   那个会把消息连图片打包 POST 到 `audit-enrollment.json` 里写死的服务器。
   这套代码**没有任何对外请求**，除了去 Telegram 自己那儿取图片文件。
   数据只落在你自己的 `data/` 目录里。

和原版的区别：
  · asyncio + httpx → 线程 + requests（面板是线程模型）
  · 原来靠 PTB 的 HTTPXRequest 钩子抓所有 API 响应 → 改成由 runner 喂进来
  · 保留期从写死的 24 小时 → 面板可配（默认 7 天）
  · 原版 `payload` 里存 `media_path` 这种本机绝对路径，跨机器看不了 ——
     照旧（本地用，够）
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ★★ 这个 import 以前**漏了**，而下面 550 多行在用它 ——
#    那几行在「下载图片」的后台线程里，而且嵌在 except 里：
#    一旦某张图下载失败，log(...) 抛 NameError → 被外层 except 接住 →
#    外层又调 log(...) 再炸一次 → **整个下载线程静默死掉**，
#    之后所有图都不再下（还查不出原因）。
#    （2026-09-29 加私聊清理时才暴露出来）
from core import log

TZ = timezone(timedelta(hours=8))
# ★ 保留期**固定 48 小时**（用户指定，不再让面板配）。
#   原版也是 24 小时左右 —— 这类"审计窗口"本来就该短，
#   留太久既占地方、又是隐私风险。
KEEP_HOURS = 48
MAX_MEDIA_BYTES = 10 * 1024 * 1024        # 超过 10MB 的图不存（原版就是这个限制）
MEDIA_SUFFIXES = {'.jpg', '.jpeg', '.png', '.webp'}

# 消息种类 → 中文标签（原样搬的）
KIND_LABELS = {
    'photo': '图片', 'document': '文件', 'video': '视频', 'voice': '语音',
    'audio': '音频', 'sticker': '贴纸', 'animation': '动图',
    'contact': '联系人', 'location': '位置',
    'new_chat_members': '成员加入', 'left_chat_member': '成员退出',
    'text': '消息',
}
KIND_ORDER = ('photo', 'document', 'video', 'voice', 'audio', 'sticker',
              'animation', 'contact', 'location',
              'new_chat_members', 'left_chat_member')


@contextlib.contextmanager
def _db(path, timeout=5):
    """开一个 sqlite 连接，用完**一定关掉**

    ★ 千万别写成 `with sqlite3.connect(...) as db:` ——
      那个上下文只管事务，**不关连接**，连接要等垃圾回收才释放。
      Windows 上文件会一直被占着：删不掉、换商户清不干净。
    """
    conn = sqlite3.connect(path, timeout=timeout)
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


def _now_iso():
    return datetime.now(TZ).isoformat()


def _reply_preview(message):
    sender = message.get('sender_chat') or message.get('from') or {}
    kind = next((k for k in KIND_ORDER if k in message), 'text')
    return dict(message_id=str(message['message_id']),
                display_name=sender.get('title') or ' '.join(filter(None, [sender.get('first_name'), sender.get('last_name')])),
                username=sender.get('username', ''), kind=kind,
                text=message.get('text') or message.get('caption') or '[%s]' % KIND_LABELS[kind],
                has_photo=bool(message.get('photo') or (message.get('document') or {}).get('mime_type', '').startswith('image/')),
                media='')


class MessageArchive:
    """一个机器人一个库：data/<bid>.archive.sqlite3

    ★ 图片存 data/<bid>.archive-media/，后台线程慢慢下（原版也是这么干的），
      下载走 Telegram 官方 getFile，不经过任何第三方。
    """

    def __init__(self, path, token='', keep_hours=KEEP_HOURS,
                 owner_id='', media_dir=None):
        self.path = Path(path)
        self.media = Path(media_dir) if media_dir else self.path.parent / (
            self.path.stem + '-media')
        self.token = token or ''
        self.keep_hours = max(1, int(keep_hours or KEEP_HOURS))
        self.owner_id = str(owner_id or '')
        self.stopped = threading.Event()
        self.wake = threading.Event()
        self.worker = None
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _db(self.path) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS events ('
                       ' chat_id TEXT, message_id TEXT, ts TEXT,'
                       ' payload TEXT, file_id TEXT,'
                       ' PRIMARY KEY(chat_id, message_id))')
            db.execute('CREATE INDEX IF NOT EXISTS events_time ON events(ts)')
            # ★★ 一次性清理：把**以前存进去的私聊消息**删掉
            #    （2026-09-29 用户要求「只记群聊」后补的 —— 光改写入过滤
            #      的话，之前那些私聊还挂在记录列表里，看着还是杂乱）
            #    用 user_version 做标记：**每个库文件只跑一次**，不会每开一次
            #    库就删一遍（这个 __init__ 每次读接口都会走）
            try:
                version = db.execute('PRAGMA user_version').fetchone()[0]
                if version < 1:
                    n = db.execute(
                        "SELECT COUNT(*) FROM events WHERE"
                        " json_extract(payload,'$.chat_type')='private'"
                    ).fetchone()[0]
                    if n:
                        db.execute("DELETE FROM events WHERE"
                                   " json_extract(payload,'$.chat_type')"
                                   "='private'")
                        log('清理私聊记录：删掉 %d 条（以后不再记录私聊）' % n)
                    db.execute('PRAGMA user_version=1')
            except sqlite3.Error as e:
                # 老版本 sqlite 没有 json 函数之类 —— 清不了就算了，
                # 写入那道过滤照样生效
                log('清理私聊记录失败（不影响新记录）：%s' % e)

    # ================= 收 =================
    def cutoff_iso(self):
        return (datetime.now(TZ) - timedelta(hours=self.keep_hours)).isoformat()

    def record_result(self, result, mute=None, is_own=False):
        """把 getUpdates 回来的结果写进库。result 可以是单条也可以是列表

        mute = 不想记的会话 id 集合（面板上按群「关闭记录」用的）。
        ★ 是**整个跳过**，不是存了再隐藏 —— 不然停记了还照样占地方。

        is_own=True 表示这是机器人自己发的（账单/回执）——
        标记成 is_bot，界面上单独一种样式，而且不算未读。
        """
        mute = {str(x) for x in (mute or ())}
        photo_pending = False
        messages = []
        for item in (result if isinstance(result, list) else [result]):
            if not isinstance(item, dict):
                continue
            message = next((item[k] for k in ('message', 'edited_message', 'channel_post', 'edited_channel_post') if k in item), item)
            reply = message.get('reply_to_message') or {}
            chat = message.get('chat') or {}
            if reply.get('message_id') and str((reply.get('chat') or chat).get('id')) == str(chat.get('id')):
                # Bot API includes the original message even when the poller missed it.
                messages.append((dict(reply, chat=chat), False))
            messages.append((message, is_own))
        with self._lock, _db(self.path, timeout=5) as db:
            for message, own in messages:
                chat = message.get('chat') or {}
                if (not chat.get('id') or not message.get('message_id')
                        or not message.get('date')):
                    continue
                # ★★ 私聊**一概不记录**（用户 2026-09-29 定的）。
                #   私聊机器人是「他自己在操作」：绑定、广播、查地址 ——
                #   混进群聊记录里全是噪音，翻起来找不到真正要看的东西。
                #   （只有记账类型有归档，客服那种靠私聊干活的不受影响）
                if (chat.get('type') or '') == 'private':
                    continue
                if str(chat['id']) in mute:
                    continue
                sender = message.get('sender_chat') or message.get('from') or {}
                name = sender.get('title') or ' '.join(
                    filter(None, [sender.get('first_name'),
                                  sender.get('last_name')]))
                photo = (message.get('photo') or [{}])[-1]
                document = message.get('document') or {}
                if not photo and document.get('mime_type') in (
                        'image/jpeg', 'image/png', 'image/webp'):
                    photo = document
                file_id = str(photo.get('file_id') or '')
                kind = next((k for k in KIND_ORDER if k in message), 'text')
                event = {
                    'chat_id': str(chat['id']),
                    'message_id': str(message['message_id']),
                    'chat_title': (chat.get('title') or chat.get('first_name')
                                   or str(chat['id'])),
                    'chat_type': chat.get('type') or '',
                    'ts': datetime.fromtimestamp(message['date'], TZ).isoformat(),
                    'display_name': name,
                    'username': sender.get('username', ''),
                    'user_id': sender.get('id', ''),
                    # 自己发的也标成 is_bot —— 归档里本来就分「人说的」和
                    # 「机器人说的」，界面上是两种样式
                    'is_bot': bool(own or sender.get('is_bot')),
                    'is_owner': bool(self.owner_id)
                                and str(sender.get('id')) == self.owner_id,
                    'kind': kind,
                    'text': (message.get('text') or message.get('caption')
                             or '[%s]' % KIND_LABELS[kind]),
                    # ★ 只存文件名 —— 存绝对路径的话换台机器/换个目录就看不了了
                    'media': '',
                    'has_photo': bool(file_id),
                    'edited': bool(message.get('edit_date')),
                }
                reply = message.get('reply_to_message') or {}
                if reply.get('message_id') and str((reply.get('chat') or chat).get('id')) == str(chat['id']):
                    event['reply'] = _reply_preview(reply)
                if event['ts'] < self.cutoff_iso():
                    continue        # 超过保留期的不存（原版是 24 小时）
                key = (event['chat_id'], event['message_id'])
                old = db.execute('SELECT payload,file_id FROM events'
                                 ' WHERE chat_id=? AND message_id=?',
                                 key).fetchone()
                if old and old[1] == file_id:
                    # 编辑过的消息：图没换就沿用已经下好的那张
                    event['media'] = json.loads(old[0]).get('media', '')
                db.execute('INSERT OR REPLACE INTO events VALUES(?,?,?,?,?)',
                           (*key, event['ts'],
                            json.dumps(event, ensure_ascii=False), file_id))
                photo_pending = photo_pending or bool(
                    file_id and not event['media'])
        if photo_pending:
            self.wake.set()

    def put_media(self, chat_id, message_id, file_id, data, suffix='.jpg'):
        """★★ 「客户自带服务器」专用入口：**外部下好的图片字节，直接落盘**

        为什么要单独开一个口：正常下载走 _download()，它得拿**本机这个机器人的
        token** 去 Telegram 要图。但客户是自己跑机器人的，图在他那边 ——
        而且 Telegram 的 file_id **不跨机器人通用**，我拿他的 file_id 永远下不到。
        所以：客户那边用自己的 token 下好，把字节传过来，走这里入库。

        ★ 文件名跟 _download 算的**一模一样**
          （`<chat_id>_<message_id>_<sha256(file_id)[:12]><后缀>`），
          所以传过来的图能被现成的 media_path()、`GET /api/archive/<bid>/media/<名>`
          和前端那个 <img> 直接认出来，那几处**一行都不用改**。

        返回 True = 收下了；False = 这条记录不在库里（没收到过，或者已经被清了）。
        """
        file_id = str(file_id or '')
        if not (file_id and data):
            return False
        try:
            cid, mid = int(chat_id), int(message_id)
        except (TypeError, ValueError):
            return False                      # 文件名里要做 int()，非数字直接拒
        if len(data) > MAX_MEDIA_BYTES:
            return False
        if suffix not in MEDIA_SUFFIXES:
            suffix = '.jpg'
        name = '%s_%s_%s%s' % (cid, mid,
                               hashlib.sha256(file_id.encode()).hexdigest()[:12],
                               suffix)
        self.media.mkdir(parents=True, exist_ok=True)
        with self._lock, _db(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload FROM events'
                             ' WHERE chat_id=? AND message_id=? AND file_id=?',
                             (str(cid), str(mid), file_id)).fetchone()
            if not row:
                # 没这条记录就别存图 —— 跟 _download 里那条判断一个道理，
                # 不然图会永远留在磁盘上没人清
                return False
            (self.media / name).write_bytes(data)
            event = json.loads(row[0])
            event['media'] = name
            # ★ 补上 media 之后，后台下载线程会因为 media != '' 自动跳过这条
            #   （见 _run 里那句 json_extract(payload,'$.media') = ''）
            db.execute('UPDATE events SET payload=?'
                       ' WHERE chat_id=? AND message_id=?',
                       (json.dumps(event, ensure_ascii=False),
                        str(cid), str(mid)))
        return True

    # ================= 清 =================
    def prune(self):
        """删过期消息和无引用图片，保留最近 48 小时记录。"""
        cutoff = self.cutoff_iso()
        with self._lock, _db(self.path) as db:
            db.execute('DELETE FROM events WHERE ts < ?', (cutoff,))
            keep = set()
            keep.update(row[0] for row in db.execute(
                "SELECT DISTINCT json_extract(payload,'$.media') FROM events "
                "WHERE COALESCE(json_extract(payload,'$.media'),'') != ''"))
            removed = 0
            if self.media.is_dir():
                for p in self.media.iterdir():
                    if p.is_file() and p.name not in keep:
                        try:
                            p.unlink()
                            removed += 1
                        except OSError:
                            pass
        return removed

    def forget_chat(self, chat_id):
        """把某个群的记录整个删掉（面板上的「清空这个群」）"""
        with self._lock, _db(self.path) as db:
            names = []
            for (payload,) in db.execute(
                    'SELECT payload FROM events WHERE chat_id=?', (str(chat_id),)):
                n = (json.loads(payload) or {}).get('media') or ''
                if n:
                    names.append(n)
            n = db.execute('DELETE FROM events WHERE chat_id=?',
                           (str(chat_id),)).rowcount
        self._unlink(names)
        return n

    def clear_all(self):
        with self._lock, _db(self.path) as db:
            n = db.execute('DELETE FROM events').rowcount
            db.execute('DELETE FROM events')
        self._unlink([p.name for p in self.media.iterdir()]
                     if self.media.is_dir() else [])
        return n

    def _unlink(self, names):
        for name in names:
            try:
                (self.media / name).unlink(missing_ok=True)
            except OSError:
                pass

    # ================= 读（给面板用）=================
    def _read_messages(self, db, rows):
        out = []
        for row in rows:
            payload = row[0]
            try:
                event = json.loads(payload)
            except (ValueError, TypeError):
                continue
            if len(row) > 1:
                event['archive_seq'] = row[1]
            reply = event.get('reply')
            if reply:
                original = db.execute('SELECT payload FROM events WHERE chat_id=? AND message_id=?',
                                      (event['chat_id'], reply['message_id'])).fetchone()
                if original:
                    original = json.loads(original[0])
                    reply['media'] = original.get('media', '')
                    if not reply.get('display_name'):
                        reply['display_name'] = original.get('display_name', '')
                        reply['username'] = original.get('username', '')
                    if reply.get('text') == '[消息]':
                        reply['text'] = original.get('text', reply['text'])
                        reply['kind'] = original.get('kind', 'text')
                        reply['has_photo'] = original.get('has_photo', False)
            out.append(event)
        return out

    def chats(self):
        """记录里出现过的群/会话，按最后发言时间倒序"""
        with self._lock, _db(self.path) as db:
            rows = db.execute(
                'SELECT chat_id, MAX(ts) AS last_ts, COUNT(*) AS n,'
                '       json_extract(payload,"$.chat_title") AS title,'
                '       json_extract(payload,"$.chat_type") AS ctype, MAX(rowid)'
                ' FROM events GROUP BY chat_id ORDER BY last_ts DESC'
            ).fetchall()
        return [{'chat_id': r[0], 'last_ts': r[1], 'count': r[2],
                 'title': r[3] or r[0], 'type': r[4] or '', 'last_seq': r[5]} for r in rows]

    def messages(self, chat_id='', keyword='', limit=200, before=''):
        """列消息。keyword 会在发言人和正文里找"""
        sql = 'SELECT payload, rowid FROM events WHERE 1=1'
        args = []
        if chat_id:
            sql += ' AND chat_id=?'
            args.append(str(chat_id))
        if before:
            sql += ' AND ts < ?'
            args.append(before)
        if keyword:
            sql += (' AND (payload LIKE ? OR payload LIKE ?)')
            kw = '%' + keyword + '%'
            args += [kw, kw]
        # ★★ 排序必须带 message_id 兜底：ts 只精确到**秒**，
        #    机器人同一秒里回的话（`bj` 和它的报价就是）ts 完全一样 ——
        #    只按 ts 排的话这两条的先后是**不确定的**，实测把机器人的
        #    排到了提问前面（2026-09-29 用户截图报的）。
        #    群里的 message_id 是递增的，就是真实先后。
        #    CAST 成整数：message_id 是 TEXT 存的，直接比会变成字符串排序
        #    （'9' > '10'，那就更乱了）
        sql += ' ORDER BY ts DESC, CAST(message_id AS INTEGER) DESC LIMIT ?'
        args.append(max(1, min(int(limit or 200), 1000)))
        with self._lock, _db(self.path) as db:
            rows = db.execute(sql, args).fetchall()
            return self._read_messages(db, rows)

    def messages_since(self, after, chat_id='', limit=200, after_seq=None):
        """只取比 after 新的消息 —— 页面「实时刷新」用

        ★ 用 `ts >= after`（含等于）：同一秒里可能有好几条，
          用 `>` 会漏掉和游标同一秒的后半截。
          代价是可能重复返回游标那条 —— 客户端按 (会话,消息id) 去重，很简单。
        """
        if after_seq is not None:
            with self._lock, _db(self.path) as db:
                cursor = max(0, int(after_seq))
                newest = db.execute('SELECT MAX(rowid) FROM events').fetchone()[0] or 0
                if cursor > newest:
                    cursor = 0
                sql = 'SELECT payload, rowid FROM events WHERE rowid > ?'
                args = [cursor]
                if chat_id:
                    sql += ' AND chat_id=?'
                    args.append(str(chat_id))
                sql += ' ORDER BY rowid ASC LIMIT ?'
                args.append(max(1, min(int(limit or 200), 500)))
                rows = db.execute(sql, args).fetchall()
                return self._read_messages(db, rows[::-1])
        if not after:
            return self.messages(chat_id=chat_id, limit=limit)
        sql = 'SELECT payload FROM events WHERE ts >= ?'
        args = [after]
        if chat_id:
            sql += ' AND chat_id=?'
            args.append(str(chat_id))
        # ★ 跟 messages() 一样带上 message_id 兜底，不然同一秒里的
        #   两条（提问 + 机器人的回复）先后不定，实时刷新时会跳来跳去
        sql += ' ORDER BY ts DESC, CAST(message_id AS INTEGER) DESC LIMIT ?'
        args.append(max(1, min(int(limit or 200), 500)))
        with self._lock, _db(self.path) as db:
            rows = db.execute(sql, args).fetchall()
            return self._read_messages(db, rows)

    def members(self, chat_id='', limit=3000):
        """这个群里**出现过的人**：名字 + 用户名（按发言多少排）

        ★★ 为什么是这个口径（别再想「拉全量成员」了）：
          **Telegram 不让机器人拉群成员全表** —— Bot API 只有
          `getChatMemberCount`（个数）和 `getChatAdministrators`（管理员），
          **没有「列出所有成员」这个方法**。
          所以这里给的是**机器人见过发言的人**，名字和用户名都取自他的消息
          —— 没发过言的人，机器人根本不知道他存在。

        ★ 为什么用归档当来源：它同时覆盖两种机器人 ——
          面板自己跑的那种、和「客户自建」那台（后者的数据库在**客户的
          服务器上**，面板这边只有归档）。别的来源都只能覆盖一半。

        ★ 从**最新**那条开始遍历，所以每个用户第一次出现时拿到的
          就是最新的名字（改过名的显示新名）。
        """
        sql = 'SELECT payload FROM events'
        args = []
        if chat_id:
            sql += ' WHERE chat_id=?'
            args.append(str(chat_id))
        sql += ' ORDER BY ts DESC, CAST(message_id AS INTEGER) DESC LIMIT ?'
        args.append(max(1, min(int(limit or 3000), 5000)))
        with self._lock, _db(self.path) as db:
            rows = db.execute(sql, args).fetchall()
        people = {}
        for (payload,) in rows:
            try:
                m = json.loads(payload)
            except (ValueError, TypeError):
                continue
            uid = str(m.get('user_id') or '')
            if not uid:
                continue
            p = people.get(uid)
            if p is None:
                people[uid] = {'user_id': uid,
                               'display_name': m.get('display_name') or '',
                               'username': m.get('username') or '',
                               'is_bot': bool(m.get('is_bot')),
                               'count': 1,
                               'last_ts': m.get('ts') or ''}
            else:
                p['count'] += 1
                if (m.get('ts') or '') > p['last_ts']:
                    p['last_ts'] = m.get('ts') or ''
        out = list(people.values())
        # ★ 人排前面、机器人垫底；同样是人就按发言多少排
        out.sort(key=lambda x: (x['is_bot'], -x['count'],
                                x['display_name'] or ''))
        return out

    def unread_counts(self, seen):
        """每个会话有多少条没看过

        seen = {chat_id: 看到哪一刻的时间}（页面存在 localStorage 里）
        没出现在 seen 里的会话 = 从没点开过 → 全部算未读。

        ★ 第一次打开页面时前端会先把所有会话标成「已读」当基线，
          不然一堆历史记录全顶着红点，反而看不出真正的新消息。
        """
        seen = seen if isinstance(seen, dict) else {}
        out = {}
        with self._lock, _db(self.path) as db:
            # ★ 机器人自己发的（账单/回执）不算未读 ——
            #   它是对别人消息的反应，不是新来的信息。
            #   算进去的话，群里聊 100 句、机器人回 50 句，未读显示 150，
            #   完全看不出到底有多少条是真的没看
            rows = db.execute(
                "SELECT chat_id, ts, rowid FROM events"
                " WHERE COALESCE(json_extract(payload,'$.is_bot'),0) != 1"
            ).fetchall()
            newest_seq = db.execute('SELECT MAX(rowid) FROM events').fetchone()[0] or 0
        for chat_id, ts, seq in rows:
            mark = seen.get(chat_id) or ''
            if isinstance(mark, dict):
                try:
                    cursor = int(mark.get('seq', 0))
                except (ValueError, TypeError):
                    cursor = 0
                unread = seq > (cursor if cursor <= newest_seq else 0)
            else:
                unread = ts > str(mark)
            if unread:
                out[chat_id] = out.get(chat_id, 0) + 1
        return out

    def baseline(self, with_seq=False):
        """每个会话「到现在为止」的时间点 —— 第一次打开页面时当已读基线用

        不这么做的话，一进来所有历史记录全顶着红点，真正的新消息反而看不出来。
        """
        with self._lock, _db(self.path) as db:
            rows = db.execute(
                'SELECT chat_id, MAX(ts), MAX(rowid) FROM events GROUP BY chat_id').fetchall()
        return {r[0]: ({'ts': r[1] or '', 'seq': r[2]} if with_seq else r[1] or '') for r in rows}

    def newest_ts(self):
        """目前库里最新一条的时间（页面开轮询时的起点）"""
        with self._lock, _db(self.path) as db:
            row = db.execute('SELECT MAX(ts) FROM events').fetchone()
        return (row[0] if row else '') or ''

    def stats(self):
        with self._lock, _db(self.path) as db:
            n, first, last = db.execute(
                'SELECT COUNT(*), MIN(ts), MAX(ts) FROM events').fetchone()
        size = 0
        if self.media.is_dir():
            for p in self.media.iterdir():
                try:
                    size += p.stat().st_size
                except OSError:
                    pass
        return {'count': n or 0, 'first': first or '', 'last': last or '',
                'media_bytes': size}

    def media_path(self, name):
        """安全地取图片路径 —— 只认文件名的，防目录穿越"""
        if not name:
            return None
        name = os.path.basename(name)
        p = self.media / name
        try:
            if p.is_file() and p.resolve().parent == self.media.resolve():
                return p
        except OSError:
            pass
        return None

    # ================= 图片下载（后台）=================
    def start(self):
        if self.worker is None and self.token:
            self.worker = threading.Thread(target=self._run,
                                           name='archive-media', daemon=True)
            self.worker.start()

    def close(self):
        self.stopped.set()
        self.wake.set()
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=3)

    def _run(self):
        # ★★ 千万别像 ApiTrx 那样写 `sess.trust_env = False`！
        #   那边必须直连（走代理会被 Cloudflare 挡成 429），
        #   但 Telegram 这边**必须走代理** —— 关掉就是连不上 api.telegram.org，
        #   表现成「图片永远显示下载中」。踩过。
        #   这里跟 TgAPI 保持一致：用 requests 默认设置（自动读系统代理）。
        sess = requests.Session()
        next_prune = 0
        while not self.stopped.is_set():
            self.wake.clear()
            try:
                if time.monotonic() >= next_prune:
                    self.prune()
                    next_prune = time.monotonic()+60
                # 只挑「还没下到图」的 —— 不然最新的 30 条都有图时，
                # 更早那些没下来的永远轮不到
                with self._lock, _db(self.path) as db:
                    rows = db.execute(
                        "SELECT chat_id,message_id,file_id FROM events"
                        " WHERE file_id != ''"
                        "   AND COALESCE(json_extract(payload,'$.media'),'') = ''"
                        " ORDER BY ts DESC LIMIT 30"
                    ).fetchall()
                failed = 0
                for chat_id, message_id, file_id in rows:
                    if self.stopped.is_set():
                        break
                    try:
                        self._download(sess, chat_id, message_id, file_id)
                    except Exception as e:
                        failed += 1
                        # ★ 只报第一次，别把日志刷爆 —— 但**一定要报**，
                        #   静默吞掉的话「图片一直下载中」根本无从查起
                        if failed == 1:
                            log('[归档] 取图片失败（%s）：%s'
                                % (self.path.stem, str(e)[:120]))
            except Exception as e:
                log('[归档] 维护出错（%s）：%s' % (self.path.stem, str(e)[:120]))
            self.wake.wait(30)

    def _has_media(self, chat_id, message_id):
        with self._lock, _db(self.path) as db:
            row = db.execute('SELECT payload FROM events'
                             ' WHERE chat_id=? AND message_id=?',
                             (chat_id, message_id)).fetchone()
        if not row:
            return True
        name = (json.loads(row[0]) or {}).get('media') or ''
        return bool(name) and (self.media / name).is_file()

    def _download(self, sess, chat_id, message_id, file_id):
        r = sess.post('https://api.telegram.org/bot%s/getFile' % self.token,
                      json={'file_id': file_id}, timeout=15)
        r.raise_for_status()
        result = (r.json() or {}).get('result') or {}
        remote = result.get('file_path') or ''
        if not remote or (result.get('file_size') or 0) > MAX_MEDIA_BYTES:
            return
        suffix = Path(remote).suffix.lower()
        if suffix not in MEDIA_SUFFIXES:
            return
        # ★★ 必须用 sess.request(..., stream=True)，**不能**用 sess.stream(...)：
        #   在 requests 2.34 里 `Session.stream` 是个**布尔属性**（不是方法），
        #   调它就报「'bool' object is not callable」。踩过。
        with sess.request('GET', 'https://api.telegram.org/file/bot%s/%s'
                          % (self.token, remote), timeout=30,
                          stream=True) as stream:
            stream.raise_for_status()
            data = bytearray()
            for chunk in stream.iter_content(65536):
                data.extend(chunk)
                if len(data) > MAX_MEDIA_BYTES:
                    return
        version = hashlib.sha256(file_id.encode()).hexdigest()[:12]
        name = '%s_%s_%s%s' % (int(chat_id), int(message_id), version, suffix)
        self.media.mkdir(parents=True, exist_ok=True)
        with self._lock, _db(self.path) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload FROM events'
                             ' WHERE chat_id=? AND message_id=? AND file_id=?',
                             (chat_id, message_id, file_id)).fetchone()
            if not row:
                return            # 记录已经被清了，图就别存了
            (self.media / name).write_bytes(data)
            event = json.loads(row[0])
            event['media'] = name
            db.execute('UPDATE events SET payload=?'
                       ' WHERE chat_id=? AND message_id=?',
                       (json.dumps(event, ensure_ascii=False),
                        chat_id, message_id))
