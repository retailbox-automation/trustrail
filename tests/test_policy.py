import unittest
from decimal import Decimal
from trustrail.policy import currency, decide, money


class PolicyTests(unittest.TestCase):
    def test_rejects_ambiguous_nonfinite_and_nonmajor_money(self):
        # Removing parsing guards would let invalid invoices reach payment planning.
        for value in [True, False, '', ' ', '-1', 'NaN', 'Infinity', '1,000', '1e2', 1.1, '1.001', None, ' 10', '+1']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                money(value)
        self.assertEqual(money('100.01'), Decimal('100.01'))
        self.assertEqual(money(100), Decimal('100'))
        with self.assertRaises(ValueError):
            money('0')
        self.assertEqual(money('0', zero=True), Decimal('0'))

    def test_rejects_currency_aliases_and_unsupported_corridors(self):
        for value in ['', 'eur', ' EUR', '€', 'EURO', 'TRY', True, None]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                currency(value)
        self.assertEqual(currency('EUR'), 'EUR')

    def plan(self, **kw):
        args = dict(trusted=False, validated=True, accepts_card=False,
                    loss_cap='80', balance='500', floor='100')
        args.update(kw)
        return decide('100', 'EUR', **args)

    def test_deposit_cannot_exceed_thirty_percent_or_loss_cap(self):
        self.assertEqual(self.plan()['amount'], '30.00')
        self.assertEqual(self.plan(loss_cap='12.34')['amount'], '12.34')
        self.assertEqual(self.plan()['route'], 'DEPOSIT')

    def test_card_is_single_full_invoice_exposure_not_fake_loss_protection(self):
        self.assertEqual(self.plan(accepts_card=True, loss_cap='100')['route'], 'CARD')
        self.assertEqual(self.plan(accepts_card=True, loss_cap='99')['route'], 'DEPOSIT')

    def test_cny_card_reserves_three_percent_and_checks_total_loss_cap(self):
        p = decide('100', 'CNY', trusted=False, validated=True, accepts_card=True,
                   loss_cap='103', balance='500', floor='100')
        self.assertEqual((p['route'], p['amount'], p['cap']), ('CARD', '100.00', '103.00'))
        p = decide('100', 'CNY', trusted=False, validated=True, accepts_card=True,
                   loss_cap='102', balance='500', floor='100')
        self.assertEqual(p['route'], 'DEPOSIT')

    def test_trusted_supplier_gets_full_transfer_only_with_validation(self):
        self.assertEqual(self.plan(trusted=True)['amount'], '100.00')
        self.assertEqual(self.plan(trusted=True)['route'], 'TRANSFER')
        with self.assertRaises(ValueError):
            self.plan(trusted=True, validated=False)

    def test_floor_is_inclusive_and_checks_card_cap(self):
        self.assertEqual(self.plan(balance='130')['amount'], '30.00')
        with self.assertRaises(ValueError):
            self.plan(balance='129.99')
        with self.assertRaises(ValueError):
            self.plan(loss_cap='0')
        with self.assertRaises(ValueError):
            self.plan(accepts_card=True, loss_cap='100', balance='199.99')


if __name__ == '__main__':
    unittest.main()
