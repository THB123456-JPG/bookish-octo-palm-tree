# -*- coding: utf-8 -*-
"""手机号 / 身份证 / 银行卡 查询 —— 离线部分不联网，银行卡那步用桩

★ 手机号和身份证是**离线库**跑的，样本用的都是真号码段；
★ 银行卡要联网，这里把 bank_info 换掉，不真打支付宝。
"""
import os
import re
import shutil
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(HERE, '_tmp_lookup')
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(os.path.join(TMP, 'data'), exist_ok=True)

import core
core.BASE_DIR = TMP
core.DATA_DIR = os.path.join(TMP, 'data')

from runners.ledger import lookup as L
from runners.ledger import LedgerRunner

OK, BAD = [], []


def check(name, cond, extra=''):
    (OK if cond else BAD).append(name)
    print('  %s %s%s' % ('✅' if cond else '❌', name,
                         ('  → %s' % (extra,)) if extra else ''))


print('=' * 62)
print('一、认不认得出「这是一串号码」')
print('=' * 62)
YES = [
    ('13800138000', 'phone'), ('13912345678', 'phone'),
    ('19912345678', 'phone'), ('16512345678', 'phone'),
    ('110101199003076130', 'idcard'),
    ('110101199003076131', 'idcard'),      # 校验位不对，但看着就是身份证
    ('440305199001011239', 'idcard'),
    ('6222020200112233445', 'bankcard'),
    ('6217000010000000000', 'bankcard'),
    ('6222 0202 0011 2233 445', 'bankcard'),   # 按 4 位一组敲的空格
]
for text, want in YES:
    got = L.detect(text)
    check('「%s」→ %s' % (text, want), got and got[0] == want, str(got))

NO = ['+100', '-100', '1000/9', '账单', '13800138000 备注', '电话13800138000',
      '12345', '', '   ', '1380013800', '138001380000', '6222 0202 备注',
      '#1', '2026-10-08']
for text in NO:
    check('「%s」→ 不当号码' % (text or '（空）'), L.detect(text) is None,
          str(L.detect(text)))

print()
print('=' * 62)
print('二、手机号（离线库）')
print('=' * 62)
p = L.phone_info('13800138000')
check('13800138000 → 北京 + 移动',
      p.get('province') == '北京' and p.get('carrier') == '移动', str(p))
check('★ 带上了区号 010', p.get('area_code') == '010', str(p.get('area_code')))
p2 = L.phone_info('15012345678')
check('15012345678 → 云南 昭通 + 移动',
      p2.get('province') == '云南' and p2.get('city') == '昭通'
      and p2.get('carrier') == '移动', str(p2))
p3 = L.phone_info('16512345678')
check('★ 虚拟运营商也认得出来（165 → 移动虚拟运营商）',
      '虚拟' in (p3.get('carrier') or ''), str(p3.get('carrier')))
check('★★ 查不到的号段 → 返回空 dict（**不编一个省市出来**）',
      L.phone_info('19900001111') == {}, str(L.phone_info('19900001111')))

card = L.format_card('phone', '13800138000', p)
check('卡片里有号码/归属地/运营商', all(x in card for x in
      ('13800138000', '北京', '移动')))
check('★ 「北京 北京」这种重复只显示一个', '北京 北京' not in card)
miss = L.format_card('phone', '19900001111', {})
check('★★ 查不到时**明说查不到**，不是显示个空白',
      '没查到' in miss, miss.replace('\n', ' | ')[:60])

print()
print('=' * 62)
print('三、身份证（离线区划表 + 校验位）')
print('=' * 62)
check('★ 校验位算法对不对（真号通过）',
      L.id_checksum_ok('110101199003076130') is True)
check('★ 改一位就过不了', L.id_checksum_ok('110101199003076131') is False)
check('110101 → 北京市东城区', L.idcard_region('110101199003076130') == '北京市东城区',
      L.idcard_region('110101199003076130'))
check('440305 → 广东省深圳市南山区',
      L.idcard_region('440305199001011239') == '广东省深圳市南山区',
      L.idcard_region('440305199001011239'))
check('370102 → 山东省济南市历下区',
      L.idcard_region('370102199001011239') == '山东省济南市历下区',
      L.idcard_region('370102199001011239'))
check('未知区划码 → 空串（不瞎猜）', L.idcard_region('999999199001011239') == '')

c1 = L.format_card('idcard', '110101199003076130',
                   {'region': L.idcard_region('110101199003076130'), 'valid': True})
check('卡片：归属地 + 校验通过', '北京市东城区' in c1 and '✅' in c1)
c2 = L.format_card('idcard', '110101199003076131',
                   {'region': L.idcard_region('110101199003076131'), 'valid': False})
