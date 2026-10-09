# -*- coding: utf-8 -*-
"""记账机器人（第四种类型）

把朋友的「记账机器人专业版」接进面板。
业务逻辑在 `ledger/` 目录里（原样搬过来的，MIT 协议），
这个文件只管**接线**：收消息 → 拆参数 → 调 ledger.commands → 发回复。

★ 和别的机器人不一样的地方：
  · `allow_groups = True` —— 它主要在**群里**干活，别的机器人只服务私聊
  · 回复要发到**群 chat_id**，不是发言人的 user id（最容易写错的地方）
  · 数据存 sqlite（`data/<bot_id>.sqlite3`），而且是**懒加载** ——
    sqlite 连接不能跨线程，必须在 runner 自己的线程里第一次打开
  · 有**后台线程**（清理消息可能要删几千条，不能卡住收消息）
"""
import os
import re
import secrets
import sqlite3
from contextlib import closing
import threading
import time
from datetime import datetime

import core
from core import BaseRunner, TgError, log

from . import (bill_messages, calculator, group_admin as G, lookup, metal,
               price, reminders)
from . import text_utils, trc20
from . import tron_chain as tc
from . import tron_watch
from . import commands as C
from .positions import GroupPositions
from .storage import LEDGER_TZ, LedgerStore

GROUP_CHECK_PER_TICK = 5         # 每个 tick 最多核实几个群还在不在（省 API）
MENU_TTL = 1800                  # 广播/清理菜单多久没动就作废（秒）


