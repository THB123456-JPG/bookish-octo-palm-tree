"""Browser regression with fake archive responses; no Telegram requests."""
import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from playwright.async_api import async_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
TS = '2026-10-09T13:00:00+08:00'


def message(seq, chat='-101', bot=False):
    return dict(archive_seq=seq, chat_id=chat, message_id=str(seq), ts=TS,
                chat_title='群' + chat, text='消息' + str(seq), display_name='测试用户',
                kind='text', is_bot=bot, is_owner=False)


async def check(browser, standalone=False):
    context = await browser.new_context(viewport=dict(width=390, height=844))
    await context.add_init_script("localStorage.setItem('panel_pw','FAKE');localStorage.setItem('panel_tab','msg');")
    state = dict(rows=[message(1)], fail=False, delayed=False, footer='联系开发者@HCW2026', saves=0)
    bot = dict(id='ledger1', type='ledger', note='测试机器人', username='fake_bot',
               enabled=True, status='running', owner_id=111, archive=dict(enabled=True))
    errors = []

    def unread(seen):
        counts = {}
        for row in state['rows']:
            mark = seen.get(row['chat_id'], '')
            fresh = row['archive_seq'] > mark.get('seq', 0) if isinstance(mark, dict) else row['ts'] > mark
            if fresh and not row['is_bot']:
                counts[row['chat_id']] = counts.get(row['chat_id'], 0) + 1
        return counts

    async def route(request):
        parsed = urlparse(request.request.url)
        path, query = parsed.path, parse_qs(parsed.query)
        if not path.startswith('/api/'):
            name = 'messages_page.html' if standalone else 'panel_page.html'
            await request.fulfill(content_type='text/html', body=(ROOT / name).read_text(encoding='utf-8'))
            return
        data = dict(ok=True)
        if path == '/api/bots':
            data.update(bots=[bot], me=dict(role='admin'))
        elif path.endswith('/unread_all'):
            if state['fail']:
                await request.fulfill(status=503, json=dict(ok=False, error='模拟失败'))
                return
            payload = request.request.post_data_json or {}
            seen = payload.get('seen', {}).get('ledger1', {})
            base = {}
            if not seen:
                seen = {row['chat_id']: dict(ts=TS, seq=row['archive_seq']) for row in state['rows']}
                base = dict(ledger1=seen)
            data.update(unread=dict(ledger1=unread(seen)), base=base)
        elif path.endswith('/chats'):
            chats = []
            for cid in sorted(set(row['chat_id'] for row in state['rows'])):
                rows = [row for row in state['rows'] if row['chat_id'] == cid]
                chats.append(dict(chat_id=cid, title='群' + cid, count=len(rows),
                                  last_ts=TS, last_seq=max(row['archive_seq'] for row in rows)))
            data.update(chats=chats, stats=dict(count=len(state['rows'])))
        elif path.endswith('/unread'):
            data['unread'] = unread(json.loads(query.get('seen', ['{}'])[0]))
        elif path.endswith('/messages') or path.endswith('/since'):
            cid = query.get('chat', [''])[0]
            rows = [row for row in state['rows'] if not cid or row['chat_id'] == cid]
            if path.endswith('/since'):
                rows = [row for row in rows if row['archive_seq'] > int(query.get('after_seq', ['0'])[0])][:200]
            else:
                rows = rows[-int(query.get('limit', ['200'])[0]):]
                if state['delayed'] and cid == '-101':
                    await asyncio.sleep(.6)
            data.update(messages=list(reversed(rows)), newest=TS, stats=dict(count=len(state['rows'])))
        elif path == '/api/appearance':
            if request.request.method == 'POST':
                state['footer'] = request.request.post_data_json['footer_text']
                state['saves'] += 1
                await asyncio.sleep(.2)
            data['footer_text'] = state['footer']
        await request.fulfill(json=data)

    await context.route('http://archive.test/**', route)
    page = await context.new_page()
    page.on('pageerror', lambda error: errors.append(str(error)))
    await page.goto('http://archive.test/messages' if standalone else 'http://archive.test/')
    if standalone:
        await expect(page.locator('#main .msg')).to_have_count(1)
        state['rows'].append(message(2))
        await expect(page.locator('#main .msg')).to_have_count(2, timeout=4500)
        # All-conversation overview must update without clearing unread badges.
        await expect(page.locator('#side .badge').first).to_have_text('1')
        state['rows'].append(message(3, bot=True))
        await expect(page.locator('#main .msg')).to_have_count(3, timeout=4500)
        await expect(page.locator('#side .badge').first).to_have_text('1')
        await page.evaluate("pickChat('-101')")
        await expect(page.locator('#main .msg')).to_have_count(3)
        state['rows'].append(message(4, '-202'))
        await expect(page.locator('#side')).to_contain_text('群-202', timeout=4500)
        await page.locator('#refreshMessages').click()
        await expect(page.locator('#toast')).to_have_text('已刷新')
        await expect(page.locator('#refreshMessages')).to_be_enabled()
    else:
        await expect(page.locator('#msgBots tbody tr')).to_have_count(2)
        assert await page.evaluate('MSG.TIMER !== null')
        state['rows'].append(message(2))
        await expect(page.locator('#msgBots .badge')).to_have_text('1', timeout=4500)
        await page.locator('#msgBots tr').nth(1).click()
        await expect(page.locator('#msgChats .badge')).to_have_text('1')
        state['rows'].append(message(3))
        await expect(page.locator('#msgChats .badge')).to_have_text('2', timeout=4500)
        await page.locator('#msgChats tr').nth(1).click()
        await expect(page.locator('#msgStream .mb')).to_have_count(3)
        await expect(page.locator('#msgLv3 button', has_text='返回')).to_be_visible()
        state['rows'].append(message(4))
        await expect(page.locator('#msgStream .mb')).to_have_count(4, timeout=4500)
        state['rows'][-1]['text'] = '修订消息4'
        await page.locator('#msgLv3 .msg-refresh').click()
        await expect(page.locator('#msgStream')).to_contain_text('修订消息4')
        await expect(page.locator('#toast')).to_have_text('已刷新')
        # Reading older messages keeps the scroll position and a visible new-message entry.
        state['rows'] = [message(i) for i in range(1, 151)]
        await page.evaluate("msgOpenChat('-101','群-101')")
        await expect(page.locator('#msgStream .mb')).to_have_count(150)
        await page.locator('#msgStream').evaluate('el => el.scrollTop = 0')
        state['rows'].append(message(151))
        await expect(page.locator('#msgNew')).to_be_visible(timeout=4500)
        assert await page.locator('#msgStream').evaluate('el => el.scrollTop') < 10
        assert await page.evaluate("msgSeen('ledger1')['-101'].seq") == 150
        await page.locator('#msgNew').click()
        assert await page.evaluate("msgSeen('ledger1')['-101'].seq") == 151
        await page.locator('#msgLv3 button', has_text='返回').click()
        await expect(page.locator('#msgLv2')).to_be_visible()
        await expect(page.locator('#msgChats .badge')).to_have_count(0)
        await page.locator('#msgLv2 button', has_text='返回').click()
        await expect(page.locator('#msgLv1')).to_be_visible()
        state['fail'] = True
        await page.locator('#msgLv1 .msg-refresh').click()
        await expect(page.locator('#toast')).to_have_text('刷新失败，请重试')
        await expect(page.locator('#msgLv1 .msg-refresh')).to_be_enabled()
        state['fail'] = False
        await page.locator('#msgLv1 .msg-refresh').click()
        await expect(page.locator('#toast')).to_have_text('已刷新')
        await page.locator('[data-pane=appearance]').click()
        await expect(page.locator('#footerText')).to_have_value('联系开发者@HCW2026')
        await page.locator('#footerText').fill('联系客服@HCW2026')
        await page.locator('#appearanceForm button[type=submit]').click()
        await expect(page.locator('#appearanceStatus')).to_have_text('已保存，全局生效。')
        assert state['saves'] == 1
        await page.locator('[data-pane=bots]').click()
        await page.locator('[data-pane=appearance]').click()
        await expect(page.locator('#footerText')).to_have_value('联系客服@HCW2026')
        await page.locator('[data-pane=msg]').click()
        # A late response from a previously selected group cannot replace the new group.
        state['rows'].append(message(152, '-202'))
        await page.evaluate("msgOpenBot('ledger1')")
        await expect(page.locator('#msgChats')).to_contain_text('群-202')
        state['delayed'] = True
        await page.evaluate("msgOpenChat('-101','群-101');msgOpenChat('-202','群-202')")
        await expect(page.locator('#msgStream')).to_contain_text('消息152')
        await page.wait_for_timeout(800)
        await expect(page.locator('#msgStream .mb')).to_have_count(1)
        assert await page.locator('#msgStream').evaluate('el => el.scrollWidth <= el.clientWidth + 1')
    assert not errors, errors
    output = ROOT / 'output/playwright'
    output.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=str(output / ('messages-live.png' if standalone else 'panel-messages-live.png')))
    await context.close()


async def main():
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        await check(browser)
        await check(browser, standalone=True)
        await browser.close()
    print('Both message pages: two-second updates, unread counts, navigation, refresh recovery and stale-response isolation passed.')


if __name__ == '__main__':
    asyncio.run(main())
