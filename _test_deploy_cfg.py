# -*- coding: utf-8 -*-
"""测 deploy.sh 里「写 config.json」那段

★ 为什么要这么测：那段是嵌在 shell heredoc 里的 Python，改动了没法靠
  syntax check 发现（bash -n 只看 shell 部分）。所以这里**从 deploy.sh
  里原样把那段抠出来**跑，测的是真发货的代码，不是抄一份。
"""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.stdout.reconfigure(encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))
OK, BAD = [], []


def check(name, cond, extra=''):
    (OK if cond else BAD).append(name)
    print('  %s %s%s' % ('✅' if cond else '❌', name,
                         ('  → %s' % (extra,)) if extra else ''))


src = io.open(os.path.join(HERE, 'deploy.sh'), encoding='utf-8').read()
head, sep, rest = src.partition("<<'PYEOF'\n")
check('deploy.sh 里有那段 heredoc', bool(sep))
body, sep2, _ = rest.partition('\nPYEOF')
check('heredoc 有结束标记', bool(sep2))
check('heredoc 用的是单引号（不会被 shell 提前展开 $）',
      "<<'PYEOF'" in src)

tmp = tempfile.mkdtemp(prefix='_dep_test_')
snippet = os.path.join(tmp, 'cfg.py')
io.open(snippet, 'w', encoding='utf-8').write(body)
conf = os.path.join(tmp, 'config.json')


def run(port='8080', given='1', host='0.0.0.0', host_given='0'):
    # ★ 第 5 个参数是 `--host` 有没有显式传（2026-10-07 加的）——
    #   它决定「反代模式」下要不要保住 127.0.0.1。
    r = subprocess.run([sys.executable, snippet, conf, host, port, given,
                        host_given], capture_output=True)
    out = (r.stdout or b'').decode('utf-8', 'replace')
    err = (r.stderr or b'').decode('utf-8', 'replace')
    return r.returncode, out, err


# ---- ① 全新服务器：没有 config.json ----
print('\n一、全新服务器（没有 config.json）')
code, out, err = run()
check('跑得通', code == 0, err.strip()[:200])
cfg = json.load(io.open(conf, encoding='utf-8')) if os.path.exists(conf) else {}
check('★ 生成了 config.json', os.path.exists(conf))
check('★★ host 是 0.0.0.0（不然服务器外面访问不了）',
      cfg.get('host') == '0.0.0.0', str(cfg.get('host')))
check('port 跟着参数走', cfg.get('port') == 8080, str(cfg.get('port')))
pw1 = cfg.get('password') or ''
check('★ 自动生成了密码', len(pw1) >= 8, '%d 位' % len(pw1))
check('★★ 密码纯 ASCII（中文会让 HTTP 头报错）',
      pw1.isascii() and pw1.isprintable(), repr(pw1[:3]) + '...')
check('★ 生成了签名密钥', len(cfg.get('token_secret') or '') == 32)
check('把密码打到屏幕上了（不然用户不知道密码）', pw1 in out)

# ---- ② 再跑一次：不能把原来的密码换掉 ----
print('\n二、重跑一次（不能把密码换掉）')
code, out, err = run('9000')
check('跑得通', code == 0, err.strip()[:200])
cfg2 = json.load(io.open(conf, encoding='utf-8'))
check('★★ 密码没被改（重跑部署不该踢掉用户）',
      cfg2.get('password') == pw1)
check('★ token_secret 也没被改', cfg2.get('token_secret') == cfg.get('token_secret'))
check('★★ 显式传了 --port 就得生效', cfg2.get('port') == 9000,
      str(cfg2.get('port')))
check('屏幕上还是把原密码显示出来',
      (cfg2.get('password') or '') in out)

# 没传 --port（PORT_GIVEN=0）→ 保留原来的端口，别把用户手改的冲掉
code, out, err = run('8080', given='0')
cfg2b = json.load(io.open(conf, encoding='utf-8'))
check('★ 没传 --port → 保留原端口', cfg2b.get('port') == 9000,
      str(cfg2b.get('port')))