class LedgerRunner(BaseRunner):
    kind = 'ledger'
    poll_timeout = 10
    allow_groups = True              # ★ 要收群消息（只有这个类型）
    allowed_updates = ['message', 'callback_query', 'my_chat_member']

    def __init__(self, manager, bot):
        super().__init__(manager, bot)
        self._db = None              # ★ 懒加载，别在 __init__ 里开（跨线程会炸）
        self._pos = None             # 群消息位置（同上）
        self._welcome_at = {}        # 入群防重（原 state.welcome_sent_at）
        self._cool = {}              # @全体冷却（原 state.notify_all_cooldowns）
        self._group_count = 0        # 给面板看的内存快照（面板绝不读库）
        self._reminded = ''
        self._bc = {}                # 广播菜单状态 {uid: {...}}
        self._mine = {}              # 「/我」选群状态 {uid: {...}}
        self._wel = {}               # 等主人发「入群欢迎语」内容 {uid: 开始时间}
        self._cl = {}                # 清理菜单状态 {uid: {...}}
        self._cleanup_done = {}      # 每个群清理到哪个 id 了 {chat_id: id}
        self._cleanup_busy = set()   # 正在清理的群（防重复起线程）
        self._check_idx = 0          # 群存活轮询游标（后台线程用）
        self._check_lock = threading.Lock()
        # 私聊查 TRC20 地址 + 监听（见 tron_watch.py）。
        # ★ 它每次操作都**自己开一个 sqlite 连接再关掉**，所以哪个线程调都行 ——
        #   不像 self.store 那样只能在 runner 线程里用。
        self.tronw = tron_watch.TronWatch(self)

    # ================= 数据 =================
    @property
    def db_path(self):
        return os.path.join(core.bot_data_dir(self.bot), '%s.sqlite3' % self.bid)

    @property
    def pos_path(self):
        return os.path.join(core.bot_data_dir(self.bot), '%s.positions.sqlite3' % self.bid)

    @property
    def store(self):
        """★ 只能在自己线程里第一次访问 —— sqlite 连接跨线程用会报错"""
        if self._db is None:
            self._db = LedgerStore(self.db_path)
            reminders.initialize_schedule(self._db, datetime.now(LEDGER_TZ))
        return self._db

    @property
    def positions(self):
        if self._pos is None:
            self._pos = GroupPositions(self.pos_path)
        return self._pos

    def on_stop(self):
        """线程退出前关库。

        ★★ 必须在这里关、不能在别的线程关：sqlite 连接默认不允许跨线程，
           `close()` 跨线程会抛异常。以前是主线程关的，异常被吞掉，
          结果就是「文件一直锁着」—— 删机器人删不干净、换商户清不了账本。
        """
        self.close_db()

    def close_db(self):
        for attr in ('_db', '_pos'):
            obj = getattr(self, attr)
            if obj is None:
                continue
            try:
                obj.close()
            except Exception as e:
                # ★ 绝对不能静默吞 —— 吞掉就变成「以为关了、其实还锁着」
                log('[%s] 关 %s 失败：%s' % (self.note(), attr, e))
            setattr(self, attr, None)

    def stat(self):
        """给面板看的数字：管着几个群（读内存，不碰库）"""
        return self._group_count

    def stat_label(self):
        return '记账群'

    # ================= 生命周期 =================
    def on_start(self):
        self.customer.sync()
        log('[%s] 记账机器人启动（数据 data/%s.sqlite3）' % (self.note(), self.bid))

    def tick_interval(self):
        return 60.0

    def on_tick(self):
        self.customer.sync()
        self.customer.tick()
        # 顺手更新给面板看的快照
        try:
            self._group_count = len(self.store.list_active_bot_groups())
        except Exception as e:
            log('[%s] 统计记账群失败：%s' % (self.note(), e))
        # 日切提醒（原本是每天北京时间 9:00 推一次）
        try:
            now = datetime.now(LEDGER_TZ)
            if reminders.should_check(now):
                key = now.strftime('%Y%m%d%H')
                if self._reminded != key:
                    self._reminded = key
                    n = reminders.send_due_reminders(self.api, self.store, now)
                    if n:
                        log('[%s] 日切提醒推了 %d 个群' % (self.note(), n))
        except Exception as e:
            log('[%s] 日切提醒出错：%s' % (self.note(), e))
        # 轮着核实几个群还在不在（被踢了就清掉，免得广播往死群发）
        try:
            self.refresh_groups()
        except Exception as e:
            log('[%s] 核实群列表出错：%s' % (self.note(), e))
        self._expire_menus()
        # 链上监听：到点就**起后台线程**扫（★ 绝不能在这个线程里扫 ——
        # on_tick 和收消息是同一个线程，扫链要打好多个接口，
        # 在这儿扫就是「群里记账跟着卡」）
        try:
            if self.tronw.should_scan():
                threading.Thread(target=self.tronw.scan, daemon=True,
                                 name='tron-scan').start()
        except Exception as e:
            log('[%s] 起扫链线程失败：%s' % (self.note(), e))

    # ================= 权限 =================
    def admins(self):
        import customer_config as CC
        ids = ({CC.owner(self.bot)} | {int(i) for i in self.bot.get('admin_ids') or []} |
               {int(i) for i in (self.bot.get('ledger') or {}).get('staff_grants', {})})
        return {i for i in ids if i and CC.grants(self.bot, i)['global_ops']}

    @property
    def customer(self):
        if not hasattr(self, '_customer'):
            from .customer_runtime import CustomerRuntime
            self._customer = CustomerRuntime(self)
        return self._customer

    def can_broadcast(self, uid):
        import customer_config as CC
        return CC.grants(self.bot, uid)['broadcast']

    def can_configure(self, uid):
        import customer_config as CC
        return CC.grants(self.bot, uid)['manage']

    def cmd_admin(self, aid, user, text):
        if self.owner_id() and aid != self.owner_id():
            self.send(aid, '机器人已激活，请由拥有者在内部人员中授权。')
            return
        super().cmd_admin(aid, user, text)

    def ledger_owners(self, chat_id):
        """群主人身份必须来自机器人授权，拉入机器人本身不授予权限。"""
        self.customer.sync()
        return self.admins()

    def can_manage(self, chat_id, uid):
        """能管这个群吗：主人 / 面板管理员 / 已授权的操作员"""
        if not uid:
            return False
        if int(uid) in self.admins():
            return True
        try:
            return bool(self.store.is_operator(int(chat_id), int(uid),
                                               self.ledger_owners(chat_id)))
        except Exception:
            return False

    @staticmethod
    def actor_of(user):
        """TG 的 user dict → 记账模块要的 Actor

        ★ 原版用 getattr 取字段，对 dict 会全都返回 None（静默记错人），
          所以这个必须自己写。
        """
        if not user:
            return None
        try:
            uid = int(user.get('id'))
        except (TypeError, ValueError):
            return None
        name = ' '.join(x for x in (user.get('first_name'),
                                    user.get('last_name')) if x)
        return C.Actor(uid, (user.get('username') or '').strip(),
                       name or str(uid))

    # ================= 消息入口 =================
    def handle(self, u):
        """★ 在基类分发之前先记一下群消息位置（删消息要用）"""
        try:
            msg = u.get('message') or (u.get('edited_message') or {})
            chat = msg.get('chat') or {}
            if chat.get('type') in ('group', 'supergroup'):
                self.positions.record(chat.get('id'), msg.get('message_id'),
                                      chat.get('title'))
        except Exception:
            pass                      # 记位置失败绝不能影响收消息
        super().handle(u)

    def on_owner(self, msg):
        self.handle_any(msg)

    def on_guest(self, msg):
        # 群里已经由 on_group 处理；这里是私聊普通人
        self.handle_any(msg)

    def on_group(self, msg):
        self.handle_any(msg)

    def on_my_chat_member(self, mcm):
        """机器人自己被拉进群 / 踢出群"""
        chat = mcm.get('chat') or {}
        if chat.get('type') not in ('group', 'supergroup'):
            return
        chat_id = chat.get('id')
        new_status = ((mcm.get('new_chat_member') or {}).get('status') or '')
        old_status = ((mcm.get('old_chat_member') or {}).get('status') or '')
        frm = mcm.get('from') or {}

        if new_status in ('left', 'kicked'):
            try:
                self.store.deactivate_bot_chat(int(chat_id))
                log('[%s] 已退出群 %s，不再广播给它' % (self.note(), chat.get('title')))
            except Exception:
                pass
            return

        if old_status not in ('left', 'kicked') or new_status not in ('member',
                                                                     'administrator'):
            return      # 只是权限变了，不是新入群
        now = time.monotonic()
        if now - self._welcome_at.get(chat_id, 0) < G.WELCOME_COOLDOWN:
            return      # 反复拉进拉出会刷屏
        self._welcome_at[chat_id] = now

        title = chat.get('title') or str(chat_id)
        try:
            self.customer.new_group(int(chat_id))
            self.store.remember_bot_chat(int(chat_id), title, chat.get('type') or '')
            self.store.ensure_chat(int(chat_id))
            if frm.get('id') and int(frm['id']) in self.admins():
                self.store.set_chat_owner(int(chat_id), int(frm['id']))
        except Exception as e:
            log('[%s] 记录新群失败：%s' % (self.note(), e))

        log('[%s] 被拉进群「%s」(%s)，拉的人 %s'
            % (self.note(), title, chat_id, frm.get('id')))
        self.send_html(chat_id, self.welcome_text())

    # ================= 新成员进群的欢迎语 =================
    # ★★ 主人私聊发「设置欢迎语」→ 机器人问「请输入…」→ 他发内容 → 存下来。
    #    新成员进群时就用这条欢迎。
    # ★★ 跟「机器人被拉进群」那个欢迎语**不是一回事**（那个在面板里配）：
    #    · 机器人被拉进群 → welcome_text()（固定简短就绪提示）
    #    · 新成员进群     → newbie_welcome()（下面这套，主人用命令配）
    #    2026-10-08 用户要的是后者（原来那句是**写死**的 💐欢迎XXX加入该群~）。
    WELCOME_INPUT_TTL = 600      # 等他发内容最多等 10 分钟
    WELCOME_MAX = 300            # 欢迎语最多几个字

    def newbie_welcome(self):
        """新成员进群时说什么。

        ★ 三种状态分清楚，别把 `None` 和 `''` 搞混：
          · 没设过（None）→ 用默认那句
          · 设了内容      → 用他设的
          · 明确关掉（''）→ **不欢迎**（他发「关闭」就是这个意思）
        """
        cfg = self.bot.get('ledger') or {}
        text = cfg.get('newbie_welcome')
        if text is None:
            return G.DEFAULT_NEWBIE_WELCOME
        return str(text).replace('\\n', '\n').strip()

    def newbie_welcome_photo(self):
        """新成员进群时配的那张图（TG 的 file_id）。没配就是空串。

        ★ 存 file_id，**不下载图片** —— 不占空间、不用管文件过期，
          TG 那边这个文件一直认（只要不是换了个机器人）。
        ★ 只有**主人私聊**发来的图才会存进去（见 welcome_input）。
        ★ 发图时文字是图下面的**说明（caption）**，上限 1024 字 ——
          我们的欢迎语最多 300 字，够用。
        """
        cfg = self.bot.get('ledger') or {}
        return str(cfg.get('newbie_welcome_photo') or '').strip()

    @staticmethod
    def _biggest_photo(msg):
        """从一条消息里取**最大的**那张图的 file_id

        TG 会把同一张图压成好几档尺寸一起发（photo 是升序数组），
        取最后一档最清楚。不是图片消息就返回空串。
        """
        photos = msg.get('photo')
        if not isinstance(photos, list) or not photos:
            return ''
        best = None
        for p in photos:
            if isinstance(p, dict) and (best is None or
                                        (p.get('width') or 0) >= (best.get('width') or 0)):
                best = p
        return str((best or {}).get('file_id') or '')

    def _welcome_line(self, member, tg_link=True):
        import customer_config as CC
        if (self.bot.get('ledger') or {}).get('customer', {}).get('basics'):
            b = CC.settings(self.bot)['basics']
            if not b['welcome_enabled']:
                return ''
            actor = self.actor_of(member)
            label = C.escape(actor.display_name)
            if b['welcome_nickname'] and actor.username:
                label += ' (@%s)' % C.escape(actor.username)
            line = C.escape(b['welcome_text']).replace('{name}', label)
            if b['welcome_mention']:
                mention = G.member_mention_html(member, tg_link=tg_link)
                line = mention + '\n' + line
            return line
        """把欢迎语里的 {name} 换成成员的可点击名字。返回空串 = 不欢迎

        ★ tg_link=False 用在**图片说明**里（见 group_admin.member_mention_html）
        """
        text = self.newbie_welcome()
        if not text:
            return ''
        mention = G.member_mention_html(member, tg_link=tg_link)
        # ★ 主人填的内容当**纯文本**（先转义再替换）——
        #   不然他打半个标签，整条消息会被 TG 拒收（HTML 不闭合）
        safe = C.escape(text).replace('{name}', mention)
        if mention not in safe:
            # 没写 {name} 就补在末尾 —— 不然"欢迎"了个寂寞，没人知道欢迎谁
            safe = safe + ' ' + mention
        return safe

    def welcome_command(self, actor, msg, cmd):
        """主人私聊发「设置欢迎语」→ 开个头，等他发内容。返回 True = 处理了"""
        text = (cmd or '').strip()
        if text[:1] in ('/', '／'):
            text = text[1:].strip()
        if text not in ('设置欢迎语', '欢迎语', '设置入群欢迎', '入群欢迎',
                        '入群欢迎语'):
            return False
        if not actor:
            return True
        uid = actor.user_id
        if not self.can_configure(uid):
            self.send_html(uid, '只有机器人主人可以设置欢迎语。')
            return True
        self._wel[uid] = time.time()
        cur = self.newbie_welcome()
        photo = self.newbie_welcome_photo()
        now = ('现在用的是<b>默认</b>欢迎语。' if not cur else
               '现在的欢迎语是：\n<code>%s</code>' % C.escape(cur))
        if photo:
            now += '\n现在<b>配了欢迎图</b>。'
        self.send_html(
            uid, '请输入您要设置的欢迎语\n\n%s\n\n'
                 '· <code>{name}</code> 会变成新成员的名字（不写就自动加在末尾）\n'
                 '· ★ <b>直接发一张图片也行</b>：发图就换欢迎图，'
                 '图上的说明文字会一起换掉（不带说明就只换图）\n'
                 '· 发「删除图片」只去掉图，文字留着\n'
                 '· 发「默认」恢复成默认那句\n'
                 '· 发「关闭」新成员进群就不欢迎了\n'
                 '· 发「取消」不动它\n\n'
                 '（文字最多 %d 个字）' % (now, self.WELCOME_MAX))
        return True

    def _welcome_preview(self, uid, actor):
        """把「新成员进群会看到什么」直接演示给他看

        ★ 用**他自己**当样本渲染 {name} —— 光看 <code> 里的原文看不出效果。
        ★ 配了图就直接发一张图给他，比描述一百句都清楚。
        """
        sample = {'id': actor.user_id,
                  'first_name': actor.display_name or '新成员',
                  'username': actor.username or ''}
        photo = self.newbie_welcome_photo()
        line = self._welcome_line(sample, tg_link=not photo)
        if photo:
            self.send_photo_html(uid, photo, line or '（图上没文字）')
            return
        self.send_html(uid, '✅ 设置好了。新成员进群时，群里会看到：\n\n%s\n\n'
                            '（去群里试一下：让别人退了重进，或拉个小号）'
                        % (line or '（现在什么都不发）'))

    def welcome_input(self, actor, msg, text):
        """正在等他发欢迎语内容时，这条消息就是内容。返回 True = 吃掉了

        ★ 收三种：纯文字 / 纯图片 / 图片+说明（说明就是 caption）
        """
        if not actor:
            return False
        uid = actor.user_id
        at = self._wel.get(uid)
        if not at:
            return False
        if not self.can_configure(uid):
            self._wel.pop(uid, None)
            self.send_html(uid, '管理权限已撤销。')
            return True
        if time.time() - at > self.WELCOME_INPUT_TTL:
            # ★ 过期了**不吃**，放行走正常流程 —— 别把人家的记账吞了
            self._wel.pop(uid, None)
            return False
        body = (text or '').strip()
        photo = self._biggest_photo(msg)
        # ★ 既没文字也没图（文件/视频/贴纸…）→ 提示一下，但**不结束等待**，
        #   让他接着重发就行（不然一不小心发错东西就得从头再来一遍）
        #   ★ 「勾了『作为文件发送』的图片」也落在这儿 —— 它没有 photo，
        #     所以在提示里专门点一句
        if not body and not photo:
            self.send_html(uid, '这条里没看到文字或图片。\n'
                                '可以发一段文字，或者直接发一张<b>图片</b>\n'
                                '（发图时别勾「作为文件发送」，勾了机器人收不到）\n\n'
                                '还在等您设置，直接重发就行。')
            return True
        self._wel.pop(uid, None)
        led = self.bot.setdefault('ledger', {})

        # ★ 光发图、没带说明 —— 意思是「换张图」，别把原来的文字也清了，
        #   更不能当成「内容是空的」报错（图片消息本来就没有文字）
        if photo and not body:
            led['newbie_welcome_photo'] = photo
            __import__('customer_config').sync_welcome(self.bot)
            self.mgr.save()
            self.send_html(uid, '✅ 换好欢迎图了。新成员进群会看到：')
            self._welcome_preview(uid, actor)
            log('[%s] 主人换了入群欢迎图' % self.note())
            return True

        if body in ('取消', '/取消', '不设了'):
            self.send_html(uid, '好，没动。')
            return True
        if body in ('删除图片', '删除欢迎图', '删图', '去掉图片', '不要图片'):
            if led.pop('newbie_welcome_photo', None):
                __import__('customer_config').sync_welcome(self.bot)
                self.mgr.save()
                self.send_html(uid, '✅ 欢迎图删了，新成员进群只发文字。')
            else:
                self.send_html(uid, '本来就没配图。')
            return True
        if body in ('默认', '/默认', '恢复默认'):
            led.pop('newbie_welcome', None)
            __import__('customer_config').sync_welcome(self.bot)
            self.mgr.save()
            self.send_html(uid, '✅ 文字已恢复成默认：\n\n<code>%s</code>%s'
                            % (C.escape(G.DEFAULT_NEWBIE_WELCOME),
                               '\n（欢迎图还留着，要删就发「删除图片」）'
                               if self.newbie_welcome_photo() else ''))
            return True
        if body in ('关闭', '清空', '不欢迎', '不要欢迎'):
            # ★ 「关闭」是**彻底停掉** —— 连图一起清。
            #   要是只清文字留住图，就变成「关了但还有张图赖在那」的怪状态
            led['newbie_welcome'] = ''
            led.pop('newbie_welcome_photo', None)
            __import__('customer_config').sync_welcome(self.bot)
            self.mgr.save()
            self.send_html(uid, '✅ 已关闭，以后新成员进群不再发欢迎语。\n'
                                '（想开回来就再发一次「设置欢迎语」）')
            return True
        if len(body) > self.WELCOME_MAX:
            self.send_html(uid, '太长了（%d 个字，最多 %d）。'
                                '再发一次「设置欢迎语」重新来。'
                            % (len(body), self.WELCOME_MAX))
            return True

        # ★ 带说明的图 → 图和文字一起换；纯文字 → 只换文字（图保留）
        if photo:
            led['newbie_welcome_photo'] = photo
        led['newbie_welcome'] = body
        __import__('customer_config').sync_welcome(self.bot)
        self.mgr.save()
        self.send_html(uid, '✅ 设置好了。新成员进群时，群里会看到：')
        self._welcome_preview(uid, actor)
        log('[%s] 主人改了入群欢迎语' % self.note())
        return True

    def welcome_text(self):
        """机器人自己入群统一显示简短提示；成员欢迎语独立配置。"""
        return C.GROUP_READY_TEXT

    def handle_any(self, msg):
        chat = msg.get('chat') or {}
        chat_id = chat.get('id')
        frm = msg.get('from') or {}
        if not chat_id or frm.get('is_bot'):
            return

        # ① 成员进出 / 群升级 —— 这些消息没有 text，得先接住
        try:
            if self._service_message(chat_id, chat, msg, frm):
                return
        except TgError as e:
            log('[%s] 群事件处理失败：%s' % (self.note(), e))
        except Exception as e:
            log('[%s] 群事件异常：%s' % (self.note(), e))

        text = (msg.get('text') or msg.get('caption') or '').strip()
        if not text:
            # ★ 主人正在设欢迎语时发来的**纯图片**（没带说明文字）——
            #   这种消息没有 text，得在这里单独接住，否则永远走不到下面。
            #   ★ 只有私聊才管：群里别人发的图一概不碰
            #     （不能谁发张图就改了欢迎图）。没在等他输入的私聊
            #     媒体消息（贴纸/语音…）welcome_input 会直接返回 False，
            #     行为跟以前一样 —— 一概不理。
            if chat.get('type') == 'private' and not frm.get('is_bot'):
                try:
                    self.welcome_input(self.actor_of(frm), msg, '')
                except Exception as e:
                    log('[%s] 处理欢迎图失败：%s' % (self.note(), e))
            return
        try:
            self._dispatch(chat_id, chat, msg, frm, text)
        except TgError as e:
            log('[%s] 记账发消息失败：%s' % (self.note(), e))
        except Exception as e:
            log('[%s] 记账处理异常：%s' % (self.note(), e))

    def _service_message(self, chat_id, chat, msg, frm):
        """群成员进出、群升级成超级群。返回 True 表示已处理掉"""
        # 群升级：普通群 → 超级群，群 id 会变，不改的话老数据全变孤儿
        if msg.get('migrate_to_chat_id'):
            old_id, new_id = int(chat_id), int(msg['migrate_to_chat_id'])
        elif msg.get('migrate_from_chat_id'):
            old_id, new_id = int(msg['migrate_from_chat_id']), int(chat_id)
        else:
            old_id = new_id = 0
        if old_id and old_id != new_id:
            try:
                self.store.migrate_bot_chat(old_id, new_id, chat.get('title') or '')
                self.positions.forget(old_id)
                log('[%s] 群升级：%s → %s' % (self.note(), old_id, new_id))
            except Exception as e:
                log('[%s] 群升级迁移失败：%s' % (self.note(), e))
            return True

        if chat.get('type') not in ('group', 'supergroup'):
            return False
        import customer_config as CC
        self.customer.new_group(int(chat_id))
        if msg.get('new_chat_title') and CC.feature(self.bot, 'title_notice'):
            self.customer.notify(chat_id, '群名变更：' + C.escape(msg['new_chat_title']))
            self.store.remember_bot_chat(int(chat_id), msg['new_chat_title'], chat['type'])

        # 新人进群 → 欢迎
        newbies = msg.get('new_chat_members') or []
        if newbies:
            my_id = (self.me or {}).get('id')
            humans = []
            for m in newbies:
                if int(m.get('id') or 0) == int(my_id or 0):
                    self.on_my_chat_member({'chat': chat, 'from': frm,
                        'old_chat_member': {'status': 'left'},
                        'new_chat_member': {'status': 'member'}})
                    continue
                if G.is_human_member(m, my_id):
                    humans.append(m)
            # ★ 配了欢迎图、而且**只有一个人**进群 → 发图（文字挂在图的说明上）。
            #   同时进好几个人就退回一条纯文字：一次刷好几张图太吵，
            #   而且图的说明只有 1024 字，塞不下几行欢迎语。
            photo = self.newbie_welcome_photo() if len(humans) == 1 else ''
            if humans and CC.feature(self.bot, 'join_notice'):
                self.customer.notify(chat_id, '群 %s 新成员：%s' % (chat_id, ', '.join(C.escape((self.actor_of(m)).display_name) for m in humans)))
            lines = []
            for m in humans:
                # ★ 走主人配的欢迎语（没配就是默认那句）。返回空串 = 被关了。
                #   tg_link=False：图片说明里的 tg://user 链接点了没反应，
                #   所以发图时没用户名的人只能退回纯名字（有用户名的照旧可点）
                line = self._welcome_line(m, tg_link=not photo)
                if line:
                    lines.append(line)
            if photo and lines:
                self.send_photo_html(chat_id, photo, lines[0])
            elif lines:
                self.send_html(chat_id, '\n'.join(lines))
            return True

        # 有人退群 → 送别
        left = msg.get('left_chat_member')
        if left:
            b = CC.settings(self.bot)['basics']
            if b['leave_enabled'] and b['leave_text'] and G.is_human_member(left, (self.me or {}).get('id')):
                self.send_html(chat_id, C.escape(b['leave_text']).replace('{name}', G.member_mention_html(left)))
            return True
        return False

    def _dispatch(self, chat_id, chat, msg, frm, text):
        st = self.store
        actor = self.actor_of(frm)
        is_group = chat.get('type') in ('group', 'supergroup')
        import customer_config as CC
        cfg = self.customer.sync()
        if is_group:
            self.customer.new_group(int(chat_id))
            if actor and actor.user_id in self.admins():
                st.set_chat_owner(int(chat_id), actor.user_id,
                                  replace=st.get_chat_owner_id(int(chat_id)) not in self.admins())
            self.customer.metadata(int(chat_id), chat, actor)
            self.customer.sync()
        if is_group and actor and self.customer.address_command(int(chat_id), actor, text):
            return
        if is_group and text.lower() == '/start@' + str(
                (self.me or {}).get('username') or self.bot.get('username') or '').lower():
            text = '/start'

        # ① 先把这个群和这个人记下来（广播、@全体、操作员都要用）
        try:
            if is_group:
                st.remember_bot_chat(int(chat_id), chat.get('title') or '',
                                     chat.get('type') or '')
            if actor:
                st.remember_user(int(chat_id), actor.user_id, actor.username,
                                 actor.display_name, bool(frm.get('is_bot')))
        except Exception as e:
            log('[%s] 记账记用户失败：%s' % (self.note(), e))

        # ② 群管理命令（要先于记账判断 —— 关了记账也得能清理消息）
        if is_group and self._group_command(chat_id, actor, text, msg):
            return

        # ③ 私聊里的 /del 和 广播（都是主人/管理员用的多步流程）
        if not is_group:
            cmd = text.strip()
            # ★ 正在等他输「地址备注 / 最低金额」—— **必须排在记账和算式前面**，
            #   不然他输的备注会被当成记账吃掉（备注写「+100」就中招了）
            if self.tronw.take_input(int(chat_id), cmd):
                return
            # ★ 正在等主人发「入群欢迎语」的内容 —— **必须排在其它命令前面**
            #   （跟 take_input 一个道理：他自己发的欢迎语里写个「广播」
            #     就会被下面那条吃掉，跑去开广播菜单了）
            if self.welcome_input(actor, msg, cmd):
                return
            if cmd in ('/del', 'del', '/清理', '清理消息'):
                self.start_cleanup_menu(actor, msg)
                return
            if cmd in ('广播', '/broadcast') or cmd.startswith('广播 '):
                self.start_broadcast(actor, msg, cmd)
                return
            if self.set_tron_key(actor, msg, cmd):
                return
            if self.set_bank_key(actor, msg, cmd):
                return
            if self.broadcast_input(actor, msg, text):
                return      # 正在等广播内容
            # ★ 排在 broadcast_input **后面** —— 正在等广播内容时，
            #   他打的内容（哪怕是「欢迎语」这仨字）该当广播内容，别被抢走
            if self.welcome_command(actor, msg, cmd):
                return

        if text.strip() == '/统计':
            if is_group:
                self.send_html(chat_id, '请由机器人拥有者私聊发送 /统计。', reply_to=msg.get('message_id'))
            else:
                self.start_mine(actor, msg, all_members=True)
            return

        # ③.5 「/我」—— 自己的加账明细（只算**他自己**记的账）
        #      群里：直接统计本群
        #      私聊：先弹群列表勾选（可多选/全选），再统计
        if C.is_mine_command(text):
            if is_group:
                if actor:
                    self.send_html(
                        chat_id,
                        C.format_mine(st, int(chat_id), actor.user_id,
                                      self.mine_title(actor)),
                        reply_to=msg.get('message_id'))
            else:
                self.start_mine(actor, msg)
            return

        # ④ TRC20 地址
        addr = trc20.extract_trc20_address(text)
        if addr:
            if is_group:
                if not cfg['features']['tron_verify']:
                    return
                # 群里：防篡改核对图。★ **一个字都不能变** ——
                #   那是客户拿来核对收款地址的，跟查余额是两回事。
                trc20.reply_trc20_verify_image(self.api, chat_id, addr,
                                               reply_to=msg.get('message_id'))
                if cfg['features']['admin_confirm']:
                    self.send_html(chat_id, '<code>%s</code>' % addr, kb={'inline_keyboard': [[{'text':'群管理员确认地址','callback_data':'lc:verify:'+addr}]]})
                if cfg['features']['tron_balance'] or cfg['features']['tron_details'] or cfg['features']['tron_count']:
                    self.tronw.send_card(chat_id, addr)
            elif not tc.address_valid(addr):
                # 私聊里发错一位就别去查了 —— 查回来「未激活」会把人吓一跳
                self.send_html(chat_id, '❌ 这不是有效的 TRC20 地址'
                                        '（校验位对不上，检查一下是不是抄错了）。')
            else:
                # ★★ 这里原来有个「3 秒内不许再查」的冷却拦截，已按用户要求**去掉**：
                #    「我不怕额度刷光……我要发一个它查询一个，发一个查询一个，
                #      额度怕刷光我多申请几个 api 的 key 就行了」
                #    → 额度问题改用**多把 Key 轮询**解决（见 tron_watch.keys()），
                #      不再拿「少查几次」来省额度。
                # ★ 查询在后台线程里跑（见 tron_watch.send_card），
                #   所以连着发也不会堵住收消息。
                self.tronw.send_card(chat_id, addr)
            return

        # ⑤ 币价 / 实时汇率（★ 必须在**算式之前** —— 完整版也是这个顺序）
        if price.is_realtime_rate_command(text):
            self.set_realtime_rate(chat_id, actor, msg, st)
            return
        if price.is_price_command(text):
            if cfg['features']['okx']:
                self.reply_price(chat_id, msg)
            return
        # ★ `G` 查贵金属（用户 2026-10-08 要的，跟 `z0` 查 U 价并排）
        #   放在算式**之前** —— `G` 不是合法算式、也进不了记账，
        #   但摆在这儿最清楚：它跟「币价」是一类东西
        if metal.is_metal_command(text):
            self.reply_metal(chat_id, msg)
            return

        # ★ 手机号 / 身份证 / 银行卡 —— **整条消息就是一个号码**才认
        #   （用户 2026-10-08 要的：群里或私聊直接发号码就查）
        #   判据卡得很死（见 lookup.detect），
        #   所以不会跟记账、算式、币价这些抢活
        hit = lookup.detect(text)
        if hit:
            key = {'phone':'phone', 'id':'idcard', 'idcard':'idcard', 'bank':'bank', 'bankcard':'bank'}.get(hit[0], hit[0])
            if cfg['features']['lookup'] and cfg['features'].get(key, False):
                self.reply_lookup(chat_id, msg, hit[0], hit[1])
            return

        # ⑥ 算式 → 直接算
        # ★★ 但「就是一条记账」的写法（`+1000/9`）要让给记账 ——
        #   那种写法**两边都合法**，而算式排在记账前面，
        #   不加备注就会被算式抢走（用户 2026-10-08 实测：
        #   `+1000/9 努力` 对，`+1000/9` 却回了个 111.11）。
        #   `1000/9`（不带符号）照旧是算式。
        if not C.is_entry_text(text):
            calc = calculator.calculate_expression(text)
            if calc is not None:
                self.send_html(chat_id, calc, reply_to=msg.get('message_id'))
                return

        # ⑦ 记账主逻辑
        reply = msg.get('reply_to_message') or {}
        if is_group and actor:
            explicit = str(actor.user_id) in (self.bot.get('ledger') or {}).get('staff_grants', {})
            if explicit and not self.can_manage(int(chat_id), actor.user_id):
                try:
                    entry_text = C._parse_entry(text) is not None or text.startswith('分红')
                except ValueError:
                    entry_text = True
                if entry_text or text in ('撤销', '清空', '/clear', '/undo') or text.startswith('设置'):
                    self.send_html(chat_id, '你没有这个群的操作权限。')
                    return
            edit_text = text
            if cfg['features']['pure_u']:
                if text.startswith(('下发','/下发','/out','/payout','出款','下分')) or re.match(r'\S+/?下发-?\d', text):
                    return
                if text.startswith('分红'):
                    edit_text = '下发' + text[2:]
            if self.customer.edit(int(chat_id), actor, edit_text, msg):
                return
        from contextlib import nullcontext
        with getattr(self.mgr, 'lock', nullcontext()):
            self.customer.sync()
            result = C.handle_text(
                store=st,
                chat_id=int(chat_id),
                actor=actor,
                text=text,
                owner_ids=self.ledger_owners(chat_id),
                reply_user=self.actor_of(reply.get('from')) if reply else None,
                reply_text=((reply.get('text') or reply.get('caption'))
                            if reply else None),
                message_id=msg.get('message_id'),
                reply_message_id=reply.get('message_id') if reply else None,
            )
        if result and result.text:
            kb = (self.bill_keyboard(chat_id)
                  if self._looks_like_bill(result.text) else None)
            self.send_html(chat_id, result.text, kb=kb,
                           reply_to=None if kb else msg.get('message_id'))
            if result.changed and is_group:
                self.customer.deposit(int(chat_id))
            archive = getattr(st, 'last_customer_archive', None)
            if archive and '已清空' in result.text:
                self.send_html(chat_id, '已保存清空前的完整账单。', kb={'inline_keyboard': [[{'text':'查看完整账单存档','callback_data':'lc:archive:'+str(archive)}]]})
        elif result is None and is_group:
            self.customer.reply(int(chat_id), text, msg.get('message_id'))

    @staticmethod
    def _looks_like_bill(text):
        return '账单' in text and ('总入款' in text or '应下发' in text)

    # ================= 币价 / 实时汇率 =================
    # ★★ 这两条原来**是死的**：完整版里写在 services/runtime.py 里（async 的），
    #    搬过来时函数原样抄进了 price.py，但**没有任何地方调用它** ——
    #    于是说明书上写的「币价」「设置实时汇率」发出去一点反应都没有。
    #    2026-09-29 用户实测报上来的就是这个。
    def reply_price(self, chat_id, msg):
        """`币价` / `bj` / `z0` —— 报欧意 USDT/CNY 最新 5 档

        ★★ 抓价放**后台线程**：这是联网动作，而这儿是收消息的主线程。
           欧意慢（或被墙）的时候主线程一卡，群里所有人的记账都得跟着等 ——
           记账是本职，绝不能为了「看一眼价格」给它让路。
        ★ 谁都能问，不用权限（完整版也是这样）。
        ★ 抓不到就直说抓不到，**绝不编一个价格出来**。
        """
        reply_to = msg.get('message_id')

        def work():
            try:
                prices, source = price.fetch_prices()
                text = price.format_okx_prices(prices, source)
            except Exception as e:
                log('[%s] 币价抓取失败：%s' % (self.note(), e))
                text = '币价获取失败，请稍后再试。'
            self.send_html(chat_id, text, reply_to=reply_to)

        threading.Thread(target=work, daemon=True,
                         name='price%s' % chat_id).start()

    BANK_HEADS = ('设置银行密钥', '设置银行卡密钥', '银行密钥', '银行卡密钥',
                  '/bankkey', '设置银行key', '设置银行Key')

    def _bank_key_help(self):
        return ('银行卡查询可以配一个**查询密钥**（配了才查得到'
                '<b>归属地、联行号</b>）。\n\n'
                '去百度 API 商城申请「银行卡基本信息」这个接口：\n'
                '  https://apis.baidu.com/store/detail/'
                '851818e8-03e4-4a24-8a03-62a9bd0fd3db\n'
                '· 有 <b>0元/50次</b> 的试用额度（每人限一次），够用很久\n'
                '· 下单后到「买家中心 → 已购服务」里拿 <b>AppCode</b>\n\n'
                '拿到之后私聊发：\n'
                '  <code>设置银行密钥 你的AppCode</code>\n'
                '<code>银行密钥</code> 看现在配没配\n'
                '（<b>不要发在这个群里</b>）\n\n'
                '不配密钥可查银行、卡类型和常见银行客服电话；归属地需配置接口。')

    def set_bank_key(self, actor, msg, cmd):
        """主人私聊设置「银行卡查询密钥」（百度 API 商城的 AppCode）

        ★ **不配也能用** —— 银行名和卡类型走支付宝那个免费接口。
          配了才有**归属地 / 联行号**；常见银行客服电话由本地表补充。
        ★ 只认机器人主人。设置了**不回显**，只报后 4 位。
        ★★★ 密钥存在 `data/<机器人id>.json` 的 `bank_appcode` 里 ——
          跟 TronGrid Key 一个地方（**不推送、不进日志、不进归档**），
          **绝不写进代码**（代码要传公开仓库）。
        """
        text = (cmd or '').strip()
        if not text.startswith(self.BANK_HEADS):
            return False
        if not actor:
            return True
        uid = actor.user_id
        if not self.is_owner(uid):
            self.send_html(uid, '只有机器人主人可以设置查询密钥。')
            return True

        args = text.split(None, 1)[1].strip() if ' ' in text else ''
        cur = str(self.data.get('bank_appcode') or '')

        if args in ('', '列表', 'list', '查看'):
            if cur:
                self.send_html(uid, '现在已经配了银行卡查询密钥（最后 4 位 …%s），'
                                    '能查归属地/联行号。\n\n'
                                    '换一把：<code>设置银行密钥 新的</code>\n'
                                    '不要了：<code>设置银行密钥 清空</code>'
                                % cur[-4:])
            else:
                self.send_html(uid, '现在<b>没配</b>银行卡查询密钥 —— '
                                    '可查银行、卡类型和常见银行客服电话。\n\n' +
                                self._bank_key_help())
            return True

        if args in ('清空', 'clear', '-', '删除'):
            self.data.pop('bank_appcode', None)
            self.save_data()
            self.send_html(uid, '✅ 已清除银行卡查询密钥。'
                                '查询照常能用，只是查不到归属地了。')
            return True

        if not re.fullmatch(r'[A-Za-z0-9_\-]{8,128}', args):
            self.send_html(uid, self._bank_key_help())
            return True

        self.data['bank_appcode'] = args
        self.save_data()
        self.send_html(uid, '✅ 配好了（…%s）。\n'
                            '发一个银行卡号试试，能出「归属地/联行号」就对了。'
                        % args[-4:])
        log('[%s] 主人设置了银行卡查询密钥' % self.note())
        return True

    def reply_lookup(self, chat_id, msg, kind, num):
        """手机号 / 身份证 / 银行卡 查询（群里私聊都行，谁都能用）

        ★ 手机号和身份证是**离线库**，本地查完就回，不用起线程。
        ★ 银行卡要**联网**（支付宝那个接口）→ 放后台线程，别拖住记账。
        ★ 谁都能查，不设权限。
        ★ 查不到就说查不到，**绝不编** —— 编一个「北京市朝阳区」出来，
          他拿去核对客户身份，是要出事的。
        ★★ 号码**不进运行日志**（那个文件会传阅、也会上传）——
          出错信息里带号码的地方要抹掉再记。群里回复本身是另一回事：
          号码本来就是他发在群里的。
        """
        reply_to = msg.get('message_id')

        def scrub(text):
            """日志里别出现完整号码"""
            text = str(text)
            if num and num in text:
                text = text.replace(num, num[:4] + '****')
            return text

        def work():
            try:
                if kind == 'phone':
                    info = lookup.phone_info(num)
                elif kind == 'idcard':
                    info = {'region': lookup.idcard_region(num),
                            'valid': lookup.id_checksum_ok(num)}
                else:
                    # ★ 配了密钥就查得到归属地/联行号（付费接口），
                    #   没配就走免费接口（只有银行名+卡类型）
                    info = lookup.bank_info(
                        num, str(self.data.get('bank_appcode') or ''))
                text = lookup.format_card(kind, num, info)
            # ★★ 必须写 `lookup.LookupError`，不能只写 `LookupError` ——
            #    那个名字是 **Python 内置的**（KeyError/IndexError 的爹），
            #    我们自己那个异常根本不会被这条接住，会掉到下面的
            #    「查询失败」去（表现：数据文件缺了却提示「稍后再试」，
            #    把人往错的方向带）。2026-10-08 测试当场抓出来的。
            except lookup.LookupError as e:
                log('[%s] 查询库缺失：%s' % (self.note(), scrub(e)))
                text = '查询功能暂时不可用（数据文件缺失），请联系管理员。'
            except Exception as e:
                log('[%s] %s 查询失败：%s' % (self.note(), kind, scrub(e)))
                text = '查询失败，请稍后再试。'
            self.send_html(chat_id, text, reply_to=reply_to)

        if kind == 'bankcard':
            # 只有银行卡要联网
            threading.Thread(target=work, daemon=True,
                             name='lookup%s' % chat_id).start()
        else:
            work()

    def reply_metal(self, chat_id, msg):
        """`G` / `贵金属` —— 报黄金·白银·铂金的上金所实时价

        ★ 跟「币价」一样：抓价放**后台线程**（联网动作，不能拖住收消息的
          主线程 —— 记账才是本职）。
        ★ 谁都能问，不用权限。
        ★ 抓不到就直说抓不到，**绝不编一个价格出来**。
        """
        reply_to = msg.get('message_id')

        def work():
            try:
                quotes, recycle = metal.fetch_quotes()
                text = metal.format_card(quotes, recycle)
                if not text:
                    text = '贵金属价格获取失败，请稍后再试。'
            except Exception as e:
                log('[%s] 贵金属价抓取失败：%s' % (self.note(), e))
                text = '贵金属价格获取失败，请稍后再试。'
            self.send_html(chat_id, text, reply_to=reply_to)

        threading.Thread(target=work, daemon=True,
                         name='metal%s' % chat_id).start()

    def set_realtime_rate(self, chat_id, actor, msg, store):
        """`设置实时汇率` —— 按欧意最新 1 档价，把本群汇率改成实时价

        ★ 这条**同步**抓（不像「币价」走后台线程），因为它要写库，
          而 sqlite 连接不能跨线程（storage 建连接用的是默认的
          check_same_thread=True）。代价是抓价那几秒主线程在等 ——
          所以 price.FETCH_TIMEOUT 卡得很短，而且：

        ★★ 抓不到就**一个字节都不改**，报错里带上原汇率。
           汇率直接决定账单金额（应下发 = 入款 / 汇率）——
           要是失败还硬写一个数进去，或者写个 0，客户的钱就算错了。
        """
        reply_to = msg.get('message_id')
        if chat_id >= 0:
            self.send_html(chat_id, '请在群内设置实时汇率。', reply_to=reply_to)
            return
        if actor is None or not store.is_operator(int(chat_id), actor.user_id,
                                                  self.ledger_owners(chat_id)):
            self.send_html(chat_id, '无权限设置实时汇率。', reply_to=reply_to)
            return
        old_rate, _fee = store.get_settings(int(chat_id))
        try:
            prices, source = price.fetch_prices()
        except Exception as e:
            log('[%s] 实时汇率抓取失败：%s' % (self.note(), e))
            prices, source = [], ''
        if not prices:
            self.send_html(
                chat_id,
                '❌ 获取欧意实时汇率失败，已保留当前群原汇率：%s'
                % calculator.format_calc_result(old_rate),
                reply_to=reply_to)
            return
        # ★★ 取**最新 1 档**（prices[0]）—— 完整版的算法就是这个
        new_rate = store.set_rate(int(chat_id), prices[0], is_realtime=True)
        self.send_html(
            chat_id,
            '\n'.join([
                '✅ 当前群实时汇率已更新',
                '',
                '汇率：%s' % calculator.format_calc_result(new_rate),
                '来源：欧意 USDT/CNY 最新 1 档（%s）' % source,
                '更新时间：%s'
                % datetime.now(LEDGER_TZ).strftime('%Y-%m-%d %H:%M:%S'),
            ]),
            reply_to=reply_to)

    # ================= 链上密钥（可选）=================
    KEY_HEADS = ('设置密钥', '/tronkey', '设置TRON密钥', '设置链上密钥',
                 '添加密钥', '增加密钥', '/addkey',
                 '密钥列表', '查看密钥', '我的密钥')

    def set_tron_key(self, actor, msg, cmd):
        """主人私聊设置 TronGrid API Key。返回 True = 这条我处理了

        ★ **不配也能用** —— 公开接口够查几个地址（实测跑得通）。
          配了额度宽裕很多：监听地址多、扫得勤的时候建议配上。
        ★ **可以配好几把**（最多 5 把），机器人会**轮着用**：
          一把被限流就自动换下一把，不用干等恢复。
            设置密钥 K   → 只留这一把（覆盖原来的全部）
            添加密钥 K   → 再加一把
            密钥列表     → 看现在有几把（只显示后 4 位）
            设置密钥 清空 → 全清掉
        ★ 只认机器人主人（跟「广播」一个规矩）。
        ★ 设置了**不回显**密钥；Telegram 私聊不是端到端加密，
          提醒用户别发钱包私钥/助记词。
        ★★★ 密钥存在 `data/<机器人id>.json` 里，**绝不写进代码** ——
          代码要传公开仓库，写死等于公开（totp_secret 就是这么出事的）。
        """
        text = (cmd or '').strip()
        if not text.startswith(self.KEY_HEADS):
            return False
        if not actor:
            return True
        uid = actor.user_id
        if not self.is_owner(uid):
            self.send_html(uid, '只有机器人主人可以设置查询密钥。')
            return True

        adding = text.startswith(('添加密钥', '增加密钥', '/addkey'))
        args = text.split(None, 1)[1].strip() if ' ' in text else ''

        if args in ('', '列表', 'list') or text.startswith('密钥列表'):
            got = self.tronw.custom_keys()
            if not got:
                self.send_html(uid, '尚未绑定自己的查询密钥。'
                                    + ('当前使用平台默认密钥轮换查询。' if self.tronw.keys() else '当前使用公共接口。')
                                    + '\n\n' + self._key_help())
            else:
                masked = '\n'.join('  %d. …%s' % (i + 1, k[-4:])
                                   for i, k in enumerate(got))
                self.send_html(uid, '现在配了 <b>%d</b> 把查询密钥（最后 4 位）：\n'
                                    '%s\n\n机器人会轮着用，一把被限流自动换下一把。'
                                    % (len(got), masked))
            return True

        if args in ('清空', 'clear', '-', '删除'):
            self.data.pop('tron_key', None)
            self.data.pop('tron_keys', None)
            self.save_data()
            self.send_html(uid, '✅ 已清除全部查询密钥。'
                                '查询和监听照常能用，就是额度低一些。')
            return True

        if not re.fullmatch(r'[A-Za-z0-9_\-]{8,256}', args):
            self.send_html(uid, self._key_help())
            return True

        got = list(self.tronw.custom_keys())
        if args in got:
            self.send_html(uid, '这一把已经在里面了（最后 4 位 …%s），没重复加。'
                            % args[-4:])
            return True
        if adding and len(got) >= tron_watch.MAX_KEYS:
            self.send_html(uid, '最多存 %d 把，已经满了。'
                                '想换的话用 <code>设置密钥 新的Key</code> 覆盖。'
                            % tron_watch.MAX_KEYS)
            return True

        # ★★ 存之前**先真查一次**，确认这个 Key 能用。
        #    为什么要验：Key 填错了（多粘一个空格、复制成别的东西）存进去，
        #    表现是「以后所有查询都失败」—— 而且失败得没头没脑。
        #    验一下当场就能发现；而且**失败保留旧 Key**，不会把好用的搞坏。
        #    ★ 两个接口都要过：账户查询 + 交易查询。
        #    ★★ 验证要联网（最多十来秒），所以**必须放后台线程** ——
        #       收消息和 on_tick 是同一个线程，堵在这里群里记账就卡住了。
        self.send_html(uid, '⏳ 正在验证这个 Key（要真查一次 TRX/USDT 接口）…')

        def work():
            good, why = tc.probe(args)
            if not good:
                self.send_html(uid, '❌ <b>这个 Key 用不了，没有保存。</b>\n'
                                    '原因：%s\n\n'
                                    '原来那个 Key（如果配过）照旧用着，'
                                    '没受影响。\n'
                                    '再核对一下复制的内容，'
                                    '或者重新去 trongrid.io 控制台复制一次。'
                                    % why)
                return
            if adding:
                got.append(args)
                self.data['tron_keys'] = got
            else:
                self.data['tron_keys'] = [args]
            self.data.pop('tron_key', None)     # 老的单个字段不再用，免得混淆
            self.save_data()
            # ★ 把他刚才那条**带着 Key 的消息删掉**（尽力而为）。
            #   私聊虽然只有两个人看得到，但那条消息会一直躺在聊天记录里，
            #   手机丢了、账号被盗、截屏发出去 —— 都会漏。
            #   ★ 删不掉也不算失败：机器人可能没删消息的权限。
            self._try_delete(uid, msg)
            n = len(self.tronw.keys())
            self.send_html(uid, '✅ 已保存查询密钥（不回显）。现在一共 <b>%d</b> 把，'
                                '机器人会轮着用。\n'
                                '验证结果：%s\n'
                                '（你刚才那条带密钥的消息我顺手删了）' % (n, why))

        threading.Thread(target=work, daemon=True, name='tronkey').start()
        return True

    @staticmethod
    def _key_help():
        return ('用法：\n'
                '  <code>设置密钥 你的Key</code>　只留这一把（覆盖）\n'
                '  <code>添加密钥 你的Key</code>　再加一把（最多 %d 把，'
                '轮着用更抗限流）\n'
                '  <code>密钥列表</code>　看现在有几把\n'
                '  <code>设置密钥 清空</code>　全清掉\n\n'
                '去 trongrid.io 控制台申请（免费）。\n'
                '★ 那是个查询用的 Key，'
                '<b>不是钱包私钥，也绝不要发助记词</b>。' % tron_watch.MAX_KEYS)

    def _try_delete(self, chat_id, msg):
        """删掉一条消息，**删不掉就算了** —— 绝不能因此让主流程失败"""
        try:
            mid = msg.get('message_id')
            if mid:
                self.api.call('deleteMessage', chat_id=chat_id, message_id=mid)
        except Exception as e:                       # noqa: BLE001
            log('[%s] 删除含密钥的消息失败（不影响绑定）：%s' % (self.note(), e))

    # ================= 「/我」：自己的加账明细 =================
    def can_use_mine(self, uid):
        """谁能用「/我」：机器人主人、面板管理员，或**任意一个群**里的操作员

        ★ 权限是**按群**授的，而私聊没有群 —— 所以「在其中任何一个群
          是操作员」就算数，不然操作员在私聊里根本用不了。
        """
        if int(uid) in self.admins():
            return True
        try:
            return bool(self.store.operator_chats(int(uid)))
        except Exception as e:
            log('[%s] 查操作员身份失败：%s' % (self.note(), e))
            return False

    @staticmethod
    def mine_title(actor):
        """抬头显示「昵称 @用户名」（跟消息记录一致，**不带数字 id**）"""
        if not actor:
            return ''
        name = actor.display_name or actor.username or str(actor.user_id)
        return '%s @%s' % (name, actor.username) if actor.username else name

    def start_mine(self, actor, msg, *, all_members=False):
        """私聊选群：/我 查看本人明细，拥有者 /统计 查看每个人合计。"""
        if not actor:
            return
        uid = actor.user_id
        allowed = self.is_owner(uid) if all_members else self.can_use_mine(uid)
        if not allowed:
            self.send_html(uid, '只有机器人拥有者可以使用「/统计」。' if all_members else
                               '只有机器人主人或操作员可以用「/我」。')
            return
        try:
            groups = [dict(r) for r in self.store.list_active_bot_groups()]
        except Exception as e:
            log('[%s] 取群列表失败：%s' % (self.note(), e))
            groups = []
        if not groups:
            self.send_html(uid, '还没有记录到群。'
                                '先让机器人加入群，并在群里发一条消息。')
            return
        self._mine[uid] = {'groups': groups, 'selected': set(),
                           'title': self.mine_title(actor), 'at': time.time(), 'all_members': all_members}
        result = self.send_html(uid, self._mine_pick_text(0), kb=self._mine_kb(uid))
        if all_members and isinstance(result, dict):
            self._mine[uid]['message_id'] = result.get('message_id')

    @staticmethod
    def _mine_pick_text(n):
        return ('要统计哪些群？' if not n else '已选 %d 个群。' % n) + \
               '\n（点一下打勾／取消，也可以直接点「全选」）'

    def _mine_kb(self, uid):
        st = self._mine.get(uid) or {}
        prefix = 'stats' if st.get('all_members') else 'mine'
        kb = G.group_selection_keyboard(st.get('groups') or [],
                                        st.get('selected') or set(), prefix)
        # ★ 用户要「也可以全选」—— 广播那套键盘没有全选，这里补上。
        #   只加在「/我」这套上，**不动广播的界面**（那个是好的，别碰）。
        kb['inline_keyboard'].insert(-1, [
            {'text': '全选', 'callback_data': prefix + ':all'},
            {'text': '清空', 'callback_data': prefix + ':none'}])
        return kb

    def mine_callback(self, cb_id, uid, mid, data, *, all_members=False):
        """「/我」与拥有者「/统计」的选群菜单。"""
        allowed = self.is_owner(uid) if all_members else self.can_use_mine(uid)
        if not allowed:
            self._answer(cb_id, '无权限')
            return
        st = self._mine.get(uid)
        command = '/统计' if all_members else '/我'
        if (st and bool(st.get('all_members')) != all_members) or (all_members and (
                not st or st.get('message_id') != mid or time.time()-st['at'] > MENU_TTL)):
            self._answer(cb_id, '菜单已失效，请重新发送「%s」' % command)
            return
        if all_members:
            data = data.replace('stats:', 'mine:', 1)
        if data == 'mine:cancel':
            self._mine.pop(uid, None)
            self._answer(cb_id, '已取消')
            self.edit_html(uid, mid, '已取消。')
            return
        if not st:
            self._answer(cb_id, '菜单已失效，请重新发送「%s」' % command)
            return
        if data in ('mine:all', 'mine:none'):
            st['at'] = time.time()
            st['selected'] = ({int(g['chat_id']) for g in st['groups']}
                              if data == 'mine:all' else set())
            self._answer(cb_id, '已全选' if data == 'mine:all' else '已清空')
            self.edit_html(uid, mid, self._mine_pick_text(len(st['selected'])),
                           kb=self._mine_kb(uid))
            return
        if data.startswith('mine:toggle:'):
            try:
                cid = int(data.rsplit(':', 1)[1])
            except ValueError:
                self._answer(cb_id, '')
                return
            if all_members and cid not in {int(g['chat_id']) for g in st['groups']}:
                self._answer(cb_id, '该群不在当前列表')
                return
            if cid in st['selected']:
                st['selected'].discard(cid)
            else:
                st['selected'].add(cid)
            st['at'] = time.time()
            self._answer(cb_id, '')
            self.edit_html(uid, mid, self._mine_pick_text(len(st['selected'])),
                           kb=self._mine_kb(uid))
            return
        if data == 'mine:next':
            if not st['selected']:
                self._answer(cb_id, '请至少选一个群')
                return
            picked = [(int(g['chat_id']), g.get('title') or '')
                      for g in st['groups']
                      if int(g['chat_id']) in st['selected']]
            title = st.get('title') or ''
            self._mine.pop(uid, None)
            self._answer(cb_id, '统计中…')
            try:
                text = C.format_owner_statistics(self.store, picked) if all_members else \
                       C.format_mine_multi(self.store, uid, picked, title)
            except Exception as e:
                log('[%s] 「%s」统计失败：%s' % (self.note(), command, e))
                self.send_html(uid, '统计失败，稍后再试。')
                return
            if all_members:
                self.edit_html(uid, mid, '统计完成。', kb={'inline_keyboard': []})
                for chunk in text_utils.split_html_message(text):
                    self.send_html(uid, chunk)
            else:
                self.send_html(uid, text)
            return

    # ================= 群管理命令 =================
    def _group_command(self, chat_id, actor, text, msg):
        """群里能用的管理命令。返回 True 表示已处理"""
        cmd = text.strip()

        # @全体 / 通知所有人
        if cmd.startswith(('通知所有人', '/notify_all', '/at_all', '@全体', '@所有人')):
            self.notify_all(chat_id, actor, text, msg)
            return True

        # 群成员统计
        if cmd in ('群成员', '/members', '成员统计'):
            self.send_html(chat_id, self.member_stats(chat_id),
                           reply_to=msg.get('message_id'))
            return True

        # /del —— 群里直接清理本群
        if cmd in ('/del', 'del', '/清理'):
            self.cleanup_this_group(chat_id, actor, msg)
            return True
        return False

    def notify_all(self, chat_id, actor, text, msg):
        if not self.can_manage(chat_id, actor.user_id if actor else 0):
            self.send_html(chat_id, '无权限。', reply_to=msg.get('message_id'))
            return
        now = time.monotonic()
        last = self._cool.get(chat_id)
        if last is not None and now - last < G.NOTIFY_COOLDOWN:
            self.send_html(chat_id, '通知所有人冷却中，请 %d 秒后再试。'
                           % int(G.NOTIFY_COOLDOWN - (now - last)))
            return
        try:
            members = self.store.list_active_known_members(int(chat_id), days=30)
        except Exception as e:
            log('[%s] 取群成员失败：%s' % (self.note(), e))
            members = []
        mentions = [G.mention_html(r) for r in members]
        if not mentions:
            self.send_html(chat_id, '当前群没有最近30天活跃成员缓存。')
            return
        content = G.extract_notify_all_text(text)
        self._cool[chat_id] = now        # ★ 先占坑再发，不然连点会重复刷
        chunks = G.chunked(mentions, G.NOTIFY_CHUNK)
        for i, part in enumerate(chunks):
            lines = ['📢 通知所有人']
            if content and i == 0:
                lines += ['', C.escape(content)]
            lines += ['', ' '.join(part)]
            self.send_html(chat_id, '\n'.join(lines))
            if i < len(chunks) - 1:
                time.sleep(1)            # 连发太快会被限流

    def member_stats(self, chat_id):
        st = self.store
        try:
            total = st.count_active_known_members(int(chat_id))
            d7 = st.count_active_known_members(int(chat_id), days=7)
            d30 = st.count_active_known_members(int(chat_id), days=30)
        except Exception as e:
            log('[%s] 统计成员失败：%s' % (self.note(), e))
            return '暂时取不到成员统计。'
        return ('当前群成员缓存\n缓存人数：%d\n最近7天活跃：%d\n最近30天活跃：%d'
                % (total, d7, d30))

    # ================= 清理消息 =================
    def cleanup_this_group(self, chat_id, actor, msg):
        """群里发 /del → 清理本群的机器人可见消息"""
        if not self.can_manage(chat_id, actor.user_id if actor else 0):
            self.send_html(chat_id, '只有群主或操作员可以使用 /del。',
                           reply_to=msg.get('message_id'))
            return
        latest = self.positions.latest(chat_id)
        if not latest:
            self.send_html(chat_id, '尚无消息记录，请稍后再试（机器人要先看到群里一条消息）。')
            return
        first = self._cleanup_done.get(chat_id, 0) + 1
        permission = G.check_permission(self.api, chat_id, 'supergroup',
                                        (self.me or {}).get('id'))
        if permission is False:
            return          # 没权限，静默（原版也是直接不吭声）
        if permission is None:
            self.send_html(chat_id, '暂时无法确认机器人权限，未开始清理，请稍后重试。')
            return
        self._start_cleanup_thread(chat_id, 'supergroup', first, latest,
                                   notify_chat=chat_id)

    def _start_cleanup_thread(self, chat_id, chat_type, first, latest,
                              notify_chat=None, on_done=None):
        if chat_id in self._cleanup_busy:
            return False
        self._cleanup_busy.add(chat_id)

        def work():
            ok, err = True, ''
            try:
                end = G.delete_range(
                    self.api, chat_id, first, latest, chat_type,
                    (self.me or {}).get('id'),
                    latest=lambda: self.positions.latest(chat_id),
                    on_progress=lambda e: self._cleanup_done.__setitem__(chat_id, e),
                    stop=self.stop_evt.is_set)
                self._cleanup_done[chat_id] = max(
                    self._cleanup_done.get(chat_id, 0), end)
            except Exception as e:
                ok, err = False, str(e)
                log('[%s] 清理群 %s 中断：%s' % (self.note(), chat_id, e))
            finally:
                self._cleanup_busy.discard(chat_id)
                if on_done:
                    try:
                        on_done(ok, err)
                    except Exception:
                        pass
            if notify_chat and not ok:
                try:
                    self.send_html(notify_chat,
                                   '清理中断，部分消息可能已删除；'
                                   '请检查机器人权限后重新发送 /del。')
                except Exception:
                    pass

        threading.Thread(target=work, daemon=True,
                         name='ledger-clean-%s' % chat_id).start()
        return True

    # -------- 私聊清理菜单 --------
    def start_cleanup_menu(self, actor, msg):
        if not actor:
            return
        uid = actor.user_id
        if uid not in self.admins():
            self.send_html(uid, '只有机器人主人可以使用 /del。')
            return
        sent = self.send_html(uid, '正在加载可清理的群列表……')
        if not sent:
            return
        mid = sent.get('message_id') if isinstance(sent, dict) else None
        try:
            rows = [dict(r) for r in self.store.list_active_bot_groups()]
        except Exception as e:
            log('[%s] 取群列表失败：%s' % (self.note(), e))
            rows = []
        if not rows:
            self.edit_html(uid, mid, '目前没有可显示的群，请确认机器人已加入群。')
            return
        # 权限查询要走网络，扔后台线程，别卡住收消息
        threading.Thread(target=self._check_then_show_menu,
                         args=(uid, mid, rows), daemon=True,
                         name='ledger-clr-menu').start()

    def _check_then_show_menu(self, uid, mid, rows):
        my_id = (self.me or {}).get('id')
        eligible, unknown = [], False
        for row in rows:
            if self.stop_evt.is_set():
                return
            r = G.check_permission(self.api, int(row['chat_id']),
                                   row.get('chat_type') or 'supergroup', my_id)
            if r is True:
                eligible.append(row)
            elif r is None:
                unknown = True
                eligible.append(row)    # 不确定的也列出来，让用户自己决定
        if not eligible:
            self.edit_html(uid, mid, '目前没有可显示的群，请确认机器人已加入群'
                                     '并具有管理员删除消息权限。')
            return
        self._cl[uid] = {
            'token': secrets.token_hex(4), 'groups': eligible, 'selected': set(),
            'page': 0, 'stage': 'select', 'mid': mid, 'at': time.time(),
        }
        self.edit_html(uid, mid, self._cleanup_text(uid),
                       kb=self._cleanup_kb(uid),
                       extra='\n部分群权限尚未确认，结果仅供参考。' if unknown else '')

    def _cleanup_text(self, uid):
        st = self._cl.get(uid) or {}
        return ('请选择要清理消息的群（可多选，已选 %d 个）：\n'
                '仅清理未满 48 小时的可删除消息，群内不发送提醒。\n'
                '尚无消息记录的群，需要先在群里发一条消息。'
                % len(st.get('selected') or ()))

    def _cleanup_kb(self, uid):
        st = self._cl.get(uid) or {}
        return G.selection_keyboard(st.get('groups') or [],
                                    st.get('selected') or set(),
                                    st.get('page') or 0,
                                    'cleanup:' + st.get('token', ''))

    def cleanup_callback(self, cb_id, uid, mid, data):
        st = self._cl.get(uid)
        parts = data.split(':')
        if (not st or len(parts) < 3
                or parts[:2] != ['cleanup', st['token']] or mid != st['mid']):
            self._answer(cb_id, '这个菜单已失效，请重新发送 /del')
            return
        action = parts[2]
        if action == 'cancel':
            self._cl.pop(uid, None)
            self._answer(cb_id, '已取消')
            self.edit_html(uid, mid, '已取消清理。')
            return
        if action == 'back':
            st['stage'] = 'select'
        elif action in ('toggle', 'page') and st['stage'] == 'select' and len(parts) == 4:
            try:
                value = int(parts[3])
            except ValueError:
                self._answer(cb_id, '')
                return
            ids = {int(r['chat_id']) for r in st['groups']}
            if action == 'toggle':
                if value not in ids:
                    self._answer(cb_id, '')
                    return
                st['selected'] ^= {value}
            elif 0 <= value <= (len(st['groups']) - 1) // 20:
                st['page'] = value
        elif action == 'next' and st['stage'] == 'select':
            if not st['selected']:
                self._answer(cb_id, '请至少选择一个群')
                self.edit_html(uid, mid, self._cleanup_text(uid),
                               kb=self._cleanup_kb(uid))
                return
            st['stage'] = 'confirm'
            st['at'] = time.time()
            titles = G.bounded_lines('• ' + (r.get('title') or str(r['chat_id']))
                                     for r in st['groups']
                                     if int(r['chat_id']) in st['selected'])
            prefix = 'cleanup:' + st['token']
            self._answer(cb_id, '')
            self.edit_html(uid, mid,
                           '将清理以下 %d 个群未满 48 小时的可删除消息：\n%s\n\n删除后无法恢复。'
                           % (len(st['selected']), titles),
                           kb={'inline_keyboard': [[
                               {'text': '确认清理', 'callback_data': prefix + ':confirm'},
                               {'text': '返回', 'callback_data': prefix + ':back'},
                               {'text': '取消', 'callback_data': prefix + ':cancel'}]]})
            return
        elif action == 'confirm' and st['stage'] == 'confirm' and st['selected']:
            groups = [r for r in st['groups']
                      if int(r['chat_id']) in st['selected']]
            self._cl.pop(uid, None)
            self._answer(cb_id, '开始清理')
            self.edit_html(uid, mid, '正在清理已选的 %d 个群，结果会显示在这里。'
                           % len(groups))
            threading.Thread(target=self._run_menu_cleanup,
                             args=(uid, mid, groups), daemon=True,
                             name='ledger-clr-run').start()
            return
        else:
            self._answer(cb_id, '')
            return
        st['at'] = time.time()
        self._answer(cb_id, '')
        self.edit_html(uid, mid, self._cleanup_text(uid), kb=self._cleanup_kb(uid))

    def _run_menu_cleanup(self, uid, mid, groups):
        done, failed = 0, []
        for row in groups:
            if self.stop_evt.is_set():
                break
            chat_id = int(row['chat_id'])
            title = row.get('title') or str(chat_id)
            latest = self.positions.latest(chat_id)
            if not latest:
                failed.append('%s：尚无消息记录，请先在群里发一条消息。' % title)
                continue
            first = self._cleanup_done.get(chat_id, 0) + 1
            ok, err = [False], ['']

            def on_done(good, error, _ok=ok, _err=err):
                _ok[0], _err[0] = good, error

            if not self._start_cleanup_thread(chat_id,
                                              row.get('chat_type') or 'supergroup',
                                              first, latest, on_done=on_done):
                failed.append('%s：正在清理中，请稍后。' % title)
                continue
            # 等这个群删完再下一个（串行，免得撞限流）
            while chat_id in self._cleanup_busy and not self.stop_evt.is_set():
                time.sleep(0.5)
            if ok[0]:
                done += 1
            else:
                failed.append('%s：清理中断，部分消息可能已删除。' % title)
        text = ('本次清理结束：完成 %d 个群，未完成 %d 个群。\n'
                '超过 48 小时及不可删除的消息保留。' % (done, len(failed)))
        if failed:
            text += '\n\n' + G.bounded_lines(failed)
        self.edit_html(uid, mid, text)

    # ================= 广播 =================
    def start_broadcast(self, actor, msg, cmd_text):
        """私聊里发「广播」→ 选群 → 输内容 → 确认"""
        if not actor:
            return
        uid = actor.user_id
        mid = msg.get('message_id')
        if not self.can_broadcast(uid):
            self.send_html(uid, '只有机器人主人可以广播。')
            return
        try:
            groups = [dict(r) for r in self.store.list_active_bot_groups()]
        except Exception as e:
            log('[%s] 广播取群列表失败：%s' % (self.note(), e))
            groups = []
        if not groups:
            self.send_html(uid, '还没有记录到可广播的群。请先让机器人加入群，'
                                '并让群里产生一条消息。')
            return
        self._bc[uid] = {'groups': groups, 'selected': set(), 'waiting': False,
                         'text': '', 'at': time.time()}
        # 「广播 内容」可以一句话带内容，省一步
        inline = G.extract_broadcast_text(cmd_text, '广播')
        if inline:
            self._bc[uid]['text'] = inline
        self.send_html(uid, '请选择要广播的群：%s'
                       % ('（已写内容，选完直接点「下一步」→「确认发送」）'
                          if inline else ''),
                       kb=self._broadcast_kb(uid))

    def _broadcast_kb(self, uid):
        st = self._bc.get(uid) or {}
        return G.group_selection_keyboard(st.get('groups') or [],
                                          st.get('selected') or set(), 'broadcast')

    def broadcast_input(self, actor, msg, text):
        """正在等广播内容时，这条消息就是内容。返回 True 表示吃掉了"""
        if not actor:
            return False
        st = self._bc.get(actor.user_id)
        if not st or not st.get('waiting'):
            return False
        uid = actor.user_id
        if text.strip() in ('取消', '取消广播', '/broadcast_cancel'):
            self._bc.pop(uid, None)
            self.send_html(uid, '已取消广播。')
            return True
        if not st['selected']:
            self._bc.pop(uid, None)
            self.send_html(uid, '没有选择群，请重新发送「广播」。')
            return True
        st['text'] = text
        st['waiting'] = False
        st['at'] = time.time()
        titles = self._broadcast_titles(uid)
        self.send_html(uid,
                       '广播目标：\n%s\n\n广播内容：\n%s'
                       % (titles, C.escape(text)),
                       kb={'inline_keyboard': [[
                           {'text': '确认发送', 'callback_data': 'broadcast:confirm'},
                           {'text': '取消', 'callback_data': 'broadcast:cancel'}]]})
        return True

    def _broadcast_titles(self, uid):
        st = self._bc.get(uid) or {}
        titles = G.group_titles(st.get('groups') or [])
        return '\n'.join('- %s' % C.escape(titles.get(c, str(c)))
                         for c in sorted(st.get('selected') or ()))

    def broadcast_callback(self, cb_id, uid, mid, data):
        st = self._bc.get(uid)
        if not self.can_broadcast(uid):
            self._answer(cb_id, '无权限')
            return
        if data == 'broadcast:cancel':
            self._bc.pop(uid, None)
            self._answer(cb_id, '已取消')
            self.edit_html(uid, mid, '已取消广播。')
            return
        if data == 'broadcast:next':
            if not st:
                self._answer(cb_id, '菜单已失效，请重新发送「广播」')
                return
            if not st['selected']:
                self._answer(cb_id, '请至少选择一个群')
                return
            st['at'] = time.time()
            if st.get('text'):
                # 内容已经带了（「广播 内容」那种），跳过输入直接给确认
                st['waiting'] = False
                self._answer(cb_id, '')
                self.edit_html(uid, mid, self._broadcast_preview(uid),
                               kb={'inline_keyboard': [[
                                   {'text': '确认发送',
                                    'callback_data': 'broadcast:confirm'},
                                   {'text': '取消',
                                    'callback_data': 'broadcast:cancel'}]]})
                return
            st['waiting'] = True
            self._answer(cb_id, '')
            self.edit_html(uid, mid, '请输入要广播的内容。')
            return
        if data == 'broadcast:confirm':
            if not st or not st['selected'] or not st.get('text'):
                self._answer(cb_id, '广播任务已失效，请重新发送「广播」')
                return
            # 群列表可能已经变了（被踢了），确认一下
            try:
                active = {int(r['chat_id']) for r in self.store.list_active_bot_groups()}
            except Exception:
                active = set(st['selected'])
            if not st['selected'] <= active:
                self._bc.pop(uid, None)
                self._answer(cb_id, '群列表已更新，请重新选择')
                self.edit_html(uid, mid, '群列表已更新，请重新发送「广播」选择目标群。')
                return
            targets, content = sorted(st['selected']), st['text']
            self._bc.pop(uid, None)
            self._answer(cb_id, '开始广播')
            self.edit_html(uid, mid, '正在广播，请稍候……')
            threading.Thread(target=self._run_broadcast,
                             args=(uid, mid, targets, content),
                             daemon=True, name='ledger-bc').start()
            return
        if data.startswith('broadcast:toggle:'):
            try:
                cid = int(data.rsplit(':', 1)[1])
            except ValueError:
                self._answer(cb_id, '')
                return
            if not st:
                self._answer(cb_id, '菜单已失效')
                return
            if cid in st['selected']:
                st['selected'].discard(cid)
            else:
                st['selected'].add(cid)
            st['at'] = time.time()
            self._answer(cb_id, '')
            if mid:
                try:
                    self.api.call('editMessageReplyMarkup', chat_id=uid,
                                  message_id=mid,
                                  reply_markup=self._broadcast_kb(uid))
                except TgError as e:
                    log('[%s] 刷新广播键盘失败：%s' % (self.note(), e))
            return
        self._answer(cb_id, '')

    def _broadcast_preview(self, uid):
        st = self._bc.get(uid) or {}
        return ('广播目标：\n%s\n\n广播内容：\n%s'
                % (self._broadcast_titles(uid), C.escape(st.get('text') or '')))

    def _run_broadcast(self, uid, mid, targets, content):
        started = time.time()
        ok = fail = 0
        for cid in targets:
            if self.stop_evt.is_set() or self.expired() or not self.can_broadcast(uid):
                break
            with closing(sqlite3.connect(self.db_path)) as db:
                active = db.execute('SELECT 1 FROM bot_chats WHERE chat_id=? AND is_active=1', (cid,)).fetchone()
            if not active:
                continue
            try:
                self.api.call('sendMessage', chat_id=cid, text=content)
                ok += 1
            except TgError as e:
                log('[%s] 广播到 %s 失败：%s' % (self.note(), cid, e))
                fail += 1
            time.sleep(0.5)      # 慢点发，别撞限流
        self.send_html(uid, '广播完成\n成功：%d\n失败：%d\n耗时：%.2f秒'
                       % (ok, fail, time.time() - started))

    # ================= 菜单过期 =================
    def _expire_menus(self):
        now = time.time()
        for store in (self._bc, self._cl):
            for uid in [k for k, v in store.items()
                        if now - (v.get('at') or 0) > MENU_TTL]:
                store.pop(uid, None)
        if len(self._bc) > 200:
            self._bc.clear()
        if len(self._cl) > 200:
            self._cl.clear()

    # ================= 群目录维护 =================
    def refresh_groups(self):
        """轮着核实群还在不在 —— 每次只查几个，别一次刷几十个 API"""
        try:
            rows = [dict(r) for r in self.store.list_active_bot_groups()]
        except Exception:
            return
        if not rows:
            return
        with self._check_lock:
            start = self._check_idx % len(rows)
            self._check_idx = (start + GROUP_CHECK_PER_TICK) % max(len(rows), 1)
        for row in rows[start:start + GROUP_CHECK_PER_TICK]:
            if self.stop_evt.is_set():
                return
            cid = int(row['chat_id'])
            try:
                chat = self.api.call('getChat', chat_id=cid)
            except TgError as e:
                msg = str(e)
                # ★ 只有明确说「找不到/被踢」才注销。超时、限流这些
                #   不确定的错误绝不能当「群没了」—— 那样会把好群清掉
                if 'chat not found' in msg.lower() or 'kicked' in msg.lower() \
                        or 'bot was blocked' in msg.lower() or '403' in msg:
                    self.store.deactivate_bot_chat(cid)
                    self.positions.forget(cid)
                    log('[%s] 群 %s 已不可用，不再广播给它' % (self.note(), cid))
                continue
            except Exception:
                continue
            new_id = int(chat.get('id') or cid)
            if new_id != cid:      # 群升级了，id 变了
                try:
                    self.store.migrate_bot_chat(cid, new_id, chat.get('title') or '')
                except Exception:
                    pass
                continue

    # ================= 账单 =================
    def bill_keyboard(self, chat_id):
        import customer_config as CC
        try:
            mode = self.store.get_ledger_view_mode(int(chat_id))
        except Exception:
            mode = 'detailed'
        other = 'compact' if mode == 'detailed' else 'detailed'
        label = '简洁' if mode == 'detailed' else '详细'
        keyboard = {'inline_keyboard': [
            [{'text': '今日', 'callback_data': 'ledger:today'},
             {'text': '昨日', 'callback_data': 'ledger:yesterday'}],
            [{'text': '↪️ 切换%s' % label,
              'callback_data': 'ledger:view:%s:today' % other}],
        ]}
        if not CC.feature(self.bot, 'bill_switch'):
            keyboard['inline_keyboard'] = keyboard['inline_keyboard'][:1]
        return keyboard

    def send_bill(self, chat_id, scope='today'):
        """出账单（带按钮 + 长消息自动分块）"""
        st = self.store
        self.customer.sync()
        try:
            mode = st.get_ledger_view_mode(int(chat_id))
        except Exception:
            mode = 'detailed'
        text = C.format_bill(st, int(chat_id), scope=scope,
                             show_all_records=(mode == 'detailed'))
        chunks = text_utils.split_html_message(text)
        return bill_messages.send_bill(self.api, int(chat_id), chunks,
                                       self.bill_keyboard(chat_id), st)

    # ================= 按钮 =================
    def allow_callback(self, cid):
        """群里谁都能点账单按钮；真正的权限在具体动作里判"""
        return True

    def on_callback(self, cq):
        if self.customer.callback(cq):
            return
        data = cq.get('data') or ''
        if not data.startswith(('ledger:', 'broadcast:', 'cleanup:',
                                'mine:', 'stats:', tron_watch.PREFIX)):
            return
        cb_id = cq.get('id')
        frm = cq.get('from') or {}
        uid = frm.get('id')
        msg = cq.get('message') or {}
        chat = msg.get('chat') or {}
        chat_id = chat.get('id')
        mid = msg.get('message_id')

        # 到期了就不响应（callback 走不到基类的到期检查）
        if self.expired():
            self._answer(cb_id, '机器人已到期')
            return
        try:
            if data.startswith('broadcast:'):
                self.broadcast_callback(cb_id, uid, mid, data)
            elif data.startswith('cleanup:'):
                self.cleanup_callback(cb_id, uid, mid, data)
            elif data.startswith('mine:'):
                self.mine_callback(cb_id, uid, mid, data)
            elif data.startswith('stats:'):
                if chat.get('type') != 'private' or chat_id != uid:
                    self._answer(cb_id, '请在自己的私聊统计菜单操作')
                else:
                    self.mine_callback(cb_id, uid, mid, data, all_members=True)
            elif data.startswith(tron_watch.PREFIX):
                self._answer(cb_id, '')
                # ★ 按钮是**私聊**里的，chat_id 就是那个人的私聊
                self.tronw.on_callback(chat_id or uid, uid, mid, data)
            elif data == 'ledger:help':
                self._answer(cb_id, '')
                self.send_html(chat_id or uid, C.HELP_TEXT)
            elif not chat_id:
                self._answer(cb_id, '')
            elif data.startswith('ledger:view:') and not self.can_manage(int(chat_id), uid):
                self._answer(cb_id, '没有本群账单修改权限')
            else:
                self._do_callback(cb_id, int(chat_id), mid, data)
        except TgError as e:
            log('[%s] 按钮处理失败：%s' % (self.note(), e))
            self._answer(cb_id, '操作失败')
        except Exception as e:
            log('[%s] 按钮异常：%s' % (self.note(), e))
            self._answer(cb_id, '操作失败')

    def _do_callback(self, cb_id, chat_id, mid, data):
        st = self.store
        self.customer.sync()
        parts = data.split(':')
        scope = None
        if data in ('ledger:today', 'ledger:yesterday'):
            scope = 'today' if data.endswith('today') else 'yesterday'
        elif len(parts) == 4 and parts[1] == 'view':
            import customer_config as CC
            if not CC.feature(self.bot, 'bill_switch'):
                self._answer(cb_id, '账单切换已关闭')
                return
            mode, scope = parts[2], parts[3]
            if mode not in ('compact', 'detailed'):
                self._answer(cb_id, '')
                return
            if scope not in ('today', 'yesterday', 'full'):
                scope = 'today'
            try:
                st.set_ledger_view_mode(chat_id, mode)
            except Exception:
                pass
        else:
            self._answer(cb_id, '')
            return

        self._answer(cb_id, '刷新中…')
        try:
            mode = st.get_ledger_view_mode(chat_id)
        except Exception:
            mode = 'detailed'
        text = C.format_bill(st, chat_id, scope=scope,
                             show_all_records=(mode == 'detailed'))
        chunks = text_utils.split_html_message(text)
        bill_messages.replace_bill(self.api, chat_id, mid, chunks,
                                   self.bill_keyboard(chat_id), st)

    def _answer(self, cb_id, text=''):
        try:
            self.api.call('answerCallbackQuery', callback_query_id=cb_id, text=text)
        except TgError:
            pass

    # ================= 发消息 =================
    def send_photo_html(self, chat_id, photo, caption, kb=None):
        """发图，文字挂在图的**说明**上（caption）。

        ★ 历史图片使用 TG file_id，小程序上传的图片使用本地欢迎图。
        ★ 说明（caption）上限 **1024 字**，比普通消息的 4096 紧得多。
        ★ 说明里**不能用 tg://user 链接**（点了没反应）—— 调用方负责
          （见 _welcome_line 的 tg_link 参数）。
        """
        from customer_images import LOCAL_PHOTO, image_path
        if photo == LOCAL_PHOTO:
            try:
                with image_path(self.bot).open('rb') as image:
                    return self.api.call_file('sendPhoto', 'photo', 'welcome.jpg', image,
                                              chat_id=chat_id, parse_mode='HTML', caption=caption or '')
            except (OSError, TgError):
                return self.send_html(chat_id, caption or '')
        params = dict(chat_id=chat_id, photo=photo, parse_mode='HTML',
                      caption=caption or '')
        if kb:
            params['reply_markup'] = kb
        try:
            return self.api.call('sendPhoto', **params)
        except TgError as e:
            # 说明里有没转义的 < > 会让整条被拒 —— 退回纯文本说明重发一次
            log('[%s] 发图失败，退回纯文本说明：%s' % (self.note(), e))
            try:
                return self.api.call(
                    'sendPhoto', chat_id=chat_id, photo=photo,
                    caption=(caption or '').replace('<', '＜').replace('>', '＞'))
            except TgError as e2:
                log('[%s] 发图也失败了：%s' % (self.note(), e2))
                # ★ 连图都发不出去（最常见的原因是**面板里换过机器人 token**，
                #   老图的那个 file_id 就作废了）—— 退回发文字。
                #   不兜这一下的话，新成员进群**一句话都看不到**，
                #   而且主人完全不知道发生了什么。
                if caption:
                    log('[%s] 退回发文字（欢迎图可能已经失效，'
                        '让主人重发一张）' % self.note())
                    return self.send_html(chat_id, caption)
                return None

    def send_html(self, chat_id, text, kb=None, reply_to=None):
        params = dict(chat_id=chat_id, text=text, parse_mode='HTML',
                      disable_web_page_preview=True)
        if kb:
            params['reply_markup'] = kb
        if reply_to:
            params['reply_to_message_id'] = reply_to
        try:
            return self.api.call('sendMessage', **params)
        except TgError as e:
            # ★ 账单里混进了没转义的 < > 会让 TG 整条拒收 —— 退回纯文本，
            #   并且一定要 log 出来，否则账单悄悄退化了都不知道
            log('[%s] HTML 发送失败，退回纯文本：%s' % (self.note(), e))
            if kb:
                params.pop('reply_markup', None)
            try:
                return self.api.call('sendMessage', chat_id=chat_id,
                                     text=text.replace('<', '＜').replace('>', '＞'),
                                     disable_web_page_preview=True)
            except TgError as e2:
                log('[%s] 退回纯文本也失败了：%s' % (self.note(), e2))
                return None

    def edit_html(self, chat_id, mid, text, kb=None, extra=''):
        """改自己发过的消息（菜单刷新用）。mid 丢了就退化成发新消息"""
        if not mid:
            return self.send_html(chat_id, text + extra, kb=kb)
        params = dict(chat_id=chat_id, message_id=mid, text=text + extra,
                      parse_mode='HTML', disable_web_page_preview=True)
        if kb:
            params['reply_markup'] = kb
        try:
            return self.api.call('editMessageText', **params)
        except TgError as e:
            if 'message is not modified' in str(e).lower():
                return None
            log('[%s] 编辑消息失败：%s' % (self.note(), e))
            return self.send_html(chat_id, text + extra, kb=kb)

    # ================= 面板配置 =================
    def help_text(self, is_admin=False):
        return C.HELP_TEXT
