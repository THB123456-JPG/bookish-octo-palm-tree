"""Customer-facing ledger settings and explicit, combinable staff grants."""
from copy import deepcopy

DEFAULT_WELCOME = '{name} 已加入该群。'
DEFAULT_LEAVE = '{name} 已离开该群。'

FEATURES = [
    ('通知类', 'join_notice', '入群通知', False, '用户进群后通知管理人员。'),
    ('通知类', 'deposit_notice', '押金提醒', False, '未下发达到群押金80%时提醒，达到押金时预警。'),
    ('通知类', 'show_address', '群内展示下发地址', True, '关闭后群内不展示下发地址。'),
    ('通知类', 'user_notice', '用户修改信息通知', False, '在群内显示昵称或用户名的修改前后内容。'),
    ('通知类', 'title_notice', '群名更改通知', False, '群标题变化时提醒。'),
    ('账单显示', 'bill_switch', '详细／简洁账单切换', True, '显示账单底部的切换详细／切换简洁按钮。'),
    ('账单显示', 'edit_confirm', '修改账单二次确认', True, '回复原记账消息修改金额时先确认。'),
    ('账单显示', 'carry_balance', '上期结余未下发', False, '上期未下发作为期初余额展示，不生成流水。'),
    ('账单显示', 'categories', '分类明细', False, '汇总各备注分类的入款与支出。'),
    ('账单显示', 'pure_u', '纯 U／分红模式', False, '仅无历史账目的机器人可切换；入款与分红均按U，旧下发命令不响应。'),
    ('账单显示', 'archive_bill', '删除账单完整账单存档', False, '清空前保留完整账单，发送存档查看按钮。'),
    ('账单显示', 'category_settlement', '完整账单备注分类结算', False, '完整账单按备注展示笔数、总额、已下发及未下发U。'),
    ('外部数字查询', 'lookup', '启用查询总开关', True, '控制手机号、身份证、银行卡查询。'),
    ('外部数字查询', 'phone', '查询手机号', True, ''),
    ('外部数字查询', 'idcard', '查询身份证', True, ''),
    ('外部数字查询', 'bank', '查询银行卡', True, ''),
    ('USDT 防篡改验证', 'tron_verify', '启用 USDT 防篡改验证', True, '群内生成地址核对图片。'),
    ('USDT 防篡改验证', 'tron_balance', '显示余额', False, ''),
    ('USDT 防篡改验证', 'balance_usdt', 'USDT', True, ''),
    ('USDT 防篡改验证', 'balance_trx', 'TRX', True, ''),
    ('USDT 防篡改验证', 'admin_confirm', '允许群管理员确认', False, '群管理员可确认核对地址；不执行转账。'),
    ('USDT 防篡改验证', 'tron_details', '显示地址交易详情', False, ''),
    ('USDT 防篡改验证', 'tron_count', '显示链上交易次数', False, '查询链上累计交易数，获取失败时明确提示。'),
    ('汇率和工具', 'okx', '查询欧易汇率', True, '触发欧易USDT报价查询。'),
]


FEATURE_DEFAULTS = {key: default for _, key, _, default, _ in FEATURES}


def owner(bot):
    ids = bot.get('admin_ids') or []
    return int(bot.get('owner_id') or (ids[0] if ids else 0))


def grants(bot, uid):
    uid = int(uid)
    if uid == owner(bot):
        return dict(manage=True, global_ops=True, broadcast=True, groups=[])
    explicit = (bot.get('ledger') or {}).get('staff_grants', {})
    if str(uid) in explicit:
        return dict(explicit[str(uid)])
    legacy = uid in {int(i) for i in bot.get('admin_ids') or []}
    return dict(manage=legacy, global_ops=legacy, broadcast=legacy, groups=[])


def has_staff(bot, uid):
    p = grants(bot, uid)
    return any(p.get(k) for k in ('manage', 'global_ops', 'broadcast', 'groups'))


def can_manage_group_members(bot, uid, group_owner):
    return int(uid) == owner(bot) or (group_owner == int(uid) and grants(bot, uid)['global_ops'])


def settings(bot):
    ledger = bot.get('ledger') or {}
    saved = ledger.get('customer') or {}
    welcome = ledger.get('newbie_welcome')
    basics = dict(cutoff_hour=0, view_mode='compact', currency='CNY',
                  all_members_can_record=False,
                  welcome_enabled=welcome != '', welcome_mention=True,
                  welcome_nickname=True, welcome_text=welcome if welcome is not None else DEFAULT_WELCOME,
                  leave_enabled=True, leave_text=DEFAULT_LEAVE,
                  payout_address=ledger.get('payout_address', ''))
    basics.update(saved.get('basics') or {})
    features = dict(FEATURE_DEFAULTS)
    features.update(saved.get('features') or {})
    addresses = list(saved.get('addresses') or []) if 'addresses' in saved else ([basics['payout_address']] if basics['payout_address'] else [])
    return deepcopy(dict(revision=saved.get('revision', 0), basics=basics,
                         features=features, ads=saved.get('ads', []),
                         replies=saved.get('replies', []), deposits=saved.get('deposits', {}),
                         addresses=list(dict.fromkeys(addresses))))


def addresses_for_panel(bot):
    addresses = settings(bot)['addresses']
    for local in ((bot.get('ledger') or {}).get('group_addresses') or {}).values():
        addresses.extend(local)
    return list(dict.fromkeys(addresses))


def feature(bot, key):
    saved = ((bot.get('ledger') or {}).get('customer') or {}).get('features') or {}
    return saved.get(key, FEATURE_DEFAULTS[key])


def sync_welcome(bot):
    ledger = bot.get('ledger') or {}
    saved = ledger.get('customer') or {}
    if not saved.get('basics'):
        return
    value = ledger.get('newbie_welcome')
    saved['basics'].update(welcome_enabled=value != '',
                          welcome_text=value if value is not None else DEFAULT_WELCOME)
    saved['revision'] = saved.get('revision', 0) + 1
