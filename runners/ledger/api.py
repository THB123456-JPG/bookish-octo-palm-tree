# -*- coding: utf-8 -*-
"""记账机器人的面板接口 —— 实现部分

包含两组接口：
    /api/ledger/*   记账自己的配置
    /api/archive/*  群消息记录（★ 目前只有记账类型有这个功能，
                    见 core.ARCHIVE_TYPES）

★ 路由的判断条件在 panel.py 里，这里只放**实现**。
  分开放是为了：改记账的接口，一个字都不会碰到商城 / 客服的代码。

★ 约定：每个 handle_xxx(h, path[, me]) 返回 **True = 这个请求我处理了**，
  返回 False = 没匹配上，panel.py 继续往下找。
  h 就是 panel.PanelHandler 实例。

★★ 消息记录是**管理端专属**，商户一律看不到 —— 记录里是群聊内容，
   只给运营者自己看。两道防线：① 这里 _admin_only 拦 ② 前端对商户不渲染。
   改这里的时候别把 _admin_only 去掉。
"""
import json
import os
from urllib.parse import parse_qs, urlparse

import requests

import core
from core import log
# 常数时间比较，回传密钥用它（★ 这个 import 只在面板里用；
# 客户那份精简版**不含本文件**，所以不影响「记账对框架零依赖」）
from merchants import safe_eq


TG_TIMEOUT = 8          # 问 Telegram 要管理员名单时最多等这么久


def _tg_admins(token, chat_id):
    """用机器人自己的 token 问 Telegram 要**管理员名单**。

    ★★ 这是 Bot API 唯一能拿到「**没发过言的人**」的口子：
      `getChatAdministrators` 会把群主和管理员**连名字带用户名**一起给你，
      哪怕他们一句话都没说过。
      （**没有**「列出所有群成员」这个方法 —— 普通成员只有他发了言，
        机器人才能知道他的 id 和用户名。任何机器人都一样，不是我们的问题）

    ★ 故意**不用 core.TgAPI**：那个是给业务用的，带 3 次重试 + 70 秒超时 ——
      点一下成员表要等好几分钟。这里要的是「快问快答」。
    ★ 任何失败都返回空列表 —— 这只是锦上添花，绝不能因为网络问题
      让整个成员表打不开。
    """
    if not token or not chat_id:
        return []
    try:
        r = requests.post(
            core.API_URL.format(token=token, method='getChatAdministrators'),
            json={'chat_id': int(chat_id)}, timeout=TG_TIMEOUT)
        data = r.json()
    except Exception as e:
        log('问 Telegram 要管理员名单失败（不影响成员表）：%s' % e)
        return []
    if not data.get('ok'):
        log('取群管理员被拒：%s' % str(data.get('description'))[:80])
        return []
    out = []
    for row in (data.get('result') or []):
        if row.get('is_anonymous'):
            # ★ 匿名管理员返回的是个**假 id**（GroupAnonymousBot），
            #   放进去只会误导 —— 名字都显示不出来
            continue
        u = row.get('user') or {}
        if not u.get('id'):
            continue
        out.append({
            'user_id': str(u['id']),
            'display_name': ' '.join(x for x in (u.get('first_name'),
                                                 u.get('last_name')) if x),
            'username': u.get('username') or '',
            'is_bot': bool(u.get('is_bot')),
            'is_admin': True,
            # ★ 排位：群主（creator）排在其他管理员前面 ——
            #   不排的话同一档里就按名字的码位排，出来「管理」在「群主」上面，
            #   看着莫名其妙
            'admin_rank': 0 if row.get('status') == 'creator' else 1,
            'admin_title': (row.get('custom_title')
                            or ('群主' if row.get('status') == 'creator'
                                else '管理员')),
        })
    return out


def _owner_of(bot):
    """这台机器人的「主人」id（字符串）；没有就是空串。

    ★ 跟 `core.owner_id()` 用**同一套兜底**：没有 owner_id 就把第一个
      绑定的人当主人。两边口径不一致的话，同一条消息在「面板自己跑的
      机器人」和「客户自建那台」上会左右不一样。
    """
    b = bot or {}
    oid = str(b.get('owner_id') or '')
    if not oid:
        ids = b.get('admin_ids') or []
        oid = str(ids[0]) if ids else ''
    return oid


