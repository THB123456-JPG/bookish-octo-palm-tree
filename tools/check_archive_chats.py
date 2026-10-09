"""Verify migration labels, scoped deletion and new-message recovery with fake data."""
import copy
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

from playwright.sync_api import sync_playwright, expect

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import core
from archive import MessageArchive
from _test_archive_replies import message
from _test_miniapp import setup_fixture
from manager import BotManager
from panel import PanelHandler
from runners.ledger import api
from runners.ledger.storage import LedgerStore

previous = core.BASE_DIR
with tempfile.TemporaryDirectory() as folder:
    fixture = setup_fixture(folder)
    manager = BotManager(fixture.cfg)
    manager.bots = copy.deepcopy(fixture.bots)
    manager.find('ledger1')['archive'] = dict(enabled=True)
    with closing(LedgerStore(Path(core.DATA_DIR)/'ledger1.sqlite3')) as store:
        store.migrate_bot_chat(-101, -202, 'Fake group')
    archive = MessageArchive(Path(core.DATA_DIR)/'ledger1.archive.sqlite3')
    for cid, text in ((-101, 'Before upgrade'), (-202, 'After upgrade'), (-303, 'Unrelated group')):
        archive.record_result(message(1, text, cid=cid))
    class Handler(PanelHandler):
        password = 'FAKE_ARCHIVE'
        merch = None
        token_secret = 'FAKE_ARCHIVE_SECRET'
    Handler.mgr = manager
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with patch.object(api, '_tg_admins', return_value=[]), sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport=dict(width=1280, height=900))
            errors = []
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.add_init_script("localStorage.setItem('panel_pw','FAKE_ARCHIVE');localStorage.setItem('panel_tab','msg');")
            base = 'http://127.0.0.1:%s' % server.server_port
            page.goto(base+'/')
            page.wait_for_function('LAST_ALL && LAST_ALL.length')
            assert page.locator('#tabs [data-pane]').evaluate_all(
                "els => els.map(e => e.dataset.pane)") == ['msg','bots','add','server','merch','help','logs','appearance']
            page.evaluate("msgOpenBot('ledger1')")
            expect(page.locator('#msgChats')).to_contain_text('升级前记录')
            expect(page.locator('#msgChats tr')).to_have_count(4)
            expect(page.locator('#msgChats')).to_contain_text('ID -101')
            expect(page.locator('#msgChats')).to_contain_text('ID -202')
            expect(page.locator('#msgChats')).to_contain_text('ID -303')
            assert page.locator('#msgChats').inner_text().count('升级前记录') == 1
            output = ROOT/'output/playwright'
            output.mkdir(parents=True, exist_ok=True)
            page.locator('#msgChats').screenshot(path=str(output/'archive-migration-label.png'))
            for cid, text in (('-101', 'Before upgrade'), ('-202', 'After upgrade')):
                response = page.request.get(base+'/api/archive/ledger1/messages?chat='+cid,
                                            headers={'X-Panel-Pass':'FAKE_ARCHIVE'})
                assert response.json()['messages'][0]['text'] == text
            page.locator('#msgChats tr').filter(has_text='ID -101').click()
            expect(page.locator('#msgStream')).to_contain_text('Before upgrade')
            page.goto(base+'/messages?bot=ledger1')
            expect(page.locator('#side')).to_contain_text('升级前记录')
            expect(page.locator('#side')).to_contain_text('ID -202')
            page.locator('#side .item').filter(has_text='ID -101').click()
            expect(page.locator('#main')).to_contain_text('Before upgrade')
            page.goto(base+'/')
            page.wait_for_function('LAST_ALL && LAST_ALL.length')
            page.evaluate("msgOpenBot('ledger1')")
            row = page.locator('#msgChats tr').filter(has_text='ID -101')
            page.once('dialog', lambda dialog: dialog.dismiss())
            row.get_by_role('button', name='删除记录').click()
            expect(row).to_have_count(1)
            headers = {'X-Panel-Pass':'FAKE_ARCHIVE'}
            endpoint = base+'/api/archive/ledger1/clear'
            assert page.request.post(endpoint, data={'chat_id':-101}).status == 401
            with patch.object(Handler, '_gate', return_value={'role':'merchant','mid':'fake'}):
                assert page.request.post(endpoint, data={'chat_id':-101}, headers=headers).status == 403
            for body in ({}, [], {'chat_id':101}, {'chat_id':'-101/../../'}, {'chat_id':True}):
                assert page.request.post(endpoint, data=body, headers=headers).status == 400
            assert page.request.post(base+'/api/archive/missing/clear', data={'chat_id':-101}, headers=headers).status == 404
            ledger = Path(core.DATA_DIR)/'ledger1.sqlite3'
            ledger_before, settings_before = ledger.read_bytes(), copy.deepcopy(manager.bots)
            page.evaluate("localStorage.setItem('panel_pinned_ledger1',JSON.stringify(['-101']));msgSaveSeen('ledger1',{'-101':{seq:9999}})")
            def accept_delete(dialog):
                assert 'Telegram 消息、记账流水和群权限保留' in dialog.message
                dialog.accept()
            page.once('dialog', accept_delete)
            with patch('requests.sessions.Session.request', side_effect=AssertionError('No Telegram requests')):
                row.get_by_role('button', name='删除记录').click()
                expect(row).to_have_count(0)
            expect(page.locator('#msgChats tr')).to_have_count(3)
            assert page.evaluate("msgSeen('ledger1')['-101'] === undefined && getPinChats('ledger1').indexOf('-101') < 0")
            assert ledger.read_bytes() == ledger_before and manager.bots == settings_before
            assert page.request.post(endpoint, data={'chat_id':-101}, headers=headers).json()['deleted'] == 0
            archive.record_result(message(2, 'New message', cid=-101))
            page.evaluate("msgOpenBot('ledger1',true)")
            expect(row).to_have_count(1)
            expect(page.locator('#msgChats tr')).to_have_count(4)
            assert [m['text'] for m in archive.messages(chat_id=-101)] == ['New message']
            page.screenshot(path=str(output/'archive-delete-and-tabs.png'))
            assert not errors, errors
            browser.close()
        print('Panel: migration labels, tab order, delete/cancel, authorization, scope and new-message recovery passed.')
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        archive.close()
        core.set_base_dir(previous)
