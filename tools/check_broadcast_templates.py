"""Exercise broadcast template layout against the temporary-data preview server."""
import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

root = Path(__file__).resolve().parent
auth = json.loads((root/'preview_auth.json').read_text(encoding='utf-8'))
output = root.parent/'output/playwright'
output.mkdir(parents=True, exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(viewport={'width': 390, 'height': 844})
    stub = 'window.Telegram={WebApp:{initData:'+json.dumps(auth['ledger1'])+',ready(){},expand(){},enableClosingConfirmation(){},disableClosingConfirmation(){},BackButton:{onClick(){},show(){},hide(){}}}}'
    context.route('https://telegram.org/js/telegram-web-app.js', lambda r: r.fulfill(content_type='text/javascript', body=stub))
    base = 'http://127.0.0.1:8765'
    def request(action, payload=None):
        r = context.request.post(base+'/api/miniapp/ledger1/'+action,
            data={'init_data': auth['ledger1'], 'payload': payload or {}})
        assert r.ok, r.status
        return r.json()['data']
    cfg = request('overview')['customer']
    request('customer', dict(section='broadcast_templates', value=[], revision=cfg['revision']))
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(base+'/miniapp/ledger1')
    page.locator('[data-go=broadcast_templates]').click()
    expect(page.get_by_text('不使用按钮（默认）', exact=True)).to_be_visible()
    page.get_by_role('button', name='新增模板', exact=True).click()
    page.get_by_label('模板名称', exact=True).fill('客户服务')
    row = page.locator('.template-button-row').first
    row.get_by_label('按钮1文字', exact=True).fill('联系客服')
    row.get_by_label('按钮1链接', exact=True).fill('https://t.me/example')
    expect(page.locator('#templatePreview .broadcast-button')).to_have_text('联系客服 ↗')
    row.get_by_role('button', name='向下增加按钮', exact=True).click()
    second = page.locator('.template-button-row').nth(1)
    second.get_by_role('button', name='向右增加按钮', exact=True).last.click()
    second.get_by_role('button', name='向右增加按钮', exact=True).last.click()
    expect(second.get_by_role('button', name='向右增加按钮', exact=True).first).to_be_disabled()
    for i, label in enumerate(('官网', '帮助', '活动'), 1):
        second.get_by_label(f'按钮{i}文字', exact=True).fill(label)
        second.get_by_label(f'按钮{i}链接', exact=True).fill('https://example.com/'+str(i))
    assert page.locator('#templatePreview .broadcast-button-row').evaluate_all('(rows)=>rows.map(r=>r.children.length)') == [1,3]
    second.get_by_role('button', name='向下增加按钮', exact=True).last.click()
    third = page.locator('.template-button-row').nth(2)
    third.get_by_role('button', name='向右增加按钮', exact=True).first.click()
    assert page.locator('#templatePreview .broadcast-button-row').evaluate_all('(rows)=>rows.map(r=>r.children.length)') == [1,3,2]
    third.get_by_role('button', name='删除此按钮').last.click()
    third.get_by_role('button', name='删除此按钮').last.click()
    page.evaluate("document.documentElement.style.cssText='--tg-theme-bg-color:#17212b;--tg-theme-secondary-bg-color:#0e1621;--tg-theme-text-color:#edf3f7;--tg-theme-hint-color:#91a5b1;--tg-theme-button-color:#4e9be1'")
    for width in (320,390):
        page.set_viewport_size({'width':width,'height':844})
        page.locator('#templatePreview').scroll_into_view_if_needed()
        assert page.locator('#editor').evaluate('(e)=>e.scrollWidth<=e.clientWidth+1')
        assert page.locator('#templatePreview .broadcast-button-row').evaluate_all('(rows)=>rows.every(r=>r.scrollWidth<=r.clientWidth+1)')
        page.screenshot(path=str(output/f'broadcast-template-editor-{width}.png'))
    page.get_by_role('button', name='保存模板', exact=True).click()
    expect(page.locator('#editor')).not_to_be_visible()
    expect(page.get_by_text('不使用按钮（默认）', exact=True)).to_be_visible()
    saved = request('overview')['customer']
    assert saved['broadcast_template_id'] == ''
    assert [len(r) for r in saved['broadcast_templates'][0]['buttons']] == [1,3]
    page.get_by_role('button', name='用于广播', exact=True).click()
    expect(page.get_by_role('button', name='已用于广播', exact=True)).to_be_disabled()
    assert request('overview')['customer']['broadcast_template_id'] == saved['broadcast_templates'][0]['id']
    page.screenshot(path=str(output/'broadcast-template-list.png'), full_page=True)
    page.get_by_role('button', name='编辑', exact=True).click()
    page.locator('.template-button-row').first.get_by_label('按钮1文字', exact=True).fill('专属客服')
    expect(page.locator('#templatePreview .broadcast-button').first).to_have_text('专属客服 ↗')
    page.locator('#closeEditor').click()
    expect(page.locator('#confirmation')).to_be_visible()
    page.locator('#confirmCancel').click()
    expect(page.locator('#editor')).to_be_visible()
    page.get_by_role('button', name='保存模板', exact=True).click()
    expect(page.locator('#editor')).not_to_be_visible()
    page.locator('#refresh').click()
    expect(page.locator('#content .broadcast-button').first).to_have_text('专属客服 ↗')
    page.get_by_role('button', name='不使用按钮', exact=True).click()
    expect(page.get_by_text('不使用按钮（默认）', exact=True)).to_be_visible()
    assert request('overview')['customer']['broadcast_template_id'] == ''
    page.get_by_role('button', name='用于广播', exact=True).click()
    expect(page.get_by_role('button', name='已用于广播', exact=True)).to_be_disabled()
    page.get_by_role('button', name='删除', exact=True).click()
    page.locator('#confirmCancel').click()
    expect(page.locator('[data-edit-template]')).to_have_count(1)
    page.get_by_role('button', name='删除', exact=True).click()
    page.locator('#confirmAccept').click()
    expect(page.locator('[data-edit-template]')).to_have_count(0)
    cfg = request('overview')['customer']
    assert cfg['broadcast_templates'] == [] and cfg['broadcast_template_id'] == ''
    assert not errors, errors
    browser.close()
print('Broadcast templates: live 1/2/3-column rows, add/remove, save/edit, selection, disable/delete, draft guard and 320/390px passed.')
