# -*- coding: utf-8 -*-
"""把项目**脱敏后**推送到 GitHub

用法：
    python _push_github.py                     # 只做干净副本 + 安全检查，不推送
    python _push_github.py <仓库地址>            # 再提交并推送
    python _push_github.py <仓库地址> --user 你的GitHub用户名

例：
    python _push_github.py https://github.com/zhangsan/tg-panel.git --user zhangsan

★★ 为什么要「复制一份再推」，而不是就地改：
   就地替换掉敏感值之后，**本地文件就和仓库里的版本不一样了**。
   下次手一抖 `git add .`，真密钥又被提交上去了 —— 这个坑会反复踩。
   复制法：本地一个字节都不动，仓库里干干净净。
   所以本地这个目录**永远不会**变成 git 仓库，也不会有 .git。

★★ 硬性门槛：脚本最后会拿「从原配置里抽出来的真密钥」去扫干净副本，
   只要还剩一个，**就不提交、不推送**。这是不能跳过的一步。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys

sys.stdout.reconfigure(encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))
CLEAN = os.path.join(os.path.dirname(HERE), '_github_upload')

# 三种「一看就是密钥」的格式
PATTERNS = [
    ('机器人token', re.compile(r'\b\d{8,12}:[A-Za-z0-9_-]{30,}\b')),
    ('上游APIKey', re.compile(
        r'\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}'
        r'-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b')),
    ('TRON地址', re.compile(r'\bT[1-9A-HJ-NP-Za-km-z]{33}\b')),
]

# 占位符：故意用中文，这样**不会再匹配上面任何一个格式**（不然验收会误判）
PH = {
    '机器人token': '123456789:AA换成你自己的机器人token',
    '上游APIKey': '你的APIKEY',
    'TRON地址': '你的TRON地址',
}

# ★★★ TRON 地址**不能**用上面那个中文占位符！要用**校验和有效的真地址**。
#
# 为什么（2026-09-29 真踩）：
#   测试文件里大量「拿地址当夹具」——`p.order(ADDR, ...)`、
#   `TronWatch.add(buyer, ADDR)`、`extract_trc20_address(ADDR)`。
#   换成「你的TRON地址」之后，校验位对不上 → **仓库里 8 个测试文件全跑不过**，
#   别人克隆下来一片红。
#
#   这跟当年那起事故是**同一类**：脱敏脚本分不清「真该删的」和
#   「程序要用的」。那次是 USDT 官方合约地址（已进 PUBLIC_CONSTANTS），
#   这次是测试夹具。
#
# ★ 必须**一对一**映射：`_test_ledger_tron.py` 用了 3 个不同地址当夹具，
#   全换成同一个的话测试照样废。
# ★ 下面这些是**自己生成的、没人用的**地址（41 + sha256 派生 + 真校验和），
#   不要在真实业务里使用。
TRON_PLACEHOLDERS = [
    'TBkECbe4SF3skk9RcCb6ko7wc7YiinUGbp',
    'TFMsoNbmzTEU7toDB8LvUhicPHhVQN1KWf',
    'TUaZ5nGofnSFpoYB5DSfqpZBZAAbmuWzcF',
    'TBKw17Fxy5kBbev8MMZ16yVKuQTQfsETta',
    'TCzHwtCrbnbmZLRwSKCTXoVtkqmntLoVcX',
    'TZDbGMYSDEscexLGcqFPHqKUAWJ3w5mPL1',
    'TVRiQ1A8z1UoHvwiK1EBtribcREBMSdY11',
    'TB9tCmCPLuub7Nvr1dVVtNZHwj4p46toy4',
    'TGKUJAc6MbDiXQFu2Y9j1zE6GWTrWXEmyk',
    'TFBA3gz6zCD13F6KFjmMbrmcNyPurZBQFM',
    'TE3ymsbEmUMyrh3c9Mw1nUxCS1T3ZBQpMv',
    'TN44KKvS24Ta9JhCGJXMU2cCC6WnHE38u7',
]


def tron_placeholder_map(patterned):
    """真地址 → 有效的占位地址，**一对一**且顺序稳定（跑多少次都一样）"""
    real = sorted(v for v in patterned
                  if PATTERNS[2][1].fullmatch(v))
    return {v: TRON_PLACEHOLDERS[i % len(TRON_PLACEHOLDERS)]
            for i, v in enumerate(real)}

# 要扫「真值」的原始文件（就是从这些文件里把真密钥抽出来当验收基准）
SECRET_SOURCES = ['bots.json', 'config.json', 'merchants.json', '运行日志.txt']

# ★★ 「长得不像密钥、但一样不能外传」的字段
#    第一版只按正则抓 token/APIKey/TRON地址，结果**面板密码和签名密钥
#    大摇大摆进了副本，验收还报「全过了」** —— 因为密码不匹配那三种格式。
#    教训：验收基准必须覆盖所有要清理的东西，否则等于没验收。
SENSITIVE_FIELDS = {
    # ★★ totp_secret 是 2026-09-29 补的：它**真漏过**（见 scrub_json 里的注释）
    'config.json': ('password', 'token_secret', 'totp_secret'),
    'merchants.json': ('salt', 'hash', 'user'),
    'bots.json': ('token', 'bind_code', 'admin_username'),
}

# ★★ config.json 里**允许非空**的键的白名单。
#    验收规则反过来写：除这几个以外的值，**一律必须是空的**。
#
#    为什么反过来写：以前是「列出敏感字段 → 逐个检查有没有清掉」，
#    漏掉一个字段就等于没验（totp_secret 就是这么漏的）。
#    反过来之后，**新加一个字段默认就是「必须为空」** —— 要么清掉它，
#    要么显式把它加进这个白名单，两种都得动脑子，不会再悄悄漏。
CONFIG_MAY_BE_NONEMPTY = {
    # 只留**当前真实存在**的这几项（少留 = 更严）。
    # 以后 config.json 加了新键，验收会直接报失败逼着做决定：
    # 要么在 scrub_json 里清掉，要么显式加到这里来。
    'host', 'port', 'welcome',
    # ★ 商户专用域名（2026-10-07 加）：它是个**公开域名**，不是密钥 ——
    #   写进仓库无所谓，而且不放进来的话，本机配了它就没法推送了
    #   （兜底断言会报「有非空的私密字段」）。
    'merchant_host',
}

# 短于这个长度的值不拿去当验收基准 —— 太短会到处巧合命中，把验收搞成噪音
MIN_LITERAL = 6

# ★★ 「公开常量」白名单：**永远不许替换**
#
#   为什么需要：脱敏是按「真值清单」在**所有文本文件**里做替换的，
#   它分不清「这是密钥」和「这是公开常量」。
#
#   ★ 踩过的坑（2026-09-28，用户报「USDT 助手查出来余额永远是 0」）：
#     USDT 官方合约地址**出现在运行日志里**（扫链时打印过），
#     于是被当成「真地址」收进了清单，然后从 `usdt.py` 里也替换掉了 ——
#     `USDT = 'TR7NHqje...'` 变成了 `USDT = '你的TRON地址'`，
#     比对合约时永远不匹配 → **余额永远显示 0**。
#     代码没坏，是脱敏把公开常量当密钥删了。
#
#   所以：凡是「协议/官方的公开常量」，都要写在这里。
PUBLIC_CONSTANTS = {
    'TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t',   # 官方 USDT-TRC20 合约地址
}

# 复制时跳过的东西
SKIP_DIRS = {'__pycache__', 'build', '.git', '_github_upload'}
SKIP_FILES = {'运行日志.txt'}

# 只扫这些后缀（exe 那种二进制的已经单独验过是干净的，跳过）
TEXT_EXT = {'.py', '.json', '.html', '.txt', '.js', '.md', '.spec', '.bat', '.css'}


def say(msg=''):
    print(msg, flush=True)


# ================= ① 抽真值（验收基准）=================
def _strings(o):
    """递归把 json 里所有字符串值掏出来"""
    if isinstance(o, dict):
        for v in o.values():
            yield from _strings(v)
    elif isinstance(o, list):
        for v in o:
            yield from _strings(v)
    elif isinstance(o, str):
        yield o


def _fields(o, names):
    """递归把指定字段名的值掏出来（不管嵌多深）"""
    out = []
    if isinstance(o, dict):
        for k, v in o.items():
            if k in names and isinstance(v, str) and v.strip():
                out.append(v.strip())
            else:
                out.extend(_fields(v, names))
    elif isinstance(o, list):
        for v in o:
            out.extend(_fields(v, names))
    return out


def _too_generic(v):
    """太普通的值不当验收基准（比如纯数字、全是同一个字符）"""
    return v.isdigit() or len(set(v)) <= 2


def collect_real_values():
    """把原项目里真正的密钥全找出来 —— 待会儿用它们验收干净副本

    返回 (patterned, literal)：
      · patterned —— 长得就像密钥的（token / APIKey / TRON 地址），用正则抓
      · literal   —— 长得不像、但按字段名认定私密的（面板密码、签名密钥、
                     商户密码哈希、绑定码），用原文比对
    """
    patterned, literal = set(), set()
    for f in SECRET_SOURCES:
        p = os.path.join(HERE, f)
        if not os.path.exists(p):
            continue
        raw = open(p, encoding='utf-8', errors='replace').read()
        if f.endswith('.json'):
            try:
                for s in _strings(json.loads(raw)):
                    for _, ptn in PATTERNS:
                        patterned |= set(ptn.findall(s))
                continue
            except ValueError:
                pass
        for _, ptn in PATTERNS:
            patterned |= set(ptn.findall(raw))

    # ★ 按字段名抓第二类
    for f, names in SENSITIVE_FIELDS.items():
        p = os.path.join(HERE, f)
        if not os.path.exists(p):
            continue
        try:
            d = json.load(open(p, encoding='utf-8'))
        except (ValueError, OSError):
            continue
        for v in _fields(d, names):
            if len(v) >= MIN_LITERAL and not _too_generic(v):
                literal.add(v)
    # ★ 公开常量永远不算密钥（见 PUBLIC_CONSTANTS 的注释）
    patterned -= PUBLIC_CONSTANTS
    # 这些值如果本身就长得像密钥，已经在上面的 patterned 里了，去重
    return patterned, (literal - patterned - PUBLIC_CONSTANTS)


# ================= ② 生成干净副本 =================
def build_clean_copy():
    if os.path.exists(CLEAN):
        say('删掉上一次的副本：%s' % CLEAN)
        shutil.rmtree(CLEAN, ignore_errors=True)
    dst = os.path.join(CLEAN, os.path.basename(HERE))
    os.makedirs(dst, exist_ok=True)
    n = 0
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        rel = os.path.relpath(root, HERE)
        for fn in files:
            if fn in SKIP_FILES:
                continue
            src = os.path.join(root, fn)
            out = os.path.join(dst, rel, fn) if rel != '.' else os.path.join(dst, fn)
            os.makedirs(os.path.dirname(out), exist_ok=True)
            shutil.copy2(src, out)
            n += 1
    say('复制了 %d 个文件 → %s' % (n, dst))
    return dst


# ================= ③ 脱敏 =================
def scrub_text(dst, patterned, literal):
    """把副本里出现的真密钥全换成占位符。返回 {文件名: 替换了几处}"""
    hit = {}
    # ★ 真地址 → 有效占位地址（一对一）。见 TRON_PLACEHOLDERS 的说明：
    #   换成无效的中文占位符会把测试全弄挂
    tmap = tron_placeholder_map(patterned)
    for root, dirs, files in os.walk(dst):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in files:
            if os.path.splitext(fn)[1].lower() not in TEXT_EXT:
                continue
            p = os.path.join(root, fn)
            try:
                t = open(p, encoding='utf-8').read()
            except (UnicodeDecodeError, OSError):
                continue
            orig = t
            # ① 长得像密钥的（按格式换，占位符保持同一种格式）
            for _, ptn in PATTERNS:
                def _sub(m):
                    val = m.group()
                    if val in patterned:
                        for name, p2 in PATTERNS:
                            if p2.fullmatch(val):
                                # ★ TRON 地址换成**有效的**占位地址，
                                #   不然测试里那些「拿地址当夹具」的地方全废
                                #   （见 TRON_PLACEHOLDERS 的说明）
                                if name == 'TRON地址':
                                    # 兜底用中文占位符（会让测试挂 = 看得见），
                                    # 不用 val 本身 —— 那样是**静默泄露**
                                    return tmap.get(val, PH['TRON地址'])
                                return PH[name]
                    return val
                t = ptn.sub(_sub, t)
            # ② 不像密钥的（密码、签名密钥那类）—— 按原文替换
            #    ★ 长的先换，免得短的是长的子串、把长的截断掉
            for val in sorted(literal, key=len, reverse=True):
                if val in t:
                    t = t.replace(val, '换成你自己的')
            if t != orig:
                open(p, 'w', encoding='utf-8').write(t)
                hit[os.path.relpath(p, dst)] = 1
    return hit


def scrub_json(dst, rel, fn, real):
    """把配置里那些「不是上面三种格式」的敏感字段也清掉

    比如面板密码、登录签名密钥、商户密码哈希、绑定码、TG 用户 id ——
    这些不长成密钥的样子，但一样是私密的。
    """
    # ★★ 这里必须拼上 fn。第一版写成 os.path.join(dst, rel) ——
    #    那拿到的是**目录**，于是这个函数对着目录 open()，静默返回空，
    #    真密码和真签名密钥就这么留在了副本里。
    p = os.path.join(dst, rel, fn)
    if not os.path.exists(p):
        return []
    try:
        d = json.load(open(p, encoding='utf-8'))
    except (ValueError, OSError):
        return []
    done = []
    # ★ 这几项一律清成**空串**，不要留「像样的假值」：
    #   · 递一个假的假密钥（比如 32 个 0）进去，`主程序.load_cfg()` 会当成
    #     有效密钥直接用 —— 可预测的签名密钥 = 能伪造商户登录态。
    #   · 留一句「改成你自己的」当密码，等于公布了一个已知密码。
    #   空串则会触发程序自己生成随机值（启动时打印出来），最安全。
    if fn == 'config.json':
        # ★★ `totp_secret` 是 2026-09-29 才补上的 —— 它是**真的漏出去了**：
        #    GitHub 上那份 config.json 里 password / token_secret 都清空了，
        #    唯独 totp_secret 留着 32 位真值 = **管理员的两步验证等于没有**。
        #    根因就是这里字段清单不全，而验收基准也不覆盖它（同一类毛病：
        #    验收没覆盖到的东西 = 等于没验）。
        for k in ('password', 'token_secret', 'totp_secret'):
            if k in d:
                d[k] = ''
                done.append(k)
    elif fn == 'merchants.json':
        for m in (d.get('merchants') or []):
            for k in ('salt', 'hash', 'user'):
                if k in m:
                    m[k] = ''
                    done.append('商户.' + k)
    elif fn == 'bots.json':
        for b in (d.get('bots') or []):
            if 'token' in b:
                b['token'] = PH['机器人token']
                done.append('token')
            for k, v in (('bind_code', '占位绑定码'), ('admin_name', '管理员'),
                         ('admin_username', ''), ('mid', ''),
                         ('owner_id', 0), ('admin_ids', [])):
                if k in b:
                    b[k] = v
                    done.append(k)
            arc = b.get('archive')
            if isinstance(arc, dict):
                arc['chat_notes'] = {}
                arc['mute_chats'] = []
                done.append('archive')
    else:
        return []
    json.dump(d, open(p, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
    return sorted(set(done))


def clean_data_dir(dst):
    """data/ 里是客户聊天归档、账本、订单、图片 —— 整个清掉，
    只留一个说明文件（拉下来的人自己跑一遍就会生成）"""
    d = os.path.join(dst, 'data')
    removed = []
    if os.path.isdir(d):
        for name in os.listdir(d):
            p = os.path.join(d, name)
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    os.remove(p)
                removed.append(name)
            except OSError as e:
                say('  ★ 删不掉 %s：%s' % (name, e))
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, 'README.txt'), 'w', encoding='utf-8').write(
        '这个目录是程序运行时自动生成的：\r\n'
        '\r\n'
        '  <机器人id>.json            每个机器人的运行数据（管理员、监听地址等）\r\n'
        '  <机器人id>.sqlite3         记账机器人的账本\r\n'
        '  <机器人id>.positions.sqlite3  群消息位置（删消息用）\r\n'
        '  <机器人id>.archive.sqlite3 群消息记录\r\n'
        '  <机器人id>.archive-media/  群消息里的图片\r\n'
        '\r\n'
        '不用手动建，第一次运行会自动生成。\r\n')
    return removed


GITIGNORE = """# 运行时会变的东西 —— 别提交，每个人的都不一样
data/
运行日志.txt
config.json
bots.json
merchants.json

