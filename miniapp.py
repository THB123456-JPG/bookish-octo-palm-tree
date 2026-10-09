"""Telegram customer portal. No management-panel session or bot secrets leave it."""
import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone, date
from contextlib import closing
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import parse_qsl

import core
import customer_config as CC
from customer_ui import base_url
from urllib.error import HTTPError

DEFAULT_FOOTER = '联系开发者@HCW2026'


class RemoteRequests:
    """Transient requests only; customer configuration stays on their server."""
    def __init__(self):
        self.changed = threading.Condition()
        self.jobs = {}

    def call(self, bid, name, body, timeout=25):
        request_id = uuid.uuid4().hex
        job = dict(bid=bid, id=request_id, action=name, body=body,
                   expires=time.time() + timeout, taken=False, result=None,
                   done=threading.Event())
        with self.changed:
            if len(self.jobs) >= 32 or sum(j['bid'] == bid for j in self.jobs.values()) >= 4:
                raise OSError('配置请求较多，请稍后重试')
            self.jobs[request_id] = job
            self.changed.notify_all()
        try:
            if not job['done'].wait(timeout):
                raise OSError('客户服务器暂未响应，请刷新确认结果后再操作')
            return job['result']
        finally:
            with self.changed:
                self.jobs.pop(request_id, None)

    def exchange(self, bid, reply, timeout=10):
        with self.changed:
            if reply:
                result = reply.get('result')
                if (not isinstance(result, dict) or type(result.get('ok')) is not bool
                    or reply.get('status') not in (200, 400, 403, 503)
                    or (result['ok'] and not isinstance(result.get('data'), dict))
                    or (not result['ok'] and not isinstance(result.get('error'), str))):
                    raise ValueError('配置响应格式无效')
                job = self.jobs.get(reply.get('id'))
                if job and job['bid'] == bid and job['taken'] and job['result'] is None:
                    job['result'] = reply
                    job['done'].set()
            end = time.monotonic() + timeout
            while True:
                for job in self.jobs.values():
                    if job['bid'] == bid and not job['taken'] and job['expires'] > time.time():
                        # Dispatch once: lost responses must never repeat a write.
                        job['taken'] = True
                        return {k: job[k] for k in ('id', 'action', 'body', 'expires')}
                left = end - time.monotonic()
                if left <= 0:
                    return None
                self.changed.wait(left)


def remote_requests(mgr):
    with mgr.lock:
        if not hasattr(mgr, '_remote_requests'):
            mgr._remote_requests = RemoteRequests()
        return mgr._remote_requests

def identity(raw, token):
    if not isinstance(raw, str) or len(raw) > 16384:
        raise ValueError('请从机器人菜单重新打开配置中心')
    pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    data = dict(pairs)
    if len(data) != len(pairs):
        raise ValueError('登录数据无效')
    received = data.pop('hash', '')
    secret = hmac.new(b'WebAppData', token.encode(), hashlib.sha256).digest()
    check = '\n'.join('%s=%s' % item for item in sorted(data.items()))
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        raise ValueError('登录验证失败，请从自己的机器人重新打开')
    try:
        age = time.time() - int(data.get('auth_date', '0'))
        user = json.loads(data.get('user', '{}'))
    except (ValueError, TypeError):
        raise ValueError('登录数据无效') from None
    if age < -30 or age > 3600:
        raise ValueError('登录已过期，请关闭后重新打开配置中心')
    uid = user.get('id') if isinstance(user, dict) else None
    if type(uid) is not int or not 0 < uid < 2**52:
        raise ValueError('无法确认 Telegram 身份')
    return uid


def owner_id(bot):
    return CC.owner(bot)


def number(value, low, high, *, integer=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)) or len(str(value)) > 40:
        raise ValueError('请输入有效数字')
    try:
        n = Decimal(str(value))
    except InvalidOperation:
        raise ValueError('请输入有效数字') from None
    if not n.is_finite() or not low <= n <= high or (integer and n != int(n)):
        raise ValueError('数值范围应为 %s 到 %s' % (low, high))
    return int(n) if integer else str(n)


def boolean(value):
    if type(value) is not bool:
        raise ValueError('开关值无效')
    return value


def text(value, limit=2000):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError('文本过长或格式无效')
    return value.strip()


def ledger_path(bot):
    return Path(core.bot_data_dir(bot)) / (bot['id'] + '.sqlite3')


