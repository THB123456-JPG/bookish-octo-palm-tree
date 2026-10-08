# -*- coding: utf-8 -*-
"""贵金属价排查工具 —— 抓不到价的时候先跑这个

用法：
    python _probe_metal.py

它会分别报告两个数据源通不通，并把卡片原样打出来。
★ 数据源要是哪天挂了/改版了，先跑这个，别去猜代码。
"""
import sys

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, __import__('os').path.dirname(__import__('os').path.abspath(__file__)))

from runners.ledger import metal as M


def main():
    print('一、上金所行情（新浪）')
    try:
        quotes = M.parse_sina(_get(M.SINA_URL, M.SINA_HEADERS))
        for code, label, _icon, _kg in M.SPECS:
            got = quotes.get(code)
            if got:
                print('   %-10s %s 元/克   （%s %s）'
                      % (label, got['price'], got['date'], got['time']))
            else:
                print('   %-10s ✗ 这个品种没数据' % label)
    except Exception as e:
        print('   ✗ 失败：%s: %s' % (type(e).__name__, e))
        print('   （接口是不是改版了？或是这台机器连不上新浪）')

    print()
    print('二、黄金回收参考价（极数本源）')
    rec = M.fetch_recycle()
    print('   %s' % ('%s 元/克' % rec if rec is not None else
                     '✗ 没抓到（这一行会不显示，不影响上面的行情价）'))

    print()
    print('三、群里会看到的卡片')
    print('-' * 40)
    try:
        q, r = M.fetch_quotes()
        print(M.format_card(q, r))
    except Exception as e:
        print('   抓不到，群里会回「贵金属价格获取失败」：%s' % e)


def _get(url, headers):
    import httpx
    with httpx.Client(timeout=10.0, headers=headers) as c:
        resp = c.get(url)
        resp.raise_for_status()
        resp.encoding = 'gbk'
        return resp.text


if __name__ == '__main__':
    main()
