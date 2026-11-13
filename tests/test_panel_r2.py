"""Regressions from the round-2 re-acceptance panel (t1_token.py, attacks.py T2-T6, refuterB hostile_b.py)."""
import http.client
import json
import re
import tempfile
import threading
import unittest
from pathlib import Path

from trustrail import inbox, web
from trustrail.airwallex import Airwallex
from trustrail.core import Ledger, compare_details
from tests.test_inbox import ACME_IBAN, NEW_IBAN, seed
from tests.test_panel_regressions import EUR_DETAILS, GBP_DETAILS, Fake, hold_mail

A = 'Acme Accounts <billing@acme-tools.de>'
B = 'Brit Parts Ltd <accounts@britparts.co.uk>'
S = 'Shenzhen Motor Co <finance@shenzhen-motor.cn>'
OLDI = 'DE89 3704 0044 0532 0130 00'


def em(frm, subj, body, mid):
    return {'from': frm, 'reply_to': None, 'subject': subj, 'body': body, 'received_at': '2026-11-09T12:00:00Z', 'message_id': mid}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'l.sqlite3')

    def trusted(self, ledger, p):
        ledger.add_supplier('acme', EUR_DETAILS, accepts_card=False)
        ledger.set_contact('acme', 'acme-tools.de', 'Acme Tools GmbH')
        ledger.validate_supplier('acme', p)
        ledger.attest_delivery('acme', reviewer='human', evidence_ref='bl-1')


class TokenIsNotHandedOut(Base):
    def test_t1_get_page_and_assets_carry_no_token_and_cannot_unlock_actions(self):
        L, p = Ledger(self.path), Fake()
        self.trusted(L, p)
        L.add_invoice('w1', 'acme', '1000.00', 'EUR')
        inbox.ingest(L, hold_mail('<w@x>'), env={})
        srv = web.make_server(self.path, provider_factory=lambda: p)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)

        def req(m, u, body=None, hdr=None):
            c = http.client.HTTPConnection('127.0.0.1', srv.server_address[1], timeout=5)
            c.request(m, u, body=json.dumps(body).encode() if body is not None else None, headers=hdr or {})
            r = c.getresponse()
            d = r.read()
            c.close()
            return r.status, d
        for path in ('/', '/index.html', '/card-tile-bg.png'):
            st, data = req('GET', path)
            self.assertEqual(st, 200)
            self.assertNotIn(srv.token.encode(), data)
        self.assertEqual(req('POST', '/api/release-hold', {'supplier': 'acme', 'actor': 'x', 'reason_ref': 'y'},
                             {'Content-Type': 'application/json'})[0], 403)
        self.assertEqual(L.supplier_view('acme')['tier'], 'HOLD')
        self.assertEqual([c for c in p.calls if c[0] == 'SUBMIT'], [])

    def test_cli_banner_carries_the_token_in_the_fragment_only(self):
        srv = web.make_server(self.path, provider_factory=Fake)
        self.addCleanup(srv.server_close)
        banner = web.banner(srv)
        port = srv.server_address[1]
        self.assertIn('http://127.0.0.1:%d/#t=%s' % (port, srv.token), banner)
        html = (Path(web.STATIC) / 'index.html').read_text()
        self.assertIn('location.hash', html)
        self.assertIn('replaceState', html)


