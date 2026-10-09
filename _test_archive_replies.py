"""Reply snapshots and photo references survive paging without crossing chats."""
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from datetime import datetime, timedelta

import core
from archive import MessageArchive, TZ


def message(mid, text='', cid=-101, **extra):
    return dict(message_id=mid, date=int(time.time()),
                chat=dict(id=cid, type='supergroup', title='Fake group'),
                **{'from': dict(id=11, first_name='Fake sender', username='fake')},
                **dict(text=text, **extra))


class ArchiveReplyTests(unittest.TestCase):
    def test_expired_media_is_removed_without_touching_current_media_or_bills(self):
        now = datetime.now(TZ)
        self.archive.record_result(message(1,photo=[dict(file_id='FAKE_OLD')]))
        self.assertTrue(self.archive.put_media(-101,1,'FAKE_OLD',b'old'))
        old = self.archive.messages()[0]['media']
        later = message(2,photo=[dict(file_id='FAKE_NEW')])
        later['date'] = int((now+timedelta(hours=2)).timestamp())
        self.archive.record_result(later)
        self.assertTrue(self.archive.put_media(-101,2,'FAKE_NEW',b'new'))
        fresh = self.archive.messages()[0]['media']
        bills = Path(self.tmp.name)/'fake.sqlite3';bills.write_bytes(b'protected ledger')
        with patch.object(self.archive,'cutoff_iso',return_value=(now+timedelta(hours=1)).isoformat()):
            self.assertEqual(self.archive.prune(),1)
        self.assertIsNone(self.archive.media_path(old))
        self.assertEqual(self.archive.media_path(fresh).read_bytes(),b'new')
        self.assertEqual(bills.read_bytes(),b'protected ledger')
        self.assertEqual(self.archive.keep_hours,48)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.archive = MessageArchive(Path(self.tmp.name) / 'fake.archive.sqlite3')

    def tearDown(self):
        self.archive.close()
        self.tmp.cleanup()

    def test_incremental_sequence_drains_batches_and_keeps_same_second_messages(self):
        self.archive.record_result(message(1, 'baseline'))
        baseline = self.archive.messages(chat_id=-101)[0]
        mark = {'seq': baseline['archive_seq'], 'ts': baseline['ts']}
        self.archive.record_result([message(i, str(i)) for i in range(2, 452)])
        self.archive.record_result(message(1, 'Other group', cid=-202))
        self.assertEqual(self.archive.unread_counts({'-101': mark})['-101'], 450)
        cursor, collected = mark['seq'], []
        while True:
            batch = self.archive.messages_since('', chat_id=-101, limit=200, after_seq=cursor)
            if not batch:
                break
            self.assertTrue(all(row['chat_id'] == '-101' for row in batch))
            collected.extend(row['message_id'] for row in reversed(batch))
            cursor = max(row['archive_seq'] for row in batch)
        self.assertEqual(collected, [str(i) for i in range(2, 452)])
        self.assertNotIn('-101', self.archive.unread_counts(self.archive.baseline(with_seq=True)))
        self.assertEqual(self.archive.chats()[0]['last_seq'] > 0, True)
        self.archive.clear_all()
        self.archive.record_result(message(453, 'After clearing'))
        self.assertEqual(self.archive.unread_counts({'-101': {'seq': cursor}})['-101'], 1)
        self.assertEqual(self.archive.messages_since('', after_seq=cursor)[0]['message_id'], '453')

    def test_text_quote_survives_paging_and_same_message_id_in_another_chat(self):
        original = message(1, '<script>original text</script>')
        self.archive.record_result(message(2, 'reply body', reply_to_message=original))
        self.archive.record_result(message(1, 'Other chat secret', cid=-202))
        rows = self.archive.messages(chat_id=-101, limit=1)
        self.assertEqual(rows[0]['text'], 'reply body')
        self.assertEqual(rows[0]['reply']['text'], original['text'])
        self.assertEqual(rows[0]['reply']['display_name'], 'Fake sender')
        self.assertNotIn('Other chat secret', str(rows))
        self.assertEqual(self.archive.messages_since(rows[0]['ts'], chat_id=-101)[0]['reply'], rows[0]['reply'])

    def test_photo_quote_uses_original_media_and_retains_caption(self):
        original = message(1, '', photo=[dict(file_id='FAKE_PHOTO')], caption='Photo caption')
        self.archive.record_result(message(2, 'Reply to photo', reply_to_message=original))
        self.assertTrue(self.archive.put_media(-101, 1, 'FAKE_PHOTO', b'fake image bytes', '.png'))
        quote = self.archive.messages(chat_id=-101, limit=1)[0]['reply']
        self.assertEqual(quote['text'], 'Photo caption')
        self.assertTrue(quote['has_photo'])
        self.assertEqual(self.archive.media_path(quote['media']).read_bytes(), b'fake image bytes')
        self.assertEqual(len(self.archive.messages(chat_id=-101)), 2)

    def test_bot_reply_parameters_resolve_original_message(self):
        self.archive.record_result(message(1, 'User original'))
        runner = SimpleNamespace(record_archive=lambda value, is_own: self.archive.record_result(value, is_own=is_own))
        core.BaseRunner.archive_sent(runner, 'sendMessage', {'reply_parameters': {'message_id': 1}}, message(2, 'Bot reply'))
        rows = self.archive.messages(chat_id=-101)
        self.assertTrue(rows[0]['is_bot'])
        self.assertFalse(rows[1]['is_bot'])
        self.assertEqual(rows[0]['reply']['text'], 'User original')

    def test_private_muted_and_cross_chat_quotes_do_not_leak_messages(self):
        private = message(2, 'Private reply', reply_to_message=message(1, 'Private original'))
        private['chat']['type'] = 'private'
        self.archive.record_result(private)
        self.archive.record_result(message(2, 'Muted', reply_to_message=message(1, 'Muted original')), mute=[-101])
        self.assertFalse(self.archive.messages())
        self.archive.record_result(message(2, 'Cross chat', reply_to_message=message(1, 'Other chat secret', cid=-202)))
        self.assertNotIn('reply', self.archive.messages()[0])
        self.assertNotIn('Other chat secret', str(self.archive.messages()))


if __name__ == '__main__':
    unittest.main()
