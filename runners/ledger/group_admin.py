# -*- coding: utf-8 -*-
"""群管理：批量清理消息、@全体、群成员统计、入群欢迎

原版这几个功能散在 `handlers/` + `services/group/` + `services/broadcast/` 里，
而且全是 asyncio 的（`await bot.delete_messages(...)`）。
面板是**线程模型 + requests 同步调用**，所以这里全部改写成同步函数，
判断逻辑（48 小时限制、权限判断、错开再试）**原样保留**。

★ 这里只放纯逻辑，不碰线程、不碰 runner —— 方便单测。
"""
from __future__ import annotations

import json
import time
from customer_config import DEFAULT_WELCOME as DEFAULT_NEWBIE_WELCOME

# 群消息 48 小时后 TG 不让删；这几个错误说明「这条删不掉」，跳过继续
# （注意：不能因为某条删不掉就认为后面的也删不掉）
SKIP_ERRORS = (
    "message can't be deleted",
    "message to delete not found",
    "message_id_invalid",
    "message can not be deleted",
)

WELCOME_COOLDOWN = 300      # 同个群 5 分钟内只欢迎一次（机器人被反复拉进拉出会刷屏）
NOTIFY_COOLDOWN = 300       # @全体 冷却
NOTIFY_CHUNK = 50           # 每条消息最多 @ 多少人（TG 单条上限）
DELETE_BATCH = 100          # 每次 deleteMessages 最多删多少条

DEFAULT_WELCOME = (
    "🎉 群记账机器人已加入本群\n\n"
    "主要功能：记账、账单切换、广播、群通知、消息清理、地址核对图片。\n"
    "发送 /使用说明 查看完整说明。\n\n"
    "默认汇率：1\n默认费率：0%\n默认日切：每天 03:00（北京时间）"
)

# ★★★ 注意别跟上面那个搞混 —— 这俩是**两件事**（2026-10-08 加）：
#   DEFAULT_WELCOME      机器人**自己被拉进群**时说的话（面板里能配）
#   DEFAULT_NEWBIE_WELCOME  **新成员进群**时欢迎他（主人发「设置欢迎语」配）
#   `{name}` 会换成那个人的可点击名字。


# ================= 权限 =================
def can_delete_group_messages(member, chat_type):
    """机器人能不能删这个群的消息

    member 是 getChatMember 返回的 dict：
      群主 → 一定能；管理员 → 普通群可以，超级群要看 can_delete_messages
    """
    if not member:
        return False
    status = member.get('status') or ''
    if status == 'creator':
        return True
    if status != 'administrator':
        return False
    return chat_type == 'group' or member.get('can_delete_messages') is True


def is_human_member(user, bot_id):
    """过滤掉机器人自己和其它机器人（欢迎/离开提示只管真人）"""
    if not user:
        return False
    if int(user.get('id') or 0) == int(bot_id or 0):
        return False
    return not user.get('is_bot')


# ================= 文本 =================
def extract_notify_all_text(text):
    """「通知所有人 内容」→ 「内容」。只发命令不带内容时返回空串"""
    stripped = (text or '').strip()
    for command in ('通知所有人', '/notify_all', '/at_all', '@全体', '@所有人'):
        if stripped == command:
            return ''
        if stripped.startswith(command):
            return stripped[len(command):].strip()
    return ''


def extract_broadcast_text(text, command):
    """「/broadcast 内容」→ 「内容」"""
    stripped = (text or '').strip()
    if stripped == command:
        return ''
    if stripped.startswith(command):
        return stripped[len(command):].lstrip(' \t\r\n')
    return stripped


def chunked(values, size):
    return [values[i:i + size] for i in range(0, len(values), size)]


def bounded_lines(lines, limit=3000):
    """拼多行文本，超长就截断 —— 免得一条消息超 TG 的 4096 上限被整条拒收"""
    result = []
    length = 0
    for line in lines:
        if length + len(line) > limit:
            result.append('……内容较多，返回群列表可查看全部已选群。')
            break
        result.append(line)
        length += len(line) + 1
    return '\n'.join(result)


# ================= 群列表 / 选择键盘 =================
def group_titles(groups):
    """{chat_id: 标题}。重名的群补上 id，否则选的时候分不清"""
    titles = {}
    for row in groups:
        cid = int(row['chat_id'])
        titles[cid] = (row['title'] or str(cid))
    counts = {}
    for t in titles.values():
        counts[t] = counts.get(t, 0) + 1
    return {cid: ('%s（%s）' % (t, cid) if counts[t] > 1 else t)
            for cid, t in titles.items()}


def group_selection_keyboard(groups, selected, prefix):
    """一个群一行，打勾多选 + 下一步/取消"""
    rows = []
    for cid, title in group_titles(groups).items():
        mark = '√' if cid in selected else '□'
        rows.append([{'text': '%s %s' % (mark, title),
                      'callback_data': '%s:toggle:%s' % (prefix, cid)}])
    rows.append([{'text': '下一步', 'callback_data': '%s:next' % prefix},
                 {'text': '取消', 'callback_data': '%s:cancel' % prefix}])
    return {'inline_keyboard': rows}


