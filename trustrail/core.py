"""Local, single-ledger sandbox coordinator. No automatic retries after unknown outcomes.

SUBMITTING is committed before I/O; a second transaction serializes bank changes
with the outbound call. A crash leaves SUBMITTING and must be reconciled, never
resubmitted. Holding SQLite's write lock during bounded I/O is intentional here.
"""
from contextlib import contextmanager
from decimal import Decimal, ROUND_UP
import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone

from .policy import LocalRefusal, currency, decide, money

# Invoices whose payout is not reserved yet but whose approval could still be acted on later.
STOP_UNSENT = ("UPDATE invoices SET state='BLOCKED',approval=NULL WHERE supplier=? AND state IN ('DRAFT','APPROVED')",
               # waiting on a conversion: keep the state for reconcile, but the approval no longer counts
               "UPDATE invoices SET approval=NULL WHERE supplier=? AND request_id IS NULL AND state IN ('CONVERTING','UNKNOWN')")

# Sandbox authorization checks an operator may run on an issued single-use card, in display order.
CARD_CHECKS = ('over_limit', 'other_currency', 'invoice_amount')
# Audit rows a later correction may re-name (appended as a new row): event written -> what it should have said.
AUDIT_CORRECTIONS = {'HUMAN_CARD_FROZEN': ('CARD_ALREADY_CLOSED', 'CARD_ALREADY_INACTIVE')}


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(obj):
    return hashlib.sha256(canonical(obj).encode()).hexdigest()


def required_text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('explicit nonempty text required')
    return value


