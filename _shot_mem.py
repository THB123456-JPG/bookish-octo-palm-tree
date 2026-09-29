# -*- coding: utf-8 -*-
"""把「👥 群成员」那块渲染成图片，肉眼确认长什么样

    python _shot_mem.py          # 电脑宽 1400
    python _shot_mem.py 430      # 手机宽

★ 数据是**从面板真接口拉的**（live），所以看到的就是真实效果，
  不是编的例子。拉不到就退回一份写死的样例。
★ 跟 _shot_css.py 一样：只抠 panel_page.html 的 <style>，
  配上跟 msgLoadMembers() 渲染出来**一模一样**的 HTML 骨架。
  所以那边的 JS 改了，这里记得同步。
"""
import io
import json
import os
import re
import subprocess
import sys

sys.stdout.reconfigure(encoding='utf-8')
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, 'panel_page.html')
OUT_HTML = os.path.join(HERE, '_shot_mem.html')
OUT_PNG = os.path.join(HERE, '_shot_mem.png')
PANEL = 'http://152.32.225.245:8080'
BID = '5a8e0afc'
CHAT = '-5480771291'

CHROME_CANDIDATES = [
    r'C:\Program Files\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
]

SAMPLE_MEMBERS = [
    {'display_name': '群主大人', 'username': 'boss1', 'admin_title': '群主',
     'is_owner': True, 'is_bot': False, 'count': 39, 'last_ts': '2026-09-29T04:18:00+08:00'},
    {'display_name': '值班管理', 'username': '', 'admin_title': '值班',
     'is_owner': False, 'is_bot': False, 'count': 0, 'last_ts': ''},
    {'display_name': '偏门神', 'username': 'pms66',
     'is_owner': False, 'is_bot': False, 'count': 17, 'last_ts': '2026-09-29T04:19:00+08:00'},
    {'display_name': '测试3', 'username': 'jgubkjbot', 'admin_title': '管理员',
     'is_owner': False, 'is_bot': True, 'count': 20, 'last_ts': '2026-09-29T04:06:00+08:00'},
]


def esc(s):
    return (str(s if s is not None else '')
            .replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))


def fmt_ts(iso):
    """跟页面里的 fmtTs 一样：9-29 04:18"""
    if not iso:
        return ''
    m = re.match(r'\d{4}-(\d{2})-(\d{2})T(\d{2}):(\d{2})', iso)
    if not m:
        return iso
    return '%d-%s %s:%s' % (int(m.group(1)), m.group(2), m.group(3), m.group(4))


def rows_html(members):
    out = []      # 表头那行说明已经去掉了（用户说不需要）
    for m in members:
        name = m.get('display_name') or m.get('username') or m.get('user_id')
        un = '@' + m['username'] if m.get('username') else '（没设用户名）'
        right = ('%d 条 · 最后 %s' % (m['count'], fmt_ts(m.get('last_ts')))
                 if m.get('count') else '从没发过言')
        out.append(
            '<div class="mrow"><span class="nm">%s</span>'
            '<span class="un">%s</span>%s%s%s'
            '<span class="rt">%s</span></div>' % (
                esc(name), esc(un),
                '<span class="mtag adm">%s</span>' % esc(m['admin_title'])
                if m.get('admin_title') else '',
                '<span class="mtag own">主人</span>' if m.get('is_owner') else '',
                '<span class="mtag bot">机器人</span>' if m.get('is_bot') else '',
                esc(right)))
    return ''.join(out)


