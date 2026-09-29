# -*- coding: utf-8 -*-
"""shell 脚本必须是 LF 换行 —— CRLF 会让它在 Linux 上**第一行就挂**

★ 2026-09-29 真踩（新服务器部署演练时抓到的）：
  用 python 脚本改 `deploy.sh` 的一句提示语，Windows 上默认把 `\\n`
  写成了 `\\r\\n` → 整个文件变成 CRLF。上传到 Linux 后：

      deploy.sh: line 21: set: pipefail: invalid option name

  因为 `set -euo pipefail` 被读成了 `pipefail\\r`，不是合法选项，脚本当场退出。
  而这文件**之前一直是坏的却没人发现** —— `_deploy_server.py` 只传代码、
  不执行 deploy.sh（面板是早就装好的），所以这个雷一直埋着，
  等你换新服务器才会炸。

★ `.gitattributes` 里的 `*.sh text eol=lf` 只管 git 那条路；
  直接 scp 上传的话，本地是什么样就传什么样。所以本地也得盯着。
"""
import io
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
HERE = os.path.dirname(os.path.abspath(__file__))

OK, BAD = [], []


def check(name, cond, extra=''):
    (OK if cond else BAD).append(name)
    print('  %s %s%s' % ('✅' if cond else '❌', name,
                         ('  → %s' % (extra,)) if extra else ''))


sh = []
for root, dirs, files in os.walk(HERE):
    dirs[:] = [d for d in dirs if d not in
               ('__pycache__', '.git', '.venv', '_github_upload',
                'data', 'dist', 'build')]
    for fn in files:
        if fn.endswith('.sh'):
            sh.append(os.path.join(root, fn))

print('=' * 62)
print('shell 脚本换行检查（找到 %d 个 .sh）' % len(sh))
print('=' * 62)
check('至少找到了脚本（不然这个测试是空跑）', len(sh) >= 2,
      str([os.path.relpath(p, HERE) for p in sh]))

for p in sorted(sh):
    rel = os.path.relpath(p, HERE)
    b = io.open(p, 'rb').read()
    crlf = b.count(b'\r\n')
    check('%-24s 是 LF 换行' % rel, crlf == 0,
          '有 %d 行是 CRLF（Linux 上会报 pipefail: invalid option）' % crlf
          if crlf else '')
    # ★ 第一行必须是 #!...bash —— 少了的话（或者被 BOM 挡了）
    #   Linux 上要么不认、要么用 sh 跑（sh 没有 pipefail）
    first = b.split(b'\n', 1)[0]
    check('%-24s 有 bash shebang' % rel, first.startswith(b'#!') and b'bash' in first,
          repr(first[:40]))
    check('%-24s 开头没有 BOM' % rel, not b.startswith(b'\xef\xbb\xbf'))

print()
print('=' * 62)
print('结果：%d 项通过，%d 项失败' % (len(OK), len(BAD)))
for x in BAD:
    print('  ❌', x)
print('=' * 62)
sys.exit(1 if BAD else 0)
