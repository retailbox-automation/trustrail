"""Breaks caught: stale approval, duplicate wire, lost request_id, unreserved floor."""
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from trustrail.core import Ledger


class Provider:
    """External boundary only; all policy, SQLite and concurrency remain real."""
    def __init__(self):
        self.calls = []
        self.fail = False
        self.observed = None
        self.result = None
        self.balance = '500.00'

    def balances(self):
        return {'EUR': self.balance, 'CNY': self.balance}

    def validate(self, details):
        return {'status': 'OK'}

    def submit(self, request_id, plan, details):
        self.calls.append((request_id, json.loads(json.dumps(plan)), details))
        if self.observed:
            self.observed(request_id)
        if self.fail:
            raise TimeoutError('outcome not known')
        self.result = {'id': 'transfer-fixture-1', 'status': 'NEW', 'request_id': request_id}
        return self.result

    def find(self, request_id, plan):
        return self.result


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'ledger.sqlite3')
        self.p = Provider()
        self.l = Ledger(self.path)
        self.details = {'beneficiary': {'entity_type': 'COMPANY', 'bank_details': {
            'account_currency': 'EUR', 'bank_country_code': 'DE',
            'iban': 'SYNTHETIC-OLD', 'account_name': 'Synthetic supplier'}},
            'transfer_methods': ['LOCAL']}
        self.l.add_supplier('s1', self.details, accepts_card=False)
        self.l.validate_supplier('s1', self.p)
        self.l.add_invoice('inv1', 's1', '100', 'EUR')

    def approved(self, invoice='inv1', **kw):
        args = {'loss_cap': '100', 'floor': '100'}
        args.update(kw)
        plan = self.l.plan(invoice, self.p, **args)
        self.l.approve(invoice, plan['digest'], actor='owner')
        return plan

    def test_repeat_execution_and_restart_never_submit_twice(self):
        self.approved()
        first = self.l.execute('inv1', self.p)
        second = Ledger(self.path).execute('inv1', self.p)
        self.assertEqual(first['state'], 'SUBMITTED')
        self.assertEqual(first['result']['status'], 'NEW')
        self.assertEqual(first, second)
        self.assertEqual(len(self.p.calls), 1)

    def test_request_id_is_committed_before_network_call(self):
        self.approved()
        def observe(request_id):
            c = sqlite3.connect(self.path)
            self.addCleanup(c.close)
            row = c.execute('SELECT request_id,state FROM invoices WHERE id=?', ('inv1',)).fetchone()
            self.assertEqual(row, (request_id, 'SUBMITTING'))
        self.p.observed = observe
        self.l.execute('inv1', self.p)

    def test_timeout_is_unknown_durable_and_reconciles_without_repayment(self):
        self.approved()
        self.p.fail = True
        out = self.l.execute('inv1', self.p)
        self.assertEqual(out['state'], 'UNKNOWN')
        self.assertEqual(Ledger(self.path).execute('inv1', self.p)['state'], 'UNKNOWN')
        self.assertEqual(self.l.reconcile('inv1', self.p)['state'], 'UNKNOWN')
        self.p.result = {'id': 'remote-1', 'status': 'PROCESSING', 'request_id': out['request_id']}
        self.assertEqual(self.l.reconcile('inv1', self.p)['state'], 'SUBMITTED')
        self.assertEqual(len(self.p.calls), 1)

    def test_bank_change_revokes_trust_approval_and_stale_plan(self):
        self.l.attest_delivery('s1', reviewer='owner', evidence_ref='verified-delivery-1')
        old = self.approved()
        self.assertEqual(old['route'], 'TRANSFER')
        new = json.loads(json.dumps(self.details))
        new['beneficiary']['bank_details']['iban'] = 'SYNTHETIC-NEW'
        change = self.l.bank_change('s1', new)
        with self.assertRaises(ValueError):
            self.l.execute('inv1', self.p)
        with self.assertRaises(ValueError):
            self.l.approve('inv1', old['digest'], actor='owner')
        with self.assertRaises(ValueError):
            self.l.validate_supplier('s1', self.p)
        with self.assertRaises(ValueError):
            self.l.confirm_bank_change('s1', change['old_digest'], 'wrong', actor='owner', callback_ref='on-file-1')
        self.l.confirm_bank_change('s1', change['old_digest'], change['new_digest'], actor='owner', callback_ref='on-file-1')
        self.l.validate_supplier('s1', self.p)
        fresh = self.approved()
        self.assertEqual(fresh['route'], 'DEPOSIT')
        self.assertNotEqual(fresh['digest'], old['digest'])
        self.assertEqual(self.p.calls, [])

    def test_sent_payment_is_not_falsely_cancelled_by_bank_change(self):
        self.approved()
        self.l.execute('inv1', self.p)
        new = json.loads(json.dumps(self.details))
        new['beneficiary']['bank_details']['iban'] = 'SYNTHETIC-NEW'
        self.l.bank_change('s1', new)
        self.assertEqual(self.l.inspect('inv1')['state'], 'SUBMITTED')
        self.assertEqual(len(self.p.calls), 1)

    def test_digest_tamper_and_duplicate_invoice_are_rejected(self):
        p = self.l.plan('inv1', self.p, loss_cap='100', floor='100')
        with self.assertRaises(ValueError):
            self.l.approve('inv1', 'forged-digest', actor='owner')
        with self.assertRaises(ValueError):
            self.l.add_invoice('inv1', 's1', '200', 'EUR')
        self.assertEqual(self.l.inspect('inv1')['plan']['digest'], p['digest'])

    def test_concurrent_executors_submit_one_request(self):
        self.approved()
        barrier = threading.Barrier(2)
        results, errors = [], []
        def run():
            try:
                ledger = Ledger(self.path)
                barrier.wait(timeout=5)
                results.append(ledger.execute('inv1', self.p)['state'])
            except Exception as e:
                errors.append(type(e).__name__)
        threads = [threading.Thread(target=run) for _ in range(2)]
        for t in threads: t.start()
        for t in threads: t.join(timeout=10)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(self.p.calls), 1)
        self.assertEqual(len(results), 2)

    def test_currency_floor_is_rechecked_and_reservations_cover_other_invoices(self):
        self.approved()
        self.p.balance = '129.99'
        with self.assertRaises(ValueError):
            self.l.execute('inv1', self.p)
        self.assertEqual(self.p.calls, [])
        self.p.balance = '150.00'
        self.l.execute('inv1', self.p)
        self.l.add_invoice('inv2', 's1', '100', 'EUR')
        with self.assertRaises(ValueError):
            self.approved(invoice='inv2')

    def test_unverified_shipping_boolean_does_not_promote_trust(self):
        with self.assertRaises(ValueError):
            self.l.attest_delivery('s1', reviewer='', evidence_ref=True)
        self.assertEqual(self.approved()['route'], 'DEPOSIT')

    def test_crash_after_request_reservation_requires_reconciliation_not_retry(self):
        # Swallowing a process crash or dropping the durable reservation would repay.
        self.approved()
        class Crash(BaseException):
            pass
        def crash(_):
            raise Crash()
        self.p.observed = crash
        with self.assertRaises(Crash):
            self.l.execute('inv1', self.p)
        state = Ledger(self.path).execute('inv1', self.p)
        self.assertEqual(state['state'], 'SUBMITTING')
        self.assertTrue(state['request_id'])
        self.assertEqual(len(self.p.calls), 1)

    def test_concurrent_different_invoices_cannot_jointly_breach_floor(self):
        # Removing the cross-invoice reservation would send both 30-EUR deposits.
        self.p.balance = '150.00'
        self.approved()
        self.l.add_invoice('inv2', 's1', '100', 'EUR')
        self.approved(invoice='inv2')
        barrier = threading.Barrier(2)
        outcomes = []
        def run(ident):
            ledger = Ledger(self.path)
            barrier.wait(timeout=5)
            try:
                outcomes.append(ledger.execute(ident, self.p)['state'])
            except ValueError:
                outcomes.append('FLOOR_BLOCKED')
        threads = [threading.Thread(target=run, args=(i,)) for i in ['inv1', 'inv2']]
        for t in threads: t.start()
        for t in threads: t.join(timeout=10)
        self.assertFalse(any(t.is_alive() for t in threads))
        self.assertEqual(sorted(outcomes), ['FLOOR_BLOCKED', 'SUBMITTED'])
        self.assertEqual(len(self.p.calls), 1)

    def test_audit_contains_fingerprints_not_bank_details(self):
        self.approved()
        self.l.execute('inv1', self.p)
        audit = json.dumps(self.l.audit())
        self.assertNotIn('SYNTHETIC-OLD', audit)
        self.assertNotIn('Synthetic supplier', audit)
        self.assertIn('SUBMITTED', audit)


if __name__ == '__main__':
    unittest.main()