def _fix_owner(rows, bot):
    """把 is_owner 按**当前**的 owner_id 重算一遍。

    ★★ 为什么非要在读的时候算，而不是写的时候定死：
       `is_owner` = 「这条是不是机器人主人发的」，前端靠它把主人的话
       放到右边（像微信里自己的消息）。
       可是它的值是在**写库那一刻**定死的，而客户自建那台的回传路径
       （`_ingest_archive`）建库时**没传 owner_id** ——
       于是主人（灰产王）所有消息的 is_owner 都是 False，
       全跑到左边去了。**前端那段「主人靠右」的代码从来没生效过**
       （2026-09-29 用户截图报的：主人说话在左边）。

    ★ 读的时候重算还有个好处：**连之前存错的历史消息一起修好**，
      不用改库、不用写迁移脚本 —— 打开页面就是对的。
    """
    oid = _owner_of(bot)
    if not oid:
        return rows
    for r in rows:
        r['is_owner'] = str(r.get('user_id') or '') == oid
    return rows


def handle_get(h, path):
    """GET /api/ledger/*  和  /api/archive/*"""

    # ================= 群消息记录 =================
    if path.startswith('/api/archive/'):
        me = h._gate()
        if me is None:
            return True
        if not h._admin_only(me):
            return True
        parts = path.strip('/').split('/')      # api/archive/<bid>/<what>/...
        if len(parts) < 4:
            h._send(404, 'Not Found', 'text/plain; charset=utf-8')
            return True
        bid, what = parts[2], parts[3]
        bot = h._bot_of(me, bid)
        if bot is None:
            return True
        if what == 'config':
            h._json({'ok': True, 'config': bot.get('archive') or {},
                     'keep_days_default': 7})
            return True
        arc = h._archive_of(bid)
        if arc is None:
            # 文件还没建（没开记录 / 一条都没收到）—— 统一回空，
            # 前端不用分情况，实时轮询也不会报错
            h._json({'ok': True, 'chats': [], 'messages': [],
                     'newest': '',
                     'stats': {'count': 0, 'media_bytes': 0},
                     'empty': True})
            return True
        if what == 'chats':
            h._json({'ok': True, 'chats': arc.chats(), 'stats': arc.stats()})
            return True
        # 群成员（点消息流表头那个「👥 群成员」标签看的就是这个）
        # ★ 口径见 archive.members()：Telegram 不给拉全量成员，
        #   所以这里是「机器人见过发言的人」。
        if what == 'members':
            q = parse_qs(urlparse(h.path).query)
            chat = (q.get('chat') or [''])[0]
            rows = arc.members(chat_id=chat)
            by_id = {r['user_id']: r for r in rows}
            # ★★ 补上**没发过言的管理员** —— 只有 Telegram 知道他们在群里
            #    （普通潜水成员没有任何办法拿到，见 _tg_admins 的说明）
            for a in _tg_admins(bot.get('token'), chat):
                cur = by_id.get(a['user_id'])
                if cur is None:
                    rows.append(dict(a, count=0, last_ts=''))
                else:
                    cur['is_admin'] = True
                    cur['admin_title'] = a['admin_title']
                    cur['admin_rank'] = a['admin_rank']
            oid = _owner_of(bot)
            for r in rows:
                # ★ 主人标记跟消息列表用同一套口径（不然同一个人在
                #   消息里靠右、在成员表里却没标，看着自相矛盾）
                r['is_owner'] = bool(oid) and r['user_id'] == oid
            # ★ 管理员排最前（他们才是「管事的人」，哪怕从没发过言），
            #   群里是「群主 → 其他管理员 → 发过言的 → 机器人」这个顺序
            rows.sort(key=lambda x: (x['is_bot'], not x.get('is_admin'),
                                     x.get('admin_rank', 9),
                                     -x['count'],
                                     x.get('display_name') or ''))
            h._json({'ok': True, 'members': rows})
            return True
        if what == 'messages':
            q = parse_qs(urlparse(h.path).query)
            rows = arc.messages(
                chat_id=(q.get('chat') or [''])[0],
                keyword=(q.get('q') or [''])[0].strip(),
                limit=(q.get('limit') or ['200'])[0],
                before=(q.get('before') or [''])[0])
            h._json({'ok': True, 'messages': _fix_owner(rows, bot)})
            return True
        # 只取比 after 新的 —— 页面每几秒拉一次，实现「实时」
        if what == 'since':
            q = parse_qs(urlparse(h.path).query)
            after = (q.get('after') or [''])[0]
            rows = arc.messages_since(
                after,
                chat_id=(q.get('chat') or [''])[0],
                limit=(q.get('limit') or ['200'])[0],
                after_seq=(q.get('after_seq') or [None])[0])
            h._json({'ok': True,
                     'messages': _fix_owner(rows, bot),
                     'newest': max((r.get('ts', '') for r in rows), default=after),
                     'stats': arc.stats()})
            return True
        # 每个会话的未读数。seen 是前端存在 localStorage 的 {会话id: 看到哪一刻}
        if what == 'unread':
            q = parse_qs(urlparse(h.path).query)
            try:
                seen = json.loads((q.get('seen') or ['{}'])[0] or '{}')
            except (ValueError, TypeError):
                seen = {}
            if not isinstance(seen, dict):
                seen = {}
            # 会话特别多时别让 URL 无限长
            if len(seen) > 300:
                seen = dict(list(seen.items())[:300])
            h._json({'ok': True,
                     'unread': arc.unread_counts(seen),
                     'newest': arc.newest_ts()})
            return True
        if what == 'ping':
            # 空库也能拿到游标（页面刚打开、一条消息都还没有时用）
            h._json({'ok': True, 'newest': arc.newest_ts(),
                     'stats': arc.stats()})
            return True
        if what == 'media' and len(parts) >= 5:
            p = arc.media_path(parts[4])
            if p is None:
                h._send(404, 'Not Found', 'text/plain; charset=utf-8')
                return True
            ctype = {'.png': 'image/png', '.webp': 'image/webp',
                     '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg'}.get(
                         p.suffix.lower(), 'application/octet-stream')
            with open(p, 'rb') as f:
                body = f.read()
            # ★ 图片名里带着 file_id 的哈希（archive.py._download），
            #   同名 = 同内容、永远不会变 —— 可以让浏览器长期存着。
            #   不加这个的话刷新页面后所有图都要重新下一遍（用户报过）。
            #   private：只准浏览器自己存，代理/CDN 不许存（消息是私密的）
            h._send(200, body, ctype,
                    cache='private, max-age=31536000, immutable')
            return True
        h._send(404, 'Not Found', 'text/plain; charset=utf-8')
        return True

    # ================= 记账：配置 =================
    if path.startswith('/api/ledger/') and path.endswith('/config'):
        me = h._gate()
        if me is None:
            return True
        if not h._admin_only(me):
            return True
        bot = h._bot_of(me, path[len('/api/ledger/'):-len('/config')].strip('/'))
        if bot is None:
            return True
        cfg = bot.get('ledger') or {}
        try:
            from .group_admin import DEFAULT_WELCOME as dft
        except Exception:
            dft = ''
        h._json({'ok': True, 'config': cfg, 'welcome_default': dft})
        return True

    return False


