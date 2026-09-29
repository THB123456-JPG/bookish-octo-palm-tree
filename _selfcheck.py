# -*- coding: utf-8 -*-
"""部署自检：换新服务器之后跑一遍，确认**功能一个都不缺**

    python3 _selfcheck.py              # 在服务器上跑
    python3 _selfcheck.py --url http://127.0.0.1:8080   # 顺带验面板能打开

★ 为什么要有这个：换服务器时最容易出的不是「起不来」，而是
  **「起来了但某个功能悄悄瘸了」** —— 少装一个包（比如 Pillow）、
  少传一个文件（panel_page.html），表现都是「点那个功能没反应」，
  而进程活得好好的、日志也干干净净。等客户用到才发现就晚了。

★ 它只读不写：不建库、不改配置、不连 Telegram。
  唯一的外部动作是 `--url` 时发一个 HTTP 请求。
"""
import argparse
import importlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)
sys.stdout.reconfigure(encoding='utf-8')

OK, BAD = [], []


def check(name, cond, extra=''):
    (OK if cond else BAD).append(name)
    print('  %s %s%s' % ('✅' if cond else '❌', name,
                         ('  → %s' % (extra,)) if extra else ''))


def section(title):
    print()
    print('=' * 60)
    print(' ' + title)
    print('=' * 60)


# ================= ① 环境 =================
section('① 运行环境')
v = sys.version_info
check('Python 3.10 以上（3.9 跑不了）', v >= (3, 10),
      '%d.%d.%d（%s）' % (v[0], v[1], v[2], sys.executable))
check('能写工作目录', os.access(HERE, os.W_OK), HERE)

# ================= ② 第三方依赖 =================
# ★ 每一条都对着一个**真功能**，别删
section('② 依赖（缺哪个，哪个功能就废）')
DEPS = [
    ('requests', '所有网络请求（Telegram、转发回传）'),
    ('certifi', 'HTTPS 证书（不带的话 HTTPS 全失败）'),
    ('PIL', 'Pillow —— TRC20 防篡改核对图'),
    ('httpx', '记账的「币价 / 设置实时汇率」'),
]
for mod, why in DEPS:
    try:
        m = importlib.import_module(mod)
        check('%-10s %s' % (mod, why), True,
              getattr(m, '__version__', '') or '')
    except Exception as e:
        check('%-10s %s' % (mod, why), False, '%s: %s' % (type(e).__name__, e))

# ================= ③ 核心模块 =================
section('③ 核心模块能导入')
MODULES = [
    ('core', '底层（机器人循环、归档、日志）'),
    ('manager', '多机器人管理'),
    ('panel', '面板服务 + 所有接口'),
    ('archive', '群消息记录'),
    ('merchants', '商户后台'),
    ('login_log', '登录记录（时间/IP/城市/设备）'),
    ('totp', '谷歌验证码'),
    ('tron', 'TRON 客户端（USDT 助手用）'),
    ('solo_pack', '一键下载「客户安装包」'),
    ('runners.ledger', '记账机器人'),
    ('runners.kefu', '客服机器人'),
    ('runners.usdt', 'USDT 助手'),
    ('runners.shop', '商城机器人'),
    ('runners.ledger.tron_chain', '查地址（链上查询）'),
    ('runners.ledger.tron_watch', '查地址（监听 + 地址簿）'),
    ('runners.ledger.trc20', 'TRC20 防篡改图'),
    ('runners.ledger.price', '币价 / 设置实时汇率'),
    ('runners.ledger.group_admin', '群管理（@全体 / 清理消息）'),
]
for mod, why in MODULES:
    try:
        importlib.import_module(mod)
        check('%-28s %s' % (mod, why), True)
    except Exception as e:
        check('%-28s %s' % (mod, why), False,
              '%s: %s' % (type(e).__name__, e))

