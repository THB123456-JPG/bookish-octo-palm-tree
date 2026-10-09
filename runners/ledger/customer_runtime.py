"""Ledger customer rules. Scheduled sends use durable claims, not in-memory timers."""
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import closing, nullcontext
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from html import escape

import customer_config as CC
from . import commands as C

TZ = timezone(timedelta(hours=8))


def authorized(bot, uid):
    p = CC.grants(bot, uid)
    return p['manage'] or p['broadcast']


def next_due(rule, now):
    if rule['mode'] == 'once':
        return datetime.fromisoformat(rule['at']).timestamp()
    if rule['mode'] == 'interval':
        return now + rule['interval'] * 60
    hour, minute = map(int, rule['at'].split(':'))
    dt = datetime.fromtimestamp(now, TZ).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if dt.timestamp() <= now:
        dt += timedelta(days=1)
    return dt.timestamp()


class CustomerRuntime:
    def __init__(self, runner):
        self.r = runner
        self.path = str(runner.db_path) + '.customer'
        self.jobs_lock = threading.Lock()
        self.pending = {}
        self.replied = {}
        self.address_inputs = {}
        self.pruned_month = None

    def sync(self):
        month=datetime.now(TZ).strftime('%Y-%m')
        if self.pruned_month != month:
            self.r.store.prune_month()
            self.pruned_month=month
        cfg = CC.settings(self.r.bot)
        self.r.store.customer_options = cfg['features']
        self.r.store.default_currency = (self.r.bot.get('ledger') or {}).get('group_currency', {})
        self.r.store.customer_group_ops = {int(uid): p['groups'] for uid, p in
            (self.r.bot.get('ledger') or {}).get('staff_grants', {}).items()}
        return cfg

    def new_group(self, cid):
        st = self.r.store
        if st.conn.execute('SELECT 1 FROM chat_settings WHERE chat_id=?', (cid,)).fetchone():
            return
        b = CC.settings(self.r.bot)['basics']
        st.ensure_chat(cid)
        st.set_ledger_reset_hour(cid, b['cutoff_hour'])
        st.set_ledger_view_mode(cid, b['view_mode'])
        st.set_all_members_can_record(cid,b['all_members_can_record'])
        with getattr(self.r.mgr, 'lock', nullcontext()):
            self.r.bot.setdefault('ledger', {}).setdefault('group_currency', {})[str(cid)] = b['currency']
            self.r.mgr.save()

    def notify(self, cid, message):
        for uid in sorted(self.r.ledger_owners(cid)):
            self.r.send_html(uid, message)

    def metadata(self, cid, chat, actor):
        st, cfg = self.r.store, self.sync()
        row = st.conn.execute('SELECT title FROM bot_chats WHERE chat_id=?', (cid,)).fetchone()
        if cfg['features']['title_notice'] and row and row[0] != chat.get('title'):
            self.notify(cid, '群名变更：%s → %s' % (escape(row[0] or ''), escape(chat.get('title') or '')))
        if actor and cfg['features']['user_notice']:
            old = st.conn.execute('SELECT username,display_name FROM known_users WHERE chat_id=? AND user_id=?', (cid, actor.user_id)).fetchone()
            if old and (old[0], old[1]) != (actor.username, actor.display_name):
                lines = ['用户修改信息通知：' + escape(actor.label)]
                if old[0] != actor.username:
                    before = '@' + old[0] if old[0] else '未设置'
                    after = '@' + actor.username if actor.username else '未设置'
                    lines.append('用户名：%s → %s' % (escape(before), escape(after)))
                if old[1] != actor.display_name:
                    lines.append('昵称：%s → %s' % (escape(old[1]), escape(actor.display_name)))
                self.r.send_html(cid, '\n'.join(lines))

    def deposit(self, cid):
        cfg = self.sync()
        if not cfg['features']['deposit_notice']:
            return
        deposit = Decimal(cfg['deposits'].get(str(cid), '0'))
        if deposit <= 0:
            return
        entries = C._entries_for_scope(self.r.store, cid, 'today')
        balance = C._summarize_entries(self.r.store, cid, entries).balance_usdt
        balance += self.r.store.opening_balance(cid,self.r.store.current_accounting_date(cid))
        if cfg['features']['carry_balance']:
            prior = self.r.store.entries(cid, accounting_date=self.r.store.previous_accounting_date(cid))
            balance += C._summarize_entries(self.r.store, cid, prior).balance_usdt
            balance += self.r.store.opening_balance(cid,self.r.store.previous_accounting_date(cid))
        stage = 2 if balance >= deposit else (1 if balance >= deposit * Decimal('.8') else 0)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS deposit_state(chat_id INTEGER PRIMARY KEY, stage INTEGER, amount TEXT, period TEXT)')
            row = db.execute('SELECT stage,amount,period FROM deposit_state WHERE chat_id=?', (cid,)).fetchone()
            period = self.r.store.current_accounting_date(cid)
            changed = not row or row != (stage, str(deposit), period)
            db.execute('INSERT OR REPLACE INTO deposit_state VALUES(?,?,?,?)', (cid, stage, str(deposit), period))
        if changed and stage:
            self.r.send_html(cid, '⚠️ %s：未下发 %sU，群押金 %sU。' % ('押金风险预警' if stage == 2 else '押金80%提醒', balance, deposit))

    def can_record(self, cid, uid):
        p = CC.grants(self.r.bot, uid)
        explicit = str(uid) in (self.r.bot.get('ledger') or {}).get('staff_grants', {})
        if explicit:
            return (p['global_ops'] or cid in p['groups'] or
                    self.r.store.is_operator(cid, uid, {CC.owner(self.r.bot)}))
        return C._can_record(self.r.store, cid, uid, self.r.ledger_owners(cid))

    def edit(self, cid, actor, text, msg):
        reply = msg.get('reply_to_message') or {}
        original = self.r.store.entry_for_source_message(cid, reply.get('message_id'))
        if original is None:
            return False
        try:
            parsed = C._parse_entry(text)
        except ValueError:
            return False
        if parsed is None or parsed[1] == 0:
            return False
        if not self.r.store.is_ledger_enabled(cid) or not self.can_record(cid, actor.user_id):
            self.r.send_html(cid, '没有修改此群流水的权限。')
            return True
        token = uuid.uuid4().hex[:12]
        state = dict(cid=cid, uid=actor.user_id, parsed=parsed, original=original, actor=actor,
                     mid=msg.get('message_id'), until=time.time()+300)
        self.pending = {k: v for k, v in self.pending.items() if v['until'] > time.time()}
        if CC.feature(self.r.bot, 'edit_confirm'):
            self.pending[token] = state
            self.r.send_html(cid, '修改原流水 #%s：%s → %s。是否确认？' % (original.id, original.amount, parsed[1]),
                kb={'inline_keyboard': [[{'text':'确认修改','callback_data':'lc:edit:'+token}, {'text':'取消','callback_data':'lc:cancel:'+token}]]})
        else:
            self.apply_edit(state)
        return True

    def apply_edit(self, state):
        cid, actor = state['cid'], state['actor']
        self.sync()
        if self.r.expired() or not self.can_record(cid, actor.user_id) or not self.r.store.is_ledger_enabled(cid):
            raise ValueError('授权已撤销、群已暂停或机器人到期')
        kind, amount, note, rate, fee, currency = state['parsed']
        if CC.feature(self.r.bot, 'pure_u'):
            currency, rate, fee = 'U', Decimal(1), Decimal(0)
        elif currency == 'CNY' and self.r.store.default_currency.get(str(cid)) == 'U':
            currency = 'U'
        self.r.store.add_entry(cid, kind, amount, currency, note or state['original'].note,
                              actor.user_id, actor.display_name, source_message_id=state['mid'],
                              rate=rate, entry_fee_percent=fee, replace_id=state['original'].id,
                              expected_entry=state['original'])
        self.r.send_bill(cid)
        self.deposit(cid)

    def callback(self, cq):
        data = cq.get('data', '')
        if not data.startswith('lc:'):
            return False
        uid = (cq.get('from') or {}).get('id')
        cid = ((cq.get('message') or {}).get('chat') or {}).get('id')
        parts = data.split(':')
        try:
            if self.r.expired():
                raise ValueError('机器人已到期')
            if parts[1] == 'archive':
                row = self.r.store.conn.execute('SELECT body FROM customer_bill_archives WHERE id=? AND chat_id=?', (int(parts[2]), cid)).fetchone()
                if not row:
                    raise ValueError('该存档不属于当前群')
                from . import text_utils
                for chunk in text_utils.split_html_message(row[0]):
                    self.r.send_html(cid, chunk)
            elif parts[1] in ('edit', 'cancel'):
                state = self.pending.get(parts[2])
                if not state or state['cid'] != cid or state['uid'] != uid or state['until'] < time.time():
                    raise ValueError('请由发起人操作，或重新发起修改')
                self.pending.pop(parts[2])
                if parts[1] == 'edit':
                    self.apply_edit(state)
            elif parts[1] == 'verify':
                if not CC.feature(self.r.bot, 'admin_confirm') or not CC.feature(self.r.bot, 'tron_verify'):
                    raise ValueError('地址确认已关闭')
                member = self.r.api.call('getChatMember', chat_id=cid, user_id=uid)
                if member.get('status') not in ('creator', 'administrator'):
                    raise ValueError('仅群管理员可以确认')
                from .tron_chain import address_valid
                if not address_valid(parts[2]):
                    raise ValueError('地址无效')
                self.r.send_html(cid, '群管理员（ID %s）已核对地址：<code>%s</code>\n仅地址核对，不代表已收款。' % (uid, parts[2]))
            self.r._answer(cq['id'], '已处理')
        except (ValueError, sqlite3.Error) as e:
            self.r._answer(cq['id'], str(e) if isinstance(e, ValueError) else '存档暂不可用')
        return True

    def reply(self, cid, text, mid):
        now = time.monotonic()
        if now - self.replied.get(cid, -10) < 2 or self.r.expired():
            return
        for rule in CC.settings(self.r.bot)['replies']:
            if not rule['enabled'] or cid not in rule['groups'] or not authorized(self.r.bot, rule['author']):
                continue
            hit = any(text == keyword if rule['match'] == 'exact' else (keyword in text if rule['match'] == 'contains' else text.startswith(keyword)) for keyword in rule['keyword'].split())
            if hit:
                self.replied[cid] = now
                self.r.send(cid, rule['content'], reply_to_message_id=mid)
                return

    def addresses(self, cid):
        ledger = self.r.bot.get('ledger') or {}
        groups = ledger.get('group_addresses') or {}
        if str(cid) in groups:
            return list(groups[str(cid)])
        saved = ledger.get('customer') or {}
        if 'addresses' in saved:
            return list(saved['addresses'])
        default = ledger.get('payout_address') or ''
        return [default] if default else []

    def address_command(self, cid, actor, text):
        """Each group's conversation belongs to its initiating user, with a short TTL."""
        command = text.strip().lstrip('/')
        key = (cid, actor.user_id)
        self.address_inputs = {k: v for k, v in self.address_inputs.items() if v['until'] > time.time()}
        state = self.address_inputs.get(key)
        if command == '下发地址':
            if CC.feature(self.r.bot, 'show_address'):
                addresses = self.addresses(cid)
                self.r.send_html(cid, '✅️本群下发地址：\n' + '\n'.join('<code>%s</code>' % a for a in addresses) + '\n⚠️请认真核对该地址再进行转入。' if addresses else '本群尚未设置下发地址。')
            return True
        if command in ('设置下发地址', '删除下发地址'):
            if not self.r.can_manage(cid, actor.user_id):
                self.r.send_html(cid, '没有本群管理权限。')
                return True
            addresses = self.addresses(cid)
            if command == '删除下发地址' and not addresses:
                self.r.send_html(cid, '本群没有可删除的下发地址。')
                return True
            self.address_inputs[key] = dict(mode='add' if command=='设置下发地址' else 'choose',until=time.time()+300,addresses=addresses)
            self.r.send_html(cid, '请输入地址（TRON／TRC20），输入“取消”结束。' if command=='设置下发地址' else
                             '\n'.join('%d. <code>%s</code>' % (i,a) for i,a in enumerate(addresses,1))+'\n请输入要删除的编号，输入“取消”结束。')
            return True
        if not state:
            return False
        if not self.r.can_manage(cid, actor.user_id) or self.r.expired():
            self.address_inputs.pop(key,None)
            self.r.send_html(cid,'权限已撤销或机器人已到期，操作已取消。')
            return True
        if command in ('取消','取消设置','取消删除'):
            self.address_inputs.pop(key,None)
            self.r.send_html(cid,'已取消。')
            return True
        from .tron_chain import address_valid
        with self.r.mgr.lock:
            current = self.addresses(cid)
            if current != state['addresses']:
                self.address_inputs.pop(key,None)
                self.r.send_html(cid,'地址列表已变化，请重新发起设置或删除。')
                return True
            if state['mode']=='add':
                if not address_valid(command):
                    self.r.send_html(cid,'地址无效，请重新发送完整TRON地址，或输入“取消”。')
                    return True
                if command not in current:
                    if len(current)>=20:
                        self.r.send_html(cid,'本群最多保存20个地址，请先删除旧地址。')
                        return True
                    current.append(command)
                self.r.bot.setdefault('ledger',{}).setdefault('group_addresses',{})[str(cid)]=current
                saved = self.r.bot['ledger'].setdefault('customer', {})
                saved['revision'] = saved.get('revision', 0) + 1
                self.r.mgr.save()
                self.address_inputs.pop(key,None)
                self.r.send_html(cid,'设置成功：<code>%s</code>' % command)
            elif state['mode']=='choose':
                if not command.isdigit() or not 1<=int(command)<=len(current):
                    self.r.send_html(cid,'请输入列表中有效的编号。')
                    return True
                state.update(mode='confirm',selected=current[int(command)-1])
                self.r.send_html(cid,'将删除地址：<code>%s</code>\n请输入“确认”删除，或“取消”。' % state['selected'])
            elif command=='确认':
                current.remove(state['selected'])
                self.r.bot.setdefault('ledger',{}).setdefault('group_addresses',{})[str(cid)]=current
                saved = self.r.bot['ledger'].setdefault('customer', {})
                saved['revision'] = saved.get('revision', 0) + 1
                self.r.mgr.save()
                self.address_inputs.pop(key,None)
                self.r.send_html(cid,'删除成功。')
            else:
                self.r.send_html(cid,'请输入“确认”删除，或“取消”。')
        return True

    def tick(self):
        if self.jobs_lock.locked() or self.r.expired():
            return
        threading.Thread(target=self.run_jobs, daemon=True, name='customer-ads').start()

    def run_jobs(self, now=None):
        if not self.jobs_lock.acquire(False):
            return
        try:
            now = time.time() if now is None else now
            cfg = CC.settings(self.r.bot)
            rules = [r for r in cfg['ads'] if r['enabled'] and authorized(self.r.bot, r['author'])]
            with closing(sqlite3.connect(self.r.db_path)) as db:
                active = {r[0] for r in db.execute('SELECT chat_id FROM bot_chats WHERE is_active=1')}
            with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
                db.execute('CREATE TABLE IF NOT EXISTS ad_jobs(id TEXT PRIMARY KEY, fingerprint TEXT, due REAL)')
                for rule in rules:
                    fingerprint = hashlib.sha256(json.dumps(rule, sort_keys=True).encode()).hexdigest()
                    row = db.execute('SELECT fingerprint,due FROM ad_jobs WHERE id=?', (rule['id'],)).fetchone()
                    if not row or row[0] != fingerprint:
                        db.execute('INSERT OR REPLACE INTO ad_jobs VALUES(?,?,?)', (rule['id'],fingerprint,next_due(rule,now)))
                    else:
                        due = row[1]
                        if not due or due > now:
                            continue
                        upcoming = 0 if rule['mode'] == 'once' else next_due(rule, now)
                        db.execute('UPDATE ad_jobs SET due=? WHERE id=? AND due=?', (upcoming,rule['id'],due))
                        db.commit()  # Claim before sending: restart cannot send the same occurrence twice.
                        for cid in rule['groups']:
                            current = next((r for r in CC.settings(self.r.bot)['ads'] if r['id'] == rule['id']), None)
                            if self.r.stop_evt.is_set() or self.r.expired() or current != rule or not authorized(self.r.bot, rule['author']):
                                break
                            if cid in active:
                                if rule.get('image'):
                                    from customer_images import ad_path
                                    caption = rule['content'] if len(rule['content'].encode('utf-16-le')) <= 2048 else ''
                                    with ad_path(self.r.bot, rule['image']).open('rb') as image:
                                        self.r.api.call_file('sendPhoto', 'photo', 'advertisement.jpg', image,
                                                             chat_id=cid, caption=caption)
                                    if rule['content'] and not caption:
                                        self.r.send(cid, rule['content'])
                                else:
                                    self.r.send(cid, rule['content'])
                ids = {r['id'] for r in rules}
                for row in db.execute('SELECT id FROM ad_jobs').fetchall():
                    if row[0] not in ids:
                        db.execute('DELETE FROM ad_jobs WHERE id=?', (row[0],))
        finally:
            self.jobs_lock.release()