def find_chrome():
    for p in CHROME_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def main():
    width = int(sys.argv[1]) if len(sys.argv) > 1 else 1400
    members = SAMPLE_MEMBERS
    try:
        r = requests.get(PANEL + '/api/archive/%s/members' % BID,
                         params={'chat': CHAT},
                         headers={'X-Panel-Pass': '换成你自己的'}, timeout=20)
        got = (r.json() or {}).get('members') or []
        if got:
            members = got
            print('数据来自面板真接口（%d 人）' % len(got))
    except Exception as e:
        print('拉不到真数据（%s），用样例' % e)

    src = io.open(SRC, encoding='utf-8').read()
    m = re.search(r'<style>(.*?)</style>', src, re.S)
    if not m:
        raise SystemExit('!! panel_page.html 里找不到 <style>')

    label = '手机 %dpx' % width if width <= 720 else '电脑 %dpx' % width
    # ★★ Windows 上无头 Chrome 的窗口有**最小宽度**（量出来是 500）：
    #    给它 430，它按 500 排版，截图却按 430 裁 —— 看着像内容被截掉，
    #    其实页面好着呢（2026-09-29 差点为这个白改一通 CSS）。
    #    所以窄屏要**自己套一层固定宽的容器**来模拟，别指望 --window-size。
    win_w = max(width, 520)
    wrap_open = wrap_close = ''
    if width < 520:
        inner = width - 20
        wrap_open = ('<div style="width:%dpx;margin:0 auto;'
                     'border:1px dashed #6e7681">' % inner)
        wrap_close = '</div>'
    # ★★ 全屏聊天模式（body.chatting）**必须能单独看**：
    #    用户报的那个「人多的群点开只剩一条缝」，就只在这个模式下出现 ——
    #    因为只有它是 flex 列，会把没写 flex 的 #memBox 挤扁。
    chatting = '--chatting' in sys.argv
    chat_msgs = ''.join(
        u'<div class="mb"><div class="col"><div class="mmeta"><b>偏门神</b>'
        u' <span class="muted">9-29 04:20</span></div>'
        u'<div class="bb">第 %d 条消息，用来看消息区占多高</div></div></div>'
        % i for i in range(1, 41))
    html = (u'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            u'<meta name="viewport" content="width=device-width,'
            u'initial-scale=1"><style>%s</style></head><body%s>'
            u'<h1 style="font-size:16px;margin-bottom:12px">'
            u'群成员预览（%s%s）</h1>%s'
            u'<div class="card" id="msgLv3"><h2 style="font-size:14px">'
            u'<a href="#" class="crumb">📋 机器人列表</a>'
            u'<span class="muted">/</span> '
            u'<a href="#" class="crumb">群消息</a>'
            u'<span class="muted">/</span> <span>客户端部署测试</span>'
            u'<span class="cnt">（73 条）</span>'
            u'<a href="#" class="mchip">👥 群成员</a></h2>'
            u'<div id="memBox" style="display:block">%s</div>'
            u'<div id="msgStream">%s</div>'
            u'<pre id="out" style="color:#0f0;font-size:12px"></pre>'
            u'</div>%s'
            u'<script>'
            u'var b=document.getElementById("memBox");'
            u'var st=document.getElementById("msgStream");'
            u'var lv=document.getElementById("msgLv3");'
            u'var cs=getComputedStyle(b);'
            u'document.getElementById("out").textContent="'
            u'body.class=" + document.body.className'
            u' + " | lv3 高 " + Math.round(lv.getBoundingClientRect().height)'
            u' + " | stream 高 " + Math.round(st.getBoundingClientRect().height)'
            u' + "/内容 " + st.scrollHeight'
            u' + " | memBox 高 " + Math.round(b.getBoundingClientRect().height)'
            u' + "/内容 " + b.scrollHeight'
            u' + " | memBox flex=" + cs.flex + " minH=" + cs.minHeight;'
            u'</script>'
            u'</body></html>'
            % (m.group(1), ' class="chatting"' if chatting else '',
               label, '，全屏聊天模式' if chatting else '',
               wrap_open, rows_html(members), chat_msgs, wrap_close))
    io.open(OUT_HTML, 'w', encoding='utf-8', newline='\n').write(html)

    chrome = find_chrome()
    if not chrome:
        raise SystemExit('!! 找不到 Chrome / Edge')
    url = 'file:///' + OUT_HTML.replace('\\', '/')
    subprocess.run([chrome, '--headless=new', '--disable-gpu', '--no-sandbox',
                    '--hide-scrollbars', '--window-size=%d,900' % win_w,
                    '--screenshot=%s' % OUT_PNG, url],
                   check=True, capture_output=True)
    # ★ 顺手把**量出来的高度**打出来 —— 光看图判断「被挤扁了没有」不靠谱，
    #   数字最直接（尤其成员数不同的时候）
    dom = subprocess.run([chrome, '--headless=new', '--disable-gpu',
                          '--no-sandbox', '--window-size=%d,900' % win_w,
                          '--virtual-time-budget=1500', '--dump-dom', url],
                         capture_output=True, text=True, encoding='utf-8',
                         errors='replace').stdout
    mm = re.search(r'<pre id="out"[^>]*>(.*?)</pre>', dom, re.S)
    if mm:
        print(mm.group(1).strip())
    print('出图：%s（%s）' % (OUT_PNG, label))
    if width < 520:
        print('★ 注意：无头 Chrome 窗口最小 500px，所以这里是套了个 '
              '%dpx 的框模拟的（虚线框就是它）' % (width - 20))


if __name__ == '__main__':
    main()
