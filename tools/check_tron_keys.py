"""Exercise separate key bindings in a mobile browser with fake API responses."""
import json
from pathlib import Path
import sys
from unittest.mock import patch

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from _test_miniapp import signed
from _test_tron_miniapp import Tests, tc

test = Tests()
test.setUp()
try:
    with patch.object(tc, 'probe', return_value=(True, 'fake validated')), sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport=dict(width=390, height=844))
        auth = signed(test.mgr.find('ledger1')['token'], 111)
        stub = 'window.Telegram={WebApp:{initData:'+json.dumps(auth)+',ready(){},expand(){},enableClosingConfirmation(){},disableClosingConfirmation(){},BackButton:{onClick(){},show(){},hide(){}}}}'
        context.route('https://telegram.org/js/telegram-web-app.js',
                      lambda route: route.fulfill(content_type='text/javascript', body=stub))
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto('http://127.0.0.1:%s/miniapp/ledger1' % test.server.server_port)
        page.locator('[data-go=tron]').click()
        keys = ['FAKE_BROWSER_KEY_0001', 'FAKE_BROWSER_KEY_0002']
        for value, count in ((keys[0], 1), (keys[1], 2), (keys[1], 2)):
            page.locator('#tron_keys').fill(value)
            page.locator('#tronKeysForm button').click()
            page.wait_for_function('!busy')
            expect(page.locator('#tronKeyStatus')).to_have_text('已绑定 %s 把独立 Key。' % count)
        page.locator('#refresh').click()
        page.wait_for_function('!busy')
        expect(page.locator('#tronKeyStatus')).to_have_text('已绑定 2 把独立 Key。')
        expect(page.locator('#content')).to_contain_text('…0001 · …0002')
        assert all(key not in page.locator('#content').inner_text() for key in keys)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        output = ROOT/'output/playwright'
        output.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(output/'tron-keys-appended.png'), full_page=True)
        assert test.runner.tronw.custom_keys() == keys
        assert not errors, errors
        browser.close()
    print('Mobile browser: separate bindings, duplicate key, refresh, masking and layout passed.')
finally:
    test.tearDown()
