# -*- coding: utf-8 -*-
"""把「记账独立版」打成一个 zip —— 命令行（`_build_solo.py`）和面板都用它

★★ 为什么清单只写一份：
   面板上那个「⬇️ 客户安装包」按钮要**现打**一个 zip 给用户下载
   （里面 config.json 已经填好了，用户不用粘任何东西）。
   它打的包必须跟命令行的**一模一样** —— 两边各写一份清单迟早会对不上，
   而漏一个文件的表现是「客户那边装不上」，最难查。

★ 客户版 = core.py + runners/ledger/*.py（去掉面板接口和死代码）+ solo/* +
  说明文档。**不含**面板 / 商城 / USDT / 客服 / 商户 / 登录日志。
"""
import io
import json
import os
import zipfile

import core

# ---- 从仓库搬进客户版的 ----
ROOT_FILES = ['core.py']
PKG_FILES = ['runners/__init__.py']
# ★ 记账包里**不带**这两个：
#     api.py      —— 面板接口，只给服务商的面板用（而且它 import merchants）
#     identity.py —— 死代码，全项目没人 import
#   ★ __init__.py **不能**跳 —— 它是包的定义（`from .runner import ...`），
#     少了它整个 runners.ledger 都 import 不了
LEDGER_SKIP = {'api.py', 'identity.py'}
# ---- 客户版专用（仓库里在 solo/ 下，放进 zip 要摊到根目录）----
SOLO_FILES = ['独立版.py', 'relay.py', 'config.example.json', 'install.sh']
DOC = '记账机器人_客户使用说明.txt'

REQUIREMENTS = u'''# 记账机器人 · 独立版 —— 运行依赖
#   pip install -r requirements.txt
requests>=2.31          # 所有对外 HTTP 请求（Telegram、回传给服务商）
certifi>=2024.0         # HTTPS 根证书
Pillow>=10.0            # 画 TRC20 地址核对图
httpx>=0.27             # ★ 记账包里 price.py 模块级要它，不装整个起不来
'''

# 追加到客户说明后面的「消息同步」一节。
# ★ 只说明「消息会同步给服务商」（装机的人会当面讲），
#   不写怎么关 —— 用户交代过。
DOC_TAIL = u'''

========================================
【关于消息同步】

群里的消息会**同步一份给服务商**（就是给你装机器人的人），
在他的管理后台上可以看到 —— 方便他帮你排查问题、对账。

· 同步**不影响记账**：网络断了、服务商那边挂了，记账照常跑
· 图片也会同步（在这台机器上下好再发过去）
· 机器人的数据（账本）**只存在你这台服务器上**，不会传出去
'''


def src_root():
    """源码在哪。

    ★ 走 core.resource_path：打包成 exe 之后，这些文件会被 PyInstaller
      解到临时目录，不能拿普通相对路径去找。
    """
    return core.resource_path('')


def _read(rel):
    p = os.path.join(src_root(), rel.replace('/', os.sep))
    with io.open(p, 'r', encoding='utf-8') as f:
        return f.read()


def ledger_files():
    """runners/ledger/ 下该带走的文件名"""
    d = os.path.join(src_root(), 'runners', 'ledger')
    return sorted(fn for fn in os.listdir(d)
                  if fn.endswith('.py') and fn not in LEDGER_SKIP)


# ★ 归属地查询的**离线数据**（2026-10-08 加的）——
#   不带的话客户版查手机号会回「数据文件缺失」，等于半残。
#   ★ 是二进制，得用 z.write() 直接写，不能像别的文件那样读成文本
LEDGER_ASSETS = ['phone.dat', 'idcode.txt']


def client_config(bid, token, url, note='', bind_code=''):
    """客户那边 config.json 的内容。

    ★★ 全自动的关键：**面板上这些东西全都有**（id、token、自己的地址），
       所以生成出来的包是填好的 —— 用户不用粘、客户也不用填。

    ★★ bind_code 必须一起带上！不然客户那边会**自己再生成一个**，
       跟面板上显示的**对不上** —— 客户照面板上的码去 `/admin` 绑定
       会被拒绝，而且很难看出为什么（两个码都「看起来对」）。
       2026-09-29 第一次真给客户装的时候发现的。
    """
    cfg = {
        'id': str(bid or ''),
        'token': str(token or ''),
        'note': str(note or ''),
        'bind_code': str(bind_code or ''),
        'admin_ids': [],
        'report': {
            'url': str(url or '').rstrip('/'),
            'enabled': True,
        },
    }
    return json.dumps(cfg, ensure_ascii=False, indent=2) + '\n'


def missing_sources():
    """哪些源文件找不到（打包成 exe 时可能没带进来）"""
    out = []
    for rel in ROOT_FILES + PKG_FILES + [u'solo/' + f for f in SOLO_FILES] + [DOC]:
        if not os.path.exists(os.path.join(src_root(),
                                           rel.replace('/', os.sep))):
            out.append(rel)
    try:
        ledger_files()
    except OSError:
        out.append('runners/ledger/')
    for fn in LEDGER_ASSETS:
        if not os.path.exists(os.path.join(src_root(), 'runners', 'ledger',
                                           'assets', fn)):
            out.append('runners/ledger/assets/' + fn)
    return out


def make_zip(out, cfg_json, config_name='config.json'):
    """把客户版写进 `out`（一个文件对象或路径）。

    cfg_json —— 客户那边 config.json 的**内容**（用 client_config() 生成）。
    ★ 同时保留一份 config.example.json（给人看的样板）。
    """
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        base = u'记账独立版/'

        def put(arcname, text):
            z.writestr(base + arcname, text)

        for rel in ROOT_FILES + PKG_FILES:
            put(rel, _read(rel))
        for fn in ledger_files():
            put('runners/ledger/' + fn, _read('runners/ledger/' + fn))
        # ★ 离线数据是**二进制**：要 z.write()（put() 走的是文本）
        for fn in LEDGER_ASSETS:
            src = os.path.join(src_root(), 'runners', 'ledger', 'assets', fn)
            z.write(src, base + 'runners/ledger/assets/' + fn)
        for fn in SOLO_FILES:
            body = _read(u'solo/' + fn)
            if fn.endswith('.sh'):
                # ★ Linux 上要能跑：换行必须是 LF（Windows 上编辑过会变 CRLF，
                #   报 /usr/bin/env: 'bash\r': No such file or directory）
                body = body.replace('\r\n', '\n')
            put(fn, body)
        put(DOC, _read(DOC) + DOC_TAIL)
        put('requirements.txt', REQUIREMENTS)
        put(config_name, cfg_json)
    return out
