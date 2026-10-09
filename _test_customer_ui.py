"""Help navigation uses one message and respects shop capability/role visibility."""
import re
from types import SimpleNamespace
import unittest

import customer_ui as ui
from core import TgError


class Runner:
    def __init__(self, kind='ledger'):
        self.bot = {'type': kind}
        self.bid = 'test'
        self.mgr = SimpleNamespace(cfg={'miniapp_base_url': 'https://example.test'})
        self.store = SimpleNamespace(cfg={})
        self.calls = []
        self.sent = []
        self.edit_error = None
        self.api = SimpleNamespace(call=self.call)

    def call(self, method, **kw):
        self.calls.append((method, kw))
        if method == 'editMessageText' and self.edit_error:
            raise TgError(self.edit_error)
        return {'description': ''} if method == 'getMyDescription' else True

    def send(self, uid, text, **kw):
        self.sent.append((uid, text, kw))

    def admins(self):
        return {111}

    def expired(self):
        return False


def callback(key, uid=111, chat=111, ctype='private'):
    return {'callback_query': {'id': 'test-callback', 'data': 'customer:help:' + key,
        'from': {'id': uid}, 'message': {'message_id': 321,
        'chat': {'id': chat, 'type': ctype}}}}


class HelpTests(unittest.TestCase):
    def test_private_help_aliases_send_directory_only(self):
        for uid in (111, 222):
            for command in ('帮助', '菜单', '群组命令', '/使用说明', '使用说明', ui.HELP_BUTTON, 'help'):
                with self.subTest(uid=uid, command=command):
                    r = Runner()
                    self.assertTrue(ui.handle(r, {'message': {
                        'chat': {'id': uid, 'type': 'private'},
                        'from': {'id': uid}, 'text': command}}))
                    self.assertEqual(len(r.sent), 1)
                    self.assertLess(len(r.sent[0][1]), 200)
                    self.assertNotIn('【记账】', r.sent[0][1])
                    self.assertIn('inline_keyboard', r.sent[0][2]['reply_markup'])

    def test_start_and_help_send_only_directory_with_config_entry(self):
        for command in ('/start', '/help'):
            r = Runner()
            self.assertTrue(ui.handle(r, {'message': {'message_id': 7,
                'chat': {'id': 111, 'type': 'private'}, 'from': {'id': 111}, 'text': command}}))
            self.assertEqual(len(r.sent), 1)
            self.assertIn('使用说明', r.sent[0][1])
            self.assertNotIn('首次使用请发送', r.sent[0][1])
            self.assertIn('web_app', r.sent[0][2]['reply_markup']['inline_keyboard'][-1][0])
            self.assertTrue(any(method=='setChatMenuButton' for method, _ in r.calls))

    def test_directory_layout_and_labels(self):
        r = Runner()
        ui.help_menu(r, 111)
        rows = r.sent[0][2]['reply_markup']['inline_keyboard']
        self.assertEqual([len(row) for row in rows], [3, 3, 3, 1])
        self.assertEqual([[b['text'] for b in row] for row in rows[:-1]], [
            ['⚙️ 管理权限', '👥 操作权限', '🧭 配置指南'],
            ['📊 账单显示', '🧾 账单参数', '🏠 群内设置'],
            ['💱 汇率费率', '🔎 查询撤销', '🧰 辅助功能'],
        ])
        self.assertEqual(rows[-1][0]['text'], '打开配置管理')
        self.assertEqual(rows[-1][0]['web_app']['url'], 'https://example.test/miniapp/test')
        for row in rows[:-1]:
            for button in row:
                self.assertEqual(len(button['text'].split(' ', 1)[1]), 4)
        self.assertEqual(len({b['text'].split(' ', 1)[0]
                              for row in rows[:-1] for b in row}), 9)
        for _, title, text in ui.LEDGER_HELP:
            self.assertIn(title + '说明', text)
            self.assertLess(len(text), 4096)
            for label in re.findall(r'<b>([^<]+)</b> ▪️', text):
                self.assertEqual(len(label), 4, label)
        self.assertNotIn('代付配置', str(rows))

    def test_all_sections_and_return_edit_same_message(self):
        r = Runner()
        for key, _, _ in ui.LEDGER_HELP:
            for target in (key, 'home'):
                self.assertTrue(ui.handle(r, callback(target)))
                method, params = r.calls[-2]
                self.assertEqual(method, 'editMessageText')
                self.assertEqual((params['chat_id'], params['message_id']), (111, 321))
                rows = params['reply_markup']['inline_keyboard']
                self.assertIn('web_app', rows[-1][0])
                self.assertEqual([len(row) for row in rows],
                                 [3, 3, 3, 1] if target == 'home' else [1, 3, 3, 3, 1])
                self.assertEqual(r.calls[-1][0], 'answerCallbackQuery')
        self.assertEqual(r.sent, [])

    def test_old_indexed_buttons_remain_usable(self):
        r = Runner()
        for i in range(6):
            self.assertTrue(ui.handle(r, callback(str(i))))
            self.assertEqual(r.calls[-2][0], 'editMessageText')
        self.assertEqual(r.sent, [])

    def test_welcome_moves_to_tools_and_old_buttons_still_edit(self):
        for chat, ctype in ((111, 'private'), (-1001, 'supergroup')):
            r = Runner()
            _, kb = ui.help_view(r, chat)
            buttons = [b for row in kb['inline_keyboard'] for b in row]
            self.assertTrue(any(b['text'] == '🧭 配置指南' for b in buttons))
            self.assertFalse(any('入群欢迎' in b['text'] for b in buttons))
            for old_key in ('welcome', '4'):
                self.assertTrue(ui.handle(r, callback(old_key, chat=chat, ctype=ctype)))
                method, args = r.calls[-2]
                self.assertEqual((method, args['chat_id'], args['message_id']),
                                 ('editMessageText', chat, 321))
                self.assertIn('辅助功能说明', args['text'])
                self.assertIn('<code>设置欢迎语</code>', args['text'])
                self.assertIn('<code>{name}</code>', args['text'])
            self.assertTrue(ui.handle(r, callback('examples', chat=chat, ctype=ctype)))
            self.assertIn('配置指南说明', r.calls[-2][1]['text'])
            self.assertNotIn('设置欢迎语', r.calls[-2][1]['text'])
            self.assertEqual(r.sent, [])

    def test_repeated_tap_is_acknowledged(self):
        r = Runner()
        r.edit_error = '400 Bad Request: message is not modified'
        self.assertTrue(ui.handle(r, callback('permissions')))
        self.assertEqual(r.calls[-1], ('answerCallbackQuery', {'callback_query_id': 'test-callback'}))
        self.assertEqual(r.sent, [])

    def test_removed_categories_move_into_existing_sections(self):
        expected = {
            'display': ('上期结余未下发', '分类明细', '完整账单备注分类结算', '本月统计'),
            'params': ('新群默认', '群押金', '<code>显示日切</code>', '当前账期', '下次日切'),
            'query': ('删除账单完整账单存档',),
            'rates': ('查询欧易汇率',),
            'entries': ('修改账单二次确认', '纯U／分红模式'),
            'examples': ('管理欢迎图片', '退群提醒', '记账授权', '全局地址', '通知类'),
            'tools': ('设置下发地址', '删除下发地址', '外部数字查询', '允许群管理员确认', '链上交易次数'),
            'manage': ('定时广告', '自动回复', '管理或广播权限', '创建者被撤权'),
        }
        r = Runner()
        for key, phrases in expected.items():
            text, _ = ui.help_view(r, 111, key)
            for phrase in phrases:
                self.assertIn(phrase, text)
        for old_key, target in (('addresses', 'tools'), ('config', 'examples'), ('rules', 'manage')):
            for chat, ctype in ((111, 'private'), (-1001, 'supergroup')):
                self.assertTrue(ui.handle(r, callback(old_key, chat=chat, ctype=ctype)))
                self.assertEqual(r.calls[-2][1]['text'], ui.help_view(r, chat, target)[0])
        self.assertEqual(r.sent, [])

    def test_edit_failure_does_not_send_new_message(self):
        r = Runner()
        r.edit_error = '400 Bad Request: message to edit not found'
        self.assertTrue(ui.handle(r, callback('permissions')))
        self.assertTrue(r.calls[-1][1]['show_alert'])
        self.assertEqual(r.sent, [])

    def test_unknown_and_foreign_callbacks(self):
        r = Runner()
        for key in ('unknown', '9', '99'):
            self.assertTrue(ui.handle(r, callback(key)))
            self.assertEqual(r.calls[-1][0], 'answerCallbackQuery')
        self.assertFalse(any(method == 'editMessageText' for method, _ in r.calls))
        r.calls.clear()
        for update in (callback('entries', chat=222), callback('entries', ctype='supergroup')):
            self.assertFalse(ui.handle(r, update))
        self.assertEqual(r.calls, [])

    def test_shop_sections_follow_existing_flags_and_admin_roles(self):
        r = Runner('shop')
        self.assertEqual([s[0] for s in ui.help_sections(r, 222)], ['shop'])
        r.store.cfg.update(auto_enabled=True, premium_enabled=True)
        self.assertEqual([s[0] for s in ui.help_sections(r, 222)], ['shop', 'auto', 'premium'])
        self.assertEqual([s[0] for s in ui.help_sections(r, 111)], ['shop', 'auto', 'premium', 'shop_admin'])
        ui.handle(r, callback('shop_admin', uid=222, chat=222))
        self.assertFalse(any(method == 'editMessageText' for method, _ in r.calls))
        ui.handle(r, callback('shop_admin'))
        self.assertIn('/收款', r.calls[-2][1]['text'])
        self.assertEqual(r.sent, [])

    def test_group_help_directory_and_all_sections_edit_same_message(self):
        for command in ('帮助', '群组命令'):
            r = Runner()
            self.assertTrue(ui.handle(r, {'message': {'message_id': 9,
                'chat': {'id': -1001, 'type': 'supergroup'}, 'from': {'id': 222}, 'text': command}}))
            self.assertEqual(len(r.sent), 1)
            self.assertEqual(r.sent[0][0], -1001)
            self.assertIn('群组命令', r.sent[0][1])
            self.assertNotIn('【记账】', r.sent[0][1])
            for key, _, _ in ui.LEDGER_HELP:
                for target in (key, 'home'):
                    self.assertTrue(ui.handle(r, callback(target, uid=222, chat=-1001, ctype='supergroup')))
                    method, args = r.calls[-2]
                    self.assertEqual((method, args['chat_id'], args['message_id']), ('editMessageText', -1001, 321))
                    self.assertNotIn('web_app', str(args['reply_markup']))
            self.assertEqual(len(r.sent), 1)
            legacy = callback('home', uid=222, chat=-1001, ctype='supergroup')
            legacy['callback_query']['data'] = 'ledger:help'
            self.assertTrue(ui.handle(r, legacy))
            self.assertIn('群组命令', r.calls[-2][1]['text'])
            self.assertEqual(len(r.sent), 1)

    def test_profile_copy_and_no_redundant_update(self):
        for kind in ('ledger', 'shop'):
            r = Runner(kind)
            ui.configure_description(r)
            updates = [kw for method, kw in r.calls if method == 'setMyDescription']
            self.assertEqual([kw['language_code'] for kw in updates], ['', 'zh'])
            self.assertTrue(all(len(kw['description']) <= 512 for kw in updates))
            self.assertTrue(all(kw['description'].startswith('这是一款') for kw in updates))
            r.calls.clear()
            def current(method, **kw):
                r.calls.append((method, kw))
                return {'description': ui.BOT_DESCRIPTIONS[kind]}
            r.api.call = current
            ui.configure_description(r)
            self.assertFalse(any(method == 'setMyDescription' for method, _ in r.calls))

    def test_commands_are_individually_copyable_and_html_safe(self):
        text = ui.help_text('测试标题', [('测试功能', '上课 / 开启', '<特殊> & 说明')])
        self.assertIn('<code>上课</code> / <code>开启</code>', text)
        self.assertIn('&lt;特殊&gt; &amp; 说明', text)


if __name__ == '__main__':
    unittest.main(verbosity=2)
