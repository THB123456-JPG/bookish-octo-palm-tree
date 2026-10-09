"""Group start/join use one short prompt without changing ledger permissions."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import core
from runners.ledger import LedgerRunner
from runners.ledger import commands as C


class GroupReadyTests(unittest.TestCase):
    def setUp(self):
        self.original = core.BASE_DIR
        self.tmp = tempfile.TemporaryDirectory()
        core.set_base_dir(self.tmp.name)
        Path(core.DATA_DIR).mkdir(exist_ok=True)
        self.mgr = SimpleNamespace(cfg={'miniapp_base_url': 'https://example.test'},
                                   save=lambda: None, is_expired=lambda b: False)
        self.bot = {'id': 'ready-test', 'type': 'ledger', 'token': 'FAKE',
                    'owner_id': 111, 'admin_ids': [111],
                    'ledger': {'welcome_text': '以前很长的入群说明', 'newbie_welcome': '欢迎{name}'},
                    'archive': {'enabled': False}}
        self.r = LedgerRunner(self.mgr, self.bot)
        self.r.me = {'id': 999, 'username': 'test_ready_bot'}
        self.calls = []
        self.r.api = SimpleNamespace(call=lambda method, **kw:
                    self.calls.append((method, kw)) or {'message_id': len(self.calls)})

    def tearDown(self):
        self.r.close_db()
        core.set_base_dir(self.original)
        self.tmp.cleanup()

    def sent(self):
        return [p for method, p in self.calls if method == 'sendMessage']

    def test_group_start_plain_mentioned_paused_and_private_help(self):
        for text in ('/start', '/start@test_ready_bot', '/start@TEST_READY_BOT'):
            self.calls.clear()
            self.r.handle({'message': {'message_id': 1,
                'chat': {'id': -1001, 'type': 'supergroup', 'title': '测试群'},
                'from': {'id': 222, 'first_name': '用户'}, 'text': text}})
            self.assertEqual(len(self.sent()), 1)
            self.assertEqual(self.sent()[0]['text'], C.GROUP_READY_TEXT)
            self.assertNotIn('reply_markup', self.sent()[0])
        self.r.store.set_ledger_enabled(-1001, False)
        self.calls.clear()
        self.r.handle({'message': {'message_id': 2, 'chat': {'id': -1001, 'type': 'group'},
                                  'from': {'id': 222, 'first_name': '用户'}, 'text': '/start'}})
        self.assertEqual(self.sent()[0]['text'], C.GROUP_READY_TEXT)
        self.assertFalse(self.r.store.is_ledger_enabled(-1001))
        self.assertFalse(self.r.store.entries(-1001))
        self.assertFalse(self.r.store.list_operators(-1001))
        self.calls.clear()
        self.r.handle({'message': {'message_id': 3, 'chat': {'id': -1001, 'type': 'group'},
                                  'from': {'id': 222, 'first_name': '用户'}, 'text': '/start@another_bot'}})
        self.assertFalse(self.sent())
        self.calls.clear()
        self.r.handle({'message': {'message_id': 4, 'chat': {'id': 111, 'type': 'private'},
                                  'from': {'id': 111, 'first_name': '老板'}, 'text': '/start'}})
        self.assertEqual(len(self.sent()), 1)
        self.assertIn('使用说明', self.sent()[0]['text'])
        self.assertIn('web_app', self.sent()[0]['reply_markup']['inline_keyboard'][-1][0])
        self.assertNotIn('已就绪', self.sent()[0]['text'])

    def test_join_events_in_either_order_send_once_and_bind_adder(self):
        self.bot['admin_ids'].append(333)
        for cid, order in ((-10011, ('mcm', 'service')), (-10022, ('service', 'mcm'))):
            self.calls.clear()
            chat = {'id': cid, 'type': 'supergroup', 'title': '新群'}
            frm = {'id': 333, 'first_name': '拉入人员'}
            updates = {'mcm': {'my_chat_member': {'chat': chat, 'from': frm,
                         'old_chat_member': {'status': 'left'}, 'new_chat_member': {'status': 'member'}}},
                       'service': {'message': {'message_id': 5, 'chat': chat, 'from': frm,
                         'new_chat_members': [{'id': 999, 'is_bot': True, 'first_name': '机器人'}]}}}
            for kind in order:
                self.r.handle(updates[kind])
            self.assertEqual(len(self.sent()), 1)

            self.assertEqual(self.sent()[0]['text'], C.GROUP_READY_TEXT)
            self.assertNotIn('reply_markup', self.sent()[0])
            self.assertEqual(self.r.store.get_chat_owner_id(cid), 333)
            self.assertFalse(self.r.store.entries(cid))
            self.r.handle({'my_chat_member': {'chat': chat, 'from': frm,
                           'old_chat_member': {'status': 'member'}, 'new_chat_member': {'status': 'administrator'}}})
            self.assertEqual(len(self.sent()), 1)
            readd = updates['service']['message'].copy()
            readd['from'] = {'id': 444, 'first_name': '再次拉入人员'}
            self.r.handle({'message': readd})
            self.assertEqual(self.r.store.get_chat_owner_id(cid), 333)
            self.assertEqual(len(self.sent()), 1)

    def test_stranger_join_never_grants_accounting_or_old_binder_privileges(self):
        for cid, kind in ((-10033, 'my_chat_member'), (-10044, 'message')):
            chat = {'id': cid, 'type': 'supergroup', 'title': '陌生群'}
            user = {'id': 777, 'first_name': '陌生人'}
            event = ({'chat': chat, 'from': user, 'old_chat_member': {'status': 'left'},
                      'new_chat_member': {'status': 'member'}} if kind == 'my_chat_member' else
                     {'message_id': 1, 'chat': chat, 'from': user,
                      'new_chat_members': [{'id': 999, 'is_bot': True}]})
            self.r.handle({kind: event})
            self.assertIsNone(self.r.store.get_chat_owner_id(cid))
            # Old implicit bindings and the historical all-members flag cannot authorize strangers.
            self.r.store.set_chat_owner(cid, 777, replace=True)
            self.r.store.set_all_members_can_record(cid, True)
            for mid, text in enumerate(('+100', '-10', '下发5', '设置汇率9', '添加操作人 @who'), 2):
                self.r.handle({'message': {'message_id': mid, 'chat': chat, 'from': user, 'text': text}})
            self.assertFalse(self.r.store.entries(cid))
            self.assertFalse(self.r.store.list_operators(cid))
            self.assertFalse(self.r.can_manage(cid, 777))
            self.assertFalse(self.r.customer.can_record(cid, 777))
            self.assertEqual(str(self.r.store.get_settings(cid)[0]), '1.0000')
            mode = self.r.store.get_ledger_view_mode(cid)
            self.r.handle({'callback_query': {'id': 'fake', 'from': user,
                'message': {'message_id': 9, 'chat': chat}, 'data': 'ledger:view:detailed:today'}})
            self.assertEqual(self.r.store.get_ledger_view_mode(cid), mode)
        self.bot['ledger']['staff_grants'] = {'888': dict(manage=False, global_ops=False, broadcast=False, groups=[-10033])}
        for cid in (-10033, -10044):
            self.r.handle({'message': {'message_id': 99, 'chat': {'id': cid, 'type': 'supergroup'},
                'from': {'id': 888, 'first_name': '单群人员'}, 'text': '+20'}})
            self.assertEqual(len(self.r.store.entries(cid)), int(cid == -10033))
        self.r.store.add_operator(-10044, 889, '', '群内授权', 111)
        self.r.handle({'message': {'message_id': 100, 'chat': {'id': -10044, 'type': 'supergroup'},
            'from': {'id': 889, 'first_name': '群内授权'}, 'text': '+30'}})
        self.assertEqual(len(self.r.store.entries(-10044)), 1)

    def test_exact_prompt_and_human_welcome_remain_independent(self):
        self.assertEqual(C.GROUP_READY_TEXT, '✅️ 记账机器人已就绪。\n\n授权操作人可记账。\n'
                         '添加操作人：<code>添加操作人 @用户名</code>\n'
                         '记账仅限授权人员。\n使用说明：<code>帮助</code>')
        self.r.handle({'message': {'message_id': 9, 'chat': {'id': -1001, 'type': 'supergroup'},
                       'from': {'id': 111, 'first_name': '老板'},
                       'new_chat_members': [{'id': 222, 'first_name': '新人', 'is_bot': False}]}})
        self.assertEqual(len(self.sent()), 1)
        self.assertIn('欢迎', self.sent()[0]['text'])
        self.assertNotIn('已就绪', self.sent()[0]['text'])
        self.assertEqual(self.bot['ledger']['welcome_text'], '以前很长的入群说明')
        self.calls.clear()
        self.r.handle({'message': {'message_id': 10, 'chat': {'id': -1001, 'type': 'supergroup'},
                                  'from': {'id': 111, 'first_name': '老板'}, 'text': '帮助'}})
        self.assertIn('群组命令', self.sent()[0]['text'])
        self.assertIn('inline_keyboard', self.sent()[0]['reply_markup'])


if __name__ == '__main__':
    unittest.main()
