# -*- coding: utf-8 -*-
"""把「记账独立版」打成一个 zip —— 命令行（`_build_solo.py`）和面板都用它

★★ 为什么清单只写一份：
   面板上那个「⬇️ 客户安装包」按钮要**现打**一个 zip 给用户下载
   （里面 config.json 已经填好了，用户不用粘任何东西）。
   它打的包必须跟命令行的**一模一样** —— 两边各写一份清单迟早会对不上，
   而漏一个文件的表现是「客户那边装不上」，最难查。

客户版包含当前记账代码、小程序配置接口和客户运行入口。
安装包不包含服务商管理面板、商城或其他机器人的配置和数据。
"""
import io
import copy
import json
import os
import zipfile

import core

# ---- 从仓库搬进客户版的 ----
ROOT_FILES = ['core.py', 'customer_config.py', 'customer_ui.py', 'customer_images.py',
              'miniapp.py', 'miniapp_page.html']
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
httpx>=0.27             # 欧易报价、号码归属地和贵金属查询
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
· 账本保存在客户服务器；小程序经服务商域名，按你的权限读取和修改本机配置

【小程序配置中心】
私聊机器人，打开「配置中心」，即可管理群组、人员、账单、自动回复、定时广告和查询密钥。
客户服务器只需能访问 Telegram 和服务商 HTTPS 域名，无需另备域名或开放入站端口。
请先在服务商面板切换为「客户自建」，再启动客户服务器上的机器人，避免两处同时运行。
安装包包含下载时的功能版本；后续更新请重新获取安装包，保留 config.json 和 data/。
已有账本不会自动迁移到新服务器。
'''


def src_root():
    """源码在哪。

    ★ 走 core.resource_path：打包成 exe 之后，这些文件会被 PyInstaller
      解到临时目录，不能拿普通相对路径去找。
    """
    return core.resource_path('')


def _source(rel, bot=None):
    root = src_root()
    if bot and bot.get('instance_folder') and (rel in ROOT_FILES + PKG_FILES or rel.startswith('runners/ledger/')):
        from bot_instances import folder
        root = str(folder(bot))
    return os.path.join(root, rel.replace('/', os.sep))


def _read(rel, bot=None):
    p = _source(rel, bot)
    with io.open(p, 'r', encoding='utf-8') as f:
        return f.read()


def ledger_files(bot=None):
    """runners/ledger/ 下该带走的文件名"""
    d = _source('runners/ledger/', bot)
    return sorted(fn for fn in os.listdir(d)
                  if fn.endswith('.py') and fn not in LEDGER_SKIP)


# ★ 归属地查询的**离线数据**（2026-10-08 加的）——
#   不带的话客户版查手机号会回「数据文件缺失」，等于半残。
#   ★ 是二进制，得用 z.write() 直接写，不能像别的文件那样读成文本
LEDGER_ASSETS = ['phone.dat', 'idcode.txt']


def client_config(bid, token, url, note='', bind_code='', *, bot=None, settings=None):
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
        'miniapp_base_url': (settings or {}).get('miniapp_base_url') or str(url or '').rstrip('/'),
        'report': {
            'url': str(url or '').rstrip('/'),
            'enabled': True,
        },
    }
    for key in ('welcome', 'tron_api_keys', 'miniapp_footer_text'):
        if key in (settings or {}):
            cfg[key] = copy.deepcopy(settings[key])
    for key in ('username', 'owner_id', 'admin_ids', 'admin_names', 'admin_name', 'admin_username', 'ledger'):
        if key in (bot or {}):
            cfg[key] = copy.deepcopy(bot[key])
    return json.dumps(cfg, ensure_ascii=False, indent=2) + '\n'


def missing_sources(bot=None):
    """哪些源文件找不到（打包成 exe 时可能没带进来）"""
    out = []
    for rel in ROOT_FILES + PKG_FILES + [u'solo/' + f for f in SOLO_FILES] + [DOC]:
        if not os.path.exists(_source(rel, bot)):
            out.append(rel)
    try:
        ledger_files(bot)
    except OSError:
        out.append('runners/ledger/')
    for fn in LEDGER_ASSETS:
        if not os.path.exists(_source('runners/ledger/assets/' + fn, bot)):
            out.append('runners/ledger/assets/' + fn)
    return out


def make_zip(out, cfg_json, config_name='config.json', *, bot=None):
    """把客户版写进 `out`（一个文件对象或路径）。

    cfg_json —— 客户那边 config.json 的**内容**（用 client_config() 生成）。
    ★ 同时保留一份 config.example.json（给人看的样板）。
    """
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        base = u'记账独立版/'

        def put(arcname, text):
            z.writestr(base + arcname, text)

        for rel in ROOT_FILES + PKG_FILES:
            put(rel, _read(rel, bot))
        for fn in ledger_files(bot):
            put('runners/ledger/' + fn, _read('runners/ledger/' + fn, bot))
        # ★ 离线数据是**二进制**：要 z.write()（put() 走的是文本）
        for fn in LEDGER_ASSETS:
            src = _source('runners/ledger/assets/' + fn, bot)
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
        if bot:
            from customer_images import image_path, ad_path, LOCAL_PHOTO
            ledger = bot.get('ledger') or {}
            images = [image_path(bot)] if ledger.get('newbie_welcome_photo') == LOCAL_PHOTO else []
            images.extend(ad_path(bot, rule['image']) for rule in (ledger.get('customer') or {}).get('ads', []) if rule.get('image'))
            for path in set(images):
                if path.is_file() and not path.is_symlink():
                    z.write(path, base + 'data/' + path.name)
    return out
