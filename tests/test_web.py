"""Local HTTP API: loopback only, session token on every POST, foreign Origin and Host refused."""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from trustrail import web
from tests.test_inbox import ACME_IBAN, NEW_IBAN, mail, seed


class FakeProvider:
    def __init__(self):
        self.submits = []
    def balances(self):
        return {'EUR': '100000.00', 'GBP': '100000.00'}
    def card_billing(self, ccy):
        return {'EUR': 'EUR', 'CNY': 'USD'}[ccy]
    def quote(self, sell, buy, amount):
        return {'quote_id': 'q', 'client_rate': '1', 'sell_amount': amount, 'buy_amount': amount}
    def submit(self, request_id, plan, details):
        self.submits.append((request_id, plan['invoice']))
        return {'id': 'transfer-fixture', 'status': 'NEW', 'request_id': request_id}
    def find(self, request_id, plan):
        return None


class WebTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / 'ledger.sqlite3')
        self.ledger, _ = seed(self.db)
        self.provider = FakeProvider()
        self.server = web.make_server(self.db, provider_factory=lambda: self.provider)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, *, token=True, headers=None):
        c = http.client.HTTPConnection('127.0.0.1', self.port, timeout=10)
        h = {'Content-Type': 'application/json'}
        if token:
            h['X-TrustRail-Token'] = self.server.token
        h.update(headers or {})
        data = body if isinstance(body, bytes) else (json.dumps(body).encode() if body is not None else None)
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        ctype = r.getheader('Content-Type') or ''
        return r.status, (json.loads(raw) if 'json' in ctype else raw.decode()), r

    def test_binds_loopback_only(self):
        self.assertEqual(self.server.server_address[0], '127.0.0.1')
        with self.assertRaises(ValueError):
            web.make_server(self.db, provider_factory=lambda: self.provider, host='0.0.0.0')

    def test_state_has_ui_fields_and_masks_bank_details(self):
        self.assertEqual(self.request('GET', '/api/state', token=False)[0], 403)
        st, state, _ = self.request('GET', '/api/state')
        self.assertEqual(st, 200)
        self.assertEqual(set(state), {'suppliers', 'invoices', 'plans', 'holds', 'inbox', 'balances', 'audit', 'replayed', 'console'})
        self.assertNotIn(ACME_IBAN, json.dumps(state))
        acme = [s for s in state['suppliers'] if s['id'] == 'acme'][0]
        self.assertEqual(acme['tier'], 'TRUSTED')
        self.assertTrue(acme['on_file']['iban'].endswith('00'))
        self.assertEqual(state['plans'][0]['method'], 'TRANSFER')
        self.assertFalse(state['replayed'])

    def test_post_gates(self):
        email = mail()
        self.assertEqual(self.request('POST', '/api/inbox', email, token=False)[0], 403)
        self.assertEqual(self.request('POST', '/api/inbox', email, headers={'X-TrustRail-Token': 'wrong'})[0], 403)
        self.assertEqual(self.request('POST', '/api/inbox', email, headers={'Origin': 'https://evil.test'})[0], 403)
        self.assertEqual(self.request('GET', '/api/state', headers={'Host': 'evil.test'})[0], 403)
        self.assertEqual(self.request('POST', '/api/inbox', email, headers={'Content-Type': 'text/plain'})[0], 415)
        self.assertEqual(self.request('POST', '/api/inbox', b'{}', headers={'Content-Length': str(web.MAX_BODY + 1)})[0], 413)
        self.assertEqual(self.request('POST', '/api/inbox', b'[1]')[0], 400)
        self.assertEqual(self.request('POST', '/api/nope', {})[0], 404)
        self.assertEqual(len(self.ledger.inbox_items()), 0)
        ok = self.request('POST', '/api/inbox', email, headers={'Origin': 'http://127.0.0.1:%d' % self.port})
        self.assertEqual(ok[0], 200)

    def test_human_flow_ingest_plan_approve_execute(self):
        st, item, _ = self.request('POST', '/api/inbox', mail())
        self.assertEqual((st, item['action']), (200, 'PENDING_INVOICE'))
        st, plan, _ = self.request('POST', '/api/plan', {'invoice': 'acme:INV-2002', 'loss_cap': '500', 'floor': '100'})
        self.assertEqual((st, plan['route']), (200, 'TRANSFER'))
        st, err, _ = self.request('POST', '/api/execute', {'invoice': 'acme:INV-2002'})
        self.assertEqual(st, 400)  # not approved
        st, err, _ = self.request('POST', '/api/approve', {'invoice': 'acme:INV-2002', 'digest': plan['digest']})
        self.assertEqual(st, 400)  # actor missing
        st, out, _ = self.request('POST', '/api/approve', {'invoice': 'acme:INV-2002', 'digest': plan['digest'], 'actor': 'owner'})
        self.assertEqual((st, out['state']), (200, 'APPROVED'))
        st, out, _ = self.request('POST', '/api/execute', {'invoice': 'acme:INV-2002'})
        self.assertEqual((st, out['state']), (200, 'SUBMITTED'))
        self.request('POST', '/api/execute', {'invoice': 'acme:INV-2002'})
        self.assertEqual(len(self.provider.submits), 1)
        self.assertIsNotNone(self.request('GET', '/api/state')[1]['balances'])

    def test_bank_change_hold_then_human_confirmation_with_digests(self):
        body = 'We changed our bank. New IBAN: ' + NEW_IBAN
        st, item, _ = self.request('POST', '/api/inbox', mail(body=body, subject='bank', message_id='<w-1@acme-tools.de>'))
        self.assertEqual(item['action'], 'BANK_CHANGE_HOLD')
        state = self.request('GET', '/api/state')[1]
        acme = [s for s in state['suppliers'] if s['id'] == 'acme'][0]
        self.assertTrue(acme['proposed']['iban'].endswith('2051'))
        self.assertEqual(state['holds'][0]['supplier'], 'acme')
        self.assertIn('acme:INV-1001', state['holds'][0]['blocked_invoices'])
        confirm = {'supplier': 'acme', 'old_digest': acme['pending_old_digest'], 'new_digest': acme['current_digest'],
                   'actor': 'owner'}
        self.assertEqual(self.request('POST', '/api/confirm-bank-details', confirm)[0], 400)  # callback ref missing
        confirm['callback_ref'] = 'called +49 number on file, 09.11 15:10Z'
        self.assertEqual(self.request('POST', '/api/confirm-bank-details', confirm)[0], 200)
        self.assertFalse(self.ledger.supplier_view('acme')['pending_bank_change'])

    def test_release_hold_is_a_human_post(self):
        self.request('POST', '/api/inbox', mail(**{'from': 'Acme <billing@acme-tools-de.com>', 'subject': 'new bank',
                                                   'body': 'Bank details changed, new IBAN ' + NEW_IBAN,
                                                   'message_id': '<w-2@evil.test>'}))
        self.assertEqual(self.ledger.supplier_view('acme')['tier'], 'HOLD')
        r = self.request('POST', '/api/release-hold', {'supplier': 'acme', 'actor': 'owner', 'reason_ref': 'callback: fake'})
        self.assertEqual(r[0], 200)
        self.assertNotEqual(self.ledger.supplier_view('acme')['tier'], 'HOLD')

    def test_static_placeholder_and_no_traversal(self):
        st, html, r = self.request('GET', '/', token=False)
        self.assertEqual(st, 200)
        self.assertIn('/api/state', html)
        self.assertIn("default-src 'self'", r.getheader('Content-Security-Policy'))
        for path in ['/../core.py', '/static/../core.py', '/core.py', '/%2e%2e/core.py']:
            with self.subTest(path=path):
                self.assertEqual(self.request('GET', path, token=False)[0], 404)


if __name__ == '__main__':
    unittest.main()
