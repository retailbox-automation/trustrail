"""Round 5 (video polish): issued-card actions, unknown bank fields, freeze of a card that is already closed,
audit corrections that append and never rewrite, balances snapshot after a money step."""
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from trustrail import console, inbox, web
from trustrail.airwallex import Airwallex, BASE
from trustrail.core import Ledger
from tests.test_inbox import NoMoney, seed

CARD_ID = 'c0a8012e-0000-4000-8000-000000000009'
PAGE = (Path(console.__file__).parent / 'static' / 'index.html').read_text()


class Card:
    cardholder_id = 'h'

    def __init__(self):
        self.balance_reads = 0

    def balances(self):
        self.balance_reads += 1
        return {'CNY': '100000.00', 'USD': '100000.00', 'EUR': '100000.00', 'GBP': '100000.00'}

    def card_billing(self, ccy):
        return {'EUR': 'EUR', 'CNY': 'USD'}[ccy]

    def quote(self, sell, buy, amount):
        return {'quote_id': 'q', 'client_rate': '7', 'sell_amount': '15.00', 'buy_amount': amount}

    def submit(self, rid, plan, details):
        return {'id': CARD_ID, 'status': 'ACTIVE', 'request_id': rid}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'l.sqlite3')
        self.L, self.p = seed(self.path)
        self.card = Card()

    def issued_card(self):
        self.L.add_invoice('shen:SM-1', 'shen', '100.00', 'CNY')
        plan = self.L.plan('shen:SM-1', self.card, loss_cap='200', floor='0', fund_floor='0')
        self.L.approve('shen:SM-1', plan['digest'], actor='operator')
        self.assertEqual(self.L.execute('shen:SM-1', self.card)['state'], 'SUBMITTED')


class IssuedCardActions(Base):
    def actions(self):
        return console.build(self.L)['heroes']['shen:SM-1']['actions']

    def test_actions_follow_the_invoice_state(self):
        self.L.add_invoice('shen:SM-1', 'shen', '100.00', 'CNY')
        plan = self.L.plan('shen:SM-1', self.card, loss_cap='200', floor='0', fund_floor='0')
        self.assertEqual(self.actions(), {'approve': True, 'pay': False, 'issued': False, 'deposit': True, 'hold': True})
        self.L.approve('shen:SM-1', plan['digest'], actor='operator')
        self.assertEqual(self.actions(), {'approve': False, 'pay': True, 'issued': False, 'deposit': True, 'hold': True})
        self.L.execute('shen:SM-1', self.card)
        self.assertEqual(self.actions(), {'approve': False, 'pay': False, 'issued': True, 'deposit': False, 'hold': True})

    def test_page_shows_an_issued_state_instead_of_an_approve_button(self):
        self.assertIn('data-action="card_issued"', PAGE)
        self.assertIn('h.actions', PAGE)
        issued = PAGE.split('data-action="card_issued"')[0].rsplit('<button', 1)[1] + PAGE.split('data-action="card_issued"')[1].split('</button>')[0]
        self.assertIn('btn--disabled', issued)
        self.assertIn('disabled', issued.replace('btn--disabled', ''))