class MoneyPathRegressions(Base):
    def test_t3_odd_response_after_card_create_is_unknown_not_local_refusal(self):
        sent = []

        def tp(method, url, headers, body):
            sent.append(url.split('/api/v1')[1].split('?')[0])
            if url.endswith('/authentication/login'): return 200, {'token': 't'}
            if url.endswith('/account'): return 200, {'id': 'acct'}
            if url.endswith('/balances/current'): return 200, [{'currency': c, 'available_amount': '100000'} for c in ('EUR', 'USD')]
            if url.endswith('/issuing/cards/create'): return 201, ['card-created']
            return 200, {}
        a = Airwallex('c', 'k', 'acct', transport=tp, min_interval=0)
        a.cardholder_id = 'holder'
        L = Ledger(self.path)
        L.add_supplier('newco', EUR_DETAILS, accepts_card=True)
        L.add_invoice('c1', 'newco', '100.00', 'EUR')
        plan = L.plan('c1', a, loss_cap='500', floor='0')
        L.approve('c1', plan['digest'], actor='h')
        out = L.execute('c1', a)
        self.assertEqual(out['state'], 'UNKNOWN')
        self.assertTrue(out['request_id'])
        with self.assertRaises(ValueError):
            L.plan('c1', a, loss_cap='500', floor='0')
        L.execute('c1', a)
        self.assertEqual(sent.count('/issuing/cards/create'), 1)

    def test_t2_conversion_rechecks_approval(self):
        p = Fake('SETTLED')

        class Racy(Ledger):
            armed, n = False, 0

            def tx(self):
                if self.armed:
                    self.n += 1
                    if self.n == 2:
                        self.armed = False
                        inbox.ingest(self, hold_mail('<race@x>'), env={})
                        self.release_hold('acme', actor='h', reason_ref='cb')
                return Ledger.tx(self)
        L = Racy(self.path)
        L.add_supplier('acme', EUR_DETAILS, accepts_card=False)
        L.set_contact('acme', 'acme-tools.de', 'Acme Tools GmbH')
        L.validate_supplier('acme', p)
        L.add_invoice('d1', 'acme', '1000.00', 'EUR')
        plan = L.plan('d1', p, loss_cap='500', floor='0', fund_from='USD', fund_floor='0')
        L.approve('d1', plan['digest'], actor='h')
        L.armed = True
        L.execute('d1', p)
        self.assertEqual([c for c in p.calls if c[0] == 'convert'], [])
        self.assertEqual([c for c in p.calls if c[0] == 'SUBMIT'], [])

    def test_t4_freeze_is_recorded_only_when_the_readback_confirms(self):
        class Stubborn:
            def freeze_card(self, cid):
                return {'id': cid, 'status': 'ACTIVE'}
        L, p = Ledger(self.path), Fake()
        L.add_supplier('newco', EUR_DETAILS, accepts_card=True)
        L.add_invoice('c2', 'newco', '50.00', 'EUR')
        plan = L.plan('c2', p, loss_cap='500', floor='0')
        L.approve('c2', plan['digest'], actor='h')
        L.execute('c2', p)
        with self.assertRaises(ValueError):
            L.freeze_card('c2', Stubborn(), actor='h')
        events = [a['event'] for a in L.audit()]
        self.assertIn('FREEZE_UNCONFIRMED', events)
        self.assertNotIn('HUMAN_CARD_FROZEN', events)

    def test_t5b_operator_bank_change_refused_while_one_is_pending(self):
        L, p = Ledger(self.path), Fake()
        self.trusted(L, p)
        inbox.ingest(L, {'from': A, 'reply_to': None, 'subject': 'new bank account', 'received_at': '2026-11-09T00:00:00Z',
                         'message_id': '<b1@x>', 'body': 'Our bank account changed. New IBAN %s.' % NEW_IBAN}, env={})
        other = json.loads(json.dumps(EUR_DETAILS))
        other['beneficiary']['bank_details']['iban'] = 'GB82WEST12345698765432'
        with self.assertRaises(ValueError):
            L.bank_change('acme', other)
        s = L.supplier_view('acme')
        L.reject_bank_change('acme', s['pending_old_digest'], s['current_digest'], actor='h', reason_ref='call')
        with L.tx() as c:
            restored = json.loads(L.row(c, 'suppliers', 'acme')['details'])['beneficiary']['bank_details']['iban']
        self.assertEqual(restored, ACME_IBAN)

    def test_t6_on_file_digits_are_never_revealed_across_proposals(self):
        seen = set()
        for new_acct in ('99996132', '10996132', '10009932', '10006199'):
            new = json.loads(json.dumps(GBP_DETAILS))
            new['beneficiary']['bank_details']['account_number'] = new_acct
            old_shown = {r['field']: r for r in compare_details(GBP_DETAILS, new)}['account_number']['old']
            seen.add(old_shown.replace('[', '').replace(']', ''))
        self.assertEqual(len(seen), 1)  # one fixed mask for the on-file value, whatever the proposal
        self.assertTrue(seen.pop().endswith('32'))