def handle_post(h, path, me):
    """POST /api/ledger/*  和  /api/archive/*（me 是 panel.py 鉴权过的身份）"""

    # ================= 群消息记录：改配置 =================
    if (path.startswith('/api/archive/') and path.endswith('/config')
            and len(path.strip('/').split('/')) == 4):
        if not h._admin_only(me):
            return True
        bid = path[len('/api/archive/'):-len('/config')].strip('/')
        bot = h._bot_of(me, bid)
        if bot is None:
            return True
        patch = h._body() or {}
        allow = {'enabled', 'keep_days'}
        clean = {k: v for k, v in patch.items() if k in allow}
        if not clean:
            h._json({'ok': False, 'error': '没有可改的字段'})
            return True
        was = bool((bot.get('archive') or {}).get('enabled'))
        good, err = h.mgr.set_archive_cfg(bid, clean)
        if not good:
            h._json({'ok': False, 'error': err})
            return True
        now = bool((h.mgr.find(bid).get('archive') or {}).get('enabled'))
        if was != now:
            # 开关变了要重启线程，收消息的分支才切得过来
            try:
                h.mgr.restart_bot(bid)
            except Exception as e:
                log('重启机器人 %s 失败：%s' % (bid, e))
        log('群消息记录：%s → %s（%s）'
            % (bid, '开' if now else '关', bot.get('note')))
        h._json({'ok': True, 'enabled': now,
                 'need_privacy_off': now})
        return True

    # ================= 群消息记录：按群开/关 =================
    if (path.startswith('/api/archive/') and path.endswith('/mute')
            and len(path.strip('/').split('/')) == 4):
        if not h._admin_only(me):
            return True
        bid = path[len('/api/archive/'):-len('/mute')].strip('/')
        if h._bot_of(me, bid) is None:
            return True
        body = h._body() or {}
        ok2, err = h.mgr.set_archive_mute(
            bid, body.get('chat_id'), bool(body.get('mute')))
        if not ok2:
            h._json({'ok': False, 'error': err})
            return True
        h._json({'ok': True})
        return True

    # ================= 群消息记录：给某个群加备注 =================
    if (path.startswith('/api/archive/') and path.endswith('/chatnote')
            and len(path.strip('/').split('/')) == 4):
        if not h._admin_only(me):
            return True
        bid = path[len('/api/archive/'):-len('/chatnote')].strip('/')
        if h._bot_of(me, bid) is None:
            return True
        body = h._body() or {}
        ok2, err = h.mgr.set_archive_chatnote(
            bid, body.get('chat_id'), body.get('note'))
        if not ok2:
            h._json({'ok': False, 'error': err})
            return True
        h._json({'ok': True})
        return True

    # ================= 群消息记录：一次问全部机器人的未读数 =================
    # 面板上要实时刷新未读，一个机器人一次请求太浪费 —— 合成一次。
    # body: {"seen": {"<bot_id>": {"<chat_id>": "看到哪一刻"}}}
    # 回:   {"unread": {"<bot_id>": {"<chat_id>": 几条}},
    #        "base":   {"<bot_id>": {...}}}   ← 第一次打开时给的已读基线
    if path == '/api/archive/unread_all':
        if not h._admin_only(me):
            return True
        body = h._body() or {}
        seen = body.get('seen')
        seen = seen if isinstance(seen, dict) else {}
        unread, base = {}, {}
        n = 0
        for b in h.mgr.bots:
            if b.get('node_id'):
                continue
            if n >= 40:
                break                      # 别让一次请求开几十个库
            if (b.get('type') or '') not in core.ARCHIVE_TYPES:
                continue
            if not (b.get('archive') or {}).get('enabled'):
                continue
            arc = h._archive_of(b['id'])
            if arc is None:
                continue
            n += 1
            mine = seen.get(b['id'])
            if not isinstance(mine, dict) or not mine:
                # 这个机器人用户还没打开过 → 给基线，历史记录不算未读
                base[b['id']] = arc.baseline(with_seq=True)
                unread[b['id']] = {}
            else:
                unread[b['id']] = arc.unread_counts(mine)
        unavailable = []
        registry = getattr(h.mgr,'nodes',None)
        if registry:
            import base64
            for nid in {b['node_id'] for b in h.mgr.bots if b.get('node_id')}:
                try:
                    if not registry.cache.get(nid,{}).get('online'):
                        raise OSError()
                    own = {b['id'] for b in h.mgr.bots if b.get('node_id')==nid}
                    result = registry.call(nid,'unread',{'seen':{k:v for k,v in seen.items() if k in own}},timeout=3)
                    result = json.loads(base64.b64decode(result['response']['body']))
                    unread.update(result.get('unread',{}));base.update(result.get('base',{}))
                except (OSError,ValueError,KeyError):
                    unavailable.append(nid)
        h._json({'ok': True, 'unread': unread, 'base': base,'unavailable_nodes':unavailable})
        return True

    # ================= 记账：改配置 =================
    # ★ 管理端专属：商户的「配置」权限**只给商城机器人**，
    #   其他类型（记账/客服/USDT）他一律没权限
    if (path.startswith('/api/ledger/') and path.endswith('/config')
            and len(path.strip('/').split('/')) == 4):
        if not h._admin_only(me):
            return True
        bid = path[len('/api/ledger/'):-len('/config')].strip('/')
        bot = h._bot_of(me, bid)
        if bot is None:
            return True
        if (bot.get('type') or '') != 'ledger':
            h._json({'ok': False, 'error': '这不是记账机器人'})
            return True
        patch = h._body() or {}
        # 只认识这两个键，别的丢掉
        allow = {'welcome_text', 'enabled'}
        clean = {k: v for k, v in patch.items() if k in allow}
        if not clean:
            h._json({'ok': False, 'error': '没有可改的字段'})
            return True
        if 'enabled' in clean:
            good, err = h.mgr.set_enabled(bid, bool(clean.pop('enabled')))
            if not good:
                h._json({'ok': False, 'error': err})
                return True
        if clean:
            h.mgr.set_ledger_cfg(bid, clean)
        h._json({'ok': True})
        return True

    return False