class UnknownBankFields(Base):
    def stopped_slots(self, body):
        L, p = self.L, NoMoney()
        L.add_supplier('pen', {'beneficiary': {'entity_type': 'COMPANY', 'company_name': 'Pennine Weaving Ltd', 'bank_details': {
            'account_currency': 'GBP', 'bank_country_code': 'GB', 'account_name': 'Pennine Weaving Ltd', 'account_number': '10006132',
            'account_routing_type1': 'sort_code', 'account_routing_value1': '040568'}}, 'transfer_methods': ['LOCAL']},
            accepts_card=False)
        L.set_contact('pen', 'pennine-weaving.co.uk', 'Pennine Weaving')
        L.validate_supplier('pen', p)
        L.attest_delivery('pen', reviewer='owner', evidence_ref='delivery-check-1')
        L.add_invoice('pen:PW-1', 'pen', '8250.00', 'GBP')
        plan = L.plan('pen:PW-1', p, loss_cap='9000', floor='0')
        L.approve('pen:PW-1', plan['digest'], actor='owner')
        inbox.ingest(L, {'from': 'Pennine Weaving <accounts@pennine-weavlng.co.uk>', 'reply_to': None,
                         'subject': 'Our bank details have moved', 'body': body, 'received_at': '2026-11-09T10:42:00Z',
                         'message_id': '<pb-1@x>'}, env={})
        return console.build(L)['heroes']['pen:PW-1']['slots']

    def test_unknown_bank_name_is_a_dash_on_both_sides_never_invented(self):
        s = self.stopped_slots('Our bank details have moved.\nSort code: 04-00-04\nAccount number: 81239921\nPennine Weaving Ltd')
        self.assertEqual((s['onfile_bank'], s['email_bank']), ('—', '—'))
        self.assertEqual((s['onfile_sort_code'], s['email_sort_code']), ('04-05-68', '04-00-04'))

    def test_known_bank_name_still_shown(self):
        s = self.stopped_slots('Our bank details have moved to Monzo.\nBank: Monzo\nSort code: 04-00-04\nAccount number: 81239921')
        self.assertEqual((s['onfile_bank'], s['email_bank']), ('—', 'Monzo'))


def card_transport(status_before):
    calls = []

    def transport(method, url, headers, body):
        path = url.removeprefix(BASE)
        calls.append((method, path))
        if path == '/authentication/login':
            return 201, {'token': 't'}
        if path == '/account':
            return 200, {'id': 'acct_fixture'}
        if path.endswith('/update'):
            return 200, {'card_status': body['card_status']}
        if path == '/issuing/cards/' + CARD_ID:
            updated = any(c[1].endswith('/update') for c in calls)
            return 200, {'card_id': CARD_ID, 'card_status': 'INACTIVE' if updated else status_before, 'metadata': {'lane': 'CT0511-AWX'}}
        raise AssertionError(path)
    return transport, calls


class FreezeAlreadyClosed(Base):
    def test_closed_card_is_recorded_as_already_closed_without_an_update_call(self):
        self.issued_card()
        transport, calls = card_transport('CLOSED')
        a = Airwallex('c', 'k', 'acct_fixture', transport=transport, min_interval=0)
        out = self.L.freeze_card('shen:SM-1', a, actor='operator')
        self.assertEqual(out['result']['status'], 'CLOSED')
        events = [x['event'] for x in self.L.audit()]
        self.assertIn('CARD_ALREADY_CLOSED', events)
        self.assertNotIn('HUMAN_CARD_FROZEN', events)
        self.assertEqual([c for c in calls if c[1].endswith('/update')], [])

    def test_active_card_is_still_frozen_by_the_person(self):
        self.issued_card()
        transport, calls = card_transport('ACTIVE')
        a = Airwallex('c', 'k', 'acct_fixture', transport=transport, min_interval=0)
        self.assertEqual(self.L.freeze_card('shen:SM-1', a, actor='operator')['result']['status'], 'INACTIVE')
        self.assertIn('HUMAN_CARD_FROZEN', [x['event'] for x in self.L.audit()])
        self.assertEqual(len([c for c in calls if c[1].endswith('/update')]), 1)


class AuditCorrection(Base):
    def test_correction_appends_a_row_and_leaves_history_untouched(self):
        self.issued_card()
        with self.L.tx() as c:  # the row the pre-round-5 code wrote for a card that had already closed itself
            self.L.event(c, 'shen:SM-1', 'HUMAN_CARD_FROZEN', 'f' * 64)
        before = self.L.audit()
        seq = before[-1]['seq']
        with self.assertRaises(ValueError):
            self.L.correct_event(seq, corrected_to='SOMETHING_ELSE', actor='lane', reason_ref='x')
        with self.assertRaises(ValueError):
            self.L.correct_event(seq - 1, corrected_to='CARD_ALREADY_CLOSED', actor='lane', reason_ref='x')
        self.L.correct_event(seq, corrected_to='CARD_ALREADY_CLOSED', actor='lane', reason_ref='calls.jsonl: no update call')
        after = self.L.audit()
        self.assertEqual(after[:len(before)], before)
        self.assertEqual((after[-1]['subject'], after[-1]['event']), ('shen:SM-1', 'CORRECTION_OF_%d:CARD_ALREADY_CLOSED' % seq))
        with self.assertRaises(ValueError):
            self.L.correct_event(seq, corrected_to='CARD_ALREADY_CLOSED', actor='lane', reason_ref='again')