class DetectionR2(Base):
    def setUp(self):
        super().setUp()
        self.L, _ = seed(self.path)

    def held(self, supplier, m, pre=()):
        for x in pre:
            inbox.ingest(self.L, x, env={})
        item = inbox.ingest(self.L, m, env={})
        tiers = {s['id']: s['tier'] for s in self.L.suppliers()}
        self.assertIn(tiers[supplier], ('HOLD', 'HOLD_BANK_CHANGE'), (m['message_id'], item['action'], item['signals']))
        return item

    def test_dash_slash_tab_and_table_separators(self):
        self.held('acme', em(A, 'Bankverbindung', 'Ab sofort bitte nur noch auf IBAN DE02–1203–0000–0000–2020–51 ueberweisen.', '<b04@x>'))
        self.L, _ = seed(self.path + '5')
        self.held('acme', em(A, 'Datos bancarios', 'Transfieran a IBAN DE02/1203/0000/0000/2020/51.', '<b05@x>'))
        self.L, _ = seed(self.path + '6')
        self.held('acme', em(A, 'Payments', 'Please do not use IBAN %s any more. Use DE02—1203—0000—0000—2020—51 from today on.' % OLDI, '<b06@x>'))
        self.L, _ = seed(self.path + '7')
        self.held('acme', em(A, 'Remittance', 'Kindly settle INV-1001 to IBAN\tDE02\t1203\t0000\t0000\t2020\t51\tthank you.', '<b07@x>'))
        self.L, _ = seed(self.path + '8')
        self.held('brit', em(B, 'Remittance advice', 'Please use the details below for all future payments.\n'
                                                     '| Account name | Brit Parts Ltd |\n| Account number | 99887766 |\n| Sort code | 20-00-00 |', '<b08@x>'))

    def test_german_spanish_and_traditional_chinese_wording(self):
        self.held('brit', em(B, 'Bankverbindung', 'Neue Kontonummer: 99887766, Bankleitzahl/Sort code bleibt 20-00-00.', '<b09@x>'))
        self.L, _ = seed(self.path + '10')
        self.held('brit', em(B, 'Cuenta', 'Numero de cuenta nuevo: 99887766 (sort code 20-00-00). Gracias.', '<b10@x>'))
        self.L, _ = seed(self.path + '11')
        self.held('shen', em(S, '銀行帳戶變更通知', '我們的銀行帳戶已變更，請將款項匯至新帳號：6222 0202 0099 8877 66', '<b11@x>'))
        self.L, _ = seed(self.path + '12')
        self.held('acme', em(A, 'Wichtig: Bankverbindung', 'Unsere Bankverbindung hat sich geaendert. Die neuen Kontodaten finden Sie '
                                                           'im Anhang. Bitte ueberweisen Sie nur noch auf das neue Konto.', '<b12@x>'))
        self.L, _ = seed(self.path + '13')
        self.held('acme', em(A, 'Cambio', 'Hemos cambiado de banco. Les enviaremos la nueva cuenta bancaria por correo.', '<b12es@x>'))

    def test_quoted_thread_with_new_iban(self):
        old = em(A, 'Invoice INV-2002', 'Invoice INV-2002\nTotal due: EUR 980.00\nIBAN %s' % OLDI, '<b20a@x>')
        self.held('acme', em(A, 'Re: Invoice INV-2002', 'Kleine Korrektur: bitte auf DE02–1203–0000–0000–2020–51.\n\n'
                                                        '> Invoice INV-2002\n> Total due: EUR 980.00\n> IBAN %s' % OLDI, '<b20b@x>'), pre=[old])

    def test_invoice_ids_do_not_cross_lines_and_variants_match(self):
        for n, (subject, body) in enumerate([('Corrected invoice', 'Corrected invoice INV 1001\nTotal due: EUR 1350.00'),
                                             ('Corrected invoice', 'Invoice INV-01001 (corrected)\nTotal due: EUR 1350.00'),
                                             ('Re: Inv. 1001', 'Corrected amount for Inv. 1001\nAmount due: EUR 1350.00'),
                                             ('Rechnungskorrektur', 'Rechnungsnr. 1001 korrigiert\nGesamt: EUR 1350.00')]):
            with self.subTest(body=body):
                item = inbox.ingest(self.L, em(A, subject, body, '<b1%d@x>' % (3 + n)), env={})
                self.assertIn('amount_mismatch', item['signals'])
                self.assertEqual(item['action'], 'QUARANTINED')

    def test_message_id_padding_and_whitespace(self):
        first = em(A, 'Invoice INV-2002', 'Invoice INV-2002\nTotal due: EUR 980.00', '<b17@acme-tools.de>')
        inbox.ingest(self.L, first, env={})
        again = inbox.ingest(self.L, em(A, 'Invoice INV-2002', 'Invoice INV-2002\nTotal due:  EUR 980.00 \n', '<b17@acme-tools.de>'), env={})
        self.assertTrue(again['duplicate'])  # whitespace-only resend
        padded = inbox.ingest(self.L, em(A, 'Invoice INV-2002', 'Our bank details changed: IBAN DE02 1203 0000 0000 2020 51',
                                         ' <b17@acme-tools.de> '), env={})
        self.assertIn('message_id_reused', padded['signals'])
        self.assertEqual(padded['action'], 'SUPPLIER_HOLD')


if __name__ == '__main__':
    unittest.main()
