# -*- coding: utf-8 -*-
"""登录日志

记下谁在什么时候、从哪个 IP、哪个城市登录了（成功和失败都记）。

★ IP 归属地是查 `ip-api.com`（免费、不用 key、支持中文）。
  为什么是它：pconline 那个直接 403，ip.sb / ipinfo 只给英文。
★ 查询是**后台线程**做的，绝不拖慢登录 —— 先落记录（城市空着），
  查到了再回填。查不到就算了，只显示 IP。
★ 局域网 / 本机地址不查（查了也没意义，还白跑一次网络）。
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback

import requests

import core

MAX_RECORDS = 500                 # 只留最近 500 条，免得文件无限长
GEO_URL = 'http://ip-api.com/json/%s'
GEO_TIMEOUT = 6

_lock = threading.Lock()
_geo_cache = {}                   # {ip: 城市}，同一个人反复登录不用重复查


def _path():
    return os.path.join(core.DATA_DIR, 'login_log.json')


def _is_private(ip):
    """本机 / 内网 / Tailscale 的地址，查归属地没意义"""
    if not ip:
        return True
    if ip.startswith(('127.', '10.', '192.168.', '::1', 'localhost')):
        return True
    if ip.startswith('172.'):
        try:
            return 16 <= int(ip.split('.')[1]) <= 31
        except (IndexError, ValueError):
            return False
    # Tailscale 用 100.64.0.0/10
    if ip.startswith('100.'):
        try:
            return 64 <= int(ip.split('.')[1]) <= 127
        except (IndexError, ValueError):
            return False
    return False


def lookup_city(ip):
    """查 IP 归属地，返回「省 市」这种。查不到返回空串"""
    if _is_private(ip):
        return '本机/内网'
    if ip in _geo_cache:
        return _geo_cache[ip]
    city = ''
    try:
        r = requests.get(GEO_URL % ip, params={'lang': 'zh-CN',
                                               'fields': 'status,country,'
                                                         'regionName,city'},
                         timeout=GEO_TIMEOUT)
        d = r.json() if r.status_code == 200 else {}
        if d.get('status') == 'success':
            parts = [str(d.get('country') or '').strip(),
                     str(d.get('regionName') or '').strip(),
                     str(d.get('city') or '').strip()]
            # 国家和省重名时别写两遍（「中国 中国」这种）
            out = []
            for p in parts:
                if p and p not in out:
                    out.append(p)
            city = ' '.join(out)
    except Exception:
        city = ''                 # 查不到就算了，不影响记日志
    _geo_cache[ip] = city
    return city


def _load():
    d = core.load_json(_path(), [])
    return d if isinstance(d, list) else []


def _save(rows):
    core.save_json(_path(), rows[:MAX_RECORDS])


def add(ip, user, role, ok, ua='', kind='login'):
    """记一条。★ 立刻返回，城市由后台线程回填

    kind:
      'login'  点了登录按钮（成功或失败）
      'access' 用存着的凭据直接进来了 —— 有印象的浏览器会自动登录，
               **不走 /api/login**，只记 login 的话这种访问全都是隐形的
    """
    ip = (ip or '').strip() or '未知'
    rec = {'t': int(time.time()), 'ip': ip, 'city': '',
           'user': user or '', 'role': role or '',
           'ok': bool(ok), 'ua': (ua or '')[:120], 'kind': kind}
    with _lock:
        rows = _load()
        rows.insert(0, rec)       # 新的在前
        _save(rows)

    def fill():
        try:
            city = lookup_city(ip)
            if not city:
                return
            with _lock:
                rows2 = _load()
                for x in rows2:
                    if x is rec or (x.get('t') == rec['t']
                                    and x.get('ip') == ip
                                    and not x.get('city')):
                        x['city'] = city
                _save(rows2)
        except Exception:
            core.log('回填 IP 归属地失败：%s' % traceback.format_exc()[-200:])

    threading.Thread(target=fill, daemon=True, name='geo-lookup').start()
    return rec


def recent(limit=200):
    with _lock:
        return _load()[:max(1, min(int(limit or 200), MAX_RECORDS))]


def clear():
    with _lock:
        n = len(_load())
        _save([])
    return n


def stats():
    with _lock:
        rows = _load()
    ok = sum(1 for r in rows if r.get('ok'))
    return {'total': len(rows), 'ok': ok, 'fail': len(rows) - ok}
