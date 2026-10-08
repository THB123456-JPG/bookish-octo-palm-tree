# -*- coding: utf-8 -*-
"""打一个「记账独立版」给客户 —— 从**这一份**代码生成，不手工维护第二份

    python _build_solo.py                        # 生成一个空的样板包（config 是占位符）
    python _build_solo.py --id X --token Y --url Z   # ★ 生成**填好的**，客户解压即用
    python _build_solo.py --no-zip               # 只生成目录，不打 zip

★★ 为什么用脚本生成、而不是另开一个仓库：
   分出去就是两份代码了，以后改记账功能要**改两遍**，漏一遍那个客户就是坏的。
   这里拿一份源码生成客户版，**记账代码仍然只有一份**。

★ 生成出来的东西在 `_solo_build/` 下，那份**不要直接改** —— 改了下次生成就被覆盖。
  要改就改仓库里的，重新跑这个脚本。

★ 每次生成都会**真的跑一遍自检**（import + 构造 runner + 确认不存消息 +
  转发钩子不拦记账），没通过就不出包。客户那边装不上比打不出包麻烦得多。

★ 文件清单在 `solo_pack.py` —— 面板上那个「⬇️ 客户安装包」按钮用的是**同一份**，
  别在这儿另写一份。
"""
import argparse
import io
import os
import shutil
import subprocess
import sys
import zipfile

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import solo_pack                                            # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_ROOT = os.path.join(os.path.dirname(HERE), '_solo_build')
OUT = os.path.join(OUT_ROOT, '记账独立版')


def say(msg=''):
    print(msg)


def build(cfg_json):
    if os.path.exists(OUT):
        shutil.rmtree(OUT)
    os.makedirs(OUT_ROOT, exist_ok=True)

    buf = io.BytesIO()
    solo_pack.make_zip(buf, cfg_json)
    buf.seek(0)
    # ★ 先打进内存再用它解出目录 —— 保证「zip 里的」和「自检跑的」是同一份
    with zipfile.ZipFile(buf) as z:
        names = z.namelist()
        z.extractall(OUT_ROOT)
    say('打包内容 %d 个文件' % len(names))
    say('解到 %s' % OUT)
    return len(names)


