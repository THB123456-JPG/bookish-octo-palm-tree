# -*- coding: utf-8 -*-
"""「添加到桌面」—— 图标 / manifest / 免鉴权

★ 最容易写错、而且**在电脑上完全看不出来**的一条：
  浏览器来取 manifest 和图标时**不会带 X-Panel-Pass 头**
  （那是页面里的 JS 自己加的，浏览器自己发起的请求没有）。
  所以这几条路由必须免鉴权 —— 挂了鉴权的话：
    · 你在电脑上一切正常（浏览器缓存过、或者你没注意）
    · 手机上加到桌面是**白图标 + 名字显示成网址**
  这里专门断言「不带任何密码头也能拿到 200」。
"""
import io
import json
import os
import shutil
import sys
import threading
import time
from http.server import ThreadingHTTPServer

sys.stdout.reconfigure(encoding='utf-8')

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = os.path.join(HERE, '_tmp_addhome')
shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(os.path.join(TMP, 'data'), exist_ok=True)

import core                                          # noqa: E402
core.BASE_DIR = TMP
core.BOTS_FILE = os.path.join(TMP, 'bots.json')
core.DATA_DIR = os.path.join(TMP, 'data')
core.CONFIG_FILE = os.path.join(TMP, 'config.json')
core.CODES_DIR = os.path.join(TMP, 'codes')
json.dump({}, io.open(core.CONFIG_FILE, 'w', encoding='utf-8'))

from manager import BotManager                       # noqa: E402
from merchants import MerchantStore                  # noqa: E402
from panel import PanelHandler, STATIC_FILES         # noqa: E402

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

httpd = ThreadingHTTPServer(('127.0.0.1', 0), PanelHandler)
BASE = 'http://127.0.0.1:%d' % httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()
time.sleep(0.3)

STATIC = ['/manifest.webmanifest', '/icon-192.png', '/icon-512.png',
          '/apple-touch-icon.png']

print('=' * 62)
print('① ★★ 免鉴权：不带任何密码头也必须能拿到')
print('=' * 62)
for p in STATIC:
    r = requests.get(BASE + p, timeout=10)          # 故意不带 headers
    check('★ 不带密码 %s → 200' % p, r.status_code == 200, str(r.status_code))
    check('  不是被登录页糊弄（内容不是 HTML）',
          not r.content.lstrip().startswith(b'<'),
          r.content[:16])

print()
print('=' * 62)
print('② manifest 内容（手机上的名字、图标就是从这儿来的）')
print('=' * 62)
r = requests.get(BASE + '/manifest.webmanifest', timeout=10)
try:
    mf = r.json()
    okjson = True
except Exception as e:
    mf, okjson = {}, False
    check('manifest 是合法 JSON', False, str(e))
if okjson:
    check('manifest 是合法 JSON', True)
check('★ 有 name（装到桌面上显示的名字）', bool(mf.get('name')), mf.get('name'))
check('★ 有 short_name（图标下面那行，长了会被截断）',
      bool(mf.get('short_name')), mf.get('short_name'))
check('★ display=standalone（不然点开还是浏览器，有地址栏）',
      mf.get('display') == 'standalone', mf.get('display'))
check('★ start_url 是 /（点图标进的就是这一页）',
      mf.get('start_url') == '/', mf.get('start_url'))
check('theme_color 跟面板背景一个色（状态栏不突兀）',
      mf.get('theme_color') == '#0f1420', mf.get('theme_color'))
sizes = sorted(i.get('sizes') for i in mf.get('icons') or [])
check('★★ 图标齐了：192 和 512 都要有（安卓两个都查）',
      '192x192' in sizes and '512x512' in sizes, str(sizes))
check('★ 有一个是 maskable（不然安卓裁圆角会缺一块）',
      any(i.get('purpose') == 'maskable' for i in mf.get('icons') or []))
check('图标路径都是绝对路径（相对路径在子页面会 404）',
      all((i.get('src') or '').startswith('/')
          for i in mf.get('icons') or []))

print()
print('=' * 62)
print('③ 图标是真 PNG，而且尺寸跟声明的一致')
print('=' * 62)
try:
    from PIL import Image
    has_pil = True
except Exception:
    has_pil = False
WANT = {'/icon-192.png': (192, 192), '/icon-512.png': (512, 512),
        '/apple-touch-icon.png': (180, 180)}
for p, wh in WANT.items():
    r = requests.get(BASE + p, timeout=10)
    check('%s 是真 PNG' % p, r.content[:8] == b'\x89PNG\r\n\x1a\n')
    if has_pil:
        im = Image.open(io.BytesIO(r.content))
        check('★ %s 尺寸是 %dx%d' % (p, wh[0], wh[1]),
              im.size == wh, '%dx%d' % im.size)
        # ★ iOS 的桌面图标不带透明通道，带的话有的系统会糊成黑底
        if p == '/apple-touch-icon.png':
            check('★ apple-touch-icon 没有透明通道（iOS 要求）',
                  im.mode in ('RGB', 'P'), im.mode)

