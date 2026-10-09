"""Browser regressions for footer duplication, local filtering and compact layout."""
import json
from pathlib import Path
import sys

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
auth = json.loads((ROOT/'tools/preview_auth.json').read_text(encoding='utf-8'))['ledger1']
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(viewport=dict(width=390, height=844))
    stub = ('window.Telegram={WebApp:{initData:'+json.dumps(auth)+
            ',ready(){},expand(){},enableClosingConfirmation(){},disableClosingConfirmation(){},'
            'onEvent(name,fn){window[name]=fn},BackButton:{onClick(){},show(){},hide(){}}}}')
    context.route('https://telegram.org/js/telegram-web-app.js',
                  lambda route: route.fulfill(content_type='text/javascript', body=stub))
    if '--baseline' in sys.argv:
        context.route('**/miniapp/ledger1', lambda route: route.fulfill(content_type='text/html',
            body=(ROOT/'output/audit_before/miniapp_page.html').read_text(encoding='utf-8')))
    page = context.new_page()
    errors, requests = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('request', lambda request: requests.append(request.url) if '/api/miniapp/' in request.url else None)
    page.goto('http://127.0.0.1:8765/miniapp/ledger1')
    expect(page.locator('#accessScope')).to_be_visible()
    page.evaluate('themeChanged();themeChanged()')
    assert page.locator('#appFooter').count() == 1, 'Theme changes duplicated the footer'
    for width in (320, 390, 430):
        page.set_viewport_size(dict(width=width, height=844))
        assert page.locator('.overview-grid .row').count() == 6
        boxes = page.locator('.overview-grid .row').evaluate_all('els=>els.map(e=>({x:e.offsetLeft,y:e.offsetTop,w:e.offsetWidth,h:e.offsetHeight}))')
        assert all(boxes[i]['y'] == boxes[i+1]['y'] and abs(boxes[i]['w']-boxes[i+1]['w']) <= 1 for i in (0, 2, 4))
        assert page.locator('.overview-grid small').evaluate_all('els=>els.every(e=>e.getBoundingClientRect().height<=parseFloat(getComputedStyle(e).lineHeight)+1)')
        title = page.locator('#title').bounding_box()
        assert abs(title['x']+title['width']/2-width/2) < 1
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.set_viewport_size(dict(width=390, height=844))
    page.screenshot(path=str(ROOT/'output/playwright/audit-overview.png'), full_page=True)
    page.locator('.row[data-go=staff]').click()
    before = len(requests)
    page.locator('#search').fill('not-a-real-user')
    page.locator('#searchBtn').click()
    page.wait_for_timeout(250)
    assert len(requests) == before, 'Local search made an unnecessary overview request'
    page.locator('#back').click()
    page.locator('.row[data-go=basics]').click()
    widths = page.locator('#basicsForm > .actions button').evaluate_all('els=>els.map(e=>e.getBoundingClientRect().width)')
    assert len(widths) == 2 and abs(widths[0]-widths[1]) <= 1
    page.locator('#back').click()
    page.get_by_role('button', name='账单', exact=True).click()
    page.locator('[data-bill-group]').first.click()
    page.locator('[data-bill]').first.click()
    expect(page.locator('.entry-record')).to_have_count(1)
    expect(page.locator('.entry-record details')).not_to_have_attribute('open', '')
    assert page.locator('.summary-cards .metric').evaluate_all('els=>els.every(e=>getComputedStyle(e).textAlign==="center")')
    page.screenshot(path=str(ROOT/'output/playwright/audit-bill.png'), full_page=True)
    page.get_by_text('计算明细', exact=True).click()
    expect(page.locator('.entry-record details')).to_contain_text('汇率')
    for width in (320, 390, 430):
        page.set_viewport_size(dict(width=width, height=844))
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.emulate_media(color_scheme='dark')
    page.set_viewport_size(dict(width=390, height=844))
    page.screenshot(path=str(ROOT/'output/playwright/audit-bill-dark.png'), full_page=True)
    assert page.locator('body').evaluate('e=>getComputedStyle(e).backgroundColor') == 'rgb(0, 0, 0)'
    assert not errors, errors
    browser.close()
print('Footer deduplication, zero-request local search, symmetric layout, collapsed financial detail and light/dark mobile views passed.')
