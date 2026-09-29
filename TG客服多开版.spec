# -*- mode: python ; coding: utf-8 -*-
import certifi

datas = [
    # CA 证书：不加的话 exe 里 requests 走 HTTPS 会校验失败
    (certifi.where(), 'certifi'),
    # 前端页面：拆成独立文件，方便单独改
    ('panel_page.html', '.'),
    # 群消息记录页（独立页面，只有管理员进得去）
    ('messages_page.html', '.'),
    # 「添加到桌面」的图标 + manifest（panel.py 的 STATIC_FILES 读它们）
    # ★ 漏了这行的表现：手机上加到桌面是个白图标、名字是网址
    ('static', 'static'),
    # ★★ 「客户安装包」的原始素材 —— 面板上那个「⬇️ 安装包」按钮
    #    要**现打**一个 zip 给用户下载（solo_pack.py 读这些文件）。
    #    ★ 打包成 exe 之后源码在 PYZ 里解不出来，必须以 data 的形式
    #      再放一份，不然那个按钮会报「缺少源文件」。
    ('core.py', '.'),
    ('runners/__init__.py', 'runners'),
    ('runners/ledger', 'runners/ledger'),
    ('solo', 'solo'),
    ('记账机器人_客户使用说明.txt', '.'),
]

excludes = ['PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'gi', 'cefpython3',
            'torch', 'torchvision', 'scipy', 'pandas', 'matplotlib',
            'tensorflow', 'tensorboard', 'sklearn', 'sympy',
            'rapidocr', 'rapidocr_onnxruntime', 'onnxruntime', 'cv2', 'shapely']

a = Analysis(
    ['主程序.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=['core', 'manager', 'panel', 'merchants',
                   # 「客户安装包」的打包逻辑（panel.py 里是懒加载 import，
                   # 静态分析不一定扫得到，写死保险）
                   'solo_pack',
                   # 共享工具：链上查询 / 地址编解码 / 汇率
                   # （USDT 助手和商城都在用，所以放根目录，不属于任何类型）
                   'tron',
                   # ---- 各机器人类型：一个类型一个文件夹 ----
                   'runners',
                   'runners.kefu', 'runners.kefu.runner',
                   'runners.usdt', 'runners.usdt.runner',
                   'runners.shop', 'runners.shop.runner', 'runners.shop.api',
                   'runners.shop.store', 'runners.shop.pay',
                   'runners.shop.providers',
                   # 记账机器人的接线层 + 它搬过来的那些模块
                   'runners.ledger', 'runners.ledger.runner',
                   'runners.ledger.api',
                   'runners.ledger.storage', 'runners.ledger.commands',
                   'runners.ledger.calculator', 'runners.ledger.text_utils',
                   'runners.ledger.identity', 'runners.ledger.price',
                   'runners.ledger.trc20', 'runners.ledger.bill_messages',
                   'runners.ledger.reminders',
                   # 群管理：批量清理 / @全体 / 广播 / 群消息位置
                   'runners.ledger.group_admin', 'runners.ledger.positions',
                   # 登录日志（记 IP + 查归属地）
                   'login_log',
                   # 群消息记录（在 core.get_archive 里是懒加载 import，
                   # 静态分析不一定扫得到，写死保险）
                   'archive',
                   # 谷歌验证码（TOTP，纯标准库实现，不引第三方库）
                   'totp'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='TG客服多开版',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
