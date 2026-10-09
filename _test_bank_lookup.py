"""Key-free phone enrichment must not add requests or invent a card's region."""
import unittest
from unittest.mock import Mock, patch

from runners.ledger import lookup


class BankLookupTests(unittest.TestCase):
    def query(self, bank='ICBC', card_type='DC', validated=True):
        response = Mock()
        response.json.return_value = dict(validated=validated, bank=bank, cardType=card_type)
        with patch.object(lookup.httpx, 'Client') as factory, \
                patch.object(lookup, '_bank_info_rich') as rich:
            client = factory.return_value.__enter__.return_value
            client.get.return_value = response
            info = lookup.bank_info('6222020000000000')
            client.get.assert_called_once()
            rich.assert_not_called()
            self.assertNotIn('X-Bce-Signature', factory.call_args.kwargs['headers'])
            return info

    def test_basic_card_displays_verified_phone_without_key_or_region_guess(self):
        info = self.query()
        self.assertEqual(info['bank_phone'], '95588')
        card = lookup.format_card('bankcard', '6222020000000000', info)
        self.assertIn('银行电话：95588', card)
        self.assertNotIn('归属地', card)

    def test_bank_aliases_and_card_specific_hotlines(self):
        for bank, card_type, phone in (
                ('COMM', 'DC', '95559'), ('BJBANK', 'DC', '95526'),
                ('GDB', 'DC', '400-830-8003'), ('CGB', 'CC', '95508'),
                ('SPABANK', 'DC', '95511转3'), ('PAB', 'CC', '95511转2')):
            with self.subTest(bank=bank, card_type=card_type):
                self.assertEqual(self.query(bank, card_type)['bank_phone'], phone)

    def test_unknown_or_invalid_card_does_not_get_another_banks_phone(self):
        info = self.query('UNKNOWN')
        self.assertFalse(info['bank_phone'])
        self.assertNotIn('银行电话', lookup.format_card('bankcard', '6222020000000000', info))
        self.assertEqual(self.query(validated=False), {})

    def test_rich_provider_keeps_its_actual_region_and_phone(self):
        rich = dict(source='rich', bank_name='Fake bank', area='测试省 测试市', bank_phone='FAKE_PHONE')
        with patch.object(lookup, '_bank_info_rich', return_value=rich), \
                patch.object(lookup.httpx, 'Client') as client:
            info = lookup.bank_info('6222020000000000', 'FAKE_APPCODE')
            client.assert_not_called()
        card = lookup.format_card('bankcard', '6222020000000000', info)
        self.assertIn('归属地：测试省 测试市', card)
        self.assertIn('银行电话：FAKE_PHONE', card)


if __name__ == '__main__':
    unittest.main()
