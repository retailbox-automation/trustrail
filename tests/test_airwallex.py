"""Transport fixture tests: reject prod/redirects, bind account, preserve exact amounts."""
import unittest
from trustrail.airwallex import Airwallex, APIError, BASE, sanitize


class Transport:
    def __init__(self):
        self.calls = []
        self.account = 'acct_fixture'
        self.fail = False
    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, body))
        path = url.removeprefix(BASE)
        if path == '/authentication/login': return 201, {'token': 'SECRET-TOKEN'}
        if path == '/account': return 200, {'id': self.account}
        if self.fail: return 400, {'code': 'bad_request', 'message': 'SECRET-IN-ERROR'}
        if path == '/balances/current': return 200, [{'currency': 'EUR', 'available_amount': 500}]
        if path == '/beneficiaries/validate': return 200, 'OK'  # live 05.11 JSON string, not object
        if path == '/transfers/create': return 201, {'id': 'transfer-1', 'status': 'NEW', 'request_id': body['request_id'], 'beneficiary': {'iban': 'SECRET-IBAN'}}
        if path == '/issuing/cards/create': return 202, {'card_id': 'card-1', 'card_status': 'ACTIVE', 'card_number': 'SECRET-PAN'}
        if path.startswith('/transfers?'): return 200, {'items': [{'id': 'wrong', 'request_id': 'wrong'}, {'id': 'transfer-1', 'request_id': 'request-1', 'status': 'PROCESSING'}]}
        return 200, {}


class AirwallexTests(unittest.TestCase):
    def setup_api(self):
        t = Transport()
        a = Airwallex('client', 'key', 'acct_fixture', transport=t, min_interval=0)
        return a, t

    def test_production_lookalikes_and_alternate_paths_rejected_before_io(self):
        for url in ['https://api.airwallex.com/api/v1', BASE+'/', BASE+'@evil.test', 'http://api-demo.airwallex.com/api/v1', BASE+'?redirect=1']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                Airwallex('c', 'k', 'a', base=url)

    def test_wrong_account_cannot_submit(self):
        t = Transport()
        t.account = 'acct_other'
        with self.assertRaises(ValueError):
            Airwallex('c', 'k', 'acct_fixture', transport=t, min_interval=0)
        self.assertFalse(any(x[0] == 'POST' and x[1].endswith('/transfers/create') for x in t.calls))

    def test_transfer_uses_inline_validated_local_details_and_original_request_id(self):
        a, t = self.setup_api()
        details = {'beneficiary': {'bank_details': {'account_currency': 'EUR'}}, 'transfer_methods': ['LOCAL']}
        result = a.submit('request-1', {'route': 'DEPOSIT', 'amount': '0.30', 'cap': '0.30', 'currency': 'EUR'}, details)
        request = [x[2] for x in t.calls if x[1].endswith('/transfers/create')][0]
        self.assertEqual(request['transfer_amount'], '0.30')
        self.assertEqual(request['request_id'], 'request-1')
        self.assertEqual(request['transfer_method'], 'LOCAL')
        self.assertEqual(request['source_currency'], 'EUR')
        self.assertEqual(result, {'id': 'transfer-1', 'status': 'NEW', 'request_id': 'request-1'})
        self.assertEqual(a.balances(), {'EUR': '500.00'})
        self.assertEqual(a.validate(details), {'status': 'OK'})
        self.assertEqual(a.find('request-1', {'route': 'TRANSFER'})['id'], 'transfer-1')

    def test_card_controls_use_single_transaction_and_lifetime_cap(self):
        a, t = self.setup_api()
        a.cardholder_id = 'holder-1'
        result = a.submit('request-2', {'route': 'CARD', 'amount': '5.00', 'cap': '5.00', 'currency': 'EUR'}, {})
        payload = [x[2] for x in t.calls if x[1].endswith('/issuing/cards/create')][0]
        controls = payload['authorization_controls']
        self.assertEqual(controls['allowed_transaction_count'], 'SINGLE')
        self.assertEqual(controls['allowed_currencies'], ['EUR'])
        self.assertEqual(controls['transaction_limits'], {'currency': 'EUR', 'limits': [
            {'amount': '5.00', 'interval': 'PER_TRANSACTION'}, {'amount': '5.00', 'interval': 'ALL_TIME'}]})
        self.assertEqual(payload['metadata']['lane'], 'CT0511-AWX')
        self.assertNotIn('SECRET-PAN', str(result))

    def test_unbudgeted_swift_fees_and_cny_indirect_funding_are_blocked(self):
        a, _ = self.setup_api()
        with self.assertRaises(ValueError):
            a.submit('req', {'route': 'TRANSFER', 'amount': '1', 'currency': 'EUR'}, {'transfer_methods': ['SWIFT']})
        with self.assertRaises(ValueError):
            a.submit('req', {'route': 'CARD', 'amount': '1', 'cap': '1.03', 'currency': 'CNY'}, {})

    def test_evidence_keeps_authorization_result_and_drops_masked_pan(self):
        self.assertEqual(sanitize({'process_result': 'DECLINED', 'type': 'AUTHORIZATION',
            'failure_reason': 'CARD_INACTIVE', 'masked_card_number': 'SECRET'}),
            {'process_result': 'DECLINED', 'type': 'AUTHORIZATION', 'failure_reason': 'CARD_INACTIVE'})

    def test_unmeasured_card_currency_is_blocked_before_remote_creation(self):
        a, t = self.setup_api()
        a.cardholder_id = 'holder-1'
        for ccy in ['GBP', 'USD', 'CNY']:
            with self.subTest(currency=ccy), self.assertRaises(ValueError):
                a.submit('req', {'route': 'CARD', 'amount': '1', 'cap': '1.03', 'currency': ccy}, {})
        self.assertFalse(any(x[1].endswith('/issuing/cards/create') for x in t.calls))

    def test_errors_and_evidence_never_echo_secrets(self):
        a, t = self.setup_api()
        t.fail = True
        with self.assertRaises(APIError) as cm:
            a.call('GET', '/issuing/config')
        self.assertNotIn('SECRET', str(cm.exception))
        self.assertEqual(sanitize({'id': 'x', 'token': 'SECRET', 'details': {'iban': 'SECRET'}, 'message': 'SECRET'}), {'id': 'x'})
        with self.assertRaises(ValueError):
            a.call('GET', '//evil.test/anything')


if __name__ == '__main__':
    unittest.main()