# ================= ④ 页面文件 =================
section('④ 页面文件在不在')
for fn, why in (('panel_page.html', '面板全部界面（不带的话打开是空白）'),
                ('messages_page.html', '手机版消息记录页'),
                ('记账机器人_客户使用说明.txt', '客户说明书')):
    p = os.path.join(HERE, fn)
    check('%-28s %s' % (fn, why), os.path.exists(p),
          '%d 字节' % os.path.getsize(p) if os.path.exists(p) else '找不到')

# ================= ⑤ 四种机器人都能建起来 =================
section('⑤ 四种机器人都能构造（接线没断）')


class _FakeMgr:
    def save(self):
        pass

    def is_expired(self, b):
        return False


TYPES = [('runners.ledger', 'LedgerRunner', '记账'),
         ('runners.kefu', 'KefuRunner', '客服'),
         ('runners.usdt', 'UsdtRunner', 'USDT 助手'),
         ('runners.shop', 'ShopRunner', '商城')]
for mod, cls, label in TYPES:
    try:
        m = importlib.import_module(mod)
        k = getattr(m, cls)
        bot = {'id': 'selfcheck', 'token': '1:FAKE', 'note': '自检',
               'type': mod.split('.')[-1], 'enabled': True,
               'admin_ids': [], 'owner_id': ''}
        r = k(_FakeMgr(), bot)
        check('%-10s（%s）' % (label, cls), True, 'allow_groups=%s'
              % getattr(r, 'allow_groups', '?'))
        try:
            r.close_db()
        except Exception:
            pass
    except Exception as e:
        check('%-10s（%s）' % (label, cls), False,
              '%s: %s' % (type(e).__name__, e))

# ================= ⑥ 关键功能真的能跑 =================
section('⑥ 关键功能（不是「能导入」而是「能干活」）')
try:
    import io
    from runners.ledger import trc20
    img = trc20.make_trc20_verify_image(
        'TNXoiAJ3dct8Fjg4M9fkLFh9S2v9TXc32G')
    check('防篡改核对图能画出来（Pillow + 字体）',
          img is not None and len(img.getvalue()) > 1000,
          '%d 字节' % len(img.getvalue()))
except Exception as e:
    check('防篡改核对图能画出来（Pillow + 字体）', False,
          '%s: %s' % (type(e).__name__, e))

try:
    from runners.ledger import tron_chain as tc
    ok = tc.address_valid('TNXoiAJ3dct8Fjg4M9fkLFh9S2v9TXc32G')
    no = tc.address_valid('TNXoiAJ3dct8Fjg4M9fkLFh9S2v9TXc32X')
    check('TRON 地址校验（带校验和）', ok and not no,
          '真地址过=%s 改一位拒=%s' % (ok, not no))
    check('USDT 合约常量没被脱敏改掉（改了就永远查不到余额）',
          tc.USDT == 'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t', tc.USDT)
except Exception as e:
    check('TRON 地址校验', False, '%s: %s' % (type(e).__name__, e))

try:
    from runners.ledger import price
    check('币价命令认得出（币价 / bj / z0）',
          price.is_price_command('币价') and price.is_price_command('bj')
          and price.is_price_command('Z0'))
except Exception as e:
    check('币价命令认得出', False, '%s: %s' % (type(e).__name__, e))

try:
    from runners.ledger import commands as C
    check('记账帮助里有「查U · 监听」那节', '查U' in C.HELP_TEXT)
    check('「/我」命令认得出',
          C.is_mine_command('/我') and not C.is_mine_command('我'))
except Exception as e:
    check('记账帮助', False, '%s: %s' % (type(e).__name__, e))

try:
    import solo_pack
    files = solo_pack.missing_sources()
    check('「客户安装包」的源文件齐全（缺了客户那边装不上）',
          not files, str(files)[:80] if files else '')
except Exception as e:
    check('「客户安装包」的源文件齐全', False, '%s: %s' % (type(e).__name__, e))

