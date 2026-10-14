"""Exercise permanent bill deletion against a disposable, fake-token server."""
from contextlib import closing
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import core
import miniapp
from _test_miniapp import setup_fixture, signed
from panel import PanelHandler
from runners.ledger.storage import LedgerStore

output = ROOT / 'output/playwright'
output.mkdir(exist_ok=True, parents=True)
previous = core.BASE_DIR
with tempfile.TemporaryDirectory(prefix='bill-delete-preview-') as folder:
    manager = setup_fixture(folder)
    with closing(LedgerStore(Path(core.DATA_DIR)/'ledger1.sqlite3', initialize=False)) as store:
        store.clear_entries(-10011)
        store.add_entry(-10011, 'income', '25', 'U', '当前账期', 111, '所有者')
        store.add_entry(-10022, 'income', '18', 'U', '另一群', 111, '所有者')
    server = ThreadingHTTPServer(('127.0.0.1', 0), PanelHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport=dict(width=390, height=844), color_scheme='dark')
            stub = ('window.Telegram={WebApp:{initData:' + json.dumps(signed(manager.find('ledger1')['token'], 111)) +
                    ',ready(){},expand(){},enableClosingConfirmation(){},disableClosingConfirmation(){},'
                    'BackButton:{onClick(){},show(){},hide(){}}}}')
            context.route('https://telegram.org/js/telegram-web-app.js',
                          lambda route: route.fulfill(content_type='text/javascript', body=stub))
            page = context.new_page()
            errors, writes = [], []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('request', lambda request: writes.append(request.post_data_json['payload'])
                    if request.url.endswith('/deletebills') else None)
            page.goto('http://127.0.0.1:%s/miniapp/ledger1' % server.server_port)
            expect(page.locator('.overview-metrics')).to_be_visible()
            page.get_by_role('button', name='账单', exact=True).click()
            expect(page.locator('[data-delete-bills]')).to_have_count(2)
            for width in (390, 320):
                page.set_viewport_size(dict(width=width, height=844))
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output / ('bill-delete-%s.png' % width)))
            page.locator('[data-delete-bills="-10011"]').click()
            expect(page.locator('#confirmation')).to_be_visible()
            expect(page.locator('#confirmationMessage')).to_contain_text('有权限测试群')
            expect(page.locator('#confirmationMessage')).to_contain_text('全部账期')
            expect(page.locator('#confirmationMessage')).to_contain_text('不受当前日期或筛选条件限制')
            page.screenshot(path=str(output / 'bill-delete-confirmation.png'))
            page.locator('#confirmCancel').click()
            assert not writes
            expect(page.locator('[data-bill-group]')).to_have_count(2)
            page.locator('[data-bill-group="-10011"] small').last.click()
            expect(page.locator('header h1')).to_have_text('群组账单')
            page.locator('#back').click()
            page.get_by_role('button', name='统计', exact=True).click()
            expect(page.locator('[data-delete-bills]')).to_have_count(2)
            for width in (390, 320):
                page.set_viewport_size(dict(width=width, height=844))
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output / ('stats-delete-%s.png' % width)))
            page.locator('[data-delete-bills="-10011"]').click()
            page.locator('#confirmAccept').click()
            expect(page.locator('#toast')).to_have_text('已删除 2 笔账单记录')
            expect(page.locator('[data-stats-group="-10011"]')).to_have_count(0)
            expect(page.locator('[data-stats-group="-10022"]')).to_contain_text('1 笔')
            page.get_by_role('button', name='账单', exact=True).click()
            expect(page.locator('[data-bill-group]')).to_have_count(1)
            expect(page.locator('[data-bill-group="-10022"]')).to_be_visible()
            context.route('**/deletebills', lambda route: route.fulfill(status=403,
                          json={'ok': False, 'error': '没有该群账单删除权限'}))
            page.locator('[data-delete-bills="-10022"]').click()
            page.locator('#confirmAccept').click()
            expect(page.locator('#toast')).to_have_text('没有该群账单删除权限')
            expect(page.locator('[data-bill-group="-10022"]')).to_be_visible()
            context.unroute('**/deletebills')
            page.locator('[data-delete-bills="-10022"]').click()
            page.locator('#confirmAccept').click()
            expect(page.locator('#toast')).to_have_text('已删除 1 笔账单记录')
            expect(page.locator('[data-bill-group]')).to_have_count(0)
            page.get_by_role('button', name='统计', exact=True).click()
            expect(page.locator('[data-stats-group]')).to_have_count(0)
            expect(page.locator('.stats-empty')).to_be_visible()
            assert writes == [{'chat_id': -10011, 'confirm': True},
                              {'chat_id': -10022, 'confirm': True},
                              {'chat_id': -10022, 'confirm': True}], writes
            with closing(LedgerStore(Path(core.DATA_DIR)/'ledger1.sqlite3', initialize=False)) as store:
                store.add_entry(-10011, 'income', '7', 'U', '重新入账', 111, '所有者')
                period = miniapp.month_bounds()[0] + 'T01:23:45+08:00'
                store.conn.execute('UPDATE entries SET accounting_date=? WHERE chat_id=?', (period, -10011))
                store.conn.commit()
                store.add_entry(-10011, 'payout', '7', 'U', '另一账期', 111, '所有者')
            page.locator('#refresh').click()
            expect(page.locator('[data-stats-group="-10011"]')).to_contain_text('2 笔')
            expect(page.locator('[data-stats-group="-10011"]')).to_contain_text('未下发 0.00U')
            page.get_by_role('button', name='账单', exact=True).click()
            page.locator('[data-bill-group="-10011"]').click()
            expect(page.locator('[data-bill]')).to_have_count(2)
            period_writes = []
            page.on('request', lambda request: period_writes.append(request.post_data_json['payload'])
                    if request.url.endswith('/deletebill') else None)
            for width in (390, 320):
                page.set_viewport_size(dict(width=width, height=844))
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output / ('bill-period-delete-%s.png' % width)))
            page.locator('[data-delete-period]').first.click()
            expect(page.locator('#confirmationMessage')).to_contain_text('其他账期保留')
            page.locator('#confirmCancel').click()
            assert not period_writes
            page.locator('[data-bill]').first.click()
            selected_period = page.evaluate('billData.bills.find(b=>billKey(b)===selectedBill).period')
            expect(page.locator('header h1')).to_have_text('账单明细')
            page.locator('[data-delete-period]').click()
            page.locator('#confirmAccept').click()
            expect(page.locator('header h1')).to_have_text('群组账单')
            expect(page.locator('[data-bill]')).to_have_count(1)
            expect(page.locator('#toast')).to_have_text('已删除 1 笔账单记录')
            assert period_writes == [dict(chat_id=-10011, period=selected_period, confirm=True)]
            page.locator('[data-delete-period]').click()
            page.locator('#confirmAccept').click()
            expect(page.locator('header h1')).to_have_text('账单')
            expect(page.locator('[data-bill-group]')).to_have_count(0)
            page.get_by_role('button', name='统计', exact=True).click()
            expect(page.locator('[data-stats-group]')).to_have_count(0)
            assert not errors, errors
            browser.close()
        print('Bill/stat deletion: whole group and single period, cancel, denial, isolation, nested return, '
              'empty group hiding, re-entry with zero balance, refresh and 320/390px passed.')
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        core.set_base_dir(previous)
