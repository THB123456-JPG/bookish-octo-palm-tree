# -*- coding: utf-8 -*-
"""把面板的样式渲染成图片，用来**肉眼确认**表格对齐对不对

    python _shot_css.py            # 出图 _shot_css.png（默认 1400 宽，跟电脑上一样）
    python _shot_css.py 430        # 手机宽度

★ 为什么要这个：表格对齐这种事**看代码看不出来** —— 列宽是浏览器算的。
  2026-09-29 为了「操作」两个字该放哪，来回改了三次全靠猜，用户很不耐烦。
  有了这个脚本，改完自己先看一眼再上线。

★ 它只做一件事：把 panel_page.html 里的 <style> 抠出来，
  配上跟真实表格一模一样的 HTML 骨架，用无头 Chrome 截图。
  所以**改 CSS 之后跑一下就知道长什么样**，不用真的去登面板。

★ 局限：这里的是「静态骨架」，不是真跑 JS 渲染出来的表。
  骨架里的 class 和结构是从 renderTable / msgOpenBot 里抄的，
  那边改了记得同步（下面的 SAMPLE 里）。
"""
import io
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, 'panel_page.html')
OUT_HTML = os.path.join(HERE, '_shot_css.html')
OUT_PNG = os.path.join(HERE, '_shot_css.png')

CHROME_CANDIDATES = [
    r'C:\Program Files\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe',
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
]

# 跟真实表格一样的骨架。★ 结构要和 renderTable / msgOpenBot 保持一致
SAMPLE = u'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>%s</style></head><body>
<h1 style="font-size:16px;margin-bottom:12px">表格对齐预览（%s）</h1>

<div class="card">
  <h2 style="font-size:14px;margin-bottom:10px">机器人列表 · 客服（1 个）</h2>
  <div class="tblwrap"><table class="ctr">
    <tr><th>备注</th><th>机器人</th><th>绑定码</th><th>使用者</th><th>状态</th><th>到期</th><th>操作</th></tr>
    <tr><td><b>测试</b><div class="muted">09-29 01:00</div></td>
        <td>@@cckk73bot<div class="muted">测试</div></td>
        <td><span class="code">ABC12345</span></td>
        <td><span style="color:#3fb950">✅ 我</span></td>
        <td><span class="tag ok">运行中</span></td>
        <td><span style="color:#3fb950">剩 30 天</span></td>
        <td style="white-space:nowrap"><button class="gray sm">续期</button>
            <button class="gray sm">备注</button>
            <button class="red sm">删除</button></td></tr>
  </table></div>
</div>

<div class="card">
  <h2 style="font-size:14px;margin-bottom:10px">机器人列表 · 商城（1 个）</h2>
  <div class="tblwrap"><table class="ctr">
    <tr><th>备注</th><th>机器人</th><th>绑定码</th><th>使用者</th><th>状态</th><th>到期</th><th>待处理</th><th>操作</th></tr>
    <tr><td><b>能量充值</b></td><td>@trx7662bot</td><td><span class="code">XY789012</span></td>
        <td><span style="color:#3fb950">✅ 我</span></td>
        <td><span class="tag ok">运行中</span></td>
        <td><span style="color:#3fb950">剩 30 天</span></td>
        <td>3</td>
        <td style="white-space:nowrap"><button class="gray sm">续期</button>
            <button class="gray sm">⚙️ 配置</button>
            <button class="gray sm">备注</button>
            <button class="red sm">删除</button></td></tr>
  </table></div>
</div>

<div class="card">
  <h2 style="font-size:14px;margin-bottom:10px">消息记录 · 机器人列表</h2>
  <div class="tblwrap"><table class="ctr ctr-mid">
    <tr><th>机器人</th><th>未读</th><th>操作</th></tr>
    <tr><td><b>测试</b><div class="muted">@cckk73bot</div></td>
        <td><span class="badge">3</span></td>
        <td style="white-space:nowrap"><button class="gray sm">备注</button>
            <button class="gray sm">置顶</button></td></tr>
  </table></div>
</div>

<div class="card">
  <h2 style="font-size:14px;margin-bottom:10px">消息记录 · 群消息</h2>
  <div class="tblwrap"><table class="ctr ctr-mid">
    <tr><th>群</th><th>消息数</th><th>未读</th><th>操作</th></tr>
    <tr><td><b>测试群</b></td><td class="muted">4</td><td><span class="badge">2</span></td>
        <td style="white-space:nowrap"><button class="gray sm">关闭记录</button>
            <button class="gray sm">备注</button>
            <button class="gray sm">置顶</button></td></tr>
  </table></div>
</div>
</body></html>'''


def find_chrome():
    for p in CHROME_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def main():
    width = int(sys.argv[1]) if len(sys.argv) > 1 else 1400
    src = io.open(SRC, encoding='utf-8').read()
    m = re.search(r'<style>(.*?)</style>', src, re.S)
    if not m:
        raise SystemExit('!! panel_page.html 里找不到 <style>')
    label = '手机 %dpx' % width if width <= 720 else '电脑 %dpx' % width
    io.open(OUT_HTML, 'w', encoding='utf-8', newline='\n').write(
        SAMPLE % (m.group(1), label))

    chrome = find_chrome()
    if not chrome:
        raise SystemExit('!! 找不到 Chrome / Edge，装一个再来')
    subprocess.run([chrome, '--headless=new', '--disable-gpu', '--no-sandbox',
                    '--hide-scrollbars', '--window-size=%d,1200' % width,
                    '--screenshot=%s' % OUT_PNG,
                    'file:///' + OUT_HTML.replace('\\', '/')],
                   check=True, capture_output=True)
    print('出图：%s（%s）' % (OUT_PNG, label))
    print('★ 看一眼图，确认「操作」两个字落在按钮正中间')


if __name__ == '__main__':
    main()
