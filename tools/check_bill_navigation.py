"""Bill periods and statistics drill-down, against isolated fake-data preview."""
import copy
import json
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

root = Path(__file__).resolve().parent
auth = json.loads((root / 'preview_auth.json').read_text(encoding='utf-8'))['ledger1']
output = root.parent / 'output/playwright'
output.mkdir(parents=True, exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(viewport=dict(width=390, height=844))
    stub = ('window.Telegram={WebApp:{initData:' + json.dumps(auth) +
            ',ready(){},expand(){},enableClosingConfirmation(){},disableClosingConfirmation(){},'
            'BackButton:{onClick(fn){window.telegramBack=fn},show(){},hide(){}}}}')
    context.route('https://telegram.org/js/telegram-web-app.js',
                  lambda route: route.fulfill(content_type='text/javascript', body=stub))

    def two_periods(route):
        response = route.fetch()
        result = response.json()
        bills = result['data']['bills']
        assert bills
        extra = copy.deepcopy(bills[0])
        extra['period'] = '2026-10-08'
        bills[0]['period'] = '2026-10-09T00:00:00+08:00'
        bills.append(extra)
        route.fulfill(response=response, json=result)

    context.route('**/api/miniapp/ledger1/bills', two_periods)
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto('http://127.0.0.1:8765/miniapp/ledger1')
    expect(page.locator('.overview-metrics')).to_be_visible()
    page.get_by_role('button', name='账单', exact=True).click()
    expect(page.locator('[data-bill-group]')).to_have_count(1)
    expect(page.locator('[data-bill-group]')).to_contain_text('本月 2 个账期')
    expect(page.locator('[data-bill-group]')).not_to_contain_text('›')
    expect(page.locator('[data-bill-group]')).not_to_contain_text('T00')
    page.locator('#bill_search').fill('有权限')
    page.locator('#billQuery').click()
    expect(page.locator('[data-bill-group]')).to_have_count(1)
    page.locator('[data-bill-group] small').last.click()
    expect(page.locator('header h1')).to_have_text('群组账单')
    expect(page.locator('[data-bill]')).to_have_count(2)
    expect(page.locator('[data-bill]').first).not_to_contain_text('›')
    page.locator('[data-bill]').first.click()
    expect(page.locator('header h1')).to_have_text('账单明细')
    expect(page.get_by_role('heading', name='流水明细 · 1')).to_be_visible()
    selected = page.evaluate('selectedBill')
    page.locator('#refresh').click()
    page.wait_for_function('!busy')
    expect(page.locator('header h1')).to_have_text('账单明细')
    assert page.evaluate('selectedBill') == selected
    expect(page.get_by_role('heading', name='流水明细 · 1')).to_be_visible()
    expect(page.locator('#nav [aria-current=page]')).to_have_text('▤账单')
    page.locator('#back').click()
    expect(page.locator('header h1')).to_have_text('群组账单')
    page.evaluate('void telegramBack()')
    expect(page.locator('header h1')).to_have_text('账单')
    expect(page.locator('#bill_search')).to_have_value('有权限')
    expect(page.locator('[data-bill-group]')).to_have_count(1)
    page.get_by_role('button', name='统计', exact=True).click()
    expect(page.locator('[data-stats-group]')).to_have_count(2)
    group = page.locator('[data-stats-group="-10011"]')
    expect(group).not_to_contain_text('›')
    group.locator('small').click()
    expect(page.locator('header h1')).to_have_text('群组统计')
    expect(page.get_by_role('heading', name='流水明细 · 1')).to_be_visible()
    expect(page.locator('.record')).to_have_count(1)
    page.locator('#refresh').click()
    page.wait_for_function('!busy')
    expect(page.locator('header h1')).to_have_text('群组统计')
    expect(page.get_by_role('heading', name='流水明细 · 1')).to_be_visible()
    assert page.evaluate('statDetail.groups[0].chat_id') == -10011
    page.evaluate('void telegramBack()')
    expect(page.locator('header h1')).to_have_text('统计')
    expect(page.locator('#stats_group')).to_have_value('')
    page.locator('[data-stats-group="-10022"]').click()
    expect(page.locator('header h1')).to_have_text('群组统计')
    expect(page.get_by_text('所选日期范围暂无流水。')).to_be_visible()
    page.locator('#back').click()
    for width in (390, 320):
        page.set_viewport_size(dict(width=width, height=844))
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(output / 'statistics-clickable-groups.png'))
    assert not errors, errors
    browser.close()
    print('Bills: one row per group, separate periods, nested back and search retained. '
          'Statistics: whole-row detail, zero-record group, back and mobile widths passed.')