check('★ 校验位不对会明说（不是含糊地回一句查不到）',
      '❌' in c2 and '北京市东城区' in c2, c2.replace('\n', ' | ')[-40:])

print()
print('=' * 62)
print('四、银行卡（联网那步用桩）')
print('=' * 62)
real_bank = L.bank_info


class FakeResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class FakeClient:
    payload = {'validated': True, 'bank': 'ICBC', 'cardType': 'DC'}

    def __init__(self, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url):
        return FakeResp(FakeClient.payload)


FakeClient.__name__ = 'Client'
L.httpx = type('H', (), {'Client': FakeClient})

info = L.bank_info('6222020200112233445')
check('银行代号换成中文（ICBC → 中国工商银行）',
      info.get('bank_name') == '中国工商银行', str(info))
check('卡类型换成中文（DC → 借记卡）',
      '借记卡' in (info.get('card_type') or ''), str(info.get('card_type')))

FakeClient.payload = {'validated': False, 'stat': 'ok'}
check('★★ 卡号查不出来 → 返回空 dict（**不编一个银行出来**）',
      L.bank_info('6222020200112233999') == {})

FakeClient.payload = {'validated': True, 'bank': 'ZZZZ', 'cardType': 'CC'}
odd = L.bank_info('6222020200112233445')
check('★ 不认识的银行代号 → 原样显示（不瞎猜成别家银行）',
      odd.get('bank_name') == 'ZZZZ', str(odd.get('bank_name')))

cardb = L.format_card('bankcard', '6222020200112233445',
                      {'bank_name': '中国工商银行', 'card_type': '借记卡（储蓄卡）'})
check('银行卡卡片：银行 + 类型', '中国工商银行' in cardb and '借记卡' in cardb)
check('★★ 银行卡查不到时说明白（不写「归属地：未知」糊弄）',
      '没查到' in L.format_card('bankcard', '6222020200112233445', {}))

check('★ 卡片里没写 TRON', 'TRON' not in cardb.upper())

print()
print('=' * 62)
print('五、接线：群里/私聊直接发号码')
print('=' * 62)


class FakeAPI:
    def __init__(self):
        self.calls = []

    def call(self, method, **p):
        self.calls.append((method, p))
        return {'message_id': len(self.calls)}

    def call_file(self, method, field, fn, obj, **p):
        self.calls.append((method, p))
        return {'message_id': len(self.calls)}

    def last_of(self, method):
        for m, p in reversed(self.calls):
            if m == method:
                return p
        return {}


class FakeMgr:
    def save(self):
        pass

    def is_expired(self, b):
        return False


CHAT = -1001234567890


def mk():
    r = LedgerRunner(FakeMgr(), {'id': 'lktest', 'token': '1:FAKE',
                                 'admin_ids': [111], 'owner_id': 111,
                                 'enabled': True, 'type': 'ledger'})
    r.api = FakeAPI()
    r.me = {'id': 999, 'username': 'testbot'}
    return r


def upd(text, uid=222, mid=1, chat_type='supergroup', chat_id=CHAT):
    return {'update_id': mid, 'message': {
        'message_id': mid, 'date': int(time.time()),
        'chat': {'id': chat_id, 'type': chat_type, 'title': '测试群'},
        'from': {'id': uid, 'first_name': '张', 'username': 'zs'},
        'text': text}}


r = mk()
r.handle(upd('13800138000'))
t = r.api.last_of('sendMessage').get('text') or ''
check('★★ 群里发手机号 → 回归属地卡片', '归属地' in t and '北京' in t, t[:60].replace('\n', '|'))
check('★ 发到群里', r.api.last_of('sendMessage').get('chat_id') == CHAT)

r2 = mk()
r2.handle(upd('110101199003076130', uid=333, mid=2, chat_type='private', chat_id=333))
t = r2.api.last_of('sendMessage').get('text') or ''
check('★ 私聊发身份证 → 回归属地', '北京市东城区' in t, t[:60].replace('\n', '|'))
check('★ 私聊回给那个人', r2.api.last_of('sendMessage').get('chat_id') == 333)

r3 = mk()
r3.handle(upd('+100 张三', mid=3))
check('★★ 【没被抢】+100 张三 照样记账',
      len(r3.store.entries(CHAT)) == 1 and '归属地' not in
      (r3.api.last_of('sendMessage').get('text') or ''))
r3.handle(upd('1+1', mid=4, chat_id=-100999))
check('★ 【没被抢】1+1 还是算式',
      '2' in (r3.api.last_of('sendMessage').get('text') or ''))
r3.handle(upd('1000/9', mid=5, chat_id=-100998))
check('★ 【没被抢】1000/9 还是算式',
      '111.11' in (r3.api.last_of('sendMessage').get('text') or ''))
