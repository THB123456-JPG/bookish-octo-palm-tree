"""Real touch gestures against fake-data Mini App and mocked mobile panel APIs."""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
auth = json.loads((ROOT / 'tools/preview_auth.json').read_text())['ledger1']


def swipe(page, x=30, y=30, dx=220, dy=0, cancel=False, multi=False):
    session = page.context.new_cdp_session(page)
    points = lambda i: [dict(x=x+dx*i/6, y=y+dy*i/6, id=1)]
    session.send('Input.dispatchTouchEvent', dict(type='touchStart', touchPoints=points(0)))
    for i in range(1, 7):
        touches = points(i)
        if multi:
            touches.append(dict(x=x+dx*i/6+30, y=y+dy*i/6+30, id=2))
        session.send('Input.dispatchTouchEvent', dict(type='touchMove', touchPoints=touches))
        page.wait_for_timeout(20)
    session.send('Input.dispatchTouchEvent', dict(type='touchCancel' if cancel else 'touchEnd', touchPoints=[]))
    session.detach()
    page.wait_for_timeout(100)


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(viewport=dict(width=390, height=844), is_mobile=True, has_touch=True)
    stub = ('window.Telegram={WebApp:{initData:' + json.dumps(auth) +
            ',ready(){},expand(){},enableClosingConfirmation(){},disableClosingConfirmation(){},'
            'BackButton:{onClick(){},show(){},hide(){}}}}')
    context.route('https://telegram.org/js/telegram-web-app.js',
                  lambda r: r.fulfill(content_type='text/javascript', body=stub))
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto('http://127.0.0.1:8765/miniapp/ledger1')
    expect(page.locator('.overview-metrics')).to_be_visible()
    swipe(page)
    expect(page.locator('#title')).to_have_text('配置中心')
    page.get_by_role('button', name='账单', exact=True).click()
    page.wait_for_function('billData!==null && !queryBusy')
    page.locator('#bill_search').fill('有权限')
    page.locator('#billQuery').click()
    page.wait_for_function('!queryBusy')
    page.locator('[data-bill-group]').click()
    page.locator('[data-bill]').first.click()
    for args in (dict(dx=20), dict(dx=5, dy=160), dict(x=260, dx=-180), dict(cancel=True), dict(multi=True)):
        swipe(page, **args)
        expect(page.locator('#title')).to_have_text('账单明细')
    for title in ('群组账单', '账单'):
        swipe(page)
        expect(page.locator('#title')).to_have_text(title)
    expect(page.locator('#bill_search')).to_have_value('有权限')
    swipe(page)
    expect(page.locator('#title')).to_have_text('配置中心')
    page.get_by_role('button', name='用户', exact=True).click()
    page.locator('#search').fill('222')
    page.locator('[data-user="222"]').click()
    page.get_by_label('个人汇率', exact=True).fill('5')
    swipe(page, y=80)
    expect(page.locator('#confirmation')).to_be_visible()
    page.locator('#confirmCancel').click()
    expect(page.get_by_label('个人汇率', exact=True)).to_have_value('5')
    swipe(page, y=80)
    page.locator('#confirmAccept').click()
    expect(page.locator('#title')).to_have_text('用户')
    expect(page.locator('#search')).to_have_value('222')
    page.locator('#search').scroll_into_view_if_needed()
    box = page.locator('#search').bounding_box()
    swipe(page, x=box['x']+20, y=box['y']+15, dx=150)
    expect(page.locator('#title')).to_have_text('用户')
    assert not errors, errors
    page.close()

    # No live panel login or real bot API: all panel requests are intercepted.
    panel = context.new_page()
    panel.on('pageerror', lambda e: errors.append(str(e)))
    panel.route('**/api/**', lambda r: r.fulfill(json={
        'ok': True, 'me': {'role': 'admin'}, 'bots': [
            {'id': 'ledger1', 'type': 'ledger', 'note': '模拟记账', 'username': 'fake', 'enabled': True}],
        'chats': [{'chat_id': -10011, 'title': '模拟群', 'count': 0}], 'messages': [], 'nodes': [],
        'merchants': [], 'logins': [], 'stats': {}, 'unread': {}, 'base': {},
    }))
    panel.add_init_script("localStorage.setItem('panel_pw','FAKE_PANEL');localStorage.removeItem('panel_tab')")
    panel.goto('http://127.0.0.1:8765/')
    expect(panel.locator('#main')).to_be_visible()
    panel.locator('#msgBots tr[onclick] td').first.click()
    expect(panel.locator('#msgLv2')).to_be_visible()
    panel.locator('#msgChats tr[onclick] td').first.click()
    expect(panel.locator('#msgLv3')).to_be_visible()
    panel.wait_for_function('!MSG.LOADING')
    swipe(panel, y=300)
    expect(panel.locator('#msgLv2')).to_be_visible()
    swipe(panel, y=160)
    expect(panel.locator('#msgLv1')).to_be_visible()
    swipe(panel, y=160)
    expect(panel.locator('#msgLv1')).to_be_visible()
    panel.locator('[data-pane="bots"]').click()
    panel.locator('[data-pane="help"]').click()
    swipe(panel, y=200)
    expect(panel.locator('#pane-bots')).to_be_visible()
    box = panel.locator('#tabs').bounding_box()
    swipe(panel, x=80, y=box['y']+15, dx=150)
    expect(panel.locator('#pane-bots')).to_be_visible()
    panel.evaluate("openExt('ledger1','模拟记账')")
    swipe(panel, y=220)
    expect(panel.locator('#extModal')).not_to_be_visible()
    expect(panel.locator('#pane-bots')).to_be_visible()
    panel.evaluate('openChpass(true)')
    swipe(panel, y=180)
    expect(panel.locator('#chpassModal')).to_be_visible()
    panel.evaluate('cpForce=false;closeChpass()')
    panel.evaluate('openChpass(false)')
    panel.locator('#cp_new').fill('temporary-draft')
    panel.once('dialog', lambda dialog: dialog.dismiss())
    swipe(panel, y=180)
    expect(panel.locator('#cp_new')).to_have_value('temporary-draft')
    panel.once('dialog', lambda dialog: dialog.accept())
    swipe(panel, y=180)
    expect(panel.locator('#chpassModal')).not_to_be_visible()
    swipe(panel, y=200)
    expect(panel.locator('#pane-msg')).to_be_visible()
    assert not errors, errors
    browser.close()
    print('Touch back: Mini App nested navigation/search/draft confirmation; panel messages/tabs/modals; '
          'root, short/left/vertical/cancelled/multitouch gestures, input and horizontal scroll exclusions passed.')