class ReplayOfARecordedConsoleRun(unittest.TestCase):
    def test_final_states_names_and_activity(self):
        from trustrail import replay
        with tempfile.TemporaryDirectory() as d:
            run = Path(d) / 'live-run'
            run.mkdir()
            plan = {'route': 'TRANSFER', 'amount': '8250.00', 'cap': '8250.00', 'currency': 'GBP', 'digest': 'a' * 64}
            card = {'route': 'CARD', 'amount': '48600.00', 'cap': '50058.00', 'currency': 'CNY', 'digest': 'b' * 64}
            rows = [{'at': '2026-11-09T20:29:52+00:00', 'step': 'plan', 'data': {'invoice': 'pennine:PW-2291', 'state': None, 'plan': plan}},
                    {'at': '2026-11-09T20:30:07+00:00', 'step': 'card_execute', 'data': {
                        'invoice': 'foshan:HY-0417', 'state': 'SUBMITTED', 'request_id': 'r', 'plan': card,
                        'result': {'id': CARD_ID, 'status': 'ACTIVE'}}},
                    {'at': '2026-11-09T20:30:19+00:00', 'step': 'card_check', 'data': {'process_result': 'DECLINED',
                                                                                    'failure_reason': 'LIMIT_EXCEEDED'}},
                    {'at': '2026-11-09T20:31:10+00:00', 'step': 'shot', 'data': {'file': '09-balances.png'}}]
            (run / 'events.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
            (run / 'summary.json').write_text(json.dumps({'finished': '2026-11-09T20:31:12Z', 'invoices': [
                {'invoice': 'pennine:PW-2291', 'state': 'BLOCKED', 'request_id': None, 'result': None, 'plan': plan}]}))
            view = console.build_replay(replay.load(d))
        queue = {r['invoice']: r for r in view['queue']}
        self.assertEqual(queue['pennine:PW-2291']['status'], 'Stopped')
        self.assertEqual(queue['pennine:PW-2291']['supplier'], '09.11 · Pennine PW-2291')
        self.assertEqual(queue['foshan:HY-0417']['supplier'], '09.11 · Foshan HY-0417')
        self.assertNotIn('shot', ' '.join(a['text'] for a in view['activity']))
        self.assertTrue(all(r.get('replayed') for r in view['queue']))


class BalancesAfterMoneyStep(Base):
    def test_execute_from_the_console_records_a_fresh_balances_snapshot(self):
        self.L.add_invoice('shen:SM-1', 'shen', '100.00', 'CNY')
        plan = self.L.plan('shen:SM-1', self.card, loss_cap='200', floor='0', fund_floor='0')
        self.L.approve('shen:SM-1', plan['digest'], actor='operator')
        srv = web.make_server(self.path, provider_factory=lambda: self.card)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        before = len(self.L.audit())
        snapshots = self.L.last_balances()
        c = http.client.HTTPConnection('127.0.0.1', srv.server_address[1], timeout=10)
        c.request('POST', '/api/execute', body=json.dumps({'invoice': 'shen:SM-1', 'cardholder_id': 'h'}),
                  headers={'Content-Type': 'application/json', 'X-TrustRail-Token': srv.token})
        r = c.getresponse()
        self.assertEqual((r.status, json.loads(r.read())['state']), (200, 'SUBMITTED'))
        self.assertIsNone(snapshots)
        self.assertEqual(self.L.last_balances()['data']['USD'], '100000.00')
        self.assertGreater(len(self.L.audit()), before)


if __name__ == '__main__':
    unittest.main()