def verify():
    """★ 真的跑一遍 —— 打不出包事小，客户那边装不上事大"""
    say()
    say('--- 自检 ---')
    check = r'''
import io, json, os, sys, tempfile
sys.stdout.reconfigure(encoding="utf-8")
tmp = tempfile.mkdtemp()
import core
core.BASE_DIR = tmp
core.DATA_DIR = os.path.join(tmp, "data")
core.LOG_FILE = os.path.join(tmp, "log.txt")
os.makedirs(core.DATA_DIR, exist_ok=True)

# ① 记账包和转发模块都要能 import
import relay
from runners.ledger import LedgerRunner
from runners.ledger import commands, storage, group_admin, bill_messages
print("  模块导入 OK")

# ② 入口那两个函数要能跑
sys.path.insert(0, os.getcwd())
import importlib.util
spec = importlib.util.spec_from_file_location("solo_main", "独立版.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
print("  入口可加载 OK")

# ③ ★ 直接拿包里那份 config.json 来跑 —— 它要是填好了，客户解压就能用
cfg = json.load(io.open("config.json", encoding="utf-8"))
filled = bool(cfg.get("id")) and bool(cfg.get("token")) \
         and ":" in str(cfg.get("token"))
# 样板包（占位符）本来就没填，用一份假的接着把剩下的检查跑完
use = cfg if filled else {"id": "selfcheck", "token": "1:selfcheck"}
bot, state = m.load_bot(use)
# ★ 配置里带了绑定码就必须用它（跟面板显示的是同一个）；
#   不带才自己生成。反了的话客户照面板上的码绑定会被拒。
if use.get("bind_code"):
    assert bot["bind_code"] == use["bind_code"], "配置里的绑定码没生效！"
    print("  绑定码用的是配置里带的 OK")
else:
    assert bot["bind_code"], "没带绑定码时应该自己生成一个"
    print("  配置没带绑定码 → 自己生成了一个 OK")
assert bot["type"] == "ledger", bot["type"]
assert bot["archive"] == {"enabled": False}, bot["archive"]
mgr = m.MiniMgr(state)
mgr.bot = bot
r = LedgerRunner(mgr, bot)
assert hasattr(mgr, "lock") and mgr.lock is not None
print("  MiniMgr / 构造 runner OK")
print("  包里的 config.json：%s" % ("已填好，客户解压即用" if filled else "是样板（占位符）"))

# ④ ★ 确认真的不存消息（不显式关掉的话默认是开的）
assert r.archive_cfg().get("enabled") is False, r.archive_cfg()
assert r._archive is None, "不存消息的机器人在启动前不该开归档库"
print("  不存消息 OK（archive 已关）")

# ④b ★★ 入口是**模块级**调用 relay 的这几个函数 —— 它们必须真的在
#     模块级（不能只是 Reporter 类的方法）。少一层转发就会在启动那一行
#     AttributeError，整个机器人起不来，而且表现在 systemd 上就是
#     「崩溃→重启」循环，日志要翻到底才看得到。
#     （2026-09-29 给客户装完才发现）
for _fn in ("setup", "attach", "on_message"):
    assert callable(getattr(relay, _fn, None)),         "relay 模块级缺 %s() —— 入口调它就会 AttributeError" % _fn
print("  relay 的模块级函数齐全 OK（setup / attach / on_message）")

# ⑤ 转发钩子：只能塞队列，绝不能拦住记账
relay.setup({"enabled": False}, bot["id"], bot["token"])
assert relay.on_message(r, {"message": {"text": "x"}}) is None, \
    "on_message 必须返回 None，返回真值会把消息从记账那边吃掉！"
print("  转发钩子 OK（返回 None，不拦记账）")

# ⑥ ★★ 机器人**自己发的**消息（账单）也必须能转发
#    2026-09-29 客户那边就是缺这一步：群里 26 条消息、机器人自己发的 0 条,
#    账单一条都没传过来。getUpdates 不返回自己发的消息，只能挂 on_sent。
#    这条断言就是不让那个 bug 再溜进包里。
class _FakeAPI:
    def __init__(self):
        self.on_sent = self._core

    def _core(self, method, params, result):
        self.core_called = True


class _FakeRunner:
    def __init__(self):
        self.api = _FakeAPI()
        self.bot = {"admin_ids": [], "owner_id": 0}


#  ★ 直接造一个 Reporter 塞进全局，**不 start()** ——
#    纯离线：只验「有没有进队列」，不发任何网络请求。
#    （不能用 relay.setup()，那个会起后台线程去连地址）
relay._reporter = relay.Reporter(
    {"url": "http://127.0.0.1:1", "enabled": True}, bot["id"], bot["token"])
_fr = _FakeRunner()
relay.attach(_fr)
assert _fr.api.on_sent is not _fr.api._core, "attach 没钩上 api.on_sent"
_fr.api.on_sent("sendMessage", {"parse_mode": "HTML"},
                {"message_id": 1, "date": __import__("time").time(),
                 "chat": {"id": -1, "type": "supergroup", "title": "T"},
                 "from": {"id": 5, "is_bot": True}, "text": "<b>今日账单</b>"})
assert getattr(_fr.api, "core_called", False), "把 core 自己的 on_sent 顶掉了"
assert relay.stats().get("queued") == 1, \
    "机器人自己发的消息没进转发队列（账单会传不过去！）"
relay._reporter.stop()
relay._reporter = None
print("  机器人自己发的（账单）也走转发 OK")

sys.exit(0 if filled else 3)
'''
    p = subprocess.run([sys.executable, '-c', check], cwd=OUT,
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace')
    say(p.stdout or '')
    if p.returncode not in (0, 3):
        say(p.stderr or '')
        raise SystemExit('!! 自检没过，不出包')
    say('自检通过 ✓')
    return p.returncode == 0


def zipit():
    zp = os.path.join(OUT_ROOT, '记账独立版.zip')
    if os.path.exists(zp):
        os.remove(zp)
    with zipfile.ZipFile(zp, 'w', zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(OUT):
            dirs[:] = [d for d in dirs if d != '__pycache__']
            for fn in files:
                if fn.endswith('.pyc'):
                    continue
                full = os.path.join(root, fn)
                z.write(full, os.path.relpath(full, OUT_ROOT))
    say()
    say('打包完成：%s（%.0f KB）' % (zp, os.path.getsize(zp) / 1024.0))
    say('★ 客户那边：解压 → sudo bash install.sh')
    return zp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--no-zip', action='store_true')
    ap.add_argument('--id', default='', help='机器人 id（面板上那个）')
    ap.add_argument('--token', default='', help='机器人 token')
    ap.add_argument('--url', default='', help='你的面板地址，比如 http://1.2.3.4:8080')
    ap.add_argument('--bindcode', default='',
                    help='★ 面板上那个绑定码。**一定要带** —— 不带的话'
                         '客户那边会自己生成一个，跟面板显示的对不上，'
                         '客户照面板上的码绑定会被拒')
    a = ap.parse_args()

    say('=' * 56)
    say(' 生成「记账独立版」（给客户自带服务器用的）')
    say('=' * 56)

    miss = solo_pack.missing_sources()
    if miss:
        raise SystemExit('!! 找不到这些源文件：%s' % ', '.join(miss))

    if a.id and a.token and a.url:
        cfg_json = solo_pack.client_config(a.id, a.token, a.url, '',
                                           a.bindcode)
        say('★ 生成的是**填好的**包（客户解压即用，不用填任何东西）')
    else:
        cfg_json = solo_pack.client_config('', '', '')
        say('提示：不带 --id/--token/--url 的话，config.json 是样板，')
        say('      客户得自己填。要生成填好的包：')
        say('      python _build_solo.py --id X --token Y --url Z')

    build(cfg_json)
    filled = verify()
    if not a.no_zip:
        zipit()
    else:
        # ★ --no-zip 只重生成**目录**，zip 还是上一次的 ——
        #   不提醒的话很容易把旧包当新包发出去（2026-09-29 我自己就踩了）
        stale = os.path.join(OUT_ROOT, '记账独立版.zip')
        if os.path.exists(stale):
            say()
            say('⚠️ 注意：--no-zip 不会重打 zip，%s' % stale)
            say('   还是**上一次**的内容，别拿去发给客户。')
    say()
    say('（自检时那份 config.json %s）'
        % ('是填好的' if filled else '是样板'))


if __name__ == '__main__':
    main()