# ---- ③ 用户手改过密码：必须尊重 ----
print('\n三、用户自己改过密码')
cfg2['password'] = 'MyOwnPass123'
cfg2['host'] = '127.0.0.1'      # ★ 仓库里的模板就是这个值
json.dump(cfg2, io.open(conf, 'w', encoding='utf-8'))
code, out, err = run()
c3 = json.load(io.open(conf, encoding='utf-8'))
check('★★ 手改的密码没被动', c3.get('password') == 'MyOwnPass123',
      str(c3.get('password')))
# ★★ host 必须被**强制**改成 0.0.0.0。
#    用 setdefault 的话，「仓库模板里的 127.0.0.1」这个已存在的值不会被覆盖，
#    结果服务器上只监听本机、外面连不上，而且不报任何错（踩过）。
check('★★ 仓库模板里的 127.0.0.1 被强制改成 0.0.0.0（不然外网访问不了）',
      c3.get('host') == '0.0.0.0', str(c3.get('host')))
check('  而且屏幕上说明了改了什么', 'host' in out, out.strip()[:80])

# ---- ④ 中文密码（老版本可能留下这种）：要换掉 ----
print('\n四、config.json 里是中文密码（走不了 HTTP 头）')
c3['password'] = '我的密码'
json.dump(c3, io.open(conf, 'w', encoding='utf-8'))
code, out, err = run()
c4 = json.load(io.open(conf, encoding='utf-8'))
check('★ 中文密码被换成了纯 ASCII 的',
      (c4.get('password') or '').isascii() and c4['password'] != '我的密码',
      str(c4.get('password')))

# ---- ⑤ config.json 坏了：不能崩，要重建 ----
print('\n五、config.json 是坏 JSON')
io.open(conf, 'w', encoding='utf-8').write('{这不是 json')
code, out, err = run()
check('跑得通（没崩）', code == 0, err.strip()[:200])
c5 = json.load(io.open(conf, encoding='utf-8'))
check('★ 重建了一份能用的', c5.get('host') == '0.0.0.0' and c5.get('password'))

# ---- ⑥ ★★ 挂在 Nginx 后面的机器：不许把 127.0.0.1 改成 0.0.0.0 ----
# ★ 2026-10-07 加。这个坑很阴：那台面板的 host **故意**是 127.0.0.1
#   （只让 Nginx 从本机反代，密码是明文头，绝不能暴露公网）。
#   不认 behind_proxy 标记的话，**重跑一次部署脚本就会把它静默地捅回公网** ——
#   而且一切看起来都正常，没人会发现。
print('\n六、★ Nginx 反代模式（behind_proxy）')
c6 = {'host': '127.0.0.1', 'port': 8080, 'password': 'pw1234567',
      'behind_proxy': True}
io.open(conf, 'w', encoding='utf-8').write(json.dumps(c6))
code, out, err = run()                      # ← 模拟「直接重跑 deploy.sh」
check('跑得通', code == 0, err.strip()[:200])
c6 = json.load(io.open(conf, encoding='utf-8'))
check('★★★ 重跑部署脚本**没有**把 host 改回 0.0.0.0（那等于重新暴露公网）',
      c6.get('host') == '127.0.0.1', str(c6.get('host')))
check('★ 密码也没被冲掉', c6.get('password') == 'pw1234567')
check('★ 提示语明确说了「保持不动」', 'behind_proxy' in out, out.strip()[:80])

# 显式传 --host 时，用户说了算（这时可以覆盖）
code, out, err = run(host='0.0.0.0', host_given='1')
c6 = json.load(io.open(conf, encoding='utf-8'))
check('★ 显式传了 --host 就听用户的（想改回去也能改）',
      c6.get('host') == '0.0.0.0', str(c6.get('host')))

# 没有 behind_proxy 标记的普通机器：还是照旧强制 0.0.0.0
io.open(conf, 'w', encoding='utf-8').write(
    json.dumps({'host': '127.0.0.1', 'port': 8080, 'password': 'pw1234567'}))
code, out, err = run()
c6 = json.load(io.open(conf, encoding='utf-8'))
check('★ 普通服务器（没这个标记）仍然强制 0.0.0.0 —— 老行为没被破坏',
      c6.get('host') == '0.0.0.0', str(c6.get('host')))

shutil.rmtree(tmp, ignore_errors=True)
print()
print('=' * 58)
print('结果：%d 项通过，%d 项失败' % (len(OK), len(BAD)))
for x in BAD:
    print('  ❌', x)
print('=' * 58)
sys.exit(1 if BAD else 0)