# Python / 打包产物
__pycache__/
*.pyc
build/
*.spec.bak
"""


def write_extras(dst):
    """给拉下来的人留个 .gitignore（免得他们把自己的密钥也提交上去）"""
    open(os.path.join(dst, '.gitignore'), 'w', encoding='utf-8',
         newline='\n').write(GITIGNORE)


# ================= ④ 验收（硬门槛）=================
def verify(dst, patterned, literal):
    """拿真密钥扫副本 —— 还剩一个就不许推

    ★ 两类都要查：只查「长得像密钥的」会漏掉面板密码那类（踩过）。
    """
    say('\n--- 安全检查：拿真密钥扫干净副本 ---')
    if not patterned and not literal:
        say('  ★ 没抽到任何真值 —— 无法验收，先别推')
        return False, {'没抽到真值': 1}
    left = {}

    def note(rel, what):
        left.setdefault(rel, set()).add(what)

    for root, dirs, files in os.walk(dst):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for fn in files:
            if os.path.splitext(fn)[1].lower() not in TEXT_EXT:
                continue
            p = os.path.join(root, fn)
            try:
                t = open(p, encoding='utf-8', errors='replace').read()
            except OSError:
                continue
            rel = os.path.relpath(p, dst)
            for name, ptn in PATTERNS:
                if set(ptn.findall(t)) & patterned:
                    note(rel, name)
            for v in literal:
                if v in t:
                    note(rel, '私密字段的值')
                    break

    say('  验收基准：%d 个密钥 + %d 个私密字段值'
        % (len(patterned), len(literal)))
    if left:
        say('  ❌ 还有残留：')
        for f, kinds in sorted(left.items()):
            say('       %-40s %s' % (f, ', '.join(sorted(kinds))))
        return False, left
    say('  ✅ 一个都没剩')

    # ★★ 兜底断言：config.json 里除白名单外的值**一律必须是空的**。
    #    上面那套「拿真值清单去扫」的验收，**只能发现「清单里有的东西」** ——
    #    `totp_secret` 就是因为没进清单（既不在正则里、也不在字段清单里），
    #    所以扫了个「全过了」，而它就在文件里躺着。
    #    这条反过来写：不点名，默认就要空 —— 漏字段也逃不掉。
    cfg_p = os.path.join(dst, 'config.json')
    if os.path.exists(cfg_p):
        try:
            cfg = json.load(open(cfg_p, encoding='utf-8'))
        except (ValueError, OSError) as e:
            say('  ❌ config.json 读不出来（%s）—— 不许推' % e)
            return False, {'config.json': {'读不出来'}}
        dirty = []
        for k, v in sorted(cfg.items()):
            if k in CONFIG_MAY_BE_NONEMPTY:
                continue
            if v not in ('', None, [], {}, 0, False):
                dirty.append(k)
        if dirty:
            say('  ❌ config.json 里还有非空的私密字段：%s' % '、'.join(dirty))
            say('     （要么在 scrub_json 里清成空串，要么显式加进 '
                'CONFIG_MAY_BE_NONEMPTY）')
            return False, {'config.json': set(dirty)}
        say('  ✅ config.json 除 %s 外全是空的'
            % '/'.join(sorted(CONFIG_MAY_BE_NONEMPTY)))
    return True, {}


# ================= ⑤ git =================
def run(args, cwd, check=True):
    """跑一条命令并**把输出抓回来**（本地操作都用这个）

    ★ 推送**不能**用这个 —— 见 run_live()。
    """
    r = subprocess.run(args, cwd=cwd, capture_output=True)
    out = (r.stdout or b'').decode('utf-8', 'replace') + \
          (r.stderr or b'').decode('utf-8', 'replace')
    if check and r.returncode != 0:
        say('  ★ 命令失败：%s\n%s' % (' '.join(args), out.strip()[:600]))
    return r.returncode, out


def run_live(args, cwd):
    """跑一条命令，**输出直接接到当前终端**（不截获）

    ★★ 为什么推送必须用这个：git 要弹浏览器让你登录 GitHub，
      而弹窗是「凭据管理器」干的。如果这里用 capture_output=True，
      git 的 stdin/stdout 就成了管道、不是终端 —— 凭据管理器判定
      「没有可交互的终端」，**浏览器窗口根本不会弹**，
      然后 push 静默失败。表现就是「等半天什么都没发生」。

      这是我第一版踩的坑：本地那些命令截获输出没问题，
      push 也照抄，结果用户那边什么提示都没有。
    """
    return subprocess.run(args, cwd=cwd).returncode


def find_git():
    """找 git。

    ★ 刚装完 git 的那个终端里 `git` 还不在 PATH 上（PATH 是进程启动时读的），
      所以还要去默认安装位置捞一下 —— 不然会误报「没装 git」。
    """
    try:
        subprocess.run(['git', '--version'], capture_output=True, check=True)
        return 'git'
    except (OSError, subprocess.CalledProcessError):
        pass
    for p in (r'C:\Program Files\Git\cmd\git.exe',
              r'C:\Program Files (x86)\Git\cmd\git.exe',
              os.path.expanduser(r'~\AppData\Local\Programs\Git\cmd\git.exe')):
        if os.path.exists(p):
            return p
    return ''


GIT = find_git()


def git_ready():
    return bool(GIT)


def commit_and_push(dst, remote, user):
    say('\n--- 提交 ---')
    run([GIT, 'init', '-b', 'main'], dst)
    run([GIT, 'config', 'user.name', user or 'tg-panel'], dst)
    run([GIT, 'config', 'user.email',
         '%s@users.noreply.github.com' % (user or 'tg-panel')], dst)
    run([GIT, 'add', '-A'], dst)
    # ★ 配置文件被 .gitignore 挡着，这里强制加进去（放的是占位符版本）
    run([GIT, 'add', '-f', 'config.json', 'bots.json', 'merchants.json',
         'data/README.txt'], dst, check=False)
    code, _ = run([GIT, 'commit', '-m',
                   '初始化：TG 机器人管理面板（密钥已替换为占位符）'], dst,
                  check=False)
    if code != 0:
        say('  （没有改动可提交，或者提交失败）')
    else:
        say('  ✅ 已提交')
    # ★ 提交后再查一遍：确认进仓库的那份里没有真密钥
    #
    # 注意：**不能只看「长得像 token」**——面板的帮助文字里就有一句
    #   1234567890:AAHxxxxxxxxxxxxxxxxxxxx
    # 是用来说明「token 长这样」的示例，不是真 token。
    # 所以明显的占位串要放过，只揪真正可疑的。
    code, out = run([GIT, 'grep', '-n', '-E',
                     r'[0-9]{8,12}:[A-Za-z0-9_-]{30,}', 'HEAD'], dst,
                    check=False)
    if code == 0:
        bad = []
        for line in out.splitlines():
            m = re.search(r'\d{8,12}:[A-Za-z0-9_-]{30,}', line)
            if not m:
                continue
            val = m.group()
            if re.search(r'(?i)(x{4,}|0{8,}|你的|占位|换成|example|placeholder)',
                         val):
                continue          # 示例/占位，放过
            bad.append(line)
        if bad:
            say('  ❌ 提交里还有像真 token 的东西，停！')
            for b in bad[:10]:
                # ★ 只报文件名和行号，别把 token 本身打到屏幕上
                say('     %s' % b.split(':', 2)[0] + ':' +
                    (b.split(':', 2)[1] if b.count(':') >= 2 else '?'))
            return False
        say('  （那两处是面板里的示例 token，不是真的）')
    if not remote:
        say('\n★ 没给仓库地址，先到这儿。检查没问题后这样推：')
        say('    cd "%s"' % dst)
        say('    %s remote add origin <你的仓库地址>' % GIT)
        say('    %s push -u origin main' % GIT)
        return True
    run([GIT, 'remote', 'remove', 'origin'], dst, check=False)
    run([GIT, 'remote', 'add', 'origin', remote], dst)
    say('\n--- 推送 ---')
    say('  ★ 第一次会弹一个浏览器窗口让你登录 GitHub，点「Authorize」')
    say('    如果没弹出来，看下面的提示。')
    # ★★ 必须用 --force。原因：
    #   这个脚本每次都**从零重建**干净副本（删掉 .git 重新 init），
    #   所以每次提交都是**一个全新的、没有父提交的快照**。
    #   远端还留着上一次那个快照 —— 两边历史无关，git 会拒绝：
    #       ! [rejected] main -> main (non-fast-forward)
    #   （用户 2026-09-28 就撞上这个，还以为是代理问题）
    #
    #   强制推送在这里是**对的**：
    #     · 仓库内容就是「当前项目的一份快照」，历史没有意义；
    #     · 而且有个额外好处 —— 每次只剩一个提交，
    #       那两个 36MB 的 exe 不会一轮轮堆在历史里（不然仓库很快上 G）。
    #   ⚠️ 代价：如果你在 GitHub 网页上直接改过东西，会被覆盖掉。
    try:
        code = run_live([GIT, 'push', '-u', '--force', 'origin', 'main'], dst)
    except KeyboardInterrupt:
        say('\n  （你按了 Ctrl+C，推送中断）')
        return False
    if code != 0:
        say('\n  ★ 推送失败（退出码 %d）。常见原因：' % code)
        say('    · **non-fast-forward / [rejected]** → 远端有旧的快照，')
        say('      这个脚本本来就该用 --force 推（它每次都重建快照）。')
        say('      如果还是报这个，说明脚本版本旧了，看看上面 push 那行')
        say('      有没有 --force。')
        say('    · 没弹浏览器 → **不一定是问题**：上次登录过的凭据会缓存，')
        say('      这种情况压根不该弹窗。只有报「认证失败」才需要管。')
        say('    · 提示要密码 → GitHub 早就不收账号密码了，要用 token：')
        say('      https://github.com/settings/tokens 建一个（勾 repo），')
        say('      到时候密码那儿粘 token')
        say('    · 提示 403 → 这个账号对仓库没写权限，确认仓库是你自己的')
        say('    · 提示 couldn\'t connect / Connection was reset → 才是网络/代理问题：')
        say('      git config --global http.https://github.com.proxy http://127.0.0.1:7890')
    return code == 0


# ================= 主流程 =================
def main():
    remote = sys.argv[1] if len(sys.argv) > 1 else ''
    user = ''
    if '--user' in sys.argv:
        user = sys.argv[sys.argv.index('--user') + 1]

    say('=' * 62)
    say('把项目脱敏后传到 GitHub')
    say('=' * 62)
    say('源目录：%s' % HERE)
    say('（本地文件一个都不会改）\n')

    patterned, literal = collect_real_values()
    say('验收基准：%d 个密钥 + %d 个私密字段值（密码/签名密钥这类的）'
        % (len(patterned), len(literal)))

    dst = build_clean_copy()

    say('\n--- 清理配置里的私密字段（先做，免得后面又被谁读回去）---')
    for fn in ('config.json', 'merchants.json', 'bots.json'):
        done = scrub_json(dst, '.', fn, patterned)
        say('  %-18s %s' % (fn, ', '.join(done) if done else '（没有要清的）'))

    # ★★ 机器人/商户列表**直接清空**（2026-09-29）
    #    清成占位值还不够：新服务器 `git clone` 下来会得到一堆
    #    「假 token + enabled=true」的机器人 —— 面板会拿假 token 一直重连
    #    Telegram，日志被刷满，界面上还挂着一排连不上的僵尸机器人，
    #    得手动一个个删。商户同理（一个空密码的空壳）。
    #    ★ 用户说了：测试数据可以丢，新服务器要从干净的开始。
    say('\n--- 机器人/商户列表清空（新服务器要从干净的开始）---')
    for fn, key in (('bots.json', 'bots'), ('merchants.json', 'merchants')):
        p = os.path.join(dst, fn)
        if os.path.exists(p):
            n = 0
            try:
                old = json.load(open(p, encoding='utf-8'))
                n = len(old.get(key) or [])
            except (ValueError, OSError, AttributeError):
                pass
            json.dump({key: []}, open(p, 'w', encoding='utf-8'),
                      ensure_ascii=False, indent=2)
            say('  %-16s 清掉 %d 条 → 空' % (fn, n))

    say('\n--- 替换正文里的敏感值 ---')
    hit = scrub_text(dst, patterned, literal)
    say('  改了 %d 个文件' % len(hit))
    code_hit = []
    for rel in sorted(hit):
        if os.path.splitext(rel)[1].lower() in (
                '.py', '.html', '.js', '.sh', '.spec'):
            code_hit.append(rel)
            say('    ⚠️ %s   ← 注意：这是代码/页面，不是配置' % rel)
        else:
            say('    %s' % rel)
    if code_hit:
        say()
        say('  ★★ 脱敏**动了代码文件**，这只有两种情况：')
        say('     ① 探测脚本里硬编码了密钥 —— 正常，本来就该删；')
        say('     ② 把**公开常量**当成密钥删掉了 —— 就是我们踩过的那个坑：')
        say('        USDT 官方合约地址出现在运行日志里，被当成真地址，')
        say('        从 usdt.py 里也替换掉了 → 服务器上余额永远查出来是 0。')
        say('     改完**一定要跑一遍测试**（python _test_*.py），')
        say('     或者至少确认程序能起来。公开常量要加进 PUBLIC_CONSTANTS。')

    say('\n--- 清空 data/（客户聊天记录、图片、账本）---')
    removed = clean_data_dir(dst)
    say('  删了 %d 项' % len(removed))

    write_extras(dst)

    ok, _ = verify(dst, patterned, literal)
    if not ok:
        say('\n★★ 验收没过 —— 不许推送。先把上面列的文件处理掉。')
        return 1

    # ★★ `--check-only`：只生成干净副本 + 跑验收，**不碰 git、不推送**。
    #    什么时候用：想先看一眼「脱敏到底干不干净」而不想真推的时候。
    #    推送是公开且不可逆的动作，这个开关让「验证」和「发布」能分开做。
    if '--check-only' in sys.argv:
        say('\n★ --check-only：验收过了，到此为止（没有提交、没有推送）')
        say('  干净副本：%s' % dst)
        return 0

    if not git_ready():
        say('\n★ 没找到 git。先装：winget install --id Git.Git -e')
        say('  （装完把终端关掉重开，再跑一次这个脚本）')
        say('\n干净副本已经生成好了：%s' % dst)
        return 1

    ok = commit_and_push(dst, remote, user)
    say('\n' + '=' * 62)
    say('干净副本：%s' % dst)
    say('★ 本地项目没动过（这个脚本只读原目录）')
    say('=' * 62)
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
