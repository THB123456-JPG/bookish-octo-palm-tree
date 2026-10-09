"""Group-scoped personnel commands and accounting permissions, no Telegram writes."""
import tempfile
import unittest
from contextlib import closing
from decimal import Decimal
from pathlib import Path

from runners.ledger.commands import Actor, handle_text
from runners.ledger.storage import LedgerStore


class PersonnelTests(unittest.TestCase):
    def test_real_update_dispatch_without_network(self):
        import core
        from types import SimpleNamespace
        from runners.ledger import LedgerRunner
        original_base = core.BASE_DIR
        with tempfile.TemporaryDirectory() as folder:
            core.set_base_dir(folder)
            Path(core.DATA_DIR).mkdir(parents=True, exist_ok=True)
            manager = SimpleNamespace(cfg={}, save=lambda: None, is_expired=lambda bot: False)
            runner = LedgerRunner(manager, {'id': 'permission-test', 'type': 'ledger',
                'owner_id': 111, 'admin_ids': [111], 'token': 'FAKE', 'archive': {'enabled': False}})
            calls = []
            def call(method, **kw):
                calls.append((method, kw))
                return {'message_id': len(calls)}
            runner.api = SimpleNamespace(call=call)
            mid = 0
            def send(text, uid, username, reply=None):
                nonlocal mid
                mid += 1
                msg = {'message_id': mid, 'chat': {'id': -1001, 'type': 'supergroup', 'title': 'Test'},
                       'from': {'id': uid, 'username': username, 'first_name': username}, 'text': text}
                if reply:
                    msg['reply_to_message'] = {'from': reply}
                runner.handle({'message': msg})
            try:
                send('hello', 222, 'aaa')
                send('hello', 333, 'bbb')
                send('添加操作人 @aaa @bbb', 111, 'boss')
                self.assertEqual(len(runner.store.list_operators(-1001)), 2)
                send('取消全员', 111, 'boss')
                send('+10', 444, 'outsider')
                self.assertEqual(runner.store.entries(-1001), [])
                self.assertIn('没有本群记账权限', calls[-1][1]['text'])
                send('+10', 222, 'aaa')
                self.assertEqual(len(runner.store.entries(-1001)), 1)
                send('显示操作人', 111, 'boss')
                self.assertIn('@aaa', calls[-1][1]['text'])
                send('删除操作人', 111, 'boss', reply={'id': 222, 'username': 'aaa', 'first_name': 'aaa'})
                self.assertEqual(len(runner.store.list_operators(-1001)), 1)
                send('+10', 222, 'aaa')
                self.assertEqual(len(runner.store.entries(-1001)), 1)
                self.assertFalse(any(method in ('setChatPermissions', 'restrictChatMember') for method, _ in calls))
            finally:
                runner.close_db()
                core.set_base_dir(original_base)

    def test_personnel_commands_and_permission_boundaries(self):
        boss = Actor(111, 'boss', '老板')
        aaa = Actor(222, 'aaa', '<测试成员>')
        bbb = Actor(333, 'bbb', '另一成员')
        outsider = Actor(444, 'outsider', '普通成员')
        with tempfile.TemporaryDirectory() as folder, closing(LedgerStore(Path(folder)/'ledger.sqlite3')) as st:
            for a in (aaa, bbb):
                st.remember_user(-1001, a.user_id, a.username, a.display_name)
            st.remember_user(-1002, 555, 'othergroup', '别群成员')
            mid = 0

            def command(text, actor=boss, cid=-1001, reply_user=None, reply_mid=None):
                nonlocal mid
                mid += 1
                return handle_text(st, cid, actor, text, {111}, reply_user=reply_user,
                                   message_id=mid, reply_message_id=reply_mid)

            self.assertTrue(st.all_members_can_record(-1001))
            self.assertFalse(command('+100', actor=outsider).changed)
            self.assertFalse(command('添加操作人 @aaa', actor=outsider).changed)
            self.assertFalse(command('添加操作人 @aaa @unknown').changed)
            self.assertEqual(st.list_operators(-1001), [])
            self.assertFalse(command('添加操作人 @othergroup').changed)
            self.assertFalse(command('添加操作人 aaa').changed)
            self.assertTrue(command('添加操作人 @AAA @bbb @aaa').changed)
            self.assertEqual([r['user_id'] for r in st.list_operators(-1001)], [222, 333])
            listing = command('显示操作人').text
            self.assertIn('操作人列表', listing)
            self.assertIn('&lt;测试成员&gt;', listing)
            self.assertIn('记账权限：仅拥有者', listing)
            self.assertFalse(command('取消全员', actor=aaa).changed)
            self.assertTrue(st.all_members_can_record(-1001))
            self.assertTrue(command('取消全员').changed)
            self.assertFalse(st.all_members_can_record(-1001))
            original = st.conn.execute('SELECT * FROM entries ORDER BY id').fetchall()
            for text in ('+10', '-10', '出款10', '下发10', '下发-10', '+1000/9'):
                self.assertFalse(command(text, actor=outsider).changed, text)
            self.assertIsNone(command('入款10', actor=outsider))
            self.assertFalse(command('撤销', actor=outsider, reply_mid=1).changed)
            self.assertEqual(st.conn.execute('SELECT * FROM entries ORDER BY id').fetchall(), original)
            self.assertIsNotNone(command('账单', actor=outsider))
            self.assertIsNotNone(command('+0', actor=outsider))
            self.assertTrue(command('+1000/9', actor=aaa).changed)
            self.assertTrue(command('下发10', actor=boss).changed)
            # This flag does not grant configuration or personnel privileges.
            self.assertFalse(command('设置全员').changed)
            for text in ('设置汇率 9', '设置费率 10', '清空', '日切4', '下课', '添加操作人 @aaa'):
                self.assertFalse(command(text, actor=outsider).changed, text)
            self.assertFalse(command('+1', actor=outsider).changed)
            self.assertTrue(command('取消全员').changed)
            self.assertTrue(command('删除操作人 @aaa @bbb').changed)
            self.assertFalse(command('+1', actor=aaa).changed)
            self.assertEqual(st.list_operators(-1001), [])
            self.assertTrue(command('添加操作人', reply_user=aaa).changed)
            self.assertTrue(command('删除操作人', reply_user=aaa).changed)
            for old in ('添加权限', '删除权限', '添加操作员', '删除操作员', '操作员', '操作员列表'):
                self.assertIsNone(command(old, reply_user=aaa), old)
            self.assertEqual(st.list_operators(-1001), [])
            self.assertFalse(command('设置全员', cid=111).changed)
            self.assertIn('请在群内', command('显示操作人', cid=111).text)
            self.assertFalse(command('添加操作人', cid=111, reply_user=aaa).changed)
            # Pausing accounting does not disable personnel management.
            self.assertTrue(command('下课').changed)
            self.assertFalse(st.is_ledger_enabled(-1001))
            self.assertTrue(command('添加操作人', reply_user=aaa).changed)
            self.assertFalse(command('设置全员').changed)
            self.assertIsNone(command('+1', actor=aaa))
            self.assertTrue(command('上课').changed)
            self.assertTrue(st.is_ledger_enabled(-1001))
            self.assertTrue(command('取消全员').changed)
            self.assertTrue(command('+1', actor=aaa).changed)
            self.assertTrue(st.all_members_can_record(-1002))
            self.assertTrue(command('取消全员', cid=-1002).changed)
            self.assertFalse(command('+1', actor=aaa, cid=-1002).changed)
            self.assertFalse(command('设置全员', cid=-1002).changed)
            self.assertEqual(st.list_operators(-1002), [])
            st.close()
            # Settings survive a restart and are scoped to one group.
            with closing(LedgerStore(Path(folder)/'ledger.sqlite3')) as reopened:
                self.assertFalse(reopened.all_members_can_record(-1001))
                self.assertFalse(reopened.all_members_can_record(-1002))

    def test_existing_database_defaults_are_preserved(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'old.sqlite3'
            with closing(sqlite3.connect(path)) as conn:
                conn.execute('CREATE TABLE chat_settings (chat_id INTEGER PRIMARY KEY, '
                             'rate TEXT, fee_percent TEXT, owner_id INTEGER, created_at TEXT)')
                conn.execute("INSERT INTO chat_settings VALUES (-1001,'7.2','3',111,'2026-10-08')")
                conn.commit()
            st = LedgerStore(path)
            try:
                self.assertTrue(st.all_members_can_record(-1001))
                self.assertEqual(tuple(st.get_settings(-1001)),
                                 (Decimal('7.2'), Decimal('3')))
            finally:
                st.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