def connect(bot):
    conn = sqlite3.connect(ledger_path(bot).resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    return conn


LOCAL_TZ = timezone(timedelta(hours=8))
ENTRY_FIELDS = ('id', 'kind', 'flow_label', 'amount', 'currency', 'note', 'operator_name',
                'created_at', 'payable_amount', 'payable_usdt', 'net_amount', 'cleared', 'rate', 'fee_percent')


def month_bounds():
    today = datetime.now(LOCAL_TZ).date()
    return today.replace(day=1).isoformat(), today.isoformat()


def monetary(rows):
    income, payable, paid = Decimal(0), Decimal(0), Decimal(0)
    for row in rows:
        if row['kind'] == 'income':
            income += Decimal(row['payable_amount'] if row['currency']=='U' else row['amount'])
            payable += Decimal(row['payable_usdt'])
        elif row['kind'] == 'payout':
            paid += Decimal(row['net_amount'])
    return dict(income=str(income),payable=str(payable),paid=str(paid),balance=str(payable-paid),count=len(rows))


def monthly_records(conn, cid, start=None, end=None):
    month, today = month_bounds()
    start, end = start or month, end or today
    tables = ['entries']
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='customer_month_entries'").fetchone():
        tables.append('customer_month_entries')
    records = []
    for table in tables:
        records.extend(dict(row,cleared=table!='entries') for row in conn.execute(
            "SELECT * FROM "+table+" WHERE chat_id=? AND voided_at IS NULL AND date(created_at,'+8 hours')>=? AND date(created_at,'+8 hours')<=? ORDER BY id",(cid,start,end)))
    return sorted(records,key=lambda row:row['id'])


def has_history(conn):
    if (conn.execute('SELECT 1 FROM entries LIMIT 1').fetchone() or
            conn.execute('SELECT 1 FROM ledger_entry_messages LIMIT 1').fetchone()):
        return True
    exists=conn.execute("SELECT 1 FROM sqlite_master WHERE name='customer_ledger_history'").fetchone()
    return bool(exists and conn.execute('SELECT 1 FROM customer_ledger_history').fetchone())


def authorized_users(bot, uid, visible, known):
    owner = uid == owner_id(bot)
    actor_grants = CC.grants(bot, uid)
    ids = {uid}
    if owner:
        ids.update(int(i) for i in bot.get('admin_ids') or [])
    if owner:
        ids.add(owner_id(bot))
        ids.update(int(i) for i in (bot.get('ledger') or {}).get('staff_grants', {}) if CC.has_staff(bot,int(i)))
    names = {u['user_id']: u for u in known}
    by_group = {}
    for g in visible:
        group_owner = g['owner_id'] if g['owner_id'] and CC.grants(bot, g['owner_id'])['global_ops'] else None
        for i in [group_owner,*[o['user_id'] for o in g['operators']]]:
            if i:
                ids.add(i)
                by_group.setdefault(i,set()).add(g['chat_id'])
        for i, grant in (bot.get('ledger') or {}).get('staff_grants', {}).items():
            if g['chat_id'] in grant['groups']:
                ids.add(int(i))
                by_group.setdefault(int(i),set()).add(g['chat_id'])
    if CC.has_staff(bot,uid):
        ids.add(uid)
    users=[]
    visible_ids = {g['chat_id'] for g in visible}
    manage_ids = {g['chat_id'] for g in visible if owner or actor_grants['global_ops']}
    for i in sorted(ids):
        if not owner and not actor_grants['global_ops'] and not manage_ids and i != uid:
            continue
        p=CC.grants(bot,i)
        group_ids = by_group.get(i,set()) | set(p.get('groups',[]))
        if not owner:
            group_ids &= visible_ids
        if not owner and i != uid:
            p = dict(manage=False, global_ops=False, broadcast=False, groups=sorted(group_ids))
        user=names.get(i,{})
        users.append(dict(user_id=i,name=(bot.get('admin_names') or {}).get(str(i),'') or user.get('display_name','') or str(i),
                          username=user.get('username',''),owner=owner and i==owner_id(bot),
                          grants=p,group_ids=sorted(group_ids)))
    return users


def bill_query(bot, uid, payload):
    cid=payload.get('chat_id')
    if cid not in (None,''):
        cid=number(cid,-2**52+1,-1,integer=True)
    else:
        cid=None
    kind=payload.get('kind','all')
    if kind not in ('all','income','out','payout'):
        raise ValueError('账单类型无效')
    keyword=text(payload.get('search',''),100).casefold()
    if not ledger_path(bot).exists():
        if cid is not None:
            raise PermissionError('没有该群账单权限')
        return dict(bills=[], month=month_bounds()[0])
    with closing(connect(bot)) as conn:
        visible=groups(conn,bot,uid)
        if cid is not None and cid not in {g['chat_id'] for g in visible}:
            raise PermissionError('没有该群账单权限')
        bills=[]
        for g in visible:
            if cid is not None and cid!=g['chat_id']:
                continue
            periods={}
            for row in monthly_records(conn,g['chat_id']):
                flow=row.get('flow_label') or ('入款' if row['kind']=='income' else '下发')
                row['flow_label']=flow
                if kind!='all' and not (row['kind']=='income' if kind=='income' else row['kind']=='payout' and (flow=='出款' if kind=='out' else flow!='出款')):
                    continue
                if keyword and keyword not in (' '.join(str(row[k]) for k in ('operator_name','note','accounting_date'))+' '+g['title']).casefold():
                    continue
                periods.setdefault(row['accounting_date'],[]).append(row)
            for period,rows in periods.items():
                bills.append(dict(chat_id=g['chat_id'],title=g['title'],period=period,
                                  cutoff_hour=g['ledger_reset_hour'],cutoff_enabled=bool(g['ledger_cutoff_enabled']),
                                  summary=monetary(rows),entries=[{k:r.get(k) for k in ENTRY_FIELDS} for r in rows]))
        return dict(bills=sorted(bills,key=lambda b:(b['period'],b['chat_id']),reverse=True),month=month_bounds()[0])


def statistics(bot,uid,payload):
    month,today=month_bounds()
    start,end=payload.get('start',today),payload.get('end',today)
    try:
        date.fromisoformat(start);date.fromisoformat(end)
    except (ValueError,TypeError):
        raise ValueError('日期无效') from None
    if not month<=start<=end<=today:
        raise ValueError('统计范围限于本月已发生的日期')
    cid=payload.get('chat_id')
    if cid not in (None,''):
        cid=number(cid,-2**52+1,-1,integer=True)
    else:
        cid=None
    sort=payload.get('sort','income')
    if sort not in ('income','payable','paid','balance'):
        raise ValueError('排序指标无效')
    rows,all_entries=[],[]
    if not ledger_path(bot).exists():
        if cid is not None:
            raise PermissionError('没有该群统计权限')
        return dict(start=start, end=end, month=month, summary=monetary([]), groups=[])
    with closing(connect(bot)) as conn:
        visible=groups(conn,bot,uid)
        if cid is not None and cid not in {g['chat_id'] for g in visible}:
            raise PermissionError('没有该群统计权限')
        for g in visible:
            if cid is not None and cid!=g['chat_id']:
                continue
            entries=monthly_records(conn,g['chat_id'],start,end)
            all_entries.extend(entries)
            rows.append(dict(chat_id=g['chat_id'],title=g['title'],**monetary(entries)))
    result = dict(start=start,end=end,month=month,summary=monetary(all_entries),
                  groups=sorted(rows,key=lambda row:Decimal(row[sort]),reverse=True))
    if cid is not None:
        result['entries'] = [{k:r.get(k) for k in ENTRY_FIELDS} for r in all_entries]
    return result


def groups(conn, bot, uid):
    p = CC.grants(bot, uid)
    rows = conn.execute(
        "SELECT b.chat_id, b.title, s.rate, s.fee_percent, s.ledger_enabled, "
        "s.ledger_reset_hour, s.ledger_cutoff_enabled, s.ledger_view_mode, s.all_members_can_record, s.owner_id FROM bot_chats b "
        "JOIN chat_settings s ON s.chat_id=b.chat_id WHERE b.is_active=1 "
        "AND b.chat_type IN ('group','supergroup') ORDER BY b.updated_at DESC"
    ).fetchall()
    allowed = {r[0] for r in conn.execute('SELECT chat_id FROM operators WHERE user_id=?', (uid,))}
    allowed.update(p.get('groups') or [])
    return [dict(r) for r in rows if p['global_ops'] or r['chat_id'] in allowed]


def overview(mgr, bot, uid):
    now = int(time.time())
    expiry = int(bot.get('expire_at') or 0)
    result = {'id': bot['id'], 'username': bot.get('username') or '',
              'type': bot['type'], 'expire_at': expiry, 'server_time': now,
              'remaining_seconds': max(0, expiry-now) if expiry else None,
              'is_owner': uid == owner_id(bot), 'user_id': uid,
              'footer_text': mgr.cfg.get('miniapp_footer_text', DEFAULT_FOOTER),
              'enabled_group_count': 0, 'operator_count': 0,
              'groups': [], 'staff': []}
    if result['is_owner']:
        result['staff'] = [{'id': int(i), 'name': (bot.get('admin_names') or {}).get(str(i), ''),
                            'owner': int(i) == owner_id(bot)}
                           for i in bot.get('admin_ids') or []]
        if bot['type'] == 'ledger':
            ids = set(int(i) for i in bot.get('admin_ids') or [])
            ids.update(int(i) for i in (bot.get('ledger') or {}).get('staff_grants', {}))
            result['staff'] = [dict(id=i, name=(bot.get('admin_names') or {}).get(str(i), ''),
                                    owner=i == owner_id(bot), grants=CC.grants(bot, i)) for i in sorted(ids)]
    if bot['type'] == 'shop':
        if uid not in {int(x) for x in bot.get('admin_ids') or []} | {owner_id(bot)}:
            raise PermissionError('你没有这个商城的配置权限')
        from runners.shop import store as SS
        from runners.shop.providers import provider_list
        st = SS.store_for(mgr, bot['id'])
        result['shop'] = {k: v for k, v in st.cfg.items()
                          if k not in ('provider_key', 'premium_key', 'group_bot_token')}
        result['secrets_set'] = {k: bool(st.cfg.get(k)) for k in ('provider_key', 'premium_key')}
        result['providers'] = provider_list()
        # Only existing payment fields, no upstream credentials or raw API replies.
        result['entries'] = [{k: p.get(k) for k in ('txid', 'from', 'amount', 'energy', 'status', 'at')}
                             for p in st.recent(100)]
        result['shop_stats'] = st.stats(days=1)
        return result
    result['month'],result['today']=month_bounds()
    result['permission_users']=[]
    p = CC.grants(bot, uid)
    result['permissions'] = p
    result['tron'] = tron_config(mgr, bot, uid)
    cfg = CC.settings(bot)
    cfg['addresses'] = CC.addresses_for_panel(bot)
    result['welcome_photo_exists'] = bool((bot.get('ledger') or {}).get('newbie_welcome_photo')) if p['manage'] else False
    result['customer'] = cfg if p['manage'] else dict(revision=cfg['revision'])
    result['feature_fields'] = CC.FEATURES
    result['preview_basics'] = CC.settings({})['basics']
    result['message_groups'] = []
    if not ledger_path(bot).exists():
        if CC.has_staff(bot, uid):
            result['permission_users']=authorized_users(bot,uid,[],[])
        return result
    with closing(connect(bot)) as conn:
        if p['manage'] or p['broadcast'] or result['is_owner']:
            result['message_groups'] = [dict(r) for r in conn.execute(
                "SELECT chat_id,title FROM bot_chats WHERE is_active=1 AND chat_type IN ('group','supergroup')")]
        if p['broadcast'] and not p['manage']:
            result['customer'].update(ads=cfg['ads'], replies=cfg['replies'])
        if p['manage']:
            result['history_exists'] = has_history(conn)
        result['groups'] = groups(conn, bot, uid)
        known = []
        for g in result['groups']:
            g['can_manage_operators'] = CC.can_manage_group_members(bot, uid, g['owner_id'])
            g['can_manage_pricing'] = g['can_manage_operators'] or p['global_ops']
            g['currency']=(bot.get('ledger') or {}).get('group_currency',{}).get(str(g['chat_id']),'CNY')
            g['deposit'] = cfg['deposits'].get(str(g['chat_id']), '0')
            g['operators'] = [dict(r) for r in conn.execute(
                'SELECT user_id,username,display_name FROM operators WHERE chat_id=?', (g['chat_id'],))]
            known.extend(dict(r) for r in conn.execute(
                'SELECT chat_id,user_id,username,display_name FROM known_users '
                'WHERE chat_id=? AND is_bot=0 ORDER BY updated_at DESC LIMIT 300', (g['chat_id'],)))
        if result['groups'] or CC.has_staff(bot, uid):
            result['permission_users'] = authorized_users(bot,uid,result['groups'],known)
        pricing = {}
        visible = {g['chat_id'] for g in result['groups']}
        if visible:
            for row in conn.execute('SELECT user_id,chat_id,rate,fee_percent FROM user_pricing WHERE chat_id IN ('+
                                    ','.join('?' for _ in visible)+')', tuple(visible)):
                pricing.setdefault(row['user_id'], []).append({k:row[k] for k in ('chat_id','rate','fee_percent')})
        for user in result['permission_users']:
            user['pricing'] = [row for row in pricing.get(user['user_id'], []) if user['user_id'] == uid or
                               any(g['chat_id']==row['chat_id'] and g['can_manage_pricing'] for g in result['groups'])]
    result['enabled_group_count'] = sum(bool(g['ledger_enabled']) for g in result['groups'])
    visible = {g['chat_id'] for g in result['groups']}
    singles = {int(i) for i, grant in (bot.get('ledger') or {}).get('staff_grants', {}).items()
               if visible.intersection(grant.get('groups') or [])}
    result['operator_count'] = len(singles | {p['user_id'] for g in result['groups'] for p in g['operators']})
    if p['manage'] or p['broadcast']:
        result['enabled_reply_count'] = sum(bool(r['enabled']) for r in cfg['replies'])
        result['enabled_ad_count'] = sum(bool(r['enabled']) for r in cfg['ads'])
    return result


def save_customer(mgr, bot, uid, payload):
    from datetime import datetime
    from uuid import uuid4
    p = CC.grants(bot, uid)
    section = payload.get('section')
    if section not in ('basics', 'features', 'ads', 'replies', 'deposits', 'addresses'):
        raise ValueError('配置分类无效')
    if section in ('basics', 'features', 'addresses') and not p['manage']:
        raise PermissionError('没有管理权限')
    if section in ('ads', 'replies') and not (p['manage'] or p['broadcast']):
        raise PermissionError('没有管理或广播权限')
    value = payload.get('value')
    with mgr.lock:
        cfg = CC.settings(bot)
        if type(payload.get('revision')) is not int or payload['revision'] != cfg['revision']:
            raise ValueError('配置已被其他人修改，请刷新后重试')
        if section == 'basics':
            if not isinstance(value, dict) or set(value) != set(cfg['basics']):
                raise ValueError('基础配置字段无效')
            value = dict(value)
            value['cutoff_hour'] = number(value['cutoff_hour'], 0, 23, integer=True)
            if value['view_mode'] not in ('compact', 'detailed') or value['currency'] not in ('U', 'CNY'):
                raise ValueError('账单模式或币种无效')
            for k in ('welcome_enabled', 'welcome_mention', 'welcome_nickname', 'leave_enabled', 'all_members_can_record'):
                value[k] = boolean(value[k])
            if value['all_members_can_record']:
                raise ValueError('仅授权人员可记账，不能开放普通成员记账')
            value['welcome_text'] = text(value['welcome_text'], 300)
            value['leave_text'] = text(value['leave_text'], 300)
            value['payout_address'] = text(value['payout_address'], 34)
            if value['payout_address']:
                from runners.ledger.tron_chain import address_valid
                if not address_valid(value['payout_address']):
                    raise ValueError('下发地址不是有效TRON地址')
        elif section == 'addresses':
            from runners.ledger.tron_chain import address_valid
            if not isinstance(value, list) or len(value) > max(20, len(CC.addresses_for_panel(bot))):
                raise ValueError('地址列表无效；最多新增到20个地址')
            value = [text(address, 34) for address in value]
            if len(set(value)) != len(value) or any(not address_valid(address) for address in value):
                raise ValueError('请填写有效且不重复的TRON地址')
        elif section == 'features':
            if not isinstance(value, dict) or set(value) != set(cfg['features']):
                raise ValueError('功能开关字段无效')
            value = {k: boolean(v) for k, v in value.items()}
            if value['pure_u'] != cfg['features']['pure_u'] and ledger_path(bot).exists():
                with closing(connect(bot)) as conn:
                    if has_history(conn):
                        raise ValueError('存在历史账目，不能切换纯U／分红模式')
        elif section == 'deposits':
            if not isinstance(value, dict) or len(value) != 1:
                raise ValueError('押金配置格式无效')
            cid, amount = next(iter(value.items()))
            cid = number(cid, -2**52+1, -1, integer=True)
            with closing(connect(bot)) as conn:
                if cid not in {g['chat_id'] for g in groups(conn, bot, uid)}:
                    raise PermissionError('没有该群权限')
            value = dict(cfg['deposits'], **{str(cid): number(amount, 0, 1000000000000)})
        else:
            if not isinstance(value, list) or len(value) > 50:
                raise ValueError('最多配置50条规则')
            with closing(connect(bot)) as conn:
                active = {r[0] for r in conn.execute("SELECT chat_id FROM bot_chats WHERE is_active=1 AND chat_type IN ('group','supergroup')")}
            old = {r['id']: r for r in cfg[section]}
            clean, seen = [], set()
            for item in value:
                if not isinstance(item, dict):
                    raise ValueError('规则格式无效')
                rid = item.get('id') or uuid4().hex[:12]
                if not isinstance(rid, str) or not re.fullmatch('[a-zA-Z0-9_-]{1,32}', rid) or rid in seen:
                    raise ValueError('规则编号无效或重复')
                seen.add(rid)
                targets = item.get('groups')
                if not isinstance(targets, list) or not targets or any(type(i) is not int or i not in active for i in targets):
                    raise ValueError('请选择仍在使用的目标群组')
                rule = dict(id=rid, title=text(item.get('title', ''), 100),
                            content=text(item.get('content'), 1500), groups=sorted(set(targets)),
                            enabled=boolean(item.get('enabled')),
                            author=old.get(rid, {}).get('author', uid))
                if not rule['content'] and (section != 'ads' or not item.get('image')):
                    raise ValueError('消息内容不能为空')
                # Editing a rule transfers responsibility to the currently authorized editor.
                if any(item.get(k) != old.get(rid, {}).get(k) for k in ('title','content','groups','enabled','keyword','match','mode','at','interval','image')):
                    rule['author'] = uid
                if section == 'replies':
                    rule.update(keyword=text(item.get('keyword'), 100), match=item.get('match'))
                    if not rule['keyword'] or rule['match'] not in ('exact', 'contains', 'prefix'):
                        raise ValueError('请输入关键词并选择匹配方式')
                else:
                    from customer_images import ad_path
                    image = item.get('image') or ''
                    if image and not ad_path(bot, image).is_file():
                        raise ValueError('广告图片不存在，请重新上传')
                    rule['image'] = image
                    mode = item.get('mode')
                    if mode not in ('once', 'daily', 'interval'):
                        raise ValueError('定时模式无效')
                    rule.update(mode=mode, at=text(item.get('at', ''), 40), interval=number(item.get('interval', 60), 1, 10080, integer=True))
                    try:
                        if mode == 'daily':
                            datetime.strptime(rule['at'], '%H:%M')
                        elif mode == 'once':
                            dt = datetime.fromisoformat(rule['at'])
                            if dt.tzinfo is None:
                                raise ValueError()
                    except ValueError:
                        raise ValueError('定时时间格式无效，请按北京时间填写') from None
                clean.append(rule)
            value = clean
        address_changed = section == 'basics' and value['payout_address'] != cfg['basics'].get('payout_address')
        cfg[section] = value
        if section == 'addresses' or address_changed:
            addresses = value if section == 'addresses' else ([value['payout_address']] if value['payout_address'] else [])
            cfg['addresses'] = addresses
            cfg['basics']['payout_address'] = addresses[0] if addresses else ''
            bot.setdefault('ledger', {})['payout_address'] = cfg['basics']['payout_address']
            bot['ledger']['group_addresses'] = {}
        cfg['revision'] += 1
        bot.setdefault('ledger', {})['customer'] = cfg
        if section == 'basics':
            bot['ledger']['newbie_welcome'] = value['welcome_text'] if value['welcome_enabled'] else ''
            bot['ledger']['payout_address'] = value['payout_address']
        mgr.save()
    return {}


def save_shop(mgr, bot, patch):
    from runners.shop import store as SS
    from runners.shop.providers import PROVIDERS, check_public_url
    from tron import is_address
    allowed = {'enabled', 'trx_own', 'price', 'energy', 'max', 'min_units', 'provider',
               'provider_key', 'premium_key', 'contact', 'notice', 'group_ids',
               'premium_enabled', 'premium_prices', 'auto_enabled', 'auto_prices',
               'auto_type', 'custom_url', 'custom_method', 'custom_ok_path',
               'custom_ok_value', 'custom_order_path'}
    if not patch or set(patch)-allowed:
        raise ValueError('存在不支持的商城配置字段')
    clean = {}
    for k, v in patch.items():
        if k.endswith('_enabled') or k == 'enabled':
            clean[k] = boolean(v)
        elif k in ('price', 'energy', 'max', 'min_units', 'auto_type'):
            ranges = {'price': ('0.000001', '1000000'), 'energy': (1, 100000000),
                      'max': (1, 1000), 'min_units': (1, 1000), 'auto_type': (0, 3)}
            lo, hi = ranges[k]
            val = number(v, Decimal(str(lo)), Decimal(str(hi)), integer=k!='price')
            clean[k] = float(val) if k == 'price' else val
        elif k in ('premium_prices', 'auto_prices'):
            if not isinstance(v, dict) or len(v) > 30:
                raise ValueError('套餐价格格式无效')
            prices = {}
            for key, value in v.items():
                if k == 'premium_prices' and key not in ('3', '6', '12'):
                    raise ValueError('会员套餐只支持3、6、12个月')
                number(key, 1, 1000000, integer=True)
                prices[str(key)] = float(number(value, Decimal('0.000001'), 1000000))
            clean[k] = prices
        else:
            clean[k] = text(v)
    st = SS.store_for(mgr, bot['id'])
    # Blank secret inputs mean keep the existing value, not erase it.
    for key in ('provider_key', 'premium_key'):
        if key in clean and not clean[key]:
            clean.pop(key)
    cfg = dict(st.cfg, **clean)
    if cfg['trx_own'] and not is_address(cfg['trx_own']):
        raise ValueError('收款地址不是有效的 TRON 地址')
    if cfg['provider'] not in PROVIDERS or cfg['custom_method'] not in ('GET', 'POST'):
        raise ValueError('上游或请求方式无效')
    if cfg['min_units'] > cfg['max']:
        raise ValueError('最小倍数不能大于最大倍数')
    if cfg['custom_url']:
        ok, why = check_public_url(cfg['custom_url'])
        if not ok:
            raise ValueError('接口地址不能用：' + why)
    changed = cfg['trx_own'] != st.cfg['trx_own']
    st.set_cfg(clean)
    if changed:
        st.data['scan'] = {'baseline': False, 'seen': [], 'last_ts': 0}
        st.save()
    return {'baseline_reset': changed}


def tron_config(mgr, bot, uid):
    runner = mgr.runners.get(bot['id'])
    watcher = getattr(runner, 'tronw', None)
    own = watcher.custom_keys() if watcher else []
    return dict(source='custom' if own else 'platform', custom_count=len(own),
                masked_keys=['…' + k[-4:] for k in own] if uid == owner_id(bot) else [],
                default_count=len(mgr.cfg.get('tron_api_keys') or []), can_bind=uid == owner_id(bot))


def tron_action(mgr, bot, uid, name, payload):
    from runners.ledger import tron_chain as tc
    from runners.ledger.tron_watch import MAX_KEYS
    if name == 'tronkeys' and uid != owner_id(bot):
        raise PermissionError('只有机器人拥有者可以绑定查询 API Key')
    runner = mgr.runners.get(bot['id'])
    watcher = getattr(runner, 'tronw', None)
    if not watcher:
        raise OSError('地址查询服务暂不可用')
    if name == 'tronquery':
        address = text(payload.get('address'), 100)
        if not tc.address_valid(address):
            raise ValueError('请输入完整有效的 TRON／TRC20 地址')
        return dict(address=address, html=watcher.card(uid, address, include_hint=False)[0])
    keys = payload.get('keys')
    if not isinstance(keys, list) or len(keys) > MAX_KEYS or any(
            not isinstance(k, str) or not re.fullmatch(r'[A-Za-z0-9_\-]{8,256}', k) for k in keys):
        raise ValueError('请填写查询 API Key，最多 %d 把，每行一把' % MAX_KEYS)
    keys = list(dict.fromkeys(keys))
    for key in keys:
        try:
            good, _ = tc.probe(key)
        except Exception:
            good = False
        if not good:
            raise ValueError('API Key 验证未通过，原配置已保留。请检查复制内容、查询权限或稍后重试。')
    with mgr.lock:
        updated = dict(runner.data)
        updated.pop('tron_key', None)
        if keys:
            updated['tron_keys'] = keys
        else:
            updated.pop('tron_keys', None)
        core.save_json(runner.data_file, updated)
        runner.data.pop('tron_key', None)
        if keys:
            runner.data['tron_keys'] = keys
        else:
            runner.data.pop('tron_keys', None)
    return tron_config(mgr, bot, uid)


def action(mgr, bot, uid, name, payload):
    if bot.get('instance_folder') and os.environ.get('PANEL_INSTANCE_CHILD') != '1':
        from bot_instances import InstanceRunner
        runner = mgr.runners.get(bot['id'])
        if isinstance(runner, InstanceRunner) and runner.is_alive():
            try:
                with runner.request('/instance/action', dict(user_id=uid, action=name, payload=payload)) as response:
                    result = json.load(response)['data']
                    if name == 'overview':
                        result['footer_text'] = mgr.cfg.get('miniapp_footer_text', DEFAULT_FOOTER)
                    return result
            except HTTPError as exc:
                message = json.load(exc).get('error', '独立实例暂不可用')
                if exc.code == 403:
                    raise PermissionError(message) from None
                if exc.code == 400:
                    raise ValueError(message) from None
                raise OSError(message) from None
    owner = uid == owner_id(bot)
    if name == 'overview':
        return overview(mgr, bot, uid)
    if name in ('tronkeys', 'tronquery') and bot['type'] == 'ledger':
        return tron_action(mgr, bot, uid, name, payload)
    if name in ('bills','statistics') and bot['type']=='ledger':
        return bill_query(bot,uid,payload) if name=='bills' else statistics(bot,uid,payload)
    if name == 'pricing' and bot['type']=='ledger':
        cid=number(payload.get('chat_id'),-2**52+1,-1,integer=True)
        target=number(payload.get('user_id'),1,2**52-1,integer=True)
        with closing(connect(bot)) as conn:
            visible=groups(conn,bot,uid)
            if cid not in {g['chat_id'] for g in visible}:
                raise PermissionError('没有该群配置权限')
            group = next(g for g in visible if g['chat_id'] == cid)
            if target != uid and not (owner or CC.grants(bot,uid)['global_ops']):
                raise PermissionError('只能修改自己的个人参数')
            operators = [dict(r) for r in conn.execute(
                'SELECT user_id,username,display_name FROM operators WHERE chat_id=?', (cid,))]
            group['operators'] = operators
            if target not in {u['user_id'] for u in authorized_users(bot,uid,[group],[])}:
                raise PermissionError('该用户不在可配置的人员范围内')
        patch=payload.get('settings')
        if not isinstance(patch,dict) or set(patch)!={'rate','fee_percent'}:
            raise ValueError('个人配置字段无效')
        clean={k:None if v in ('',None) else number(v,Decimal('.0001') if k=='rate' else 0,1000000 if k=='rate' else Decimal('99.9999')) for k,v in patch.items()}
        from runners.ledger.storage import LedgerStore
        with closing(LedgerStore(ledger_path(bot),initialize=False)) as st:
            for k,v in clean.items():
                st.set_user_setting(cid,target,k,v)
        return {}
    if name == 'customer' and bot['type'] == 'ledger':
        return save_customer(mgr, bot, uid, payload)
    if name == 'welcomephoto' and bot['type'] == 'ledger':
        from customer_images import action as image_action
        return image_action(mgr, bot, uid, payload)
    if name == 'adphoto' and bot['type'] == 'ledger':
        from customer_images import ad_action
        return ad_action(mgr, bot, uid, payload)
    if name in ('welcome', 'staff', 'shop') and not owner:
        raise PermissionError('只有机器人所有者可以修改这项配置')
    if name == 'welcome' and bot['type'] == 'ledger':
        value = payload.get('newbie_welcome')
        if value is not None:
            value = text(value, 300)
        with mgr.lock:
            bot.setdefault('ledger', {})['newbie_welcome'] = value
            CC.sync_welcome(bot)
            mgr.save()
        return {}
    if name == 'shop' and bot['type'] == 'shop':
        with mgr.lock:
            return save_shop(mgr, bot, payload)
    if name == 'staff':
        target = number(payload.get('user_id'), 1, 2**52-1, integer=True)
        if target == owner_id(bot):
            raise ValueError('不能移除或替换所有者')
        add = boolean(payload.get('add'))
        label = text(payload.get('name', ''), 100)
        permissions = payload.get('grants')
        if bot['type'] == 'ledger' and add and permissions is not None:
            if not isinstance(permissions, dict) or set(permissions) != {'manage','global_ops','broadcast','groups'}:
                raise ValueError('授权字段无效')
            permissions = dict(permissions)
            for k in ('manage', 'global_ops', 'broadcast'):
                permissions[k] = boolean(permissions[k])
            targets = permissions['groups']
            if not isinstance(targets, list) or any(type(i) is not int for i in targets):
                raise ValueError('单群授权格式无效')
            with closing(connect(bot)) as conn:
                active = {r[0] for r in conn.execute('SELECT chat_id FROM bot_chats WHERE is_active=1')}
            if not set(targets) <= active or not any(permissions.values()):
                raise ValueError('请选择至少一项权限和有效群组')
            permissions['groups'] = sorted(set(targets))
        with mgr.lock:
            ids = [int(i) for i in bot.get('admin_ids') or []]
            if add and target not in ids:
                ids.append(target)
            if not add:
                ids = [i for i in ids if i != target]
            bot['admin_ids'] = ids
            names = bot.setdefault('admin_names', {})
            names[str(target)] = label
            if not add:
                names.pop(str(target), None)
            if bot['type'] == 'ledger':
                staff = bot.setdefault('ledger', {}).setdefault('staff_grants', {})
                if add and permissions is not None:
                    staff[str(target)] = permissions
                elif not add:
                    staff.pop(str(target), None)
            mgr.save()
        return {}
    if name in ('group', 'operator') and bot['type'] == 'ledger':
        cid = number(payload.get('chat_id'), -2**52+1, -1, integer=True)
        with closing(connect(bot)) as conn:
            if cid not in {g['chat_id'] for g in groups(conn, bot, uid)}:
                raise PermissionError('你没有这个群组的管理权限')
            row = conn.execute('SELECT owner_id FROM chat_settings WHERE chat_id=?', (cid,)).fetchone()
            if name == 'operator' and not CC.can_manage_group_members(bot, uid, row['owner_id']):
                raise PermissionError('只有所有者或群绑定者可以管理群操作员')
            if name == 'group' and isinstance(payload.get('settings'), dict) and 'all_members_can_record' in payload['settings'] and not CC.can_manage_group_members(bot, uid, row['owner_id']):
                raise PermissionError('只有所有者或群绑定者可以修改全员记账权限')
        from runners.ledger.storage import LedgerStore
        # Validate the complete patch before invoking the original setters.
        if name == 'group':
            patch = payload.get('settings')
            setters = {'rate': 'set_rate', 'fee_percent': 'set_fee_percent',
                       'ledger_enabled': 'set_ledger_enabled', 'ledger_reset_hour': 'set_ledger_reset_hour',
                       'ledger_cutoff_enabled': 'set_ledger_cutoff_enabled',
                       'ledger_view_mode': 'set_ledger_view_mode', 'deposit': None, 'currency': None,
                       'all_members_can_record': 'set_all_members_can_record'}
            if not isinstance(patch, dict) or not patch or set(patch)-set(setters):
                raise ValueError('群配置字段无效')
            clean = {}
            for k, v in patch.items():
                if k == 'rate':
                    clean[k] = number(v, Decimal('0.0001'), 1000000)
                elif k == 'deposit':
                    clean[k] = number(v, 0, 1000000000000)
                elif k == 'currency':
                    if v not in ('U','CNY'):
                        raise ValueError('币种无效')
                    clean[k]=v
                elif k == 'fee_percent':
                    val = Decimal(number(v, 0, Decimal('99.9999')))
                    if val.quantize(Decimal('0.0001')) >= 100:
                        raise ValueError('费率必须小于100')
                    clean[k] = str(val)
                elif k in ('ledger_enabled', 'ledger_cutoff_enabled', 'all_members_can_record'):
                    clean[k] = boolean(v)
                    if k == 'all_members_can_record' and clean[k]:
                        raise ValueError('仅授权人员可记账，不能开放普通成员记账')
                elif k == 'ledger_reset_hour':
                    clean[k] = number(v, 0, 23, integer=True)
                else:
                    if v not in ('compact', 'detailed'):
                        raise ValueError('显示模式无效')
                    clean[k] = v
            with closing(LedgerStore(ledger_path(bot), initialize=False)) as st:
                for k, v in clean.items():
                    if k not in ('ledger_reset_hour', 'ledger_cutoff_enabled', 'deposit', 'currency'):
                        getattr(st, setters[k])(cid, v)
                if 'ledger_reset_hour' in clean:
                    enabled = clean.get('ledger_cutoff_enabled', st.is_ledger_cutoff_enabled(cid))
                    st.set_ledger_reset_hour(cid, int(clean['ledger_reset_hour']), enable=bool(enabled))
                elif 'ledger_cutoff_enabled' in clean:
                    st.set_ledger_cutoff_enabled(cid, clean['ledger_cutoff_enabled'])
            if 'deposit' in clean:
                with mgr.lock:
                    cfg = CC.settings(bot)
                    cfg['deposits'][str(cid)] = clean['deposit']
                    cfg['revision'] += 1
                    bot.setdefault('ledger', {})['customer'] = cfg
                    mgr.save()
            if 'currency' in clean:
                with mgr.lock:
                    bot.setdefault('ledger',{}).setdefault('group_currency',{})[str(cid)]=clean['currency']
                    mgr.save()
        else:
            target = number(payload.get('user_id'), 1, 2**52-1, integer=True)
            add = boolean(payload.get('add'))
            label = text(payload.get('name', ''), 100)
            with closing(LedgerStore(ledger_path(bot), initialize=False)) as st:
                if add:
                    st.add_operator(cid, target, '', label, uid)
                else:
                    st.remove_operator(cid, target)
        return {}
    raise ValueError('不支持的操作')


def handle_get(h, path):
    if not path.startswith('/miniapp/'):
        return False
    if not re.fullmatch(r'/miniapp/[a-zA-Z0-9_-]{1,64}', path) or not base_url(h.mgr):
        h._send(404, 'Not Found', 'text/plain; charset=utf-8')
    else:
        bot = h.mgr.find(path.rsplit('/', 1)[1])
        if bot and bot.get('instance_folder') and os.environ.get('PANEL_INSTANCE_CHILD') != '1':
            from bot_instances import folder
            page = folder(bot) / 'miniapp_page.html'
        else:
            page = Path(core.resource_path('miniapp_page.html'))
        body = page.read_text(encoding='utf-8')
        h._send(200, body, 'text/html; charset=utf-8')
    return True


def handle_post(h, path):
    if not path.startswith('/api/miniapp/'):
        return False
    try:
        if not base_url(h.mgr):
            raise PermissionError('配置中心暂未启用')
        match = re.fullmatch(r'/api/miniapp/([a-zA-Z0-9_-]{1,64})/([a-z]+)', path)
        if not match:
            raise ValueError('请求路径无效')
        length = int(h.headers.get('Content-Length', '0'))
        if not 0 < length <= (9 * 1024 * 1024 if match[2] in ('welcomephoto', 'adphoto') else 65536):
            raise ValueError('请求大小无效')
        h.connection.settimeout(10)
        body = json.loads(h.rfile.read(length))
        h._body_read = True
        if not isinstance(body, dict) or not isinstance(body.get('payload', {}), dict):
            raise ValueError('请求格式无效')
        bot = h.mgr.find(match[1])
        if not bot or bot.get('type') not in ('ledger', 'shop'):
            raise PermissionError('此机器人暂不支持配置中心')
        uid = identity(body.get('init_data'), bot['token'])
        if bot.get('node_id'):
            import base64
            from nodes import for_manager
            response = for_manager(h.mgr).rpc(bot,h.path,'POST',{'role':'admin'},json.dumps(body).encode())
            result = json.loads(base64.b64decode(response['body']))
            if result.get('ok') and match[2]=='overview':
                result['data']['footer_text'] = h.mgr.cfg.get('miniapp_footer_text',DEFAULT_FOOTER)
            h._json(result,response['status'])
            return True
        if not bot.get('remote') and not owner_id(bot):
            raise PermissionError('尚未激活，请先私聊机器人发送 /admin 激活码')
        if h.mgr.is_expired(bot):
            raise PermissionError('机器人已到期，请联系开通人员续期')
        if bot.get('remote'):
            if not bot.get('enabled', True):
                raise PermissionError('机器人已停用，请联系开通人员')
            reply = remote_requests(h.mgr).call(bot['id'], match[2], body)
            result = reply['result']
            if result['ok'] and match[2] == 'overview':
                data = result['data']
                expiry = int(bot.get('expire_at') or 0)
                data.update(footer_text=h.mgr.cfg.get('miniapp_footer_text', DEFAULT_FOOTER),
                            expire_at=expiry, server_time=int(time.time()),
                            remaining_seconds=max(0, expiry-int(time.time())) if expiry else None)
            h._json(result, reply['status'])
            return True
        result = action(h.mgr, bot, uid, match[2], body.get('payload', {}))
        h._json({'ok': True, 'data': result})
    except PermissionError as e:
        h._json({'ok': False, 'error': str(e)}, 403)
    except (ValueError, TypeError, KeyError, InvalidOperation) as e:
        # Avoid returning tokens, raw signed login data, or database paths.
        message = str(e) if isinstance(e, ValueError) and not isinstance(e, json.JSONDecodeError) else '请求格式无效'
        h._json({'ok': False, 'error': message}, 400)
    except (sqlite3.Error, OSError) as e:
        message = str(e) if isinstance(e, OSError) and str(e).startswith(('客户服务器', '配置请求较多')) else '数据暂不可用，请稍后重试'
        h._json({'ok': False, 'error': message}, 503)
    return True
