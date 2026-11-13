"""Console (design variant C): demo world through real code paths, slot contract, static serving, UI actions, replay."""
import http.client
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from trustrail import console, demo, replay, web
from trustrail.core import Ledger

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / 'trustrail' / 'static'
EVIDENCE = ROOT / 'docs'


def no_network(*a, **k):
    raise AssertionError('no network in demo/console')


def page_slots():
    html = (STATIC / 'index.html').read_text()
    return set(re.findall(r'data-slot="([a-z0-9_]+)"', html)), set(re.findall(r'data-action="([a-z_]+)"', html)), html


class DemoWorld(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = str(Path(cls.tmp.name) / 'demo.sqlite3')
        with mock.patch('urllib.request.OpenerDirector.open', no_network):
            cls.summary = demo.seed(cls.db)
        cls.ledger = Ledger(cls.db)
        cls.view = console.build(cls.ledger)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_seed_uses_real_paths_and_refuses_an_existing_ledger(self):
        inv = {i['invoice']: i for i in self.ledger.invoices()}
        self.assertEqual(set(inv), {'foshan:HY-0417', 'atelier:AB-1180', 'pennine:PW-2291', 'marlow:MG-0093'})
        self.assertEqual((inv['foshan:HY-0417']['state'], inv['foshan:HY-0417']['plan']['route'],
                          inv['foshan:HY-0417']['plan']['cap']), ('DRAFT', 'CARD', '50058.00'))
        self.assertEqual((inv['atelier:AB-1180']['state'], inv['atelier:AB-1180']['plan']['route']), ('SUBMITTED', 'TRANSFER'))
        self.assertEqual(inv['pennine:PW-2291']['state'], 'BLOCKED')
        self.assertEqual((inv['marlow:MG-0093']['plan']['route'], inv['marlow:MG-0093']['plan']['amount']), ('DEPOSIT', '1800.00'))
        held = [i for i in self.ledger.inbox_items() if i['supplier'] == 'pennine' and i['action'] == 'SUPPLIER_HOLD']
        self.assertEqual(len(held), 1)
        self.assertTrue({'lookalike_domain', 'reply_to_mismatch', 'urgency_pressure', 'new_account_vs_on_file'}
                        <= set(json.loads(held[0]['signals'])))
        events = [a['event'] for a in self.ledger.audit()]
        self.assertIn('INBOX_PENDING_INVOICE', events)  # invoices came in through ingest, not direct inserts
        self.assertEqual(self.ledger.meta()['dataset'], 'demo')
        with self.assertRaises(ValueError):
            demo.seed(self.db)

    def test_page_slots_match_the_design_data(self):
        s = self.view['slots']
        expect = {
            'company_name': 'Lantern & Loom Imports', 'environment_label': 'Demo · offline stand-in, no Airwallex calls',
            'balance_usd': '10,000,000.00', 'balance_eur': '9,999,994.00', 'balance_gbp': '10,000,000.00',
            'balance_cny': '10,000,000.00',
            'q_1_supplier': 'Foshan Hongyu Ceramics', 'q_1_country': 'CN', 'q_1_invoice': 'HY-0417', 'q_1_amount': '¥48,600.00',
            'q_1_card_limit': '¥50,058.00', 'q_1_trust': 'New', 'q_1_status': 'Ready for approval',
            'q_2_supplier': 'Atelier Brandt GmbH', 'q_2_method': 'Local transfer (SEPA)', 'q_2_trust': 'Trusted',
            'q_2_status': 'Sent, processing', 'q_3_supplier': 'Pennine Weaving Ltd', 'q_3_trust': 'Trust revoked',
            'q_3_status': 'Stopped', 'q_4_amount': '£1,800.00', 'q_4_total': '£6,000.00', 'q_4_trust': 'Verified',
            'q_4_status': 'Awaiting shipment proof',
            'tl_1_supplier': 'Atelier Brandt', 'tl_2_supplier': 'Foshan Hongyu', 'tl_3_amount': '£8,250.00',
            'tl_4_status': 'Awaiting shipment proof', 'tl_4_total': '£6,000.00',
            'ladder_new_1_supplier': 'Foshan Hongyu', 'ladder_new_2_supplier': 'Pennine Weaving',
            'ladder_new_2_note': 'trust revoked', 'ladder_verified_1_amount': '£1,800.00',
            'ladder_trusted_1_supplier': 'Atelier Brandt', 'ladder_trusted_1_amount': '€12,400.00',
            'act_1_time': '10:42', 'act_1_text': 'Pennine Weaving: bank details email, transfer stopped',
            'act_2_time': '09:58', 'act_2_text': 'Marlow Glassworks: shipment notice, proof requested',
            'act_3_time': '09:31', 'act_3_text': 'Foshan Hongyu: invoice HY-0417, card plan prepared',
            'act_4_time': '08:15', 'act_4_text': 'Atelier Brandt: invoice AB-1180, transfer sent'}
        self.assertEqual({k: s.get(k) for k in expect}, expect)
        self.assertTrue(self.view['demo'])
        self.assertEqual(self.view['default_invoice'], 'pennine:PW-2291')

    def test_hero_slots_for_both_states(self):
        stop = self.view['heroes']['pennine:PW-2291']
        self.assertEqual(stop['state'], 'stopped')
        expect = {'stop_supplier_name': 'Pennine Weaving Ltd', 'stop_invoice_id': 'PW-2291', 'stop_amount': '£8,250.00',
                  'stop_method': 'Local transfer', 'stop_status': 'Stopped', 'stop_email_subject': 'Our bank details have moved',
                  'stop_email_received_at': 'Nov 9, 2026 · 10:42', 'stop_email_from': 'accounts@pennine-weavlng.co.uk',
                  'stop_domain_on_file': 'pennine-weaving.co.uk', 'onfile_verified_date': 'Oct 22, 2026',
                  'onfile_bank': 'Barclays', 'onfile_sort_code': '20-45-77', 'onfile_account': '••••3816',
                  'email_bank': 'Monzo', 'email_sort_code': '04-00-04', 'email_account': '••••9921',
                  'stop_agent_summary_amount': '£8,250.00'}
        self.assertEqual({k: stop['slots'].get(k) for k in expect}, expect)
        self.assertEqual(stop['signals'], ['Lookalike domain: weavlng vs weaving', 'Reply-To differs from sender',
                                           'Urgency: ‘pay today to avoid delay’', 'New account, never paid before'])
        self.assertEqual(stop['domain_marks'], {'onfile': ['pennine-', 'weaving', '.co.uk'],
                                                'email': ['pennine-', 'weavlng', '.co.uk']})
        card = self.view['heroes']['foshan:HY-0417']
        self.assertEqual(card['state'], 'card')
        expect = {'card_supplier_name': 'Foshan Hongyu Ceramics', 'card_invoice_id': 'HY-0417', 'card_amount': '¥48,600.00',
                  'card_method': 'Single-use card', 'card_status': 'Ready for approval', 'card_limit': '¥50,058.00',
                  'card_invoice_base': '¥48,600.00', 'card_fx_buffer': '3%', 'card_currency': 'CNY',
                  'card_limit_button': '¥50,058.00', 'deposit_amount': '¥14,580.00', 'deposit_pct': '30%',
                  'decline_over_limit': 'LIMIT_EXCEEDED', 'decline_other_currency': 'CURRENCY_NOT_ALLOWED',
                  'decline_second_charge': 'LIMIT_EXCEEDED', 'card_number_masked': '•••• •••• •••• ••••'}
        self.assertEqual({k: card['slots'].get(k) for k in expect}, expect)
        self.assertRegex(card['slots']['plan_id'], r'^[0-9a-f]{4}…[0-9a-f]{3}$')

    def test_every_page_slot_and_action_is_bound(self):
        slots, actions, html = page_slots()
        hero_slots = set().union(*(h['slots'] for h in self.view['heroes'].values()))
        missing = sorted(slots - set(self.view['slots']) - hero_slots - set(console.COMPUTED))
        self.assertEqual(missing, [])
        self.assertEqual(actions, {'keep_hold', 'verify_callback', 'approve_new_details', 'reject_new_details',
                                   'approve_card', 'pay_card', 'card_issued', 'use_deposit', 'hold'})
        markup = re.sub(r'<(style|script)>.*?</\1>', '', html, flags=re.S)
        self.assertNotIn('state-toggle', markup)  # review toggle only behind ?review=1 (built by script)
        self.assertGreaterEqual(len(slots), 100)


class ConsoleHTTP(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / 'demo.sqlite3')
        demo.seed(self.db)
        self.provider = demo.DemoSandbox()
        self.srv = web.make_server(self.db, provider_factory=lambda: self.provider)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def call(self, method, path, body=None, token=True):
        c = http.client.HTTPConnection('127.0.0.1', self.srv.server_address[1], timeout=10)
        h = {'Content-Type': 'application/json'}
        if token:
            h['X-TrustRail-Token'] = self.srv.token
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=h)
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, raw, r

    def test_static_page_asset_and_csp(self):
        st, raw, r = self.call('GET', '/', token=False)
        html = raw.decode()
        self.assertEqual(st, 200)
        self.assertNotIn(self.srv.token, html)
        csp = r.getheader('Content-Security-Policy')
        self.assertIn('https://fonts.googleapis.com', csp)
        self.assertIn('https://fonts.gstatic.com', csp)
        st, png, r = self.call('GET', '/card-tile-bg.png', token=False)
        self.assertEqual((st, r.getheader('Content-Type')), (200, 'image/png'))
        self.assertEqual(png, (STATIC / 'card-tile-bg.png').read_bytes())
        self.assertEqual(self.call('GET', '/../core.py', token=False)[0], 404)
        st, raw, _ = self.call('GET', '/api/state')
        state = json.loads(raw)
        self.assertTrue(state['console']['demo'])
        self.assertEqual(state['console']['slots']['company_name'], 'Lantern & Loom Imports')

    def test_ui_actions_reach_the_api_and_errors_come_back(self):
        st, raw, _ = self.call('POST', '/api/keep-hold', {'supplier': 'pennine', 'actor': 'payment desk', 'reason_ref': 'kept on hold from console'})
        self.assertEqual(st, 200, raw)
        st, raw, _ = self.call('POST', '/api/plan', {'invoice': 'foshan:HY-0417', 'method': 'deposit'})
        self.assertEqual(st, 400)  # new supplier has no validated account: an error, shown inline, not silent
        self.assertIn('validated', json.loads(raw)['error'])
        digest = json.loads(self.call('GET', '/api/state')[1])['console']['heroes']['foshan:HY-0417']['plan_digest']
        st, raw, _ = self.call('POST', '/api/approve', {'invoice': 'foshan:HY-0417', 'digest': digest, 'actor': 'payment desk'})
        self.assertEqual((st, json.loads(raw)['state']), (200, 'APPROVED'))
        st, raw, _ = self.call('POST', '/api/execute', {'invoice': 'foshan:HY-0417'})
        self.assertEqual((st, json.loads(raw)['state']), (200, 'SUBMITTED'))
        self.assertEqual(len(self.provider.submits), 1)
        st, raw, _ = self.call('POST', '/api/hold', {'supplier': 'marlow', 'actor': 'payment desk', 'reason_ref': 'held from console'})
        self.assertEqual(st, 200, raw)
        self.assertEqual(Ledger(self.db).supplier_view('marlow')['tier'], 'HOLD')
        st, raw, _ = self.call('POST', '/api/release-hold', {'supplier': 'pennine', 'actor': 'payment desk',
                                                            'reason_ref': 'callback CB-1: email details rejected'})
        self.assertEqual(st, 200, raw)
        events = [a['event'] for a in Ledger(self.db).audit()]
        self.assertIn('HUMAN_KEPT_HOLD', events)
        self.assertIn('HUMAN_HOLD', events)


