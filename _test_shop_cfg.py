# -*- coding: utf-8 -*-
"""商城配置：笔数档位（定价）能不能存进去、读出来

★ 为什么专门测这个：用户报「笔数套餐那里没有可以定价的地方」。
  他的商城机器人点「🔥 笔数套餐」会回「笔数套餐还没定价，请联系管理员」，
  因为 `auto_prices` 是空的。所以要么是面板没地方填、要么是填了存不上。

  这个测试把整条链路走一遍：
      面板填档位 → POST /api/shop/<id>/config → 存进 bots.json
      → GET 回来还是那个值 → 机器人 `auto_tiers()` 能读到
  只要有一环断了，用户那边就是「设不了价」。
"""
import os
import shutil
import sys
import threading
import time
from http.server import ThreadingHTTPServer

sys.stdout.reconfigure(encoding='utf-8')

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(HERE, '_tmp_shop_cfg')
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(os.path.join(TMP, 'data'), exist_ok=True)

import core
core.BASE_DIR = TMP
core.BOTS_FILE = os.path.join(TMP, 'bots.json')
core.DATA_DIR = os.path.join(TMP, 'data')

from manager import BotManager                      # noqa: E402
from merchants import MerchantStore                 # noqa: E402
from panel import PanelHandler                      # noqa: E402

OK, BAD = [], []
ADMIN_PW = 'ADMINPW123'


def check(name, cond, extra=''):
    (OK if cond else BAD).append(name)
    print('  %s %s%s' % ('✅' if cond else '❌', name,
                         ('  → %s' % (extra,)) if extra else ''))


PanelHandler.mgr = BotManager({'pass': ADMIN_PW})
PanelHandler.password = ADMIN_PW
PanelHandler.merch = MerchantStore()
PanelHandler.token_secret = 'S' * 32
PanelHandler.page_cache = None

PanelHandler.mgr.bots.append({
    'id': 'shopX', 'note': '商城测试', 'type': 'shop',
    'token': '1000:T', 'username': 'shopx_bot', 'name': '商城测试',
    'bind_code': 'CSHOPX', 'created': '2026-01-01 00:00', 'enabled': False,
    'expire_at': 0, 'status': 'stopped', 'error': '', 'mid': '',
    'admin_ids': [], 'pending_ops': [],
    'shop': {'enabled': True, 'provider': 'mock',
             'trx_own': 'TBKw17Fxy5kBbev8MMZ16yVKuQTQfsETta'},
})
PanelHandler.mgr.save()

httpd = ThreadingHTTPServer(('127.0.0.1', 0), PanelHandler)
BASE = 'http://127.0.0.1:%d' % httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()
time.sleep(0.3)

H = {'X-Panel-Pass': ADMIN_PW}


def get(p):
    return requests.get(BASE + p, headers=H, timeout=10)


def post(p, body):
    return requests.post(BASE + p, json=body, headers=H, timeout=10)


print('=' * 60)
print('① 面板能不能读到「笔数档位」')
print('=' * 60)
r = get('/api/shop/shopX/config').json()
check('配置读得到', r.get('ok') is True, str(r)[:80])
cfg = r.get('config') or {}
check('★★ 返回里带 auto_prices（前端靠它填文本框）',
      'auto_prices' in cfg, str([k for k in cfg if 'auto' in k]))
check('★ 返回里带 auto_enabled', 'auto_enabled' in cfg)
check('刚开始是空的（所以机器人说「还没定价」）',
      not cfg.get('auto_prices'), str(cfg.get('auto_prices')))

print()
print('=' * 60)
print('② 面板填档位 → 能不能存进去')
print('=' * 60)
tiers = {'10': 30, '50': 140, '100': 260, '300': 750}
r = post('/api/shop/shopX/config',
         {'auto_enabled': True, 'auto_prices': tiers}).json()
check('保存返回 ok', r.get('ok') is True, str(r)[:80])

r2 = get('/api/shop/shopX/config').json()
got = (r2.get('config') or {}).get('auto_prices')
check('★★ 存进去了、也读得回来（用户「设不了价」就是这个断了）',
      got == tiers, str(got))
check('★ 档位按「笔数」存，别被转成字符串那串乱序',
      set(got or {}) == set(tiers), str(got))
check('★ auto_enabled 也存上了',
      (r2.get('config') or {}).get('auto_enabled') is True)

print()
print('=' * 60)
print('③ 机器人那边读得到吗（它才是给客户报价的）')
print('=' * 60)
from runners.shop import store as shop_store                                   # noqa: E402


class FakeRunner:
    """ShopStore 要的替身：只要有 bot 和 data 两个属性就够了"""

    def __init__(self, bot):
        self.bot = bot
        self.data = {}

    def save_data(self):
        pass


st = shop_store.ShopStore(FakeRunner(PanelHandler.mgr.find('shopX')))
tiers_read = st.auto_tiers()
check('★★ 机器人读到 4 个档位', len(tiers_read) == 4, str(tiers_read))
check('★★ 报价正确（50 笔 = 140 TRX）',
      any(int(c) == 50 and float(p) == 140 for c, p in tiers_read),
      str(tiers_read))
check('★ 按笔数从小到大排好了',
      [int(c) for c, _ in tiers_read] == [10, 50, 100, 300],
      str([c for c, _ in tiers_read]))
check('★★ 单个档位查价也对', st.auto_price('100') == 260,
      str(st.auto_price('100')))

print()
print('=' * 60)
print('④ 边界：空档位 / 乱填 / 只填一半')
print('=' * 60)
post('/api/shop/shopX/config', {'auto_prices': {}})
check('★ 清空档位不报错，机器人也读得到空',
      get('/api/shop/shopX/config').json()['config']['auto_prices'] == {})

post('/api/shop/shopX/config',
     {'auto_prices': {'20': 55, '坏行': 'abc', '0': 0}})
got = get('/api/shop/shopX/config').json()['config']['auto_prices']
check('★ 乱数据不会让接口崩', isinstance(got, dict), str(got))

post('/api/shop/shopX/config', {'auto_prices': {'5': 12}})
check('★ 单档位也能用',
      get('/api/shop/shopX/config').json()['config']['auto_prices'] == {'5': 12})

try:
    httpd.shutdown()
except Exception:
    pass
shutil.rmtree(TMP, ignore_errors=True)

print()
print('=' * 60)
print('结果：%d 项通过，%d 项失败' % (len(OK), len(BAD)))
for b in BAD:
    print('  ❌', b)
print('=' * 60)
sys.exit(1 if BAD else 0)