# ==================================================================
#  客户自建机器人的消息回传
# ==================================================================
#  ★★ 这是**唯一对外的口子**（免面板鉴权），每一行都当成「面对公网」来写。
#
#  谁在用：客户自己服务器上那台「记账独立版」。它把收到的群消息、
#  还有自己下好的图片 POST 过来，落到这台机器人名下的归档库里 ——
#  于是面板上那套「三级下钻 / 未读 / 置顶 / 备注 / 图片」原样就能用，
#  一行都不用重写。
#
#  怎么鉴权：`X-Ingest-Sig` 头 = `core.ingest_sig(bot_id, token)` ——
#    从**机器人自己的 token** 算出来的，两边都能算，所以**没有密钥要管**。
#    ★ 绝不能让客户用面板密码（X-Panel-Pass）—— 那他能看所有商户。
#    ★ 也不能直接发 token：token 能完全接管机器人，而这个是单向的，
#      截到了也反推不出 token（见 core.ingest_sig 的说明）。
#
#  ★★ 到期 / 停用一律拒收：**客户不续费，数据就进不了库**。
#     不查这个等于白送。
INGEST_MAX_BODY = 4 * 1024 * 1024       # 一批更新的上限（图片不在这里面）
INGEST_MAX_MEDIA = 12 * 1024 * 1024     # 单张图上限
INGEST_MAX_EVENTS = 200                 # 一次最多收几条