class Ledger:
    def __init__(self, path, *, clock=None):
        self.path = os.fspath(path)
        # Injectable clock only so the offline demo world can date its steps; production uses UTC now.
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        with self.tx() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS suppliers (
              id TEXT PRIMARY KEY, details TEXT NOT NULL, revision INTEGER NOT NULL,
              accepts_card INTEGER NOT NULL, validated INTEGER NOT NULL DEFAULT 0,
              trusted INTEGER NOT NULL DEFAULT 0, pending_old TEXT);
            CREATE TABLE IF NOT EXISTS invoices (
              id TEXT PRIMARY KEY, supplier TEXT NOT NULL, amount TEXT NOT NULL,
              currency TEXT NOT NULL, state TEXT NOT NULL, plan TEXT,
              approval TEXT, request_id TEXT UNIQUE, result TEXT);
            CREATE TABLE IF NOT EXISTS audit (
              seq INTEGER PRIMARY KEY, at TEXT NOT NULL, subject TEXT NOT NULL,
              event TEXT NOT NULL, fingerprint TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS supplier_contacts (
              supplier TEXT PRIMARY KEY, domain TEXT NOT NULL, name TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS superseded_details (
              seq INTEGER PRIMARY KEY, supplier TEXT NOT NULL, revision INTEGER NOT NULL,
              details TEXT NOT NULL, at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS inbox (
              message_id TEXT PRIMARY KEY, received_at TEXT NOT NULL, supplier TEXT, kind TEXT NOT NULL,
              action TEXT NOT NULL, extractor TEXT NOT NULL, fallback TEXT, fields TEXT NOT NULL,
              signals TEXT NOT NULL, at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS conversions (
              invoice TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, state TEXT NOT NULL, result TEXT);
            CREATE TABLE IF NOT EXISTS hold_causes (
              id INTEGER PRIMARY KEY, supplier TEXT NOT NULL, fingerprint TEXT NOT NULL, message_id TEXT,
              reasons TEXT NOT NULL, at TEXT NOT NULL, released_at TEXT, released_by TEXT);
            CREATE TABLE IF NOT EXISTS holds (
              supplier TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS balances (
              seq INTEGER PRIMARY KEY, at TEXT NOT NULL, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS card_checks (
              seq INTEGER PRIMARY KEY, invoice TEXT NOT NULL, label TEXT NOT NULL, amount TEXT NOT NULL,
              currency TEXT NOT NULL, result TEXT NOT NULL, at TEXT NOT NULL);
            ''')
            # Older ledgers kept one hold row per supplier; carry each over as one open cause.
            c.execute("INSERT INTO hold_causes(supplier,fingerprint,reasons,at) SELECT supplier,fingerprint,'[]',at FROM holds")
            c.execute('DELETE FROM holds')
        os.chmod(self.path, 0o600)

    @contextmanager
    def tx(self):
        c = sqlite3.connect(self.path, timeout=35, isolation_level=None)
        c.row_factory = sqlite3.Row
        try:
            c.execute('BEGIN IMMEDIATE')
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def now(self):
        return self.clock().isoformat()

    def event(self, c, subject, event, fingerprint):
        c.execute('INSERT INTO audit(at,subject,event,fingerprint) VALUES (?,?,?,?)', (self.now(), subject, event, fingerprint))

    def set_meta(self, key, value):
        with self.tx() as c:
            if key == 'dataset':
                # Write-once, and only on an empty ledger: a real ledger can never be mistaken for demo or back.
                used = any(c.execute('SELECT 1 FROM %s LIMIT 1' % t).fetchone() for t in ('suppliers', 'invoices', 'inbox'))
                if used or c.execute("SELECT 1 FROM meta WHERE key='dataset'").fetchone():
                    raise ValueError('dataset marker can only be set once, on an empty ledger')
            c.execute('INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)', (required_text(key), required_text(value)))

    def meta(self):
        with self.tx() as c:
            return {r['key']: r['value'] for r in c.execute('SELECT key,value FROM meta')}

    @staticmethod
    def row(c, table, ident):
        if table not in ('suppliers', 'invoices'):
            raise ValueError('invalid table')
        r = c.execute(f'SELECT * FROM {table} WHERE id=?', (ident,)).fetchone()
        if r is None:
            raise ValueError('unknown object')
        return dict(r)

    def add_supplier(self, ident, details, *, accepts_card):
        required_text(ident)
        if type(accepts_card) is not bool or not isinstance(details, dict):
            raise ValueError('invalid supplier')
        with self.tx() as c:
            try:
                c.execute('INSERT INTO suppliers(id,details,revision,accepts_card) VALUES (?,?,1,?)',
                          (ident, canonical(details), int(accepts_card)))
            except sqlite3.IntegrityError as e:
                raise ValueError('supplier already exists') from e
            self.event(c, ident, 'SUPPLIER_CREATED', digest(details))

    def set_contact(self, ident, domain, name):
        """Operator setup: the supplier's registered email domain and legal name."""
        domain, name = required_text(domain).strip().lower().rstrip('.'), required_text(name).strip()
        if not all(part and part.isascii() for part in domain.split('.')) or '.' not in domain or '@' in domain:
            raise ValueError('registered domain must be plain ASCII host name')
        with self.tx() as c:
            self.row(c, 'suppliers', ident)
            c.execute('INSERT OR REPLACE INTO supplier_contacts(supplier,domain,name) VALUES (?,?,?)',
                      (ident, domain, name))
            self.event(c, ident, 'CONTACT_REGISTERED', digest([domain, name]))

    def validate_supplier(self, ident, provider):
        with self.tx() as c:
            s = self.row(c, 'suppliers', ident)
            if s['pending_old']:
                raise ValueError('human bank-change confirmation required')
            receipt = provider.validate(json.loads(s['details']))
            if receipt.get('status') != 'OK':
                raise ValueError('beneficiary validation did not return OK')
            c.execute('UPDATE suppliers SET validated=1 WHERE id=?', (ident,))
            self.event(c, ident, 'DETAILS_VALIDATED', digest(json.loads(s['details'])))

    def attest_delivery(self, ident, *, reviewer, evidence_ref):
        required_text(reviewer)
        required_text(evidence_ref)
        with self.tx() as c:
            s = self.row(c, 'suppliers', ident)
            if not s['validated'] or s['pending_old']:
                raise ValueError('validated current details required')
            c.execute('UPDATE suppliers SET trusted=1 WHERE id=?', (ident,))
            self.event(c, ident, 'HUMAN_DELIVERY_ATTESTATION', digest([reviewer, evidence_ref, s['revision']]))

    def bank_change(self, ident, new_details):
        with self.tx() as c:
            return self._bank_change(c, ident, new_details)

    def _bank_change(self, c, ident, new_details):
        if not isinstance(new_details, dict):
            raise ValueError('bank details must be an object')
        s = self.row(c, 'suppliers', ident)
        if s['pending_old']:
            # A second change would make a later rejection restore the first proposal instead of the on-file details.
            raise ValueError('a bank change is already pending; confirm or reject it first')
        old, new = digest(json.loads(s['details'])), digest(new_details)
        if old == new:
            raise ValueError('details unchanged')
        # Keep the on-file details so a human sees old and new side by side.
        c.execute('INSERT INTO superseded_details(supplier,revision,details,at) VALUES (?,?,?,?)',
                  (ident, s['revision'], s['details'], self.now()))
        c.execute('UPDATE suppliers SET details=?,revision=revision+1,validated=0,trusted=0,pending_old=? WHERE id=?',
                  (canonical(new_details), old, ident))
        # SUBMITTING/UNKNOWN/SUBMITTED may already exist remotely: never claim cancellation.
        for sql in STOP_UNSENT:
            c.execute(sql, (ident,))
        self.event(c, ident, 'BANK_CHANGE_STOP_LOCAL_ONLY', digest([old, new]))
        return {'old_digest': old, 'new_digest': new}

    def _hold(self, c, ident, fingerprint, *, message_id=None, reasons=()):
        """One open cause per suspicious email (or human hold): revoke trust and stop unsent plans."""
        self.row(c, 'suppliers', ident)
        c.execute('INSERT INTO hold_causes(supplier,fingerprint,message_id,reasons,at) VALUES (?,?,?,?,?)',
                  (ident, fingerprint, message_id, canonical(sorted(reasons)), self.now()))
        c.execute('UPDATE suppliers SET trusted=0 WHERE id=?', (ident,))
        for sql in STOP_UNSENT:
            c.execute(sql, (ident,))
        self.event(c, ident, 'EMAIL_HOLD_LOCAL_ONLY', fingerprint)

    def confirm_bank_change(self, ident, old_digest, new_digest, *, actor, callback_ref):
        required_text(actor)
        required_text(callback_ref)
        with self.tx() as c:
            s = self.row(c, 'suppliers', ident)
            if not s['pending_old'] or s['pending_old'] != old_digest or digest(json.loads(s['details'])) != new_digest:
                raise ValueError('confirmation must bind exact old and new details')
            c.execute('UPDATE suppliers SET pending_old=NULL WHERE id=?', (ident,))
            self.event(c, ident, 'HUMAN_BANK_CHANGE_CONFIRMED', digest([old_digest, new_digest, actor, callback_ref]))

    def on_file_details(self, c, s):
        """Details that were on file before a still-pending bank change."""
        for r in c.execute('SELECT details FROM superseded_details WHERE supplier=? ORDER BY seq DESC', (s['id'],)):
            if digest(json.loads(r[0])) == s['pending_old']:
                return json.loads(r[0])
        return None

    def reject_bank_change(self, ident, old_digest, new_digest, *, actor, reason_ref):
        """Human-only: discard a proposed change and keep the on-file details. Trust is not restored."""
        required_text(actor)
        required_text(reason_ref)
        with self.tx() as c:
            s = self.row(c, 'suppliers', ident)
            if not s['pending_old'] or s['pending_old'] != old_digest or digest(json.loads(s['details'])) != new_digest:
                raise ValueError('rejection must bind exact old and new details')
            old = self.on_file_details(c, s)
            if old is None:
                raise ValueError('on-file details not found; nothing changed')
            c.execute('INSERT INTO superseded_details(supplier,revision,details,at) VALUES (?,?,?,?)',
                      (ident, s['revision'], s['details'], self.now()))
            # New revision: plans bound to the rejected proposal stay stale; validation must be repeated.
            c.execute('UPDATE suppliers SET details=?,revision=revision+1,validated=0,trusted=0,pending_old=NULL WHERE id=?',
                      (canonical(old), ident))
            self.event(c, ident, 'HUMAN_BANK_CHANGE_REJECTED', digest([old_digest, new_digest, actor, reason_ref]))

    def hold(self, ident, *, actor, reason_ref):
        """Human action: put a supplier on hold (trust revoked, unsent plans blocked)."""
        required_text(actor)
        required_text(reason_ref)
        with self.tx() as c:
            self._hold(c, ident, digest(['human', actor, reason_ref]), reasons=['human_hold'])
            self.event(c, ident, 'HUMAN_HOLD', digest([actor, reason_ref]))

    def keep_hold(self, ident, *, actor, reason_ref):
        """Human action: record that a person reviewed a stop and keeps it. Changes nothing else."""
        required_text(actor)
        required_text(reason_ref)
        with self.tx() as c:
            s = self.row(c, 'suppliers', ident)
            if not self.open_causes(c, ident) and not s['pending_old']:
                raise ValueError('supplier is not on hold')
            self.event(c, ident, 'HUMAN_KEPT_HOLD', digest([actor, reason_ref]))

    def release_hold(self, ident, *, actor, reason_ref, cause_ids=None):
        """Human-only, after an out-of-band check, for the causes the person reviewed. Other open causes stay.

        Without cause ids it releases only when exactly one cause is open. Trust is not restored."""
        required_text(actor)
        required_text(reason_ref)
        with self.tx() as c:
            self.row(c, 'suppliers', ident)
            open_ids = [r['id'] for r in self.open_causes(c, ident)]
            if not open_ids:
                raise ValueError('no hold to release')
            if cause_ids is None:
                if len(open_ids) > 1:
                    raise ValueError('several open hold causes; name the ones you reviewed')
                cause_ids = open_ids
            if not isinstance(cause_ids, list) or not cause_ids or any(type(x) is not int or x not in open_ids for x in cause_ids):
                raise ValueError('unknown or already released hold cause')
            marker = digest([actor, reason_ref])
            for cid in cause_ids:
                c.execute('UPDATE hold_causes SET released_at=?, released_by=? WHERE id=?', (self.now(), marker, cid))
            self.event(c, ident, 'HUMAN_HOLD_RELEASED', digest([actor, reason_ref, sorted(cause_ids)]))

    @staticmethod
    def open_causes(c, supplier):
        return [dict(r) for r in c.execute('SELECT id,fingerprint,message_id,reasons,at FROM hold_causes '
                                           'WHERE supplier=? AND released_at IS NULL ORDER BY id', (supplier,))]

    @staticmethod
    def on_hold(c, supplier):
        if c.execute('SELECT 1 FROM hold_causes WHERE supplier=? AND released_at IS NULL', (supplier,)).fetchone():
            raise ValueError('supplier on hold; human review required')

    def still_payable(self, c, p, s):
        """Re-checked in every transaction that leads to a remote call, not only at planning."""
        self.on_hold(c, s['id'])
        if p['route'] == 'TRANSFER' and not s['trusted']:
            raise ValueError('full transfer needs a trusted supplier; plan again')

    def add_invoice(self, ident, supplier, amount, ccy):
        required_text(ident)
        with self.tx() as c:
            self._insert_invoice(c, ident, supplier, amount, ccy)

    def _insert_invoice(self, c, ident, supplier, amount, ccy):
        required_text(ident)
        amount, ccy = str(money(amount)), currency(ccy)
        self.row(c, 'suppliers', supplier)
        try:
            c.execute("INSERT INTO invoices(id,supplier,amount,currency,state) VALUES (?,?,?,?,'DRAFT')",
                      (ident, supplier, amount, ccy))
        except sqlite3.IntegrityError as e:
            raise ValueError('invoice already exists; immutable') from e
        self.event(c, ident, 'INVOICE_CREATED', digest([supplier, amount, ccy]))

    @staticmethod
    def available(c, provider, ccy, exclude):
        balance = money(provider.balances().get(ccy, '0'), zero=True)
        rows = c.execute("SELECT plan,currency FROM invoices WHERE id!=? AND plan IS NOT NULL AND "
                         "state IN ('SUBMITTING','UNKNOWN','SUBMITTED','CONVERTING')", (exclude,))
        # Conservative reservations remain until a later reconciliation design releases them.
        reserved = Decimal('0')
        for plan, plan_ccy in rows:
            plan = json.loads(plan)
            if plan_ccy == ccy:
                reserved += Decimal(plan['cap'])
            if (plan.get('funding') or {}).get('currency') == ccy:
                reserved += Decimal(plan['funding']['cap'])
        return max(Decimal('0'), balance - reserved)

    @staticmethod
    def conversion(c, ident):
        r = c.execute('SELECT * FROM conversions WHERE invoice=?', (ident,)).fetchone()
        return dict(r) if r else None

    def funding(self, c, provider, ident, p, fund_from, fund_floor):
        """Which wallet a plan debits when it is not the invoice currency; the cap is bound into the plan."""
        ccy = p['currency']
        if fund_from is not None:
            if p['route'] == 'CARD':
                raise ValueError('card funding follows the issuer billing wallet; fund_from is for payouts')
            if currency(fund_from) == ccy:
                raise ValueError('fund_from must differ from the invoice currency')
            if (self.conversion(c, ident) or {}).get('state') == 'SETTLED':
                raise ValueError('invoice already funded by a settled conversion')
            via, source, buy = 'CONVERSION', fund_from, p['amount']
        elif p['route'] == 'CARD' and provider.card_billing(ccy) != ccy:
            via, source, buy = 'CARD_BILLING', provider.card_billing(ccy), p['cap']
        else:
            return None
        if fund_floor is None:
            raise ValueError('explicit fund_floor required for the funding wallet')
        q = provider.quote(source, ccy, buy)
        # 2 % on top of the quote: FX may move between approval and execution; a bigger move blocks.
        cap = (money(q['sell_amount']) * Decimal('1.02')).quantize(Decimal('.01'), rounding=ROUND_UP)
        if self.available(c, provider, source, ident) - cap < money(fund_floor, zero=True):
            raise ValueError('funding wallet floor would be breached')
        return {'currency': source, 'cap': str(cap), 'via': via, 'quote_rate': str(q['client_rate']),
                'fund_floor': str(money(fund_floor, zero=True))}

    def plan(self, ident, provider, *, loss_cap, floor, fund_from=None, fund_floor=None, keep_approved=False, no_card=False):
        with self.tx() as c:
            i = self.row(c, 'invoices', ident)
            if i['request_id']:
                raise ValueError('operation already reserved; reconcile only')
            if keep_approved and i['state'] == 'APPROVED':
                raise ValueError('invoice already approved by a person; plan unchanged')
            if i['state'] in ('CONVERTING', 'UNKNOWN'):
                raise ValueError('funding outcome uncertain; reconcile only')
            s = self.row(c, 'suppliers', i['supplier'])
            if s['pending_old']:
                raise ValueError('bank change unconfirmed')
            self.on_hold(c, s['id'])
            available = self.available(c, provider, i['currency'], ident)
            p = decide(i['amount'], i['currency'], accepts_card=bool(s['accepts_card']) and not no_card,
                       trusted=bool(s['trusted']), validated=bool(s['validated']), loss_cap=loss_cap,
                       balance=available, floor=floor, check_floor=False)
            funding = self.funding(c, provider, ident, p, fund_from, fund_floor)
            minimum = money(floor, zero=True)
            # A conversion credits the payout wallet first; card billing elsewhere leaves it untouched.
            debit = Decimal('0') if funding else Decimal(p['cap'])
            if available - debit < minimum:
                raise ValueError('currency balance floor would be breached')
            p.update(invoice=ident, supplier=i['supplier'], revision=s['revision'],
                     details_digest=digest(json.loads(s['details'])), loss_cap=str(money(loss_cap)),
                     floor=str(minimum))
            if funding:
                p['funding'] = funding
            p['digest'] = digest(p)
            c.execute("UPDATE invoices SET state='DRAFT',plan=?,approval=NULL WHERE id=?", (canonical(p), ident))
            self.event(c, ident, 'PLANNED_' + p['route'], p['digest'])
            return p

    @staticmethod
    def current(i, s):
        if not i['plan'] or s['pending_old']:
            raise ValueError('current plan required')
        p = json.loads(i['plan'])
        unsigned = {k: v for k, v in p.items() if k != 'digest'}
        if p['digest'] != digest(unsigned) or p['revision'] != s['revision'] or p['details_digest'] != digest(json.loads(s['details'])):
            raise ValueError('stale or modified plan')
        return p

    def approve(self, ident, plan_digest, *, actor):
        required_text(actor)
        with self.tx() as c:
            i = self.row(c, 'invoices', ident)
            if i['request_id'] or i['state'] != 'DRAFT':
                raise ValueError('only a current draft can be approved')
            self.on_hold(c, i['supplier'])
            p = self.current(i, self.row(c, 'suppliers', i['supplier']))
            if p['digest'] != plan_digest:
                raise ValueError('approval does not match immutable plan')
            c.execute("UPDATE invoices SET state='APPROVED',approval=? WHERE id=?", (plan_digest, ident))
            self.event(c, ident, 'APPROVED', digest([actor, plan_digest]))

    @staticmethod
    def view(i, conversion=None):
        v = {'invoice': i['id'], 'state': i['state'], 'request_id': i['request_id'],
             'plan': json.loads(i['plan']) if i['plan'] else None,
             'result': json.loads(i['result']) if i['result'] else None}
        if conversion:
            v['conversion'] = {'state': conversion['state'], 'request_id': conversion['request_id'],
                               'result': json.loads(conversion['result']) if conversion['result'] else None}
        return v

    def _view(self, c, ident):
        return self.view(self.row(c, 'invoices', ident), self.conversion(c, ident))

    def inspect(self, ident):
        with self.tx() as c:
            return self._view(c, ident)

    def _convert(self, ident, provider):
        """Explicit FX funding before a payout. Request id is durable before I/O; unknown is reconcile-only."""
        quote_failed = False
        with self.tx() as c:
            i = self.row(c, 'invoices', ident)
            conv = self.conversion(c, ident)
            try:
                p = self.current(i, self.row(c, 'suppliers', i['supplier']))
                self.still_payable(c, p, self.row(c, 'suppliers', i['supplier']))
                if i['approval'] != p['digest']:
                    raise ValueError('approval was cleared meanwhile')
            except ValueError:
                c.execute("UPDATE conversions SET state='NOT_SENT' WHERE invoice=?", (ident,))
                c.execute("UPDATE invoices SET state='BLOCKED',approval=NULL WHERE id=?", (ident,))
                self.event(c, ident, 'STOPPED_BEFORE_CONVERSION', digest(conv['request_id']))
                return
            f = p['funding']
            try:
                q = provider.quote(f['currency'], p['currency'], p['amount'])
            except Exception:
                q = None
                # Committed with this transaction; the error is raised after commit (see below).
                c.execute("UPDATE conversions SET state='NOT_SENT' WHERE invoice=?", (ident,))
                c.execute("UPDATE invoices SET state='APPROVED' WHERE id=?", (ident,))
                self.event(c, ident, 'QUOTE_FAILED_NOTHING_SENT', digest(conv['request_id']))
            if q is None:
                quote_failed = True
            elif money(q['sell_amount']) > Decimal(f['cap']):
                c.execute("UPDATE conversions SET state='NOT_SENT' WHERE invoice=?", (ident,))
                c.execute("UPDATE invoices SET state='BLOCKED',approval=NULL WHERE id=?", (ident,))
                self.event(c, ident, 'FX_ABOVE_APPROVED_FUNDING_CAP', digest([q['sell_amount'], f['cap']]))
            else:
                try:
                    r = provider.convert(conv['request_id'], {'currency': f['currency'], 'buy_currency': p['currency'],
                                                              'buy_amount': p['amount']}, q['quote_id'])
                    if not r.get('id') or not r.get('status'):
                        raise ValueError('unrecognized conversion result')
                except Exception:
                    c.execute("UPDATE conversions SET state='UNKNOWN' WHERE invoice=?", (ident,))
                    c.execute("UPDATE invoices SET state='UNKNOWN',result=? WHERE id=?",
                              (canonical({'outcome': 'CONVERSION_UNCONFIRMED_RECONCILE_ONLY'}), ident))
                    self.event(c, ident, 'CONVERSION_UNKNOWN', digest(conv['request_id']))
                    return
                settled = r['status'] == 'SETTLED'
                c.execute('UPDATE conversions SET state=?,result=? WHERE invoice=?',
                          ('SETTLED' if settled else 'SUBMITTED', canonical(r), ident))
                c.execute('UPDATE invoices SET state=?,result=? WHERE id=?',
                          ('APPROVED' if settled else 'UNKNOWN',
                           None if settled else canonical({'outcome': 'CONVERSION_NOT_SETTLED_RECONCILE_ONLY'}), ident))
                self.event(c, ident, 'CONVERSION_' + ('SETTLED' if settled else 'SUBMITTED'), digest(conv['request_id']))
        if quote_failed:
            # Raised after commit: the invoice is APPROVED again and the conversion request NOT_SENT.
            raise ValueError('FX quote failed; nothing was sent; execute again later')

    def execute(self, ident, provider):
        with self.tx() as c:
            i = self.row(c, 'invoices', ident)
            if i['request_id'] or i['state'] in ('CONVERTING', 'UNKNOWN'):
                return self._view(c, ident)
            s = self.row(c, 'suppliers', i['supplier'])
            p = self.current(i, s)
            if i['state'] != 'APPROVED' or i['approval'] != p['digest']:
                raise ValueError('explicit current approval required')
            self.still_payable(c, p, s)
            f = p.get('funding')
            conv = self.conversion(c, ident)
            needs_conversion = bool(f and f['via'] == 'CONVERSION' and (conv or {}).get('state') != 'SETTLED')
            if f and (f['via'] == 'CARD_BILLING' or needs_conversion):
                if self.available(c, provider, f['currency'], ident) - Decimal(f['cap']) < Decimal(f['fund_floor']):
                    raise ValueError('funding wallet floor changed')
                debit = Decimal('0')
            else:
                debit = Decimal(p['cap'])
            if self.available(c, provider, i['currency'], ident) - debit < Decimal(p['floor']):
                raise ValueError('currency floor changed')
            if needs_conversion:
                rid = str(uuid.uuid4())
                c.execute('INSERT OR REPLACE INTO conversions(invoice,request_id,state) VALUES (?,?,?)',
                          (ident, rid, 'SUBMITTING'))
                c.execute("UPDATE invoices SET state='CONVERTING' WHERE id=?", (ident,))
                self.event(c, ident, 'CONVERSION_RESERVED', p['digest'])
            else:
                # Same transaction as the checks: concurrent executors serialize on this reservation.
                request_id = str(uuid.uuid4())
                c.execute("UPDATE invoices SET request_id=?,state='SUBMITTING' WHERE id=?", (request_id, ident))
                self.event(c, ident, 'RESERVED', p['digest'])
        if needs_conversion:
            self._convert(ident, provider)
            with self.tx() as c:
                if self.row(c, 'invoices', ident)['state'] != 'APPROVED':
                    return self._view(c, ident)
            return self.execute(ident, provider)
        with self.tx() as c:
            i = self.row(c, 'invoices', ident)
            s = self.row(c, 'suppliers', i['supplier'])
            try:
                p = self.current(i, s)
                self.still_payable(c, p, s)  # a hold may have committed after the reservation
            except ValueError:
                # Nothing was sent: release the reservation so the invoice can be planned again.
                c.execute("UPDATE invoices SET state='BLOCKED',approval=NULL,request_id=NULL WHERE id=?", (ident,))
                self.event(c, ident, 'STOPPED_BEFORE_REMOTE_CALL', digest(request_id))
                return self._view(c, ident)
            try:
                result = provider.submit(request_id, p, json.loads(s['details']))
                if not result.get('id') or not result.get('status'):
                    raise ValueError('unrecognized remote result')
                state = 'SUBMITTED'  # NEW is recorded as NEW, never SETTLED.
            except LocalRefusal:
                c.execute("UPDATE invoices SET state='BLOCKED',approval=NULL,request_id=NULL,result=? WHERE id=?",
                          (canonical({'outcome': 'REFUSED_LOCALLY_NOTHING_SENT'}), ident))
                self.event(c, ident, 'REFUSED_LOCALLY_NOTHING_SENT', digest(request_id))
                return self._view(c, ident)
            except Exception:
                result, state = {'outcome': 'UNCONFIRMED_RECONCILE_ONLY'}, 'UNKNOWN'
            c.execute('UPDATE invoices SET state=?,result=? WHERE id=?', (state, canonical(result), ident))
            self.event(c, ident, state, digest(request_id))
            return self._view(c, ident)

    def reconcile(self, ident, provider):
        with self.tx() as c:
            i = self.row(c, 'invoices', ident)
            conv = self.conversion(c, ident)
            if not i['request_id'] and conv and conv['state'] in ('SUBMITTING', 'UNKNOWN', 'SUBMITTED'):
                r = provider.find_conversion(conv['request_id'])
                if r and r.get('request_id') == conv['request_id'] and r.get('id') and r.get('status'):
                    settled = r['status'] == 'SETTLED'
                    c.execute('UPDATE conversions SET state=?,result=? WHERE invoice=?',
                              ('SETTLED' if settled else 'SUBMITTED', canonical(r), ident))
                    if settled:
                        # An approval survives only if no hold or bank change cleared it meanwhile;
                        # execute re-checks plan, hold, trust and floors either way.
                        c.execute('UPDATE invoices SET state=?,result=NULL WHERE id=?',
                                  ('APPROVED' if i['approval'] else 'BLOCKED', ident))
                    self.event(c, ident, 'CONVERSION_RECONCILED', digest(conv['request_id']))
                return self._view(c, ident)
            if not i['request_id'] or i['state'] not in ('SUBMITTING', 'UNKNOWN', 'SUBMITTED'):
                raise ValueError('no uncertain or submitted operation')
            result = provider.find(i['request_id'], json.loads(i['plan']))
            if result and result.get('request_id') == i['request_id'] and result.get('id') and result.get('status'):
                c.execute("UPDATE invoices SET state='SUBMITTED',result=? WHERE id=?", (canonical(result), ident))
                self.event(c, ident, 'RECONCILED', digest(i['request_id']))
            return self._view(c, ident)

    def freeze_card(self, ident, provider, *, actor):
        """Human action for a card issued before a bank-details notice: freeze it in the sandbox, read it back."""
        required_text(actor)
        with self.tx() as c:
            i = self.row(c, 'invoices', ident)
            plan, result = json.loads(i['plan'] or 'null'), json.loads(i['result'] or 'null')
            if not plan or plan.get('route') != 'CARD' or i['state'] != 'SUBMITTED' or not (result or {}).get('id'):
                raise ValueError('only an issued card of this invoice can be frozen')
            r = provider.freeze_card(result['id'])
            result = {**result, 'status': r.get('status')}
            frozen = result['status'] in ('INACTIVE', 'CLOSED')
            c.execute('UPDATE invoices SET result=? WHERE id=?', (canonical(result), ident))
            if r.get('updated') is False and frozen:
                # The card had already closed (single use) or been set inactive; the person's action changed nothing.
                event = 'CARD_ALREADY_' + result['status']
            else:
                event = 'HUMAN_CARD_FROZEN' if frozen else 'FREEZE_UNCONFIRMED'
            self.event(c, ident, event, digest([actor, result['id'], result['status']]))
            view = self._view(c, ident)
        if not frozen:
            raise ValueError('freeze not confirmed by the read-back; card status %s' % result['status'])
        return view

    def audit(self, limit=None):
        with self.tx() as c:
            if limit is None:
                return [dict(r) for r in c.execute('SELECT * FROM audit ORDER BY seq')]
            rows = c.execute('SELECT * FROM audit ORDER BY seq DESC LIMIT ?', (int(limit),))
            return [dict(r) for r in rows][::-1]

    def correct_event(self, seq, *, corrected_to, actor, reason_ref):
        """Appends a correction for a mis-named audit row; the original row is never edited or deleted."""
        required_text(actor)
        required_text(reason_ref)
        with self.tx() as c:
            row = c.execute('SELECT * FROM audit WHERE seq=?', (int(seq),)).fetchone()
            if not row or corrected_to not in AUDIT_CORRECTIONS.get(row['event'], ()):
                raise ValueError('no such correctable audit row')
            event = 'CORRECTION_OF_%d:%s' % (row['seq'], corrected_to)
            if c.execute("SELECT 1 FROM audit WHERE event LIKE ?", ('CORRECTION_OF_%d:%%' % row['seq'],)).fetchone():
                raise ValueError('audit row already corrected')
            self.event(c, row['subject'], event, digest([row['seq'], row['event'], corrected_to, actor, reason_ref]))

    def supplier_view(self, ident):
        with self.tx() as c:
            return self._supplier_view(c, self.row(c, 'suppliers', ident))

    @staticmethod
    def _supplier_view(c, s):
        contact = c.execute('SELECT domain,name FROM supplier_contacts WHERE supplier=?', (s['id'],)).fetchone()
        current, on_file, proposed, change = json.loads(s['details']), None, None, None
        if s['pending_old']:
            for r in c.execute('SELECT details FROM superseded_details WHERE supplier=? ORDER BY seq DESC', (s['id'],)):
                if digest(json.loads(r[0])) == s['pending_old']:
                    on_file = mask_details(json.loads(r[0]))
                    change = compare_details(json.loads(r[0]), current)
                    break
            proposed = mask_details(current, last=4)
        else:
            on_file = mask_details(current)
        causes = [{'id': r['id'], 'at': r['at'], 'message_id': r['message_id'], 'reasons': json.loads(r['reasons'])}
                  for r in Ledger.open_causes(c, s['id'])]
        tier = ('HOLD_BANK_CHANGE' if s['pending_old'] else 'HOLD' if causes else 'TRUSTED' if s['trusted']
                else 'VALIDATED' if s['validated'] else 'NEW')
        return {'id': s['id'], 'name': contact['name'] if contact else None,
                'domain': contact['domain'] if contact else None, 'tier': tier,
                'accepts_card': bool(s['accepts_card']), 'revision': s['revision'],
                'pending_bank_change': bool(s['pending_old']), 'pending_old_digest': s['pending_old'],
                'hold_since': causes[0]['at'] if causes else None, 'hold_causes': causes,
                'current_digest': digest(current), 'on_file': on_file, 'proposed': proposed, 'change': change}

    def suppliers(self):
        with self.tx() as c:
            return [self._supplier_view(c, dict(s)) for s in c.execute('SELECT * FROM suppliers ORDER BY id')]

    def invoices(self):
        with self.tx() as c:
            rows = [dict(r) for r in c.execute('SELECT * FROM invoices ORDER BY id')]
            conv = {r['invoice']: dict(r) for r in c.execute('SELECT * FROM conversions')}
        out = []
        for i in rows:
            v = self.view(i)
            v.update(supplier=i['supplier'], amount=i['amount'], currency=i['currency'])
            if i['id'] in conv:
                v['conversion'] = {'state': conv[i['id']]['state'], 'request_id': conv[i['id']]['request_id'],
                                   'result': json.loads(conv[i['id']]['result']) if conv[i['id']]['result'] else None}
            out.append(v)
        return out

    def inbox_items(self):
        with self.tx() as c:
            return [dict(r) for r in c.execute('SELECT * FROM inbox ORDER BY at, message_id')]

    def record_balances(self, data):
        if not isinstance(data, dict):
            raise ValueError('balances must be an object')
        with self.tx() as c:
            c.execute('INSERT INTO balances(at,data) VALUES (?,?)', (self.now(), canonical(data)))

    def record_card_check(self, ident, *, label, amount, currency, response):
        """Records the outcome of one sandbox authorization run against this invoice's issued card.
        Evidence only: it never moves money and never changes the invoice."""
        if label not in CARD_CHECKS:
            raise ValueError('unknown card check')
        amount = str(money(amount))
        if currency not in ('EUR', 'GBP', 'CNY', 'USD'):
            raise ValueError('unsupported explicit currency')
        if not isinstance(response, dict):
            raise ValueError('authorization response object required')
        kept = {k: response[k] for k in ('process_result', 'failure_reason', 'card_transaction_id') if response.get(k) is not None}
        with self.tx() as c:
            if (c.execute("SELECT value FROM meta WHERE key='dataset'").fetchone() or [None])[0] == 'demo':
                raise ValueError('a demo ledger records no sandbox checks')
            i = self.row(c, 'invoices', ident)
            plan, result = json.loads(i['plan'] or 'null'), json.loads(i['result'] or 'null')
            if not plan or plan.get('route') != 'CARD' or i['state'] != 'SUBMITTED' or not (result or {}).get('id'):
                raise ValueError('only an issued card of this invoice can be checked')
            if response.get('card_id') not in (None, result['id']):
                raise ValueError('authorization belongs to another card')
            if kept.get('process_result') not in ('APPROVED', 'DECLINED'):
                raise ValueError('authorization outcome missing')
            c.execute('INSERT INTO card_checks(invoice,label,amount,currency,result,at) VALUES (?,?,?,?,?,?)',
                      (ident, label, amount, currency, canonical(kept), self.now()))
            self.event(c, ident, 'CARD_CHECK_RECORDED', digest([label, amount, currency, kept]))

    def card_checks(self):
        with self.tx() as c:
            rows = [dict(r) for r in c.execute('SELECT * FROM card_checks ORDER BY seq')]
        out = {}
        for r in rows:
            out.setdefault(r['invoice'], []).append({**r, 'result': json.loads(r['result'])})
        return out

    def last_balances(self):
        with self.tx() as c:
            r = c.execute('SELECT at,data FROM balances ORDER BY seq DESC LIMIT 1').fetchone()
        return {'at': r['at'], 'data': json.loads(r['data'])} if r else None


def mask(value, keep=4):
    text = str(value)
    return text if len(text) <= keep else '*' * (len(text) - keep) + text[-keep:]


# IBAN characters after country + check digits that name the bank (and branch).
IBAN_BANK_CODE = {'DE': 8, 'GB': 10, 'FR': 10, 'ES': 8, 'IT': 11, 'NL': 4, 'BE': 3, 'AT': 5, 'CH': 5}
IDENTIFIERS = ('iban', 'account_number', 'account_routing_value1', 'swift_code')


def visible(field, value, last=4):
    """Positions safe to show: bank and branch codes in full, the account part only by its last characters."""
    n = len(value)
    if field == 'iban':
        return set(range(min(n, 4 + IBAN_BANK_CODE.get(value[:2], 4)))) | set(range(max(0, n - last), n))
    if field in ('account_routing_value1', 'swift_code'):
        return set(range(n))
    return set(range(max(0, n - last), n))


def bank_code(field, value):
    if field == 'iban':
        return value[4:4 + IBAN_BANK_CODE.get(value[:2], 4)]
    if field == 'account_routing_value1':
        return value
    if field == 'swift_code':
        return value[:4]
    return None


def show(value, positions, marked=()):
    out = ''
    for i, ch in enumerate(value):
        cell = ch if i in positions else '\u2022'
        out += '[' + cell + ']' if i in marked else cell
    return out.replace('][', '')


def mask_details(details, last=2):
    """Display form: bank/branch codes, names and routing type kept; account part only by its last characters.

    On-file values always use the same rule (last 2) so no sequence of proposals can reveal more of them."""
    bank = (details.get('beneficiary') or {}).get('bank_details') or {}
    out = {k: bank[k] for k in ('account_currency', 'bank_country_code', 'account_name', 'bank_name',
                                 'account_routing_type1') if k in bank}
    for k in IDENTIFIERS:
        if k in bank:
            out[k] = show(str(bank[k]), visible(k, str(bank[k]), last))
    out['transfer_methods'] = details.get('transfer_methods')
    return out


def revealable(field, value, diff):
    """Differing characters of a PROPOSED value we may show: at most six, at least two account characters hidden."""
    base = visible(field, value)
    budget = max(0, len(value) - len(base) - 2)
    return set([i for i in diff if i < len(value) and i not in base][:min(6, budget)])


def compare_details(old, new):
    """What a person confirming a bank change must see: every changed identifier, differing characters in [brackets].

    At most six differing characters are revealed beyond the usual mask and at least two account characters stay
    hidden, so a full account number is never printed (bank and branch codes are shown in full).
    """
    ob = (old.get('beneficiary') or {}).get('bank_details') or {}
    nb = (new.get('beneficiary') or {}).get('bank_details') or {}
    rows = []
    for k in IDENTIFIERS + ('account_name', 'bank_country_code', 'account_routing_type1'):
        o, n = ob.get(k), nb.get(k)
        if o == n:
            continue
        if k not in IDENTIFIERS:
            rows.append({'field': k, 'old': o, 'new': n, 'differs_at': None, 'bank_code': None})
            continue
        o, n = str(o or ''), str(n or '')
        diff = [i for i in range(max(len(o), len(n))) if o[i:i + 1] != n[i:i + 1]]
        rows.append({'field': k, 'differs_at': diff,
                     # The on-file value never reveals more than its fixed mask, whatever differs.
                     'old': show(o, visible(k, o, 2), set(diff)) if o else None,
                     'new': show(n, visible(k, n) | revealable(k, n, diff), set(diff)) if n else None,
                     'bank_code': {'old': bank_code(k, o) if o else None, 'new': bank_code(k, n) if n else None}})
    return rows
