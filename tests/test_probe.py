"""Probe harness tests use transport fixtures, not sandbox/e2e evidence."""
import tempfile
import unittest
from pathlib import Path
from trustrail.airwallex import Airwallex, BASE
from trustrail.probe import run


class Fixture:
    def __init__(self):
        self.cards = {}
        self.transfers = []
        self.creates = 0
        self.fail_simulation = False
    def __call__(self, method, url, headers, body):
        p = url.removeprefix(BASE)
        if p == '/authentication/login': return 201, {'token': 'fixture-token'}
        if p == '/account': return 200, {'id': 'acct_fixture'}
        if p == '/balances/current': return 200, [{'currency': 'EUR', 'available_amount': '500.00'}, {'currency': 'USD', 'available_amount': '500.00'}]
        if p == '/issuing/config': return 200, {'primary_currency': 'USD'}
        if p == '/issuing/cardholders': return 200, {'items': [{'cardholder_id': 'holder', 'email': 'spike+trustrail@retailbox-automation.com', 'status': 'READY'}]}
        if p == '/beneficiaries/validate': return 200, 'OK'
        if p == '/beneficiaries/verify_account': return 200, {'code': 'VERIFIED', 'details': {'account_name_match_result': 'NOT_MATCHED'}}
        if p == '/issuing/cards/create':
            self.creates += 1
            cid = 'card-' + str(self.creates)
            self.cards[cid] = {**body, 'card_id': cid, 'card_status': 'ACTIVE'}
            return 202, self.cards[cid]
        if p.startswith('/issuing/cards/'):
            cid = p.split('/')[3]
            if p.endswith('/update'):
                self.cards[cid]['card_status'] = body['card_status']
            return 200, self.cards[cid]
        if p == '/simulation/issuing/create':
            if self.fail_simulation: raise TimeoutError('fixture')
            if self.cards[body['card_id']]['card_status'] == 'INACTIVE': reason = 'CARD_INACTIVE'
            elif body['transaction_currency'] == 'USD': reason = 'CURRENCY_NOT_ALLOWED'
            elif body['transaction_amount'] == '5.01': reason = 'LIMIT_EXCEEDED'
            else: reason = None
            return 201, {'card_id': body['card_id'], 'process_result': 'DECLINED' if reason else 'APPROVED', 'failure_reason': reason}
        if p == '/fx/quotes/create': return 201, {'quote_id': 'quote-fixture', 'client_rate': '0.9', 'usage': 'SINGLE_USE'}
        if p == '/transfers/create':
            r = {'id': 'transfer-fixture', 'status': 'NEW', 'request_id': body['request_id']}
            self.transfers.append(r)
            return 201, r
        if p.startswith('/transfers?'): return 200, {'items': self.transfers}
        raise AssertionError('unexpected fixture path ' + p)


class ProbeTests(unittest.TestCase):
    def exercise(self, root, fixture):
        api = Airwallex('c','k','acct_fixture',transport=fixture,min_interval=0)
        return run(str(root/'ledger.db'), str(root/'evidence'), api)

    def test_probe_asserts_real_flow_and_leaves_only_inactive_own_cards(self):
        # Missing cleanup, duplicate transfer or accepting NEW as SETTLED breaks this.
        with tempfile.TemporaryDirectory() as d:
            fixture = Fixture()
            result = self.exercise(Path(d), fixture)
            self.assertEqual(result['issuing'], 'PASS')
            self.assertEqual(result['transfer_status'], 'NEW')
            self.assertEqual(result['transfer'], 'PASS_SUBMITTED_NOT_SETTLED')
            self.assertEqual(len(fixture.transfers), 1)
            self.assertEqual(len(fixture.cards), 2)
            self.assertTrue(all(x['card_status']=='INACTIVE' for x in fixture.cards.values()))
            with self.assertRaises(FileExistsError): self.exercise(Path(d), fixture)
            self.assertEqual(len(fixture.transfers), 1)

    def test_failed_simulation_is_blocked_but_cards_still_freeze(self):
        with tempfile.TemporaryDirectory() as d:
            f = Fixture()
            f.fail_simulation = True
            result = self.exercise(Path(d), f)
            self.assertEqual(result['issuing'], 'BLOCKED')
            self.assertTrue(f.cards)
            self.assertTrue(all(x['card_status']=='INACTIVE' for x in f.cards.values()))


if __name__ == '__main__': unittest.main()
