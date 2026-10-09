"""Real mobile browser against the extracted customer package; all data is fake."""
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.request import urlopen
import zipfile

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from _test_solo_config import CLIENT, SoloConfigTests
from _test_miniapp import signed

test = SoloConfigTests()
test.setUp()
try:
    with urlopen(test.url + '/api/bots/ledger1/solozip?url=' + test.url) as response:
        package = response.read()
    with tempfile.TemporaryDirectory(prefix='solo-browser-') as folder:
        with zipfile.ZipFile(io.BytesIO(package)) as archive:
            archive.extractall(folder)
        process = subprocess.Popen([sys.executable, '-u', '-c', CLIENT], cwd=Path(folder)/'记账独立版',
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding='utf-8')
        try:
            while process.stdout.readline().strip() != 'READY':
                if process.poll() is not None:
                    raise AssertionError(process.stderr.read())
            assert test.call('staff', dict(user_id=555, name='客户人员', add=True))[0] == 200
            assert test.call('tronkeys', dict(keys=['CUSTOM_FAKE_QUERY_KEY']))[0] == 200
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                for uid in (111, 222, 999):
                    context = browser.new_context(viewport=dict(width=390, height=844))
                    auth = signed(test.bot['token'], uid)
                    stub = 'window.Telegram={WebApp:{initData:'+json.dumps(auth)+',ready(){},expand(){},enableClosingConfirmation(){},disableClosingConfirmation(){},BackButton:{onClick(){},show(){},hide(){}}}}'
                    context.route('https://telegram.org/js/telegram-web-app.js', lambda r: r.fulfill(content_type='text/javascript', body=stub))
                    page = context.new_page()
                    errors = []
                    page.on('pageerror', lambda e: errors.append(str(e)))
                    page.goto(test.url + '/miniapp/ledger1')
                    expect(page.locator('#accessScope')).to_be_visible()
                    page.locator('.row[data-go=basics]').click()
                    if uid == 111:
                        page.get_by_label('进群欢迎语', exact=True).fill('客户自建小程序验证{name}')
                        page.get_by_label('日切时间（北京时间）').select_option('5')
                        page.get_by_role('button', name='保存配置', exact=True).click()
                        expect(page.locator('#toast')).to_have_text('配置已保存')
                        page.locator('#refresh').click()
                        expect(page.locator('#toast')).to_have_text('已刷新')
                        expect(page.get_by_label('进群欢迎语', exact=True)).to_have_value('客户自建小程序验证{name}')
                    else:
                        expect(page.locator('#content')).to_contain_text('功能预览')
                        assert page.locator('#content input:enabled, #content textarea:enabled, #content select:enabled').count() == 0
                    page.locator('#nav [data-go=groups]').click()
                    expect(page.locator('#title')).to_have_text('群组')
                    assert page.locator('[data-group]').count() == {111:2, 222:1, 999:0}[uid]
                    if uid == 111:
                        page.locator('[data-group="-10011"]').click()
                        page.get_by_label('固定汇率', exact=True).fill('7')
                        page.get_by_role('button', name='保存配置', exact=True).click()
                        expect(page.locator('#toast')).to_have_text('配置已保存')
                        expect(page.get_by_label('固定汇率', exact=True)).to_have_value('7.0000')
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    assert not errors, errors
                    context.close()
                browser.close()
            print('Customer package mobile UI: save/refresh, owner/group-operator/visitor scopes and overflow passed.', flush=True)
        finally:
            stdout, stderr = process.communicate('\n', timeout=22)
            assert process.returncode == 0, stderr
            assert 'PERSISTED' in stdout
finally:
    test.tearDown()