def selection_keyboard(groups, selected, page, prefix, page_size=20):
    """清理菜单用：带翻页的群选择键盘"""
    page_groups = groups[page * page_size:(page + 1) * page_size]
    rows = list(group_selection_keyboard(page_groups, selected, prefix)
                ['inline_keyboard'])
    nav = []
    if page:
        nav.append({'text': '上一页', 'callback_data': '%s:page:%d' % (prefix, page - 1)})
    if (page + 1) * page_size < len(groups):
        nav.append({'text': '下一页', 'callback_data': '%s:page:%d' % (prefix, page + 1)})
    if nav:
        rows.insert(-1, nav)
    return {'inline_keyboard': rows}


# ================= @全体 =================
def mention_html(row):
    """把 known_users 的一行变成能 @ 到的 HTML

    有用户名就用 @xxx（能点），没用户名只能用 tg://user?id= 的隐藏链接。
    """
    from html import escape
    username = (row['username'] or '').strip()
    if username:
        return '@' + escape(username.lstrip('@'))
    name = escape((row['display_name'] or '').strip() or str(row['user_id']))
    return '<a href="tg://user?id=%d">%s</a>' % (int(row['user_id']), name)


def member_mention_html(user, tg_link=True):
    """新成员入群的欢迎语里 @ 他（这里拿到的是 TG 的 user dict）

    ★ tg_link=False 时**不用 tg://user 链接**（有用户名的照样用 t.me 链接）——
      这种链接在**图片说明（caption）里点了没反应**，发出来就是一段
      没用的下划线文字，还不如老老实实写个名字。
    """
    from html import escape
    name = ' '.join(x for x in (user.get('first_name'), user.get('last_name')) if x)
    name = name or user.get('username') or '新成员'
    escaped = escape(str(name))
    username = str(user.get('username') or '').lstrip('@')
    if username:
        return '<a href="https://t.me/%s">%s</a>' % (escape(username), escaped)
    if not tg_link:
        return escaped
    return '<a href="tg://user?id=%d">%s</a>' % (int(user.get('id')), escaped)


# ================= 删除消息 =================
def _is_skippable(exc):
    s = str(exc).lower()
    return any(k in s for k in SKIP_ERRORS)


def delete_batch(api, chat_id, ids, chat_type='supergroup', bot_id=None):
    """删一批 id。返回 True 表示这批处理完了（可能有跳过的）

    删不掉的处理方式和原版一致：
      · 「消息不存在 / 不可删」→ 二分拆小继续，拆到单条就跳过
      · 权限没了 → 直接抛出去，别白刷几百次 API
    """
    if not ids:
        return True
    try:
        api.call('deleteMessages', chat_id=chat_id,
                 message_ids=json.dumps(list(ids)))
        return True
    except Exception as e:
        if not _is_skippable(e):
            raise
        # 先确认是不是权限没了 —— 权限没了就别再试了
        # ★ 查权限失败（网络/限流）**不等于**没权限：那种情况要接着往下二分，
        #   否则一次抖动就把整个清理中断了
        if bot_id:
            try:
                member = api.call('getChatMember', chat_id=chat_id, user_id=bot_id)
            except Exception:
                member = None
            if member is not None and not can_delete_group_messages(member, chat_type):
                raise
        if len(ids) > 1:
            mid = len(ids) // 2
            delete_batch(api, chat_id, ids[:mid], chat_type, bot_id)
            delete_batch(api, chat_id, ids[mid:], chat_type, bot_id)
        return True


def delete_range(api, chat_id, first_id, last_id, chat_type='supergroup',
                 bot_id=None, latest=None, on_progress=None, stop=None):
    """删 [first_id, last_id] 这段 id

    ★ 没有历史消息 API，只能按 id 从大到小**猜着删** —— 这正是原版的做法：
      删不到的 id（不存在/超48小时）会被二分跳过去，不影响别的。

    返回「已经处理到的最大 id」。中途 stop() 返回 True 就停下，返回当前进度。
    """
    end = last_id
    while True:
        if stop and stop():
            return end
        # 清理期间群里还在发言 → 把新消息也带上
        if latest:
            newest = latest()
            if newest > last_id:
                delete_range(api, chat_id, last_id + 1, newest, chat_type,
                             bot_id, latest, on_progress, stop)
                last_id = newest
        if end < first_id:
            return last_id
        low = max(first_id - 1, end - DELETE_BATCH)
        delete_batch(api, chat_id, list(range(end, low, -1)), chat_type, bot_id)
        if on_progress:
            on_progress(end)
        end -= DELETE_BATCH
        time.sleep(0.05)     # 别把限流撞爆


def check_permission(api, chat_id, chat_type, bot_id):
    """机器人有没有删消息权限。拿不到信息时返回 None（不确定，不是「没有」）"""
    try:
        member = api.call('getChatMember', chat_id=chat_id, user_id=bot_id)
    except Exception:
        return None
    return can_delete_group_messages(member, chat_type)
