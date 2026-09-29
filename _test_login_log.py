# -*- coding: utf-8 -*-
"""登录日志测试

★ 两条底线：
  1. **写日志绝不能拖慢/弄挂登录** —— 它是旁路，不是主流程
  2. 归属地查询在后台做，查不到也不影响记录本身
"""
import json
import os
import shutil
import sys
import time

sys.stdout.reconfigure(encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(HERE, '_tmp_loginlog')
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(os.path.join(TMP, 'data'), exist_ok=True)

import core
core.BASE_DIR = TMP
core.DATA_DIR = os.path.join(TMP, 'data')

import login_log as LL

OK, BAD = [], []


def check(name, cond, extra=''):
    (OK if cond else BAD).append(name)
    print('  %s %s%s' % ('✅' if cond else '❌', name,
                         ('  → %s' % (extra,)) if extra else ''))


print('=' * 62)
print('一、记一条')
print('=' * 62)
LL.add('1.2.3.4', '（管理员）', 'admin', True, 'Mozilla/5.0 Windows')
rows = LL.recent()
check('记上了', len(rows) == 1, '%d 条' % len(rows))
check('IP 对', rows[0]['ip'] == '1.2.3.4', rows[0]['ip'])
check('账号对', rows[0]['user'] == '（管理员）')
check('角色对', rows[0]['role'] == 'admin')
check('结果是成功', rows[0]['ok'] is True)
check('★ 时间戳是刚才', abs(rows[0]['t'] - time.time()) < 5,
      str(rows[0]['t']))
check('★ 归属地一开始是空的（后台慢慢查）', rows[0]['city'] == '',
      repr(rows[0]['city']))

print()
print('=' * 62)
print('二、新的在前面')
print('=' * 62)
LL.add('5.6.7.8', 'zhangsan', 'merchant', False, '')
rows = LL.recent()
check('现在两条', len(rows) == 2, '%d 条' % len(rows))
check('★ 新的排最前', rows[0]['ip'] == '5.6.7.8', rows[0]['ip'])
check('失败的也记下来了', rows[0]['ok'] is False)
check('商户角色对', rows[0]['role'] == 'merchant')

print()
print('=' * 62)
print('三、本机 / 内网地址不查归属地')
print('=' * 62)
for ip in ('127.0.0.1', '192.168.1.5', '10.0.0.3', '100.64.1.2',
           '172.16.0.1', '::1', ''):
    check('★ %-14s → 不查' % (repr(ip)), LL.lookup_city(ip) == '本机/内网',
          LL.lookup_city(ip))
check('公网地址会去查（这里只看它没被当成内网）',
      LL.lookup_city('110.53.1.1') != '本机/内网')

print()
print('=' * 62)
print('四、查不到归属地也不影响记录')
print('=' * 62)
saved = LL.GEO_URL
LL.GEO_URL = 'http://127.0.0.1:9/json/%s'      # 保证连不上
LL._geo_cache.clear()
LL.add('9.9.9.9', 'x', 'admin', True, '')
time.sleep(1.2)
rows = LL.recent()
r = [x for x in rows if x['ip'] == '9.9.9.9'][0]
check('★★ 网络不通时记录照样在', r is not None)
check('★ 归属地空着（不是报错、不是崩）', r['city'] == '', repr(r['city']))
LL.GEO_URL = saved

print()
print('=' * 62)
print('五、上限 & 清空')
print('=' * 62)
for i in range(LL.MAX_RECORDS + 30):
    LL.add('10.0.0.%d' % (i % 250), 'u', 'admin', True, '')
check('★ 只留最近 %d 条' % LL.MAX_RECORDS,
      len(LL.recent(9999)) == LL.MAX_RECORDS,
      '%d 条' % len(LL.recent(9999)))
st = LL.stats()
check('统计对得上', st['total'] == LL.MAX_RECORDS
      and st['ok'] + st['fail'] == st['total'], str(st))
n = LL.clear()
check('清空返回条数', n == LL.MAX_RECORDS, str(n))
check('清空后是空的', LL.recent() == [])

print()
print('=' * 62)
print('六、坏数据不炸')
print('=' * 62)
LL.clear()
LL.add(None, None, None, False, None)
rows = LL.recent()
check('★ IP 传 None 也记（写成「未知」）', rows[0]['ip'] == '未知',
      rows[0]['ip'])
LL.add('', '', '', True, '')
check('空字符串行', LL.recent()[0]['ip'] == '未知')

# 文件被写成乱七八糟的东西
with open(os.path.join(core.DATA_DIR, 'login_log.json'), 'w',
          encoding='utf-8') as f:
    f.write('{"不是":"列表"}')
check('★ 文件里不是列表 → 当空的处理（不崩）', LL.recent() == [])
LL.add('1.1.1.1', 'a', 'admin', True, '')
check('还能继续记', len(LL.recent()) == 1)

with open(os.path.join(core.DATA_DIR, 'login_log.json'), 'w',
          encoding='utf-8') as f:
    f.write('这不是 json')
check('★ 文件是坏 JSON 也不崩', LL.recent() == [])

print()
print('=' * 62)
print('七、★ 写日志出问题绝不能影响登录')
print('=' * 62)
saved = LL._path
LL._path = lambda: '/根本不存在的盘/x/login_log.json'


def boom():
    return '/\x00bad/login_log.json'


LL._path = boom
try:
    LL.add('1.2.3.4', 'a', 'admin', True, '')
    check('★ 路径坏掉时 add() 会抛异常（由调用方兜）', False, '居然没抛')
except Exception as e:
    check('★ 路径坏掉时 add() 抛异常（panel 那边有 try 兜住）',
          True, type(e).__name__)
LL._path = saved

print()
print('=' * 62)
print('八、UA 识别')
print('=' * 62)
import re
src = open(os.path.join(HERE, 'panel_page.html'), encoding='utf-8').read()
check('★ 页面有 uaLabel 把 UA 变成人话', 'function uaLabel' in src)
check('  认安卓 / iPhone / Windows / Mac',
      all(k in src for k in ('安卓', 'iPhone', 'Windows', 'Mac')))
check('  认微信 / Chrome / Safari / Edge',
      all(k in src for k in ('微信', 'Chrome', 'Safari', 'Edge')))

shutil.rmtree(TMP, ignore_errors=True)
print()
print('=' * 62)
print('结果：%d 项通过，%d 项失败' % (len(OK), len(BAD)))
for x in BAD:
    print('  ❌', x)
print('=' * 62)
sys.exit(1 if BAD else 0)