r3.api.calls = []            # ★ 先清空：不然等的是上一条算式的回复
r3.handle(upd('G', mid=6, chat_id=-100997))
for _ in range(80):          # 查贵金属是后台线程（还要联网），得等它回来
    if r3.api.last_of('sendMessage'):
        break
    time.sleep(0.1)
check('★ 【没被抢】G 还是查贵金属',
      '贵金属' in (r3.api.last_of('sendMessage').get('text') or ''))

print()
print('=' * 62)
print('六、号码不进日志（那个文件会传阅）')
print('=' * 62)
src = open(os.path.join(HERE, 'runners', 'ledger', 'runner.py'),
           encoding='utf-8').read()
check('★★ reply_lookup 里有「抹掉号码再记日志」的处理（scrub）',
      'def scrub(' in src and "num[:4] + '****'" in src)
check('★ 日志那一行用的是 scrub 过的文本',
      'scrub(e)' in src)

print()
print('=' * 62)
print('七、数据文件缺失时不能崩')
print('=' * 62)
saved = L.PHONE_DAT
L.PHONE_DAT = os.path.join(TMP, '不存在的文件.dat')
L._phone_buf = None
try:
    L.phone_info('13800138000')
    check('★ 手机号库没了 → 抛 LookupError（而不是崩在别处）', False)
except L.LookupError as e:
    check('★ 手机号库没了 → 抛 LookupError，说明得清清楚楚',
          '手机号库没找到' in str(e), str(e)[:60])
finally:
    L.PHONE_DAT = saved
    L._phone_buf = None
check('★ 恢复之后照样能查', L.phone_info('13800138000').get('province') == '北京')

r4 = mk()
saved = L.PHONE_DAT
L.PHONE_DAT = os.path.join(TMP, '不存在.dat')
L._phone_buf = None
r4.handle(upd('13800138000', mid=9, chat_id=-100996))
t = r4.api.last_of('sendMessage').get('text') or ''
check('★★ 库缺失时群里收到的是「暂时不可用」，**不是崩掉/装死**',
      '暂时不可用' in t, t[:60])
L.PHONE_DAT = saved
L._phone_buf = None

print()
print('=' * 62)
print('八、银行卡归属地（百度那个付费接口，用户去申请的那个）')
print('=' * 62)
# ★ 用户 2026-10-08 拿**别人机器人**的截图来问「为什么它能查归属地」。
#   查清楚了：那套字段（银行编码/卡名称/卡bin/总行联行号/银行电话/归属地）
#   是百度 API 商城的「银行卡基本信息」，**0元/50次试用**，之后要花钱。
#   所以：配了 AppCode 走付费接口，没配就退回免费接口（只有银行名+卡类型）。

RICH_OK = {"code": 0, "desc": "成功", "data": {
    "bankCode": "308584000013", "bankId": "03080000", "bankName": "招商银行",
    "abbr": "CMB", "cardName": "银联IC普卡", "cardType": "借记卡",
    "cardBin": "621483", "binLen": 6, "area": "山东省 - 济南市",
    "bankPhone": "95555", "bankUrl": "https://www.cmbchina.com"}}


class RResp:
    def __init__(self, payload):
        self._p = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._p


class RClient:
    payload = RICH_OK
    got = {}

    def __init__(self, **kw):
        RClient.got = kw

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url):
        RClient.url = url
        return RResp(RClient.payload)


RClient.__name__ = 'Client'
L.httpx = type('H', (), {'Client': RClient})

info = L.bank_info('6214835411995499', 'MYAPPCODE123456')
check('★★ 配了密钥 → 查得到归属地', info.get('area') == '山东省 - 济南市',
      str(info.get('area')))
check('★★ 联行号也在（截图里那个 308584000013）',
      info.get('bank_code') == '308584000013', str(info.get('bank_code')))
check('★ 银行名/卡名称/卡类型/卡bin 都对',
      info.get('bank_name') == '招商银行' and info.get('card_name') == '银联IC普卡'
      and info.get('card_type') == '借记卡' and info.get('card_bin') == '621483')
check('★★ 走的是 https（卡号不走明文 http）',
      RClient.url.startswith('https://'), RClient.url[:46])
check('★★ 密钥走请求头 X-Bce-Signature: AppCode/xxx',
      (RClient.got.get('headers') or {}).get('X-Bce-Signature')
      == 'AppCode/MYAPPCODE123456', str(RClient.got.get('headers'))[:70])

rc = L.format_card('bankcard', '6214835411995499', info)
for want in ('招商银行', '银联IC普卡', '借记卡', '621483', '山东省 - 济南市',
             '308584000013', '95555'):
    check('卡片里有「%s」' % want, want in rc)
check('★ 卡片里没写 TRON', 'TRON' not in rc.upper())

