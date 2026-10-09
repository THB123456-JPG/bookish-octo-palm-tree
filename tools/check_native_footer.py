"""Exercise Telegram's actual SDK with a fake native bridge and fake bot data."""
import json
from pathlib import Path
from urllib.parse import urlencode

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]


def main():
    auth = json.loads((ROOT / 'tools/preview_auth.json').read_text(encoding='utf-8'))['ledger1']
    sdk = (ROOT / 'output/telegram_sdk_reference.js').read_text(encoding='utf-8')
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        for width in (320, 390):
            context = browser.new_context(viewport=dict(width=width, height=844))
            context.add_init_script("window.__nativeCalls=[];window.TelegramWebviewProxy={postEvent(name,data){window.__nativeCalls.push({name,data:JSON.parse(data||'{}')})}};")
            context.route('https://telegram.org/js/telegram-web-app.js', lambda route: route.fulfill(content_type='text/javascript', body=sdk))
            page = context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            params = urlencode(dict(tgWebAppData=auth, tgWebAppVersion='8.0', tgWebAppPlatform='tdesktop'))
            page.goto('http://127.0.0.1:8765/miniapp/ledger1#' + params)
            expect(page.locator('#accessScope')).to_be_visible()
            expect(page.locator('#appFooter')).to_have_count(0)
            assert page.evaluate("Telegram.WebApp.MainButton.text") == '联系开发者@HCW2026'
            assert page.evaluate("Telegram.WebApp.MainButton.isVisible")
            assert page.locator('#nav').evaluate('node => node.getBoundingClientRect().height') <= 60
            assert page.locator('body').evaluate('node => node.scrollWidth <= innerWidth')
            # Rerenders must not register repeated click handlers.
            page.evaluate("render();render();window.__nativeCalls=[];Telegram.WebView.receiveEvent('main_button_pressed')")
            calls = page.evaluate("__nativeCalls.filter(call=>call.name==='web_app_open_tg_link')")
            assert len(calls) == 1 and calls[0]['data']['path_full'] == '/HCW2026'
            page.evaluate("data.footer_text='自定义联系';render()")
            assert page.evaluate("Telegram.WebApp.MainButton.text") == '自定义联系'
            page.evaluate("data.footer_text='';render()")
            assert not page.evaluate("Telegram.WebApp.MainButton.isVisible")
            page.evaluate("data.footer_text='联系开发者@HCW2026';render()")
            assert page.evaluate("Telegram.WebApp.MainButton.isVisible")
            assert not errors, errors
            output = ROOT / 'output/playwright'
            output.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(output / f'compact-native-footer-{width}.png'))
            context.close()
        browser.close()
    print('Actual Telegram SDK: native footer text, no duplicate HTML row, compact navigation, contact click deduplication, hide/restore passed.')


if __name__ == '__main__':
    main()