print()
print('=' * 62)
print('④ 路径白名单：不能顺着它读到别的文件')
print('=' * 62)
# STATIC_FILES 是个精确匹配的字典，不是按目录拼路径 —— 这里锁住这一点
for bad in ('/../config.json', '/static/manifest.webmanifest',
            '/icon-192.png/../config.json', '/%2e%2e/config.json'):
    r = requests.get(BASE + bad, timeout=10)
    check('★ %s 拿不到东西' % bad,
          r.status_code != 200 or r.content.lstrip().startswith(b'<'),
          '%s %d 字节' % (r.status_code, len(r.content)))
check('白名单里只有这 4 个',
      set(STATIC_FILES) == set(STATIC), str(sorted(STATIC_FILES)))

# ★ 反过来的：带查询串**必须照常给**。浏览器取 manifest 时经常带
#   ?v=xxx 之类的缓存参数，当成 404 的话安卓就装不上
r = requests.get(BASE + '/manifest.webmanifest?x=1', timeout=10)
check('★ 带查询串照样给（浏览器会带缓存参数来取）',
      r.status_code == 200 and r.content.lstrip().startswith(b'{'),
      str(r.status_code))
r = requests.get(BASE + '/icon-192.png?v=2', timeout=10)
check('★ 图标带查询串也照样给', r.status_code == 200
      and r.content[:8] == b'\x89PNG\r\n\x1a\n', str(r.status_code))

print()
print('=' * 62)
print('⑤ 页面头部该挂的东西都挂上了')
print('=' * 62)
html = io.open(os.path.join(HERE, 'panel_page.html'), encoding='utf-8').read()
check('★ 挂了 manifest', 'rel="manifest"' in html
      and '/manifest.webmanifest' in html)
check('★ 挂了 apple-touch-icon（iOS 只认这个）',
      'rel="apple-touch-icon"' in html and '/apple-touch-icon.png' in html)
check('★★ iOS 全屏那三个 meta（少一个点开就带地址栏）',
      'apple-mobile-web-app-capable' in html
      and 'apple-mobile-web-app-title' in html
      and 'apple-mobile-web-app-status-bar-style' in html)
check('有 theme-color（不然手机状态栏是白的）',
      'name="theme-color"' in html)

print()
print('=' * 62)
print('⑥ 按钮和逻辑')
print('=' * 62)
check('有「添加到桌面」按钮', 'id="addHome"' in html and 'addToHome()' in html)
check('★ 只在手机上显示（电脑上加桌面没意义）',
      'function isMobile()' in html and 'isMobile()' in html)
check('★ 已经装过了就不再提示',
      'function isStandalone()' in html and 'navigator.standalone' in html)
check('★ 接了 beforeinstallprompt（安卓有 HTTPS 后能真一键）',
      'beforeinstallprompt' in html and 'DEFERRED_INSTALL' in html)
check('★ 拿不到那个事件时教用户手动加（iOS 只能这样）',
      'function showHomeTip()' in html and 'id="homeTip"' in html)
check('指引里区分了 iOS 和安卓', 'htBody' in html and 'iPhone|iPad|iPod' in html)
check('弹层能关掉（点按钮 / 点背景）',
      'function closeHomeTip()' in html
      and html.count('closeHomeTip()') >= 2)
check('★ 弹层 z-index 比登录遮罩高（不然被盖住看不见）',
      '#homeTip{position:fixed' in html and 'z-index:102' in html)
check('CSS 里有 .addhome 样式', '.addhome{' in html)

print()
print('=' * 62)
print('⑦ 打包清单带上了 static/（漏了的话 exe 里没图标）')
print('=' * 62)
spec = io.open(os.path.join(HERE, 'TG客服多开版.spec'), encoding='utf-8').read()
check('★★ spec 的 datas 里有 static',
      "('static', 'static')" in spec, '漏了手机上加桌面就是白图标')
for f in ('manifest.webmanifest', 'icon-192.png', 'icon-512.png',
          'apple-touch-icon.png'):
    check('static/%s 文件在' % f,
          os.path.exists(os.path.join(HERE, 'static', f)))

try:
    httpd.shutdown()
except Exception:
    pass
shutil.rmtree(TMP, ignore_errors=True)

print()
print('=' * 62)
print('结果：%d 项通过，%d 项失败' % (len(OK), len(BAD)))
for b in BAD:
    print('  ❌', b)
print('=' * 62)
sys.exit(1 if BAD else 0)
