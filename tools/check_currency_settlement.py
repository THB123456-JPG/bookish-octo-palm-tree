"""Render real calculated bills and check the Mini App using temporary data only."""
import json
from pathlib import Path
import sys
import threading
from http.server import ThreadingHTTPServer

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root))
from _test_currency_settlement import CurrencySettlementTests
from _test_miniapp import signed
from panel import PanelHandler
from runners.ledger.commands import format_bill
from playwright.sync_api import sync_playwright, expect

output = root / 'output/playwright'
output.mkdir(parents=True, exist_ok=True)
case = CurrencySettlementTests()
case.setUp()
server = None
try:
    st = case.r.store
    with st.conn:
        st.conn.execute('DELETE FROM entries')
    panels = []
    for cid, title, payout in ((-10011, 'RMB 入款 → U 下发', '100'), (-10022, 'U 入款 → RMB 下发', '1000')):
        case.cid = cid
        st.set_rate(cid, '11')
        st.set_fee_percent(cid, '0')
        st.set_ledger_view_mode(cid, 'compact')
        case.record('+2000/11')
        case.record('+2000/11')
        case.record('下发' + payout)
        panels.append(f'<section><h2>{title}</h2><div class="phone"><header>全能小跟班 · 记账示意</header><div class="chat"><div class="input">+2000/11（两笔）<br>下发{payout}</div><div class="bill">{format_bill(st, cid)}</div><div class="buttons"><span>今日</span><span>昨日</span></div><div class="buttons"><span>↪️ 切换详细</span></div></div></div></section>')
    html = '''<!doctype html><meta charset="utf-8"><style>
    *{box-sizing:border-box}body{margin:0;background:#edf2f5;font-family:"Microsoft YaHei",sans-serif;color:#243746}main{padding:28px;width:940px}h1{font-size:25px;margin:0 0 8px}p{font-size:13px;color:#657b8b;margin:0 0 22px}.panels{display:flex;gap:24px}section{width:430px}h2{font-size:18px;margin:0 0 12px}.phone{border:1px solid #d0d9df;border-radius:18px;overflow:hidden;background:linear-gradient(125deg,#ecedc1,#b5d8bd);min-height:590px}header{background:#fff;padding:18px;font-size:15px;font-weight:700}.chat{padding:16px}.input{font-size:13px;background:#eaffdf;border-radius:12px;padding:10px 14px;width:max-content;margin:0 0 16px auto;line-height:1.7}.bill{background:white;border-radius:14px;padding:14px 12px;font:14px/1.55 Arial,"Microsoft YaHei",sans-serif;white-space:pre-wrap;overflow-wrap:anywhere;width:max-content;max-width:100%}.bill a{color:#277da5;text-decoration:none}.buttons{display:flex;width:310px;max-width:100%;gap:3px;margin-top:3px}.buttons span{flex:1;text-align:center;padding:8px;background:#6496717d;color:#fff;border-radius:5px;font-size:13px}.note{margin:20px 0 0;color:#6c7d87;font-size:12px}</style><main><h1>入款与结算方向</h1><p>相同的 2000 入款、汇率 11，按群组模式计算对应结算金额。</p><div class="panels">'''+''.join(panels)+'''</div><p class="note">示意图：账单文字由实际代码和临时测试数据生成，字体、间距以 Telegram 客户端为准。</p></main>'''
    (output/'currency-settlement-demo.html').write_text(html, encoding='utf-8')
    server = ThreadingHTTPServer(('127.0.0.1', 0), PanelHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        mock = browser.new_page(viewport={'width': 940, 'height': 740}, device_scale_factor=1.5)
        mock.goto((output/'currency-settlement-demo.html').as_uri())
        mock.screenshot(path=str(output/'currency-settlement-demo.png'), full_page=True)
        context = browser.new_context(viewport={'width': 390, 'height': 844})
        auth = signed(case.bot['token'], 111)
        stub = 'window.Telegram={WebApp:{initData:'+json.dumps(auth)+',ready(){},expand(){},enableClosingConfirmation(){},disableClosingConfirmation(){},BackButton:{onClick(){},show(){},hide(){}}}}'
        context.route('https://telegram.org/js/telegram-web-app.js', lambda r: r.fulfill(content_type='text/javascript', body=stub))
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.goto(base+'/miniapp/ledger1')
        page.locator('#nav [data-go=stats]').click()
        expect(page.get_by_role('heading', name='RMB 结算', exact=True)).to_be_visible()
        expect(page.get_by_role('heading', name='U 结算', exact=True)).to_be_visible()
        expect(page.locator('[data-stats-group="-10022"]')).to_contain_text('应下发 44000.00RMB')
        page.screenshot(path=str(output/'currency-stats-mixed-390.png'), full_page=True)
        page.locator('[data-stats-group="-10022"]').click()
        expect(page.locator('.summary-cards')).to_have_count(1)
        expect(page.get_by_text('应下发（RMB）', exact=True)).to_be_visible()
        expect(page.locator('.summary-cards')).to_contain_text('43000')
        page.locator('.entry-record details').last.click()
        expect(page.locator('.entry-record').last).to_contain_text('已下发 1000.00RMB')
        for width in (320, 390):
            page.set_viewport_size({'width': width, 'height': 844})
            page.evaluate('window.scrollTo(0, 0)')
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'), width
            page.screenshot(path=str(output/f'currency-stats-rmb-{width}.png'), full_page=True)
        page.locator('#nav [data-go=bills]').click()
        page.locator('[data-bill-group="-10022"]').click()
        expect(page.locator('[data-bill]').first).to_contain_text('应下发 44000.00RMB')
        expect(page.locator('[data-bill]').first).to_contain_text('已下发 1000.00RMB · 未下发 43000.00RMB')
        page.locator('[data-bill]').first.click()
        expect(page.get_by_text('已下发（RMB）', exact=True)).to_be_visible()
        with st.conn:
            st.conn.execute('DELETE FROM entries WHERE chat_id=?', (-10022,))
        case.cid = -10022
        st.set_rate(case.cid, '6.67')
        case.mode('CNY')
        case.record('+100')
        case.mode('U')
        case.record('+100')
        mixed_panels = []
        for mode, title in (('compact', '简洁账单'), ('detailed', '详细账单')):
            st.set_ledger_view_mode(case.cid, mode)
            mixed_panels.append(f'<section><h2>{title}</h2><div class="phone"><header>记账机器人 · 切换模式示意</header><div class="chat"><div class="input">先入款 100 RMB<br>切换 U 模式，再入款 100 U</div><div class="bill">{format_bill(st, case.cid)}</div><div class="buttons"><span>今日</span><span>昨日</span></div><div class="buttons"><span>↪️ 切换账单</span></div></div></div></section>')
        mixed_html = html.replace(''.join(panels), ''.join(mixed_panels)).replace('入款与结算方向', '切换模式后，旧账保持原样').replace('相同的 2000 入款、汇率 11，按群组模式计算对应结算金额。', '同一账期：100 RMB ÷ 6.67 = 14.99 U；100 U × 6.67 = 667 RMB。')
        mixed_file = output/'mixed-mode-bill-demo.html'
        mixed_file.write_text(mixed_html, encoding='utf-8')
        mock.goto(mixed_file.as_uri())
        mock.screenshot(path=str(output/'mixed-mode-bill-demo.png'), full_page=True)
        page.goto(base+'/miniapp/ledger1')
        page.locator('#nav [data-go=stats]').click()
        expect(page.locator('[data-stats-group="-10022"]')).to_contain_text('应下发 14.99U ＋ 667.00RMB')
        page.locator('[data-stats-group="-10022"]').click()
        expect(page.locator('.entry-record')).to_have_count(2)
        expect(page.locator('.summary-cards')).to_have_count(2)
        for item, expected in zip(page.locator('.entry-record').all(), ('应下发 14.99U', '应下发 667.00RMB')):
            item.locator('summary').click()
            expect(item).to_contain_text(expected)
        for width in (320, 390):
            page.set_viewport_size({'width': width, 'height': 844})
            page.evaluate('window.scrollTo(0,0)')
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'), width
            page.screenshot(path=str(output/f'mixed-mode-miniapp-{width}.png'), full_page=True)
        page.locator('#nav [data-go=bills]').click()
        page.locator('[data-bill-group="-10022"]').click()
        expect(page.locator('[data-bill]').first).to_contain_text('应下发 14.99U ＋ 667.00RMB')
        page.locator('[data-bill]').first.click()
        expect(page.locator('.summary-cards')).to_have_count(2)
        assert not errors, errors
        browser.close()
    print('Currency bill and Mini App checks passed; 320/390 px layouts verified.')
    print(output/'currency-settlement-demo.png')
    print(output/'mixed-mode-bill-demo.png')
finally:
    if server:
        server.shutdown()
        server.server_close()
        thread.join()
    case.tearDown()