RClient.payload = {"code": 2, "desc": "无效卡号"}
check('★★ 接口说「无效卡号」(code=2) → 返回空（**不是故障，别报错**）',
      L.bank_info('6214835411995499', 'MYAPPCODE123456') == {},
      str(L.bank_info('6214835411995499', 'MYAPPCODE123456')))


class RFail(RClient):
    def get(self, url):
        if 'bdymkt' in url:
            raise RuntimeError('付费接口超时')
        return RResp({"validated": True, "bank": "ICBC", "cardType": "DC"})


RFail.__name__ = 'Client'
L.httpx = type('H', (), {'Client': RFail})
deg = L.bank_info('6222020200112233445', 'MYAPPCODE123456')
check('★★★ 配了密钥但接口挂了 → **退回免费接口**（不是整条失败）',
      deg.get('source') == 'basic' and deg.get('bank_name') == '中国工商银行',
      str(deg.get('source')))
check('★★ 而且**如实告诉用户降级了**（不悄悄少给东西）',
      '归属地暂时查不到' in (deg.get('note') or ''), str(deg.get('note'))[:50])

nokey = L.format_card('bankcard', '6222020200112233445',
                      {"source": "basic", "bank_name": "中国工商银行",
                       "card_type": "借记卡（储蓄卡）"})
check('★★ 没配密钥时**只报查到的**（用户要求：别在卡片上加提示行）',
      '银行密钥' not in nokey and '中国工商银行' in nokey,
      nokey.replace('\n', ' | ')[-40:])

print()
print('=' * 62)
print('九、设置银行密钥（主人私聊）')
print('=' * 62)
L.httpx = type('H', (), {'Client': RClient})
RClient.payload = RICH_OK

r5 = mk()
r5.handle(upd('设置银行密钥 abcdef1234567890', uid=111, mid=20,
              chat_type='private', chat_id=111))
t = r5.api.last_of('sendMessage').get('text') or ''
check('★★ 主人发「设置银行密钥 xxx」→ 存下来了',
      (r5.data.get('bank_appcode') == 'abcdef1234567890'), str(r5.data.get('bank_appcode')))
check('★★★ 回复里**不回显密钥**，只说后 4 位',
      'abcdef1234567890' not in t and '7890' in t, t[:60].replace('\n', ' | '))

r5.api.calls = []
r5.handle(upd('银行密钥', uid=111, mid=21, chat_type='private', chat_id=111))
t = r5.api.last_of('sendMessage').get('text') or ''
check('★ 发「银行密钥」→ 报配没配（还是不回显）',
      '7890' in t and 'abcdef1234567890' not in t, t[:60].replace('\n', ' | '))

r5.api.calls = []
r5.handle(upd('设置银行密钥 清空', uid=111, mid=22,
              chat_type='private', chat_id=111))
check('★ 「清空」→ 删掉', not r5.data.get('bank_appcode'))

r5.api.calls = []
r5.handle(upd('设置银行密钥 短', uid=111, mid=23, chat_type='private', chat_id=111))
check('★★ 太短的不像 AppCode → 给说明，不当成密钥存下来',
      not r5.data.get('bank_appcode') and '百度' in
      (r5.api.last_of('sendMessage').get('text') or ''))
check('★ 说明里给了申请入口', 'apis.baidu.com' in
      (r5.api.last_of('sendMessage').get('text') or ''))

r6 = mk()
r6.api.calls = []
r6.handle(upd('设置银行密钥 abcdef1234567890', uid=222, mid=24,
              chat_type='private', chat_id=222))
check('★★ 普通人发 → 被拒，也没存下来',
      not r6.data.get('bank_appcode') and '只有机器人主人' in
      (r6.api.last_of('sendMessage').get('text') or ''))

r7 = mk()
r7.api.calls = []
r7.handle(upd('设置银行密钥 abcdef1234567890', uid=111, mid=25))   # 群里发
check('★★ 群里发这个命令 → 不生效（免得群里被人改）',
      not r7.data.get('bank_appcode'), str(r7.data.get('bank_appcode')))

src = open(os.path.join(HERE, 'runners', 'ledger', 'runner.py'),
           encoding='utf-8').read()
check('★★★ 密钥存在 data/ 里（不推送、不进日志），不写死在代码里',
      "self.data['bank_appcode'] = args" in src
      and "self.data['bank_appcode'] = '" not in src)

for x in (r, r2, r3, r4, r5, r6, r7):
    try:
        x.close_db()
    except Exception:
        pass
shutil.rmtree(TMP, ignore_errors=True)
print()
print('=' * 62)
print('结果：%d 项通过，%d 项失败' % (len(OK), len(BAD)))
for x in BAD:
    print('  ❌', x)
print('=' * 62)
sys.exit(1 if BAD else 0)
