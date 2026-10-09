"""Daily cutoff pause/resume without moving, repricing or resurrecting ledger entries."""
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from runners.ledger import commands as C, reminders
from runners.ledger.storage import LedgerStore, LEDGER_TZ


class Clock(datetime):
    current = datetime(2026, 1, 1, 12, tzinfo=LEDGER_TZ)

    @classmethod
    def now(cls, tz=None):
        return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)


class CutoffTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.st = LedgerStore(Path(self.folder.name)/'ledger.sqlite3')
        self.st.remember_bot_chat(-1001, '测试群', 'supergroup')
        self.clock = patch('runners.ledger.storage.datetime', Clock)
        self.clock.start()
        Clock.current = datetime(2026, 1, 1, 12, tzinfo=LEDGER_TZ)

    def tearDown(self):
        self.clock.stop()
        self.st.close()
        self.folder.cleanup()

    def add(self, amount='100'):
        return self.st.add_entry(-1001, 'income', amount, 'CNY', '测试备注', 111, '所有者')

    def command(self, text, uid=111, cid=-1001):
        return C.handle_text(self.st, cid, C.Actor(uid, 'user', '人员'), text, {111})

    def rows(self):
        return [tuple(r) for r in self.st.conn.execute('SELECT * FROM entries ORDER BY id')]

    def test_show_cutoff_is_read_only_and_handles_disabled_cutoff(self):
        self.add()
        before = self.rows()
        self.assertIn('下次日切', self.command('显示日切', uid=999).text)
        self.assertEqual(self.rows(), before)
        self.command('关闭日切')
        self.assertIn('已关闭', self.command('显示日切', uid=999).text)
        self.assertEqual(self.rows(), before)

    def test_pause_keeps_current_period_excludes_history_and_skips_reminders(self):
        Clock.current -= timedelta(days=1)
        historical = self.add('600')
        Clock.current += timedelta(days=1)
        first = self.add()
        before = self.rows()
        self.assertTrue(self.command('关闭日切').changed)
        self.assertEqual(self.rows(), before)
        self.assertTrue(self.st.is_ledger_enabled(-1001))
        Clock.current += timedelta(days=10)
        second = self.add('200')
        current = C._entries_for_scope(self.st, -1001, 'today')
        self.assertEqual([e.id for e in current], [first.id, second.id])
        self.assertEqual(second.accounting_date, first.accounting_date)
        self.assertNotIn(historical.id, [e.id for e in current])
        self.assertEqual(C._mine_totals(current)[0], 300)
        self.assertIsNone(self.st.next_cutoff_at(-1001))
        self.assertIsNone(self.st.latest_closed_period(-1001, Clock.current))
        self.assertIn('日切状态：已关闭', self.command('查看日切').text)
        self.assertIn('下次日切：已关闭', self.command('查看日切').text)
        calls = []
        api = SimpleNamespace(call=lambda *a, **kw: calls.append(kw))
        reminders.initialize_schedule(self.st, Clock.current-timedelta(days=1))
        self.assertEqual(reminders.send_due_reminders(api, self.st, Clock.current), 0)
        self.assertEqual(calls, [])
        self.st.close()
        self.st = LedgerStore(Path(self.folder.name)/'ledger.sqlite3')
        self.assertFalse(self.st.is_ledger_cutoff_enabled(-1001))
        self.assertEqual(self.st.current_accounting_date(-1001), first.accounting_date)

    def test_resume_same_hour_waits_for_next_cutoff_and_preserves_snapshots(self):
        self.st.set_rate(-1001, '10')
        self.st.set_fee_percent(-1001, '5')
        first = self.add()
        self.command('关闭日切')
        Clock.current += timedelta(days=3)
        self.st.set_rate(-1001, '2')
        before = self.rows()
        result = self.command('设置日切时间3')
        self.assertTrue(result.changed)
        self.assertTrue(self.st.is_ledger_cutoff_enabled(-1001))
        self.assertEqual(self.st.current_accounting_date(-1001), first.accounting_date)
        boundary = self.st.next_cutoff_at(-1001)
        Clock.current = boundary-timedelta(seconds=1)
        self.assertEqual(self.st.current_accounting_date(-1001), first.accounting_date)
        Clock.current = boundary
        self.assertNotEqual(self.st.current_accounting_date(-1001), first.accounting_date)
        self.assertEqual(self.st.previous_accounting_date(-1001), first.accounting_date)
        self.assertEqual(self.st.latest_closed_period(-1001, Clock.current), (first.accounting_date, boundary))
        Clock.current += timedelta(days=1)
        self.assertEqual(self.st.previous_accounting_date(-1001), boundary.isoformat())
        Clock.current += timedelta(days=1)
        self.assertEqual(self.st.current_accounting_date(-1001), Clock.current.date().isoformat())
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.st.get_entry(first.id).net_amount, 9.5)

    def test_pause_during_existing_transition_and_permissions(self):
        first = self.add()
        self.command('设置日切时间20')
        self.command('关闭日切')
        Clock.current += timedelta(days=1)
        self.assertEqual(self.st.current_accounting_date(-1001), first.accounting_date)
        self.command('设置日切时间20')
        Clock.current = self.st.next_cutoff_at(-1001)+timedelta(seconds=1)
        period = self.st.current_accounting_date(-1001)
        self.command('关闭日切')
        Clock.current += timedelta(days=2)
        self.assertEqual(self.st.current_accounting_date(-1001), period)
        self.assertFalse(self.command('关闭日切', uid=444).changed)
        self.assertIn('群内', self.command('关闭日切', cid=111).text)
        self.st.set_ledger_enabled(-1001, False)
        self.assertTrue(self.command('关闭日切').changed)
        self.st.add_operator(-1001, 222, 'operator', '操作人', 111)
        self.assertTrue(self.command('关闭日切', uid=222).changed)
        self.assertFalse(self.st.is_ledger_enabled(-1001))

    def test_existing_groups_default_to_enabled_and_help_is_separated(self):
        self.add()
        before = self.rows()
        self.st.conn.execute('ALTER TABLE chat_settings DROP COLUMN ledger_cutoff_enabled')
        self.st.conn.commit()
        self.st.close()
        self.st = LedgerStore(Path(self.folder.name)/'ledger.sqlite3')
        self.assertTrue(self.st.is_ledger_cutoff_enabled(-1001))
        self.assertEqual(self.rows(), before)
        import customer_ui
        sections = {key: text for key, _, text in customer_ui.LEDGER_HELP}
        self.assertIn('关闭日切', sections['params'])
        for label in ('固定汇率', '实时汇率', '入款费率'):
            self.assertNotIn(label, sections['params'])
            self.assertIn(label, sections['rates'])

    def test_empty_group_resume_also_waits_for_next_cutoff(self):
        self.command('关闭日切')
        current = self.st.current_accounting_date(-1001)
        Clock.current += timedelta(days=5)
        self.command('设置日切时间0')
        self.assertEqual(self.st.current_accounting_date(-1001), current)
        self.assertEqual(self.st.next_cutoff_at(-1001).hour, 0)
        Clock.current = self.st.next_cutoff_at(-1001)
        self.assertEqual(self.st.previous_accounting_date(-1001), current)


if __name__ == '__main__':
    unittest.main()
