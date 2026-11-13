"""Support for the recorded live run: a sanitized per-call log and card checks recorded on the ledger."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from trustrail import console
from trustrail.airwallex import Airwallex, BASE
from trustrail.core import Ledger
from tests.test_inbox import seed


def transport(method, url, headers, body):
    path = url.removeprefix(BASE)
    if path == '/authentication/login': return 201, {'token': 'SECRET-TOKEN'}
    if path == '/account': return 200, {'id': 'acct_fixture'}
    if path == '/issuing/cards/create': return 202, {'card_id': 'c-1', 'card_status': 'ACTIVE', 'card_number': 'SECRET-PAN'}
    return 200, {}


class CallLog(unittest.TestCase):
    def test_each_remote_call_is_one_sanitized_line(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / 'calls.jsonl'
            with mock.patch.dict(os.environ, {'TRUSTRAIL_CALL_LOG': str(log)}):
                a = Airwallex('client-id', 'api-key-SECRET', 'acct_fixture', transport=transport, min_interval=0)
                a.call('POST', '/issuing/cards/create', {'request_id': 'r-1', 'cardholder_id': 'h', 'metadata': {'lane': 'x'}})
            lines = [json.loads(x) for x in log.read_text().splitlines()]
            self.assertEqual([(x['method'], x['path'], x['http']) for x in lines],
                             [('POST', '/authentication/login', 201), ('GET', '/account', 200), ('POST', '/issuing/cards/create', 202)])
            self.assertEqual(lines[2]['response']['card_id'], 'c-1')
            self.assertEqual(lines[2]['request']['request_id'], 'r-1')
            text = log.read_text()
            for secret in ('SECRET-TOKEN', 'api-key-SECRET', 'SECRET-PAN', 'Bearer'):
                self.assertNotIn(secret, text)
            self.assertEqual(oct(log.stat().st_mode & 0o777), '0o600')

    def test_no_log_without_the_variable(self):
        with mock.patch.dict(os.environ, {}, clear=True), tempfile.TemporaryDirectory() as d:
            Airwallex('c', 'k', 'acct_fixture', transport=transport, min_interval=0)
            self.assertEqual(list(Path(d).iterdir()), [])


class RecordedCardChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.L, self.p = seed(str(Path(self.tmp.name) / 'l.sqlite3'))

        class Card:
            calls = []
            cardholder_id = 'h'

            def balances(self):
                return {'CNY': '100000.00', 'USD': '100000.00', 'EUR': '100000.00'}

            def card_billing(self, ccy):
                return {'EUR': 'EUR', 'CNY': 'USD'}[ccy]

            def quote(self, sell, buy, amount):
                return {'quote_id': 'q', 'client_rate': '7', 'sell_amount': '15.00', 'buy_amount': amount}

            def submit(self, rid, plan, details):
                return {'id': 'card-live-1', 'status': 'ACTIVE', 'request_id': rid}
        self.card = Card()
        self.L.add_invoice('shen:SM-1', 'shen', '100.00', 'CNY')

    def test_checks_need_an_issued_card_and_feed_the_card_panel(self):
        with self.assertRaises(ValueError):
            self.L.record_card_check('shen:SM-1', label='over_limit', amount='103.01', currency='CNY',
                                     response={'process_result': 'DECLINED', 'failure_reason': 'LIMIT_EXCEEDED'})
        view = console.build(self.L)
        plan = self.L.plan('shen:SM-1', self.card, loss_cap='200', floor='0', fund_floor='0')
        self.assertEqual(console.build(self.L)['heroes']['shen:SM-1']['checks_title'], 'Measured earlier in the Airwallex sandbox')
        self.L.approve('shen:SM-1', plan['digest'], actor='operator')
        self.L.execute('shen:SM-1', self.card)
        with self.assertRaises(ValueError):
            self.L.record_card_check('shen:SM-1', label='made_up', amount='1', currency='CNY', response={})
        for label, amount, ccy, resp in [('over_limit', '103.01', 'CNY', {'process_result': 'DECLINED', 'failure_reason': 'LIMIT_EXCEEDED',
                                                                          'card_transaction_id': 't-1'}),
                                         ('other_currency', '10.00', 'USD', {'process_result': 'DECLINED', 'failure_reason': 'CURRENCY_NOT_ALLOWED'}),
                                         ('invoice_amount', '100.00', 'CNY', {'process_result': 'APPROVED', 'failure_reason': None})]:
            self.L.record_card_check('shen:SM-1', label=label, amount=amount, currency=ccy, response=resp)
        hero = console.build(self.L)['heroes']['shen:SM-1']
        self.assertEqual(hero['checks_title'], 'Checked in the Airwallex sandbox · this card')
        self.assertEqual((hero['slots']['decline_over_limit'], hero['slots']['decline_other_currency'],
                          hero['slots']['decline_second_charge']), ('LIMIT_EXCEEDED', 'CURRENCY_NOT_ALLOWED', 'APPROVED'))
        self.assertEqual([r['label'] for r in hero['check_rows']], ['Amount over the limit', 'Other currency', 'Invoice amount'])
        self.assertEqual([r['ok'] for r in hero['check_rows']], [False, False, True])
        self.assertTrue(view)
        html = (Path(console.__file__).parent / 'static' / 'index.html').read_text()
        self.assertIn('check_rows', html)

    def test_demo_ledger_records_no_sandbox_checks(self):
        from trustrail import demo
        path = str(Path(self.tmp.name) / 'demo.sqlite3')
        demo.seed(path)
        L = Ledger(path)
        L.approve('foshan:HY-0417', L.inspect('foshan:HY-0417')['plan']['digest'], actor='demo operator')
        L.execute('foshan:HY-0417', demo.DemoSandbox())
        with self.assertRaises(ValueError):
            L.record_card_check('foshan:HY-0417', label='over_limit', amount='50058.01', currency='CNY',
                                response={'process_result': 'DECLINED', 'failure_reason': 'LIMIT_EXCEEDED'})
        self.assertEqual(console.build(L)['heroes']['foshan:HY-0417']['checks_title'], 'Measured earlier in the Airwallex sandbox')


if __name__ == '__main__':
    unittest.main()
