# -*- coding: utf-8 -*-
"""看看测试服上那台独立版到底怎么了（只读）"""
import os
import sys

sys.stdout.reconfigure(encoding='utf-8')
import paramiko

KEY = os.path.join(os.path.expanduser('~'), 'Desktop', 'aliyun秘钥.pem')
c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect('47.236.8.82', username='root', key_filename=KEY, timeout=25)


def run(cmd, t=90):
    _i, o, e = c.exec_command(cmd, timeout=t)
    return (o.read().decode('utf-8', 'replace')
            + e.read().decode('utf-8', 'replace')).rstrip()


print('=' * 70)
print('① 现在活着吗（重启完过了一分多钟，崩溃循环的话早又挂了）')
print('=' * 70)
print(run('systemctl is-active tgledger; systemctl show tgledger '
          '-p NRestarts -p ActiveEnterTimestamp -p ExecMainStartTimestamp'))

print()
print('=' * 70)
print('② systemd 记的失败原因（这是崩溃的真话）')
print('=' * 70)
print(run('journalctl -u tgledger -n 60 --no-pager 2>&1 | '
          'grep -v "Started\\|Stopping\\|Stopped\\|Deactivated\\|Scheduled" '
          '| tail -30'))

print()
print('=' * 70)
print('③ 程序自己的日志（core.log 写的那个）')
print('=' * 70)
print(run('tail -40 "/root/tgledger/记账独立版/运行日志.txt" 2>/dev/null '
          '|| echo （没有这个文件）'))

print()
print('=' * 70)
print('④ 手工跑一次 --check，看它自己怎么说')
print('=' * 70)
print(run('cd "/root/tgledger/记账独立版" && .venv/bin/python 独立版.py '
          '--check 2>&1 | tail -25'))

print()
print('=' * 70)
print('⑤ 配置长什么样（**不打印 token**，只看有没有填）')
print('=' * 70)
print(run("""cd "/root/tgledger/记账独立版" && .venv/bin/python -c "
import json
c = json.load(open('config.json'))
print('id        :', c.get('id'))
print('token 填了:', bool(c.get('token')), '（长度 %d）' % len(c.get('token') or ''))
print('bind_code :', c.get('bind_code'))
print('report    :', json.dumps(c.get('report'), ensure_ascii=False))
" 2>&1"""))

print()
print('=' * 70)
print('⑥ 绑定状态')
print('=' * 70)
print(run("""cd "/root/tgledger/记账独立版" && .venv/bin/python -c "
import json
s = json.load(open('data/5a8e0afc.state.json'))
print('admin_ids   :', s.get('admin_ids'))
print('admin_name  :', s.get('admin_name'))
print('owner_id    :', s.get('owner_id'))
print('username    :', s.get('username'))
" 2>&1"""))

print()
print('=' * 70)
print('⑦ 依赖装齐了没')
print('=' * 70)
print(run('cd "/root/tgledger/记账独立版" && .venv/bin/python -c '
          '"import httpx,requests,PIL;print(\'httpx\',httpx.__version__,'
          '\'requests\',requests.__version__)" 2>&1'))

c.close()
print()
print('查完。')
