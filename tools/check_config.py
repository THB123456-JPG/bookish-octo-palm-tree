"""Exercise customer configuration against the fake-data HTTP preview."""
import json
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

root=Path(__file__).parent
auth=json.loads((root/'preview_auth.json').read_text(encoding='utf-8'))
output=root.parent/'output/playwright'
output.mkdir(parents=True,exist_ok=True)
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    context=browser.new_context(viewport={'width':390,'height':844})
    stub='window.Telegram={WebApp:{initData:'+json.dumps(auth['ledger1'])+',ready(){},expand(){},enableClosingConfirmation(){},disableClosingConfirmation(){},BackButton:{onClick(){},show(){}}}}'
    context.route('https://telegram.org/js/telegram-web-app.js',lambda r:r.fulfill(content_type='text/javascript',body=stub))
    api_base='http://127.0.0.1:8765/api/miniapp/ledger1/'
    def request(action,payload=None):
        response=context.request.post(api_base+action,data={'init_data':auth['ledger1'],'payload':payload or {}})
        assert response.ok,action
        return response.json()['data']
    current=request('overview')
    request('customer',dict(section='features',revision=current['customer']['revision'],value={row[1]:row[3] for row in current['feature_fields']}))
    for section in ('ads','replies'):
        current=request('overview')
        request('customer',dict(section=section,revision=current['customer']['revision'],value=[]))
    request('staff',dict(user_id=555,add=False))
    page=context.new_page()
    errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.on('dialog',lambda d:d.accept())
    page.goto('http://127.0.0.1:8765/miniapp/ledger1')
    expect(page.locator('#remaining')).to_be_visible()

    def click(name):
        if name=='返回概览':
            page.locator('#back').click()
        else:
            page.get_by_role('button',name=name,exact=True).click()

    def saved():
        page.wait_for_function('!busy')
        expect(page.locator('#toast')).to_have_text('配置已保存')

    def shot(name):
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),name
        page.screenshot(path=str(output/('config-'+name+'.png')),full_page=True)

    shot('overview')
    boxes=page.locator('.overview-metrics .metric').evaluate_all('(els)=>els.map(e=>e.getBoundingClientRect().top)')
    assert len(set(boxes))==1
    assert page.locator('.metric-link small').all_text_contents()==['记账群组','权限成员','自动回复','定时消息']
    def metrics_centered():
        assert page.locator('.metric-link').evaluate_all('els=>els.every(e=>getComputedStyle(e).textAlign==="center" && getComputedStyle(e).alignItems==="center" && getComputedStyle(e).justifyContent==="center")')
        assert page.locator('.metric-link b').evaluate_all('els=>els.every(e=>getComputedStyle(e).color==="rgb(78, 155, 225)")')
        assert page.locator('.overview-metrics .metric').evaluate_all('els=>els.every(e=>Math.abs(e.getBoundingClientRect().width-e.getBoundingClientRect().height)<1)')
    metrics_centered()
    page.locator('[data-go=basics]').click()
    assert page.locator('#basicsForm h2').all_text_contents() == ['账单默认值','进群欢迎','欢迎图片','退群提醒','下发地址']
    assert page.locator('#basicsForm').evaluate('(f)=>f.lastElementChild.classList.contains("actions") && f.lastElementChild.querySelector("button[type=submit]")?.textContent==="保存配置"')
    assert page.locator('#content').evaluate('(c)=>c.lastElementChild.id==="basicsForm"')
    expect(page.get_by_label('进群欢迎语',exact=True)).to_have_value('{name} 已加入该群。')
    expect(page.get_by_label('退群提醒内容',exact=True)).to_have_value('{name} 已离开该群。')
    expect(page.get_by_label('启用退群提醒',exact=True)).to_be_checked()
    page.get_by_label('启用退群提醒',exact=True).uncheck()
    page.get_by_label('退群提醒内容',exact=True).fill('再见{name}')
    page.get_by_label('日切时间（北京时间）').select_option('5')
    page.get_by_label('进群欢迎语',exact=True).fill('欢迎{name}，手机测试')
    page.get_by_label('先 @ 新人',exact=True).uncheck()
    click('保存配置');saved()
    expect(page.get_by_label('日切时间（北京时间）')).to_have_value('5')
    expect(page.get_by_label('启用退群提醒',exact=True)).not_to_be_checked()
    expect(page.get_by_label('退群提醒内容',exact=True)).to_have_value('再见{name}')
    shot('basics')
    click('返回概览')
    page.locator('[data-go=staff]').click()
    shot('staff')
    click('添加')
    page.get_by_label('Telegram ID',exact=True).fill('555')
    page.get_by_label('用户名／备注（可选）').fill('单群授权测试')
    page.get_by_label('单群操作',exact=True).check()
    page.locator('#editor input[name=staff_groups]').first.check()
    page.get_by_label('广播',exact=True).check()
    shot('staff-sheet')
    click('确认授权');saved()
    expect(page.locator('#editor')).not_to_be_visible()
    expect(page.get_by_text('单群操作（1群） · 广播',exact=False)).to_be_visible()
    page.locator('[data-edit-staff="555"]').click()
    expect(page.get_by_label('广播',exact=True)).to_be_checked()
    click('取消')
    click('返回概览')
    page.locator('[data-go=features]').click()
    expect(page.get_by_label('详细／简洁账单切换')).to_be_checked()
    page.get_by_label('入群通知',exact=True).check()
    page.get_by_label('详细／简洁账单切换').uncheck()
    click('保存配置');saved()
    expect(page.get_by_label('详细／简洁账单切换')).not_to_be_checked()
    assert page.get_by_text('查询火币汇率').count()==0
    assert page.get_by_text('查询两地距离').count()==0
    shot('features')
    click('返回概览')
    page.locator('.row[data-go=ads]').click()
    click('新增')
    page.get_by_label('标题',exact=True).fill('每日广告')
    page.get_by_label('每天时间（北京时间）').fill('10:30')
    page.get_by_label('广告内容',exact=True).fill('手机端广告测试')
    page.locator('#editor input[name=rule_groups]').first.check()
    click('保存规则');saved()
    expect(page.get_by_text('每日广告',exact=True)).to_be_visible()
    shot('ads')
    click('返回概览')
    page.locator('.row[data-go=replies]').click()
    click('新增')
    page.get_by_label('关键词',exact=True).fill('介绍')
    page.get_by_label('回复内容',exact=True).fill('欢迎使用机器人')
    page.locator('#editor input[name=rule_groups]').first.check()
    click('保存规则');saved()
    expect(page.get_by_text('介绍',exact=True)).to_be_visible()
    shot('replies')
    click('返回概览')
    for label in ['自动回复','定时消息']:
        expect(page.locator('.metric').filter(has=page.get_by_text(label,exact=True)).locator('b')).to_have_text('1')
    page.locator('.metric-link[data-go=users]').click()
    owner=page.locator('[data-user="111"]')
    expect(owner.locator('.tag')).to_have_count(4)
    assert page.get_by_text('<script>alert(1)</script>',exact=True).count()==1
    page.locator('[data-user="222"]').click()
    click('编辑人员授权')
    expect(page.get_by_label('Telegram ID',exact=True)).to_have_value('222')
    expect(page.get_by_label('单群操作',exact=True)).to_be_checked()
    click('取消')
    click('用户')
    owner.click()
    page.get_by_label('个人汇率',exact=True).fill('10')
    page.get_by_label('个人费率（%）',exact=True).fill('3')
    click('保存个人配置');saved()
    expect(page.get_by_label('个人汇率',exact=True)).to_have_value('10.0000')
    click('概览')
    page.locator('.metric-link[data-go=groups]').click()
    page.locator('[data-group]').first.click()
    page.get_by_label('本群默认币种',exact=True).select_option('U')
    click('保存配置');saved()
    expect(page.get_by_label('本群默认币种',exact=True)).to_have_value('U')
    click('账单')
    expect(page.locator('[data-bill-group]')).to_have_count(1)
    shot('bills')
    page.locator('[data-bill-group]').first.click()
    expect(page.locator('[data-bill]')).to_have_count(1)
    page.locator('[data-bill]').first.click()
    expect(page.get_by_text('流水明细 · 1',exact=True)).to_be_visible()
    expect(page.get_by_text('111.11',exact=True)).to_have_count(2)
    shot('bill-detail')
    click('统计')
    page.wait_for_function('statData!==null && !queryBusy')
    click('本月')
    page.wait_for_function('!queryBusy')
    expect(page.locator('.summary-cards .metric')).to_have_count(4)
    expect(page.locator('[data-stats-group]')).to_have_count(2)
    expect(page.get_by_text('111.11',exact=True)).to_have_count(2)
    shot('statistics')
    click('概览')
    for width in [320,390]:
        page.set_viewport_size({'width':width,'height':844})
        metrics_centered()
        for view in ['basics','staff','features','ads','replies']:
            page.locator('.row[data-go='+view+']').click()
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),(width,view)
            click('返回概览')
        for name in ['用户','群组','账单','统计']:
            click(name)
            page.wait_for_function('!queryBusy')
            assert page.evaluate('document.documentElement.scrollWidth<=innerWidth'),(width,name)
            click('概览')
    page.evaluate("document.documentElement.style.cssText='--tg-theme-bg-color:#1b2935;--tg-theme-secondary-bg-color:#101820;--tg-theme-text-color:#edf3f7;--tg-theme-hint-color:#91a5b1;--tg-theme-button-color:#4e9be1'")
    metrics_centered()
    shot('dark')
    assert not errors,errors
    browser.close()
    print('Customer UI: configuration saves, four clickable overview tiles, colored permission tags, personal/group settings, period bills/details, date statistics, 320/390px and dark theme passed.')
