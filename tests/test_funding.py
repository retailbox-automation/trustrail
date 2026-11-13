"""GBP LOCAL, CNY card billed from USD, and explicit FX funding: quote -> conversion -> payout."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from trustrail.airwallex import Airwallex, BASE
from trustrail.core import Ledger

GBP_DETAILS = {'beneficiary': {'entity_type': 'COMPANY', 'company_name': 'Synthetic UK Supplier', 'bank_details': {
    'account_currency': 'GBP', 'bank_country_code': 'GB', 'account_name': 'Synthetic UK Supplier',
    'account_number': '10006132', 'account_routing_type1': 'sort_code', 'account_routing_value1': '040568'}},
    'transfer_methods': ['LOCAL']}


class Transport:
    def __init__(self):
        self.calls = []
    def __call__(self, method, url, headers, body):
        self.calls.append((method, url.removeprefix(BASE), body, headers))
        path = url.removeprefix(BASE)
        if path == '/authentication/login': return 201, {'token': 't'}
        if path == '/account': return 200, {'id': 'acct_fixture'}
        if path == '/fx/quotes/create':
            return 201, {'quote_id': 'q-1', 'client_rate': '1.25', 'buy_amount': body['buy_amount'],
                         'sell_amount': '125.00', 'buy_currency': body['buy_currency'], 'sell_currency': body['sell_currency']}
        if path == '/fx/conversions/create':
            return 201, {'conversion_id': 'conv-1', 'status': 'SETTLED', 'request_id': body['request_id'],
                         'buy_amount': 100, 'sell_amount': 125, 'client_rate': '1.25'}
        if path == '/transfers/create': return 201, {'id': 'tr-1', 'status': 'NEW', 'request_id': body['request_id']}
        if path == '/issuing/cards/create': return 202, {'card_id': 'card-1', 'card_status': 'ACTIVE'}
        if path.startswith('/simulation/transfers/'): return 200, {'id': path.split('/')[3], 'status': body['next_status']}
        return 200, {}


class AdapterFundingTests(unittest.TestCase):
    def setUp(self):
        self.t = Transport()
        self.a = Airwallex('c', 'k', 'acct_fixture', transport=self.t, min_interval=0)

    def body(self, path):
        return [x[2] for x in self.t.calls if x[1] == path][-1]

    def test_gbp_local_transfer_uses_sort_code_details_and_gbp_source(self):
        r = self.a.submit('rid-g', {'route': 'TRANSFER', 'amount': '1.00', 'cap': '1.00', 'currency': 'GBP'}, GBP_DETAILS)
        b = self.body('/transfers/create')
        self.assertEqual((b['transfer_currency'], b['source_currency'], b['transfer_method']), ('GBP', 'GBP', 'LOCAL'))
        self.assertEqual(b['beneficiary']['bank_details']['account_routing_type1'], 'sort_code')
        self.assertEqual(r['id'], 'tr-1')

    def test_gbp_without_complete_local_details_is_blocked_before_io(self):
        bad = json.loads(json.dumps(GBP_DETAILS))
        del bad['beneficiary']['bank_details']['account_routing_value1']
        for details in [bad, {**GBP_DETAILS, 'transfer_methods': ['SWIFT']}]:
            with self.subTest(details=details['transfer_methods']), self.assertRaises(ValueError):
                self.a.submit('rid', {'route': 'TRANSFER', 'amount': '1', 'cap': '1', 'currency': 'GBP'}, details)
        self.assertFalse(any(x[1] == '/transfers/create' for x in self.t.calls))

    def test_cny_card_limit_is_the_buffered_plan_cap_and_billing_is_usd(self):
        self.a.cardholder_id = 'holder'
        self.a.submit('rid-c', {'route': 'CARD', 'amount': '100.00', 'cap': '103.00', 'currency': 'CNY',
                                'funding': {'via': 'CARD_BILLING', 'currency': 'USD', 'cap': '16.00'}}, {})
        controls = self.body('/issuing/cards/create')['authorization_controls']
        self.assertEqual(controls['allowed_currencies'], ['CNY'])
        self.assertEqual(controls['transaction_limits']['limits'][0]['amount'], '103.00')
        self.assertEqual(self.a.card_billing('CNY'), 'USD')
        self.assertEqual(self.a.card_billing('EUR'), 'EUR')
        for ccy in ['GBP', 'USD']:
            with self.subTest(ccy=ccy), self.assertRaises(ValueError):
                self.a.card_billing(ccy)

    def test_quote_and_conversion_contracts(self):
        q = self.a.quote('USD', 'GBP', '100.00')
        self.assertEqual((q['quote_id'], q['sell_amount']), ('q-1', '125.00'))
        c = self.a.convert('rid-x', {'currency': 'USD', 'buy_currency': 'GBP', 'buy_amount': '100.00'}, 'q-1')
        body = self.body('/fx/conversions/create')
        self.assertEqual((body['request_id'], body['quote_id'], body['buy_amount'], body['sell_currency']),
                         ('rid-x', 'q-1', '100.00', 'USD'))
        self.assertEqual((c['id'], c['status']), ('conv-1', 'SETTLED'))

    def test_transfer_simulation_is_one_documented_hop(self):
        r = self.a.simulate_transfer('51765bab-b30a-4a25-b70d-4ae6ad9a0f9b', 'SENT')
        self.assertEqual(self.t.calls[-1][1], '/simulation/transfers/51765bab-b30a-4a25-b70d-4ae6ad9a0f9b/transition')
        self.assertEqual(self.t.calls[-1][2], {'next_status': 'SENT'})
        self.assertEqual(r['status'], 'SENT')
        for bad in [('../x', 'SENT'), ('51765bab-b30a-4a25-b70d-4ae6ad9a0f9b', 'SETTLED')]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.a.simulate_transfer(*bad)


class Provider:
    def __init__(self, path):
        self.path = path
        self.calls, self.fail_convert, self.sell, self.conv_status = [], False, '125.00', 'SETTLED'
        self.balance = {'GBP': '0.00', 'USD': '1000.00', 'CNY': '0.00', 'EUR': '500.00'}
        self.conversion = None
    def balances(self):
        return dict(self.balance)
    def validate(self, details):
        return {'status': 'OK'}
    def card_billing(self, ccy):
        return {'EUR': 'EUR', 'CNY': 'USD'}[ccy]
    def quote(self, sell, buy, buy_amount):
        self.calls.append(('quote', sell, buy, buy_amount))
        return {'quote_id': 'q', 'client_rate': '1.25', 'sell_amount': self.sell, 'buy_amount': buy_amount}
    def convert(self, request_id, funding, quote_id):
        c = sqlite3.connect(self.path)
        row = c.execute('SELECT request_id,state FROM conversions').fetchone()
        c.close()
        self.calls.append(('convert', request_id, row))
        if self.fail_convert:
            raise TimeoutError('unknown')
        self.conversion = {'id': 'conv-1', 'status': self.conv_status, 'request_id': request_id}
        if self.conv_status == 'SETTLED':  # a settled conversion credits the payout wallet
            buy = funding['buy_currency']
            self.balance[buy] = str(float(self.balance[buy]) + float(funding['buy_amount']))
        return self.conversion
    def find_conversion(self, request_id):
        return self.conversion
    def submit(self, request_id, plan, details):
        self.calls.append(('submit', request_id, plan['currency']))
        return {'id': 'tr-1', 'status': 'NEW', 'request_id': request_id}
    def find(self, request_id, plan):
        return None


class CoreFundingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'l.sqlite3')
        self.l, self.p = Ledger(self.path), Provider(self.path)
        self.l.add_supplier('uk', GBP_DETAILS, accepts_card=False)
        self.l.validate_supplier('uk', self.p)
        self.l.attest_delivery('uk', reviewer='owner', evidence_ref='delivery-1')
        self.l.add_invoice('g1', 'uk', '100.00', 'GBP')

    def approve(self, invoice='g1', **kw):
        args = dict(loss_cap='50', floor='0', fund_from='USD', fund_floor='100')
        args.update(kw)
        plan = self.l.plan(invoice, self.p, **args)
        self.l.approve(invoice, plan['digest'], actor='owner')
        return plan

    def test_plan_binds_funding_cap_with_buffer_and_requires_explicit_floor(self):
        with self.assertRaises(ValueError):
            self.l.plan('g1', self.p, loss_cap='50', floor='0', fund_from='USD')
        plan = self.approve()
        self.assertEqual(plan['funding'], {'currency': 'USD', 'cap': '127.50', 'via': 'CONVERSION',
                                           'quote_rate': '1.25', 'fund_floor': '100.00'})
        self.p.balance['USD'] = '227.49'
        with self.assertRaises(ValueError):
            self.l.plan('g1', self.p, loss_cap='50', floor='0', fund_from='USD', fund_floor='100')

    def test_conversion_request_is_durable_before_io_and_precedes_single_payout(self):
        self.approve()
        out = self.l.execute('g1', self.p)
        kinds = [c[0] for c in self.p.calls]
        self.assertEqual(kinds, ['quote', 'quote', 'convert', 'submit'])
        convert = [c for c in self.p.calls if c[0] == 'convert'][0]
        self.assertEqual(convert[2], (convert[1], 'SUBMITTING'))
        self.assertEqual(out['state'], 'SUBMITTED')
        self.assertEqual(out['conversion']['state'], 'SETTLED')
        self.assertEqual(out['conversion']['result']['id'], 'conv-1')
        self.l.execute('g1', self.p)
        self.assertEqual([c[0] for c in self.p.calls].count('submit'), 1)

    def test_unknown_conversion_never_pays_and_reconciles_without_new_request_id(self):
        self.approve()
        self.p.fail_convert = True
        out = self.l.execute('g1', self.p)
        self.assertEqual((out['state'], out['request_id']), ('UNKNOWN', None))
        rid = out['conversion']['request_id']
        self.assertEqual(self.l.execute('g1', self.p)['state'], 'UNKNOWN')
        self.assertNotIn('submit', [c[0] for c in self.p.calls])
        self.p.conversion = {'id': 'conv-1', 'status': 'SETTLED', 'request_id': rid}
        self.p.balance['GBP'] = '100.00'  # the conversion that did settle remotely
        self.assertEqual(self.l.reconcile('g1', self.p)['state'], 'APPROVED')
        self.p.fail_convert = False
        self.assertEqual(self.l.execute('g1', self.p)['state'], 'SUBMITTED')
        self.assertEqual([c[1] for c in self.p.calls if c[0] == 'convert'], [rid])
        self.assertEqual([c[0] for c in self.p.calls].count('submit'), 1)

    def test_rate_moving_past_approved_cap_blocks_before_conversion(self):
        self.approve()
        self.p.sell = '128.00'
        out = self.l.execute('g1', self.p)
        self.assertEqual(out['state'], 'BLOCKED')
        self.assertNotIn('convert', [c[0] for c in self.p.calls])

    def test_unsettled_conversion_is_reconcile_only(self):
        self.approve()
        self.p.conv_status = 'SCHEDULED'
        self.assertEqual(self.l.execute('g1', self.p)['state'], 'UNKNOWN')
        self.assertNotIn('submit', [c[0] for c in self.p.calls])

    def test_bank_change_during_funded_plan_stops_payout(self):
        self.approve()
        new = json.loads(json.dumps(GBP_DETAILS))
        new['beneficiary']['bank_details']['account_number'] = '99990000'
        self.l.bank_change('uk', new)
        with self.assertRaises(ValueError):
            self.l.execute('g1', self.p)
        self.assertEqual(self.p.calls, [('quote', 'USD', 'GBP', '100.00')])

    def test_cny_card_is_checked_against_usd_billing_wallet(self):
        self.l.add_supplier('cn', {'beneficiary': {'bank_details': {'account_currency': 'CNY'}}}, accepts_card=True)
        self.l.add_invoice('c1', 'cn', '100.00', 'CNY')
        with self.assertRaises(ValueError):
            self.l.plan('c1', self.p, loss_cap='200', floor='0')  # fund_floor is mandatory for USD billing
        plan = self.l.plan('c1', self.p, loss_cap='200', floor='0', fund_floor='100')
        self.assertEqual((plan['route'], plan['cap']), ('CARD', '103.00'))
        self.assertEqual(plan['funding']['currency'], 'USD')
        self.assertEqual(plan['funding']['via'], 'CARD_BILLING')
        self.assertEqual(self.p.calls[-1], ('quote', 'USD', 'CNY', '103.00'))
        with self.assertRaises(ValueError):
            self.l.plan('c1', self.p, loss_cap='200', floor='0', fund_from='USD', fund_floor='100')

    def test_funding_reservation_counts_against_the_funding_wallet(self):
        self.approve()
        self.l.execute('g1', self.p)
        self.l.add_invoice('g2', 'uk', '100.00', 'GBP')
        self.p.balance['USD'] = '300.00'  # 300 - 127.50 reserved - 127.50 < 100
        with self.assertRaises(ValueError):
            self.l.plan('g2', self.p, loss_cap='50', floor='0', fund_from='USD', fund_floor='100')

    def test_plans_without_funding_keep_their_previous_shape(self):
        self.l.add_invoice('e1', 'uk', '10.00', 'GBP')
        self.p.balance['GBP'] = '500.00'
        plan = self.l.plan('e1', self.p, loss_cap='50', floor='0')
        self.assertNotIn('funding', plan)


if __name__ == '__main__':
    unittest.main()
