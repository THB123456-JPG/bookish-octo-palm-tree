# -*- coding: utf-8 -*-
"""检查打出来的 exe 是不是「新代码」打出来的

为什么要这个：
  1. PyInstaller 单文件 exe 里，纯 Python 模块压在 PYZ（zlib）里，
     直接拿字符串搜 exe 二进制是搜不到的（压缩了），看着像「没打进去」。
  2. 反过来，页面（html）是当数据文件塞进去的，能直接解出来看内容 ——
     这个最容易忘：改了 html 忘记重新打包，跑起来还是旧的。

★ CArchiveReader.toc 是 **dict**（key=名字），不是 list —— 按 list 遍历
  会拿到名字字符串，item[-1] 就成了名字的最后一个字符。踩过。
"""
import io
import os
import re
import sys

sys.stdout.reconfigure(encoding='utf-8')
import glob

from PyInstaller.archive.readers import CArchiveReader

BAD = []

exe = glob.glob('dist/*.exe')[0]
print('exe：%s' % exe)
arc = CArchiveReader(exe)

# ---- ① 页面内容（数据文件，解得出来）----
print('\n--- 打包进去的页面是不是最新的 ---')
for name, must_have in (
        ('panel_page.html', b'function firstUnreadKey'),
        ('panel_page.html', b'if(m && m.is_bot) continue;'),
        ('panel_page.html', b'var bot = !!m.is_bot && !own;'),
        # ★ 机器人消息靠左（用户要求跟群友同一条流，别改回居中）
        ('panel_page.html', b'.mb.bot{justify-content:flex-start}'),
        ('panel_page.html', b'.mb.bot{justify-content:center}'),
        # 图片缓存：重开群消息不用重下
        ('panel_page.html', b'MSG.IMG = {};'),
        # 「添加到桌面」（漏了的话手机上加桌面是白图标 + 名字是网址）
        ('panel_page.html', b'rel="manifest"'),
        ('panel_page.html', b'apple-mobile-web-app-capable'),
        ('panel_page.html', b'id="addHome"')):
    meta = arc.toc.get(name)
    if not meta:
        print('  ❌ %s 不在包里' % name)
        BAD.append(name + ' 不在包里')
        continue
    blob = arc.extract(name)
    ok = must_have in blob
    # 最后一条是「不许出现」的（禁止回退成居中）
    if must_have.endswith(b'center}'):
        ok = not ok
    if not ok:
        BAD.append('%s 的 %s 不对' % (name, must_have.decode()))
    print('  %-18s %-36s %s' % (name, must_have.decode()[:34],
                                '✅ 对' if ok else '❌ 不对'))

# ---- ①b 「添加到桌面」的图标和 manifest 在不在包里 ----
#   ★ 这几个是**二进制**，看不出内容，只能确认「在不在」。
#     漏了的表现：手机上加到桌面是个白图标、名字显示成网址，
#     而在电脑上完全看不出来（电脑上没人加桌面）。
print('\n--- 添加到桌面的图标 / manifest ---')
# ★ Windows 上 TOC 的键用的是反斜杠（'static\\icon-192.png'），
#   直接拿 'static/icon-192.png' 去查会查不到 —— 明明打进去了却报「不在」。
#   两种分隔符都试一遍。
for name in ('static/manifest.webmanifest', 'static/icon-192.png',
             'static/icon-512.png', 'static/apple-touch-icon.png'):
    ok = (arc.toc.get(name) is not None
          or arc.toc.get(name.replace('/', '\\')) is not None)
    if not ok:
        BAD.append('%s 没打进去' % name)
    print('  %-32s %s' % (name, '✅ 在' if ok else '❌ 不在（check spec 的 datas）'))

# ---- ② Python 模块在不在（在 PYZ 里，内容解不出来，只能看清单）----
print('\n--- 纯 Python 模块 ---')
s = io.open('build/TG客服多开版/Analysis-00.toc', encoding='utf-8',
            errors='replace').read()
mods = sorted(set(re.findall(r"\('([A-Za-z_][A-Za-z0-9_.]*)',", s)))
print('  模块总数：%d' % len(mods))
for w in ['archive', 'login_log', 'core', 'panel', 'manager', 'merchants',
          # 共享工具（usdt 和 shop 都在用）
          'tron',
          # 各机器人类型（一个类型一个文件夹）
          'runners.kefu', 'runners.usdt', 'runners.shop',
          'runners.shop.store', 'runners.shop.pay', 'runners.shop.providers',
          'runners.ledger', 'runners.ledger.storage', 'runners.ledger.trc20',
          'runners.ledger.group_admin', 'runners.ledger.positions']:
    ok = w in mods
    if not ok:
        BAD.append('模块 %s 没打进去' % w)
    print('    %-20s %s' % (w, '✅ 在' if ok else '❌ 不在'))

# ---- ③ ★ 最实在的一条：exe 得比所有源码新 ----
#    前两条只能证明「打包时看到的是新代码」，这条能证明「打完之后没再改过」
print('\n--- exe 时间戳 vs 源码 ---')
newest, newest_f = 0, ''
for pat in ('*.py', '*.html'):
    for p in glob.glob(pat) + glob.glob('ledger/' + pat):
        if os.path.basename(p).startswith('_'):
            continue                      # 测试/调试脚本不算
        m = os.path.getmtime(p)
        if m > newest:
            newest, newest_f = m, p
import datetime
print('  最新源码：%s  %s' % (newest_f,
                            datetime.datetime.fromtimestamp(newest)))
print('  exe     ：%s' % datetime.datetime.fromtimestamp(
    os.path.getmtime(exe)))
if os.path.getmtime(exe) < newest:
    BAD.append('exe 比 %s 旧 —— 是旧版，得重新打包' % newest_f)
    print('  ❌ exe 比源码旧，别用')
else:
    print('  ✅ exe 比所有源码都新')

print()
if BAD:
    print('★ 有问题：')
    for x in BAD:
        print('   ❌ %s' % x)
    sys.exit(1)
print('★ 该带的都带上了，页面和代码都是最新的')
