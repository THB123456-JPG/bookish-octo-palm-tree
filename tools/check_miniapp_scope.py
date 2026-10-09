"""Check role-specific UI, duplicate queries and obsolete requests in a real browser."""
import asyncio
import json
from pathlib import Path
import sys

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from _test_miniapp import signed


async def main():
    auth = json.loads((ROOT/'tools/preview_auth.json').read_text(encoding='utf-8'))
    output = ROOT/'output/playwright'
    output.mkdir(exist_ok=True, parents=True)
    url = 'http://127.0.0.1:8765'
    roles = [
        (590, 'manage', dict(manage=True, global_ops=False, broadcast=False, groups=[])),
        (591, 'global', dict(manage=False, global_ops=True, broadcast=False, groups=[])),
        (592, 'single', dict(manage=False, global_ops=False, broadcast=False, groups=[-10011])),
        (593, 'broadcast', dict(manage=False, global_ops=False, broadcast=True, groups=[])),
        (594, 'combined', dict(manage=True, global_ops=False, broadcast=True, groups=[-10011]))]
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        setup = await browser.new_context()
        for uid, name, grants in roles:
            response = await setup.request.post(url+'/api/miniapp/ledger1/staff', data=dict(
                init_data=auth['ledger1'], payload=dict(user_id=uid, add=True, name=name, grants=grants)))
            assert response.ok, await response.text()
        await setup.close()
        for uid, name, grants in [(111, 'owner', dict(manage=True, global_ops=True, broadcast=True, groups=[])), *roles, (999, 'visitor', dict(manage=False, global_ops=False, broadcast=False, groups=[]))]:
            context = await browser.new_context(viewport=dict(width=390, height=844))
            login = auth['ledger1'] if uid == 111 else signed('111:FAKE_LEDGER', uid)
            stub = 'window.Telegram={WebApp:{initData:'+json.dumps(login)+',ready(){},expand(){},enableClosingConfirmation(){},disableClosingConfirmation(){},BackButton:{onClick(){},show(){},hide(){}}}}'
            await context.route('https://telegram.org/js/telegram-web-app.js',
                                lambda route: route.fulfill(content_type='text/javascript', body=stub))
            page = await context.new_page()
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            await page.goto(url+'/miniapp/ledger1')
            await expect(page.locator('#accessScope')).to_be_visible()
            assert await page.locator('.row[data-go=basics]').count() == 1
            assert await page.locator('.row[data-go=features]').count() == 1
            assert await page.locator('.row[data-go=staff]').count() == 1
            broadcasts = grants['manage'] or grants['broadcast']
            assert await page.locator('.row[data-go=ads]').count() == 1
            assert await page.locator('.row[data-go=replies]').count() == 1
            assert await page.locator('.metric-link[data-go=ads]').count() == int(broadcasts)
            has_groups = uid == 111 or grants['global_ops'] or bool(grants['groups'])
            assert await page.locator('#nav [data-go=bills]').count() == 1
            assert await page.locator('#nav [data-go=users]').count() == 1
            if name == 'single':
                await page.get_by_role('button', name='用户', exact=True).click()
                await expect(page.locator('[data-user]')).to_have_count(1)
                assert await page.locator('[data-user]').get_attribute('data-user') == str(uid)
                await page.locator('[data-user]').click()
                await expect(page.locator('#editUserGrants')).to_have_count(0)
                await expect(page.locator('#pricing_chat option')).to_have_count(1)
                await page.get_by_role('button', name='群组', exact=True).click()
                await expect(page.locator('[data-group]')).to_have_count(1)
                await page.locator('[data-group]').click()
                await expect(page.locator('#operatorForm')).to_have_count(0)
                await expect(page.locator('[data-remove-op]')).to_have_count(0)
                await expect(page.get_by_label('允许普通成员记账')).to_have_count(0)
                await page.screenshot(path=str(output/'scope-single-group.png'), full_page=True)
                await page.get_by_role('button', name='概览', exact=True).click()
            await page.locator('[data-go=tron]').click()
            await expect(page.locator('#tronKeysForm')).to_have_count(int(uid == 111))
            await page.get_by_role('button', name='概览', exact=True).click()
            for width in (320, 390):
                await page.set_viewport_size(dict(width=width, height=844))
                assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth'), name
            await page.screenshot(path=str(output/('scope-'+name+'.png')), full_page=True)
            if name == 'manage':
                await page.locator('.row[data-go=basics]').click()
                response = await context.request.post(url+'/api/miniapp/ledger1/staff', data=dict(
                    init_data=auth['ledger1'], payload=dict(user_id=uid, add=True, name=name,
                    grants=dict(manage=False, global_ops=False, broadcast=False, groups=[-10011]))))
                assert response.ok
                await page.locator('#refresh').click()
                await page.wait_for_function('!busy')
                await expect(page.locator('header h1')).to_have_text('基础配置')
                await expect(page.locator('#basicsForm')).to_have_count(1)
                await expect(page.get_by_role('heading', name='功能预览', exact=True)).to_be_visible()
                await expect(page.locator('#cutoff_hour')).to_be_disabled()
                await expect(page.locator('#toast')).to_have_text('已刷新')
            if name == 'visitor':
                for target in ('basics','features','staff','ads','replies'):
                    await page.locator('.row[data-go='+target+']').click()
                    await expect(page.get_by_role('heading', name='功能预览', exact=True)).to_be_visible()
                    assert await page.locator('#content input:enabled,#content select:enabled,#content textarea:enabled').count() == 0
                    assert await page.locator('#content button[type=submit]:enabled').count() == 0
                    await page.screenshot(path=str(output/('visitor-'+target+'.png')), full_page=True)
                    await page.get_by_role('button', name='概览', exact=True).click()
                await page.get_by_role('button', name='账单', exact=True).click()
                await page.wait_for_function('billData!==null&&!queryBusy')
                assert await page.locator('[data-bill-group]').count() == 0
                await page.get_by_role('button', name='统计', exact=True).click()
                await page.wait_for_function('statData!==null&&!queryBusy')
                assert await page.locator('[data-stats-group]').count() == 0
                await page.get_by_role('button', name='概览', exact=True).click()
                async def slow_refresh(route):
                    await asyncio.sleep(.4)
                    await route.continue_()
                await page.route('**/api/miniapp/ledger1/overview', slow_refresh)
                await page.locator('#refresh').click()
                await expect(page.locator('#content')).to_have_attribute('aria-busy', 'true')
                await expect(page.locator('#refresh')).to_be_disabled()
                await expect(page.locator('#toast')).to_have_text('已刷新')
                await expect(page.locator('#refresh')).to_be_enabled()
                await page.unroute('**/api/miniapp/ledger1/overview', slow_refresh)
                async def fail_refresh(route):
                    await route.fulfill(status=503,content_type='application/json',body=json.dumps({'ok':False,'error':'测试服务不可用'}))
                await page.route('**/api/miniapp/ledger1/overview', fail_refresh)
                await page.locator('#refresh').click()
                await expect(page.locator('#toast')).to_contain_text('刷新失败')
                await expect(page.locator('#refresh')).to_be_enabled()
                assert await page.locator('.row[data-go=basics]').count() == 1
                await page.unroute('**/api/miniapp/ledger1/overview', fail_refresh)
                await page.locator('#refresh').click()
                await expect(page.locator('#toast')).to_have_text('已刷新')
            if uid == 111:
                calls = []

                async def bill_route(route):
                    calls.append(route.request.post_data_json['payload'])
                    await asyncio.sleep(.25)
                    try:
                        response = await route.fetch()
                        await route.fulfill(response=response)
                    except Exception:
                        if not route.request.failure:
                            raise

                await page.route('**/api/miniapp/ledger1/bills', bill_route)
                await page.get_by_role('button', name='账单', exact=True).click()
                await page.wait_for_function('billData!==null&&!queryBusy')
                before = len(calls)
                await page.evaluate("Promise.all([queryRecords('bills'),queryRecords('bills')])")
                assert len(calls) == before+1, calls
                await page.evaluate("void queryRecords('bills');void go('stats')")
                await page.wait_for_function('view==="stats"&&statData!==null&&!queryBusy')
                await page.wait_for_timeout(350)
                await expect(page.locator('header h1')).to_have_text('统计')
                assert not await page.locator('#toast').is_visible()
            assert not errors, errors
            await context.close()
        await browser.close()
    print('Seven role views, public readonly previews, scoped writes and refresh recovery, mobile widths, query deduplication and stale-response cancellation passed.')


if __name__ == '__main__':
    asyncio.run(main())