def _ingest_archive(bid, owner_id='', bot=None):
    """打开（必要时**建**）这台机器人的归档库。

    ★ 跟面板那条读取路径（panel._archive_of）不一样：那个是「文件不存在
      就显示空」，而回传**必须把第一条消息也存下来** —— 库里没东西时
      正是要在这里建出来。MessageArchive 的 __init__ 会建表。
    ★ 故意**不传 token**：图片是客户那边下好传过来的，这边不用
      （也没法）去 Telegram 下载。不传 token 就不会起后台下载线程。
    ★★ `owner_id` 必须传（以前漏了）：归档靠它判断「这条是不是主人发的」，
      漏了的话主人所有消息的 is_owner 都是 False、全跑到左边去。
      （读的时候还会再按当前 owner_id 重算一遍，见 _fix_owner ——
        所以老库里的旧消息也能一起修好）
    """
    p = os.path.join(core.bot_data_dir(bot), '%s.archive.sqlite3' % bid)
    try:
        from archive import MessageArchive
        return MessageArchive(p, owner_id=owner_id or '')
    except Exception as e:
        log('回传：打开归档库失败 %s：%s' % (bid, e))
        return None


def _ingest_bot(h, sig):
    """用回传暗号换机器人。暗号不对 / 机器人没了 / 已到期 / 已停用 → None

    ★ 暗号是 `core.ingest_sig(bot_id, token)` —— **没有存任何密钥**，
      每次现算现比。所以：
        · 用户那边没有任何东西要生成/复制/理解
        · 换 token 就等于换了暗号，不用额外维护

    ★ 几种失败**回的话都一样**（见调用方），免得有人拿它挨个试，
      探出来哪个机器人 id 存在。
    """
    sig = str(sig or '')
    if len(sig) < 32:
        return None
    hit = None
    for b in h.mgr.bots:
        want = core.ingest_sig(b.get('id'), b.get('token'))
        if safe_eq(want, sig):          # 常数时间比较（merchants.safe_eq）
            hit = b
    if hit is None:
        return None
    if h.mgr.is_expired(hit):
        log('回传被拒（已到期）：%s' % hit.get('id'))
        return None
    if not hit.get('enabled', True):
        log('回传被拒（已停用）：%s' % hit.get('id'))
        return None
    return hit