class DemoLedgerNeverGoesLive(unittest.TestCase):
    def test_web_over_a_demo_ledger_uses_the_offline_sandbox(self):
        from trustrail import cli
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / 'demo.sqlite3')
            demo.seed(db)
            live_factory = lambda: (_ for _ in ()).throw(AssertionError('live provider used for a demo ledger'))
            self.assertIsInstance(cli.web_provider(db, live_factory)(), demo.DemoSandbox)
            srv = web.make_server(db, provider_factory=cli.web_provider(db, live_factory))
            self.addCleanup(srv.server_close)
            self.assertIn('demo ledger', web.banner(srv))
            live = str(Path(d) / 'live.sqlite3')
            Ledger(live)
            sentinel = object()
            self.assertIs(cli.web_provider(live, lambda: sentinel)(), sentinel)


class ReplayConsole(unittest.TestCase):
    def test_replay_console_uses_recorded_ids_and_is_read_only(self):
        with mock.patch('urllib.request.OpenerDirector.open', no_network):
            state = replay.load(EVIDENCE)
            view = console.build_replay(state)
        self.assertTrue(view['replayed'])
        text = ''.join(p.read_text() for p in replay.files(EVIDENCE))
        card = view['heroes'][view['default_invoice']]
        self.assertEqual(card['state'], 'card')
        for code in (card['slots']['decline_over_limit'], card['slots']['decline_other_currency']):
            self.assertIn(code, text)
        ids = set(re.findall(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', json.dumps(view)))
        self.assertTrue(ids)
        self.assertEqual([i for i in ids if i not in text], [])
        srv = web.make_server(None, provider_factory=no_network, replay=state)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        c = http.client.HTTPConnection('127.0.0.1', srv.server_address[1], timeout=10)
        c.request('GET', '/api/state', headers={'X-TrustRail-Token': srv.token})
        got = json.loads(c.getresponse().read())
        self.assertTrue(got['console']['replayed'])
        c.request('POST', '/api/keep-hold', body=b'{}', headers={'Content-Type': 'application/json', 'X-TrustRail-Token': srv.token})
        r = c.getresponse()
        self.assertEqual(r.status, 403)
        r.read()
        c.close()


if __name__ == '__main__':
    unittest.main()
