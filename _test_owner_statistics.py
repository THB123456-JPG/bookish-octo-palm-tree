"""Owner-only private statistics: real dispatch, preserved finance, complete delivery."""
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from xml.etree import ElementTree

import core
from runners.ledger import LedgerRunner
from runners.ledger import commands as C


class OwnerStatisticsTests(unittest.TestCase):
    def setUp(self):
        self.original_base = core.BASE_DIR
        self.folder = tempfile.TemporaryDirectory()
        core.set_base_dir(self.folder.name)
        Path(core.DATA_DIR).mkdir(parents=True, exist_ok=True)
        manager = SimpleNamespace(cfg={}, save=lambda: None, is_expired=lambda bot: False)
        self.runner = LedgerRunner(manager, {'id': 'statistics-test', 'type': 'ledger',
            'owner_id': 111, 'admin_ids': [111, 222], 'token': 'FAKE', 'archive': {'enabled': False}})
        self.calls = []
        self.runner.api = SimpleNamespace(call=self.call)
        self.mid = 0
        self.runner.store.remember_bot_chat(-1001, '甲群', 'supergroup')
        self.runner.store.remember_bot_chat(-1002, '乙群', 'supergroup')
        self.runner.store.add_operator(-1001, 333, 'operator', '操作人', 111)

    def tearDown(self):
        self.runner.close_db()
        core.set_base_dir(self.original_base)
        self.folder.cleanup()

    def call(self, method, **kw):
        self.calls.append((method, kw))
        return {'message_id': len(self.calls)}

    def send(self, text='/统计', uid=111, cid=None, ctype='private'):
        self.mid += 1
        self.runner.handle({'message': {'message_id': self.mid,
            'chat': {'id': cid if cid is not None else uid, 'type': ctype},
            'from': {'id': uid, 'first_name': '测试用户', 'username': 'user%d' % uid}, 'text': text}})

    def click(self, data, uid=111, cid=None, ctype='private', mid=None):
        if mid is None:
            mid = self.runner._mine.get(uid, {}).get('message_id', 0)
        self.runner.on_callback({'id': 'callback', 'data': data, 'from': {'id': uid},
            'message': {'message_id': mid, 'chat': {
                'id': cid if cid is not None else uid, 'type': ctype}}})

    def text(self):
        return '\n'.join(kw.get('text', '') for _, kw in self.calls)

    def rows(self):
        return [tuple(row) for row in self.runner.store.conn.execute('SELECT * FROM entries ORDER BY id')]

    def test_owner_only_and_private_callback_boundaries(self):
        for uid in (222, 333, 444):
            self.send(uid=uid)
            self.assertNotIn(uid, self.runner._mine)
            self.assertIn('只有机器人拥有者', self.calls[-1][1]['text'])
        self.send(cid=-1001, ctype='supergroup')
        self.assertNotIn(111, self.runner._mine)
        self.assertIn('私聊', self.calls[-1][1]['text'])
        self.send()
        mid = self.runner._mine[111]['message_id']
        for args in ({'uid': 222}, {'cid': 222}, {'cid': -1001, 'ctype': 'supergroup'}):
            self.click('stats:all', mid=mid, **args)
            self.assertEqual(self.runner._mine[111]['selected'], set())
        self.click('stats:all', mid=mid+1000)
        self.assertIn('菜单已失效', self.calls[-1][1]['text'])
        self.click('stats:toggle:-1999')
        self.assertEqual(self.runner._mine[111]['selected'], set())
        self.runner.bot['owner_id'] = 999
        self.click('stats:all', mid=mid)
        self.assertIn('无权限', self.calls[-1][1]['text'])
        self.assertEqual(self.runner._mine[111]['selected'], set())

    def test_selection_cancel_expiry_and_mode_separation(self):
        self.send()
        stats_text, stats_kb = self.calls[-1][1]['text'], self.calls[-1][1]['reply_markup']
        self.send('/我')
        self.assertEqual(self.calls[-1][1]['text'], stats_text)
        self.assertEqual(str(self.calls[-1][1]['reply_markup']).replace('mine:', 'stats:'), str(stats_kb))
        self.send()
        self.click('stats:next')
        self.assertIn('至少选一个群', self.calls[-1][1]['text'])
        self.click('stats:all')
        self.assertEqual(self.runner._mine[111]['selected'], {-1001, -1002})
        self.click('stats:none')
        self.assertEqual(self.runner._mine[111]['selected'], set())
        self.click('stats:toggle:-1001')
        self.assertEqual(self.runner._mine[111]['selected'], {-1001})
        self.click('mine:all')
        self.assertEqual(self.runner._mine[111]['selected'], {-1001})
        self.click('stats:toggle:-1001')
        self.assertEqual(self.runner._mine[111]['selected'], set())
        self.runner._mine[111]['at'] = time.time()-3600
        self.click('stats:all')
        self.assertIn('菜单已失效', self.calls[-1][1]['text'])
        self.send()
        self.click('stats:cancel')
        self.assertNotIn(111, self.runner._mine)
        self.send('/我')
        self.click('stats:all', mid=1)
        self.assertEqual(self.runner._mine[111]['selected'], set())

    def test_actual_recorders_historical_snapshots_voids_and_selection(self):
        st = self.runner.store
        st.set_rate(-1001, '10')
        st.set_fee_percent(-1001, '10')
        first = st.add_entry(-1001, 'income', '100', 'CNY', '<甲账>', 222, '同名')
        st.add_entry(-1001, 'income', '-20', 'CNY', '负入款', 222, '同名')
        st.add_entry(-1001, 'payout', '3', 'USDT', '出款', 333, '同名')
        voided = st.add_entry(-1001, 'income', '900', 'CNY', '已撤销不可见', 333, '同名')
        st.void_entry(-1001, voided.id)
        st.add_entry(-1002, 'income', '77', 'CNY', '未选群不可见', 444, '别群成员')
        # A closed ledger period inside the retained natural month still counts.
        from datetime import datetime, timezone, timedelta
        month=datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-01')
        st.conn.execute("UPDATE entries SET accounting_date=?, created_at=? WHERE id=?", (month,month+'T00:00:00+00:00',first.id))
        st.conn.commit()
        st.remember_user(-1001, 222, 'aaa', '同名')
        st.remember_user(-1001, 333, 'bbb', '同名')
        st.set_rate(-1001, '2')
        st.set_fee_percent(-1001, '50')
        before = self.rows()
        self.send()
        self.click('stats:toggle:-1001')
        mid = self.runner._mine[111]['message_id']
        self.calls.clear()
        self.click('stats:next', mid=mid)
        text = self.text()
        self.assertIn('同名 @aaa（ID 222）', text)
        self.assertIn('同名 @bbb（ID 333）', text)
        self.assertNotIn('100/10=9U', text)
        self.assertNotIn('2026-01-01', text)
        self.assertNotIn('&lt;甲账&gt;', text)
        self.assertNotIn('#', text)
        self.assertIn('共 2 笔 ｜ 加分 +7.2 U', text)
        self.assertIn('全部所选群合计 3 笔 ｜ 加分 +7.2 U ｜ 下发 -3 U', text)
        self.assertNotIn('已撤销不可见', text)
        self.assertNotIn('未选群不可见', text)
        self.assertEqual(self.rows(), before)
        sent = len([m for m, _ in self.calls if m == 'sendMessage'])
        self.click('stats:next', mid=mid)
        self.assertEqual(len([m for m, _ in self.calls if m == 'sendMessage']), sent)

    def test_many_people_split_summaries_without_notes_or_details(self):
        st = self.runner.store
        for n in range(105):
            st.add_entry(-1001, 'income', '1', 'CNY', '记录%03d' % n, 1000+n, '历史操作人<&>')
        long_note = '"<&>' * 1200
        st.add_entry(-1001, 'income', '1', 'CNY', long_note, 444, '历史操作人')
        before = self.rows()
        self.send()
        self.click('stats:all')
        self.calls.clear()
        self.click('stats:next')
        chunks = [kw['text'] for method, kw in self.calls if method == 'sendMessage']
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 3600)
            ElementTree.fromstring('<root>'+chunk+'</root>')
        text = '\n'.join(chunks)
        for n in range(105):
            self.assertNotIn('记录%03d' % n, text)
            self.assertEqual(text.count('ID %d）' % (1000+n)), 1)
        self.assertIn('合计 106 笔', text)
        self.assertIn('本群暂无未撤销流水', text)
        self.assertNotIn('只列最近', text)
        self.assertNotIn('"&lt;&amp;&gt;', text)
        self.assertNotIn('#', text)
        self.assertIn('历史操作人&lt;&amp;&gt;', text)
        self.assertEqual(self.rows(), before)
        self.assertIn('/统计', C.HELP_TEXT)
        import customer_ui
        self.assertIn('人员统计', str(customer_ui.LEDGER_HELP))

    def test_reply_attribution_keeps_actual_recorder_and_no_groups(self):
        self.runner.handle({'message': {'message_id': 100, 'chat': {
            'id': -1001, 'type': 'supergroup', 'title': '甲群'}, 'text': '+100',
            'from': {'id': 222, 'first_name': '记账人'},
            'reply_to_message': {'from': {'id': 555, 'first_name': '被回复人'}}}})
        text = C.format_owner_statistics(self.runner.store, [(-1001, '甲群')])
        self.assertIn('ID 222', text)
        self.assertNotIn('ID 555', text)
        self.assertNotIn('被回复人', text)
        self.runner.store.deactivate_bot_chat(-1001)
        self.runner.store.deactivate_bot_chat(-1002)
        self.send()
        self.assertNotIn(111, self.runner._mine)
        self.assertIn('还没有记录到群', self.calls[-1][1]['text'])


if __name__ == '__main__':
    unittest.main()