def handle_ingest(h, path):
    """客户回传的入口。返回 True = 我处理了（不管成功还是失败）。

    ★★ 注意这里**没有** h._gate()：客户那边没有面板密码，
       校验完全靠 X-Ingest-Key。
    """
    bot = _ingest_bot(h, h.headers.get('X-Ingest-Sig'))
    if bot is None:
        h._json({'ok': False,
                 'error': '身份对不上，或者这个机器人已到期/已停用'}, 403)
        return True
    bid = bot['id']

    if path == '/api/ingest/config':
        if not bot.get('remote'):
            h._json({'ok': False, 'error': '请先在面板切换为客户自建'}, 403)
            return True
        try:
            h.connection.settimeout(10)
            raw = h._raw_body(9 * 1024 * 1024)
            body = json.loads(raw)
            if not isinstance(body, dict) or not isinstance(body.get('reply', {}), dict):
                raise ValueError('配置请求格式无效')
            from miniapp import remote_requests
            job = remote_requests(h.mgr).exchange(bid, body.get('reply'))
            h._json({'ok': True, 'request': job})
        except (ValueError, TypeError):
            h._json({'ok': False, 'error': '配置请求格式无效'}, 400)
        return True

    # ---------------- 图片：原始字节 ----------------
    if path == '/api/ingest/media':
        data = h._raw_body(INGEST_MAX_MEDIA)
        if not data:
            h._json({'ok': False,
                     'error': '没有图片内容，或超过 %d MB'
                              % (INGEST_MAX_MEDIA // 1048576)}, 413)
            return True
        arc = _ingest_archive(bid, bot.get('owner_id'), bot)
        if arc is None:
            h._json({'ok': False, 'error': '服务端没准备好'}, 500)
            return True
        ok = arc.put_media(h.headers.get('X-Chat'), h.headers.get('X-Msg'),
                           h.headers.get('X-File-Id'), data,
                           h.headers.get('X-Suffix') or '.jpg')
        if not ok:
            # 记录还没到（或者已经被清掉了）—— 客户那边会重试，不用当错误
            log('回传图片没入库（记录不在）：%s %s/%s' % (
                bid, h.headers.get('X-Chat'), h.headers.get('X-Msg')))
        h._json({'ok': bool(ok)})
        return True

    # ---------------- 客户那边的**绑定状态** ----------------
    # ★★ 为什么需要这个：绑定是**在客户的服务器上**发生的，管理员 id 存在
    #    他那边的 data/<id>.state.json 里。面板自己那份永远是空的 ——
    #    不报回来的话，面板就永远显示「等待绑定」，客户明明已经绑好了。
    #    （2026-09-29 第一次真给客户装，卡在这儿）
    if path == '/api/ingest/state':
        body = h._body() or {}
        ok, err = h.mgr.set_remote_state(bid, body)
        h._json({'ok': True} if ok else {'ok': False, 'error': err})
        return True

    # ---------------- 一批消息 ----------------
    if path == '/api/ingest':
        body = h._body() or {}
        events = body.get('events')
        if not isinstance(events, list) or not events:
            h._json({'ok': False, 'error': '没有 events'}, 400)
            return True
        if len(events) > INGEST_MAX_EVENTS:
            events = events[:INGEST_MAX_EVENTS]
        arc = _ingest_archive(bid, bot.get('owner_id'), bot)
        if arc is None:
            h._json({'ok': False, 'error': '服务端没准备好'}, 500)
            return True
        # ★ 「关闭记录某个群」在面板上是按群存的，回传也得认它 ——
        #   不然你在面板上关了，消息还照样从客户那边灌进来
        mute = (bot.get('archive') or {}).get('mute_chats') or []
        try:
            arc.record_result(events, mute=mute)
        except Exception as e:
            log('回传落库出错 %s：%s' % (bid, e))
            h._json({'ok': False, 'error': '落库失败'}, 500)
            return True
        h._json({'ok': True, 'took': len(events)})
        return True

    h._send(404, 'Not Found', 'text/plain; charset=utf-8')
    return True