# ================= ⑦ 配置 =================
section('⑦ 配置')
try:
    cfg = json.load(open(os.path.join(HERE, 'config.json'), encoding='utf-8'))
    # ★ 本机开发时 host=127.0.0.1 是**对的**，只有服务器上才必须是 0.0.0.0
    #   （写 127.0.0.1 的话外面连不上，而且不报任何错）
    if sys.platform == 'win32':
        check('host（本机开发，%s 是对的）' % cfg.get('host'), True,
              '换到服务器上必须是 0.0.0.0')
    else:
        check('监听 0.0.0.0（写 127.0.0.1 的话外面连不上）',
              cfg.get('host') == '0.0.0.0', str(cfg.get('host')))
    check('面板密码已设置（空的登不进去）', bool(cfg.get('password')),
          '长度 %d' % len(cfg.get('password') or ''))
    check('签名密钥已设置（空的商户登录态可伪造）',
          bool((cfg.get('token_secret') or '').strip()))
    check('端口', isinstance(cfg.get('port'), int), str(cfg.get('port')))
except Exception as e:
    check('config.json 能读', False, '%s: %s' % (type(e).__name__, e))

for fn, key, fields, label in (
        ('bots.json', 'bots', ('token',), '机器人'),
        ('merchants.json', 'merchants', ('user', 'hash', 'salt'), '商户')):
    p = os.path.join(HERE, fn)
    if not os.path.exists(p):
        continue
    try:
        d = json.load(open(p, encoding='utf-8'))
        items = d.get(key) or []
        # ★ 判据要分类型：机器人看 token，商户看登录名/哈希/盐
        #   （商户**没有** token 字段，拿 token 去判会把正常的商户全标成坏的）
        bad = [x for x in items
               if any((not str(x.get(f) or '').strip()
                       or '换成你自己' in str(x.get(f) or ''))
                      for f in fields)]
        check('%s 里没有连不上的空壳%s' % (fn, label), not bad,
              '有 %d 个要删' % len(bad) if bad else '%d 条' % len(items))
    except Exception as e:
        check('%s 能读' % fn, False, '%s: %s' % (type(e).__name__, e))

# ================= ⑧ 面板能不能打开 =================
# ★ `--url` 不带值 = 按 config.json 里的端口自己拼
#   （部署脚本就是这么调的，不用它去猜端口）
ARGS = argparse.ArgumentParser(add_help=True)
ARGS.add_argument('--url', nargs='?', const='auto', default='',
                  help='顺带验面板能不能打开；不带值按 config.json 的端口')
ARGS = ARGS.parse_args()

_url = ARGS.url
if _url == 'auto':
    try:
        _cfg = json.load(open(os.path.join(HERE, 'config.json'),
                              encoding='utf-8'))
        _url = 'http://127.0.0.1:%s' % _cfg.get('port', 8080)
    except Exception:
        _url = 'http://127.0.0.1:8080'

if _url:
    section('⑧ 面板能不能打开（%s）' % _url)
    try:
        import requests
        base = _url.rstrip('/')
        r = requests.get(base + '/', timeout=15)
        check('面板首页 200', r.status_code == 200, str(r.status_code))
        check('首页里有界面（不是空白/兜底页）',
              len(r.content) > 50000, '%d 字节' % len(r.content))
        r2 = requests.get(base + '/messages', timeout=15)
        check('手机版消息记录页能打开', r2.status_code == 200,
              str(r2.status_code))
        r3 = requests.get(base + '/api/bots', timeout=15)
        check('接口有鉴权（没密码读不到机器人列表）',
              r3.status_code in (401, 403), str(r3.status_code))
    except Exception as e:
        check('面板能打开', False, '%s: %s' % (type(e).__name__, e))

# ================= 结果 =================
print()
print('=' * 60)
if BAD:
    print(' 结果：%d 项通过，%d 项**失败**' % (len(OK), len(BAD)))
    for b in BAD:
        print('   ❌ %s' % b)
    print()
    print(' ★ 上面这些功能是坏的，别就这么上线。')
else:
    print(' 结果：%d 项全过 ✅  功能没有缺失' % len(OK))
print('=' * 60)
sys.exit(1 if BAD else 0)
