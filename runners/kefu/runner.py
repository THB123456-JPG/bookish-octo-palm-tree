# -*- coding: utf-8 -*-
"""客服机器人：客户私聊 → 转到管理员的私聊 → 管理员回复 → 发给客户"""
import time
from datetime import datetime

from core import EXPIRED_MSG, BaseRunner, TgError, log, raw_name, tag_of

CAPTIONABLE = ('photo', 'video', 'document', 'animation', 'audio')

# 到期后提醒绑定者的冷却时间（秒），同一个客户这段时间内只提醒一次
EXPIRED_NOTICE_COOLDOWN = 1800


class KefuRunner(BaseRunner):
    kind = 'kefu'
    poll_timeout = 20

    # -------- 数据快捷方式 --------
    @property
    def mapping(self):
        return self.data.setdefault('mapping', {})

    @property
    def customers(self):
        return self.data.setdefault('customers', {})

    @property
    def blocked(self):
        return set(self.data.setdefault('blocked', []))

    def save_data(self):
        # 对应关系太多时丢掉最早的，避免文件无限增长
        if len(self.mapping) > self.max_mapping:
            keys = list(self.mapping.keys())
            for k in keys[:len(keys) - self.max_mapping]:
                self.mapping.pop(k, None)
        super().save_data()

    # ================= 到期后的行为 =================
    def on_expired(self, cid, frm, is_admin):
        """到期后：
        - 绑定者/操作人发消息 → 回一句到期提示（让他知道要续费）
        - 客户发消息 → 不回客户（不让客户知道欠费），只提醒绑定者
        """
        if is_admin:
            self.send(cid, EXPIRED_MSG)
            return

        owner = self.owner_id()
        if not owner:
            return
        # 同一个客户半小时内只提醒一次，别刷屏
        now = time.time()
        if now - self._expired_notice.get(cid, 0) < EXPIRED_NOTICE_COOLDOWN:
            return
        self._expired_notice[cid] = now
        self.send(owner,
                  '⚠️ 有客户给机器人发消息了，但机器人已到期，消息没能转给你。\n\n'
                  '客户：%s\n\n%s' % (tag_of(frm), EXPIRED_MSG))
        log('[%s] 到期中，客户 %s 的消息没能转发，已提醒绑定者'
            % (self.note(), raw_name(frm)))

    # ================= 其他人（客户）发来的消息 =================
    def on_guest(self, msg):
        user = msg.get('from') or {}
        cid = user.get('id')
        text = (msg.get('text') or '').strip()

        if text.startswith('/start'):
            rec = self.customers.setdefault(str(cid), {})
            rec.update({'name': raw_name(user), 'username': user.get('username') or '',
                        'last': datetime.now().strftime('%Y-%m-%d %H:%M')})
            self.save_data()
            self.send(cid, self.mgr.cfg.get('welcome') or '您好！请直接留言。')
            return

        if cid in self.blocked:
            return
        if not self.admins():
            self.send(cid, '【系统提示】客服尚未上线，请稍后再试。')
            return

        rec = self.customers.setdefault(str(cid), {})
        rec.update({
            'name': raw_name(user),
            'username': user.get('username') or '',
            'last': datetime.now().strftime('%Y-%m-%d %H:%M'),
            'count': int(rec.get('count') or 0) + 1,
            'last_msg': msg['message_id'],
        })
        self.relay_to_admin(user, msg)
        self.save_data()

    def relay_to_admin(self, user, msg):
        """把客户消息转发给所有管理员，并记住「哪条消息对应哪个客户」"""
        cid = user['id']
        tag = tag_of(user)
        text = msg.get('text')
        pairs = []

        if text is not None:
            pairs = self.send_to_admins('%s\n\n%s' % (tag, text))
        else:
            cap = msg.get('caption')
            mtype = next((k for k in CAPTIONABLE if msg.get(k)), None)
            one_shot = False

            if mtype:
                for aid in sorted(self.admins()):
                    try:
                        r = self.api.call('copyMessage', chat_id=aid,
                                          from_chat_id=cid, message_id=msg['message_id'],
                                          caption=(tag + '\n\n' + cap) if cap else tag)
                        pairs.append((aid, r['message_id']))
                    except TgError as e:
                        log('[%s] 带说明复制失败：%s' % (self.note(), e))
                one_shot = bool(pairs)

            if not one_shot:
                # 贴纸/语音/视频留言这类不支持 caption：先发一条说明，再复制原消息
                heads = self.send_to_admins(tag + ' 发来一条消息：')
                for aid in sorted(self.admins()):
                    try:
                        r = self.api.call('copyMessage', chat_id=aid,
                                          from_chat_id=cid, message_id=msg['message_id'])
                        pairs.append((aid, r['message_id']))
                    except TgError as e:
                        log('[%s] 复制消息失败：%s' % (self.note(), e))
                        self.send(aid, '（这条消息类型无法转发：%s）' % e)
                pairs += heads

        for aid, mid in pairs:
            self.mapping['%s:%s' % (aid, mid)] = cid
        log('[%s] 客户 %s(%s) 的留言已转出' % (self.note(), raw_name(user), cid))

    # ================= 管理员发来的消息 =================
    def on_owner(self, msg):
        frm = msg.get('from') or {}
        aid = frm.get('id')
        text = (msg.get('text') or '').strip()

        if text.startswith('/start') or text.startswith('/help'):
            self.send(aid, self.help_text())
            return
        if text.startswith('/list'):
            self.cmd_list(aid)
            return
        if text.startswith('/block'):
            self.cmd_block(aid, text, True)
            return
        if text.startswith('/unblock'):
            self.cmd_block(aid, text, False)
            return
        if text.startswith('/blocked'):
            if self.blocked:
                self.send(aid, '已拉黑 %d 个客户：\n%s' % (
                    len(self.blocked), '\n'.join(str(x) for x in sorted(self.blocked))))
            else:
                self.send(aid, '拉黑名单为空。')
            return
        if text.startswith('/reply'):
            self.cmd_reply(aid, text)
            return

        # 回复机器人转发的那条消息 → 发回给对应客户
        rt = msg.get('reply_to_message')
        if rt:
            cid = self.mapping.get('%s:%s' % (aid, rt['message_id']))
            if cid:
                self.deliver(cid, aid, msg)
                return

        self.send(aid, '⚠️ 这条没有发出去。\n\n'
                       '要给客户回消息，请【长按 / 右键 → 回复】机器人转发的那条客户消息，'
                       '再输入内容发送。\n'
                       '也可以用：/reply 客户ID 内容\n'
                       '查看客户：/list')

    def deliver(self, cid, aid, admin_msg):
        rec = self.customers.get(str(cid)) or {}
        params = dict(chat_id=cid, from_chat_id=aid, message_id=admin_msg['message_id'])
        if rec.get('last_msg'):
            params['reply_to_message_id'] = rec['last_msg']
        try:
            self.api.call('copyMessage', **params)
            log('[%s] 已回复客户 %s(%s)' % (self.note(), rec.get('name') or '', cid))
        except TgError as e:
            log('[%s] 回复客户 %s 失败：%s' % (self.note(), cid, e))
            self.send(aid, '⚠️ 发送失败（客户可能已拉黑机器人）：%s' % e)

    def cmd_reply(self, aid, text):
        parts = text.split(None, 2)
        if len(parts) < 3:
            self.send(aid, '用法：/reply 客户ID 内容\n客户ID 可以用 /list 查看')
            return
        try:
            cid = int(parts[1])
        except ValueError:
            self.send(aid, '客户 ID 必须是数字，例如 /reply 123456789 你好')
            return
        r = self.send(cid, parts[2])
        if r:
            rec = self.customers.get(str(cid)) or {}
            rec['last_msg'] = r['message_id']
            self.customers[str(cid)] = rec
            self.save_data()
            self.send(aid, '✅ 已发送给客户 %s' % cid)

    def cmd_list(self, aid):
        if not self.customers:
            self.send(aid, '还没有客户联系过。')
            return
        items = sorted(self.customers.items(),
                       key=lambda kv: kv[1].get('last') or '', reverse=True)[:30]
        lines = ['最近联系的客户（共 %d 个，显示前 30）：' % len(self.customers), '']
        for cid, rec in items:
            flag = ' 🚫' if int(cid) in self.blocked else ''
            lines.append('%s\n   ID:%s%s  最后:%s  消息:%s' % (
                rec.get('name') or '?', cid, flag,
                rec.get('last') or '?', rec.get('count') or 0))
        lines.append('')
        lines.append('回复：直接【回复】客户的消息，或 /reply ID 内容')
        self.send(aid, '\n'.join(lines))

    def cmd_block(self, aid, text, block):
        parts = text.split()
        if len(parts) < 2:
            self.send(aid, '用法：%s 客户ID' % ('/block' if block else '/unblock'))
            return
        try:
            cid = int(parts[1])
        except ValueError:
            self.send(aid, '客户 ID 必须是数字')
            return
        bl = self.blocked
        if block:
            bl.add(cid)
        else:
            bl.discard(cid)
        self.data['blocked'] = sorted(bl)
        self.send(aid, '已%s客户 %s' % ('拉黑' if block else '解除拉黑', cid))
        self.save_data()

    # -------- 文案 --------
    def welcome_owner(self):
        return ('从现在起：\n'
                '• 客户给机器人发的消息，都会转发到你这里\n'
                '• 你【回复】那条消息，内容就会发给对应客户\n'
                '• 输入 /help 查看全部指令')

    @staticmethod
    def help_text():
        return ('📖 指令说明\n'
                '\n'
                '【回复客户】\n'
                '长按 / 右键机器人转发的那条客户消息 → 点「回复」→ 输入内容发送\n'
                '\n'
                '/reply 客户ID 内容   指定发给某个客户\n'
                '/list                查看最近联系的客户\n'
                '/block 客户ID        拉黑该客户\n'
                '/unblock 客户ID      解除拉黑\n'
                '/blocked             查看拉黑名单\n'
                '/id                  查看你的 ID\n'
                '/help                显示这段说明')
