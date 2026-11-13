"""Offline demo world for the console: no keys, no network, marked "demo" in /api/state.

Everything goes through the real code paths (contacts, validation, delivery attestation, email ingest,
planning, human approval, execution) against `DemoSandbox`, a local stand-in for the Airwallex sandbox.
Its object IDs start with "demo-" so they can never be mistaken for recorded sandbox IDs.
"""
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from . import inbox
from .core import Ledger

BALANCES = {'USD': '10000000.00', 'EUR': '9999994.00', 'GBP': '10000000.00', 'CNY': '10000000.00'}
RATE_PER_USD = {'CNY': Decimal('7.10'), 'EUR': Decimal('0.92'), 'GBP': Decimal('0.79'), 'USD': Decimal('1')}
OPERATOR = 'Payment desk (demo operator)'


class DemoSandbox:
    """Local stand-in for the sandbox adapter: deterministic answers, records every money call."""
    def __init__(self):
        self.submits, self.cardholder_id = [], 'demo-cardholder'

    def balances(self):
        return dict(BALANCES)

    def validate(self, details):
        return {'status': 'OK'}

    def card_billing(self, ccy):
        return {'EUR': 'EUR', 'CNY': 'USD'}[ccy]

    def quote(self, sell, buy, buy_amount):
        sell_amount = (Decimal(buy_amount) / RATE_PER_USD[buy] * RATE_PER_USD[sell]).quantize(Decimal('.01'))
        return {'quote_id': 'demo-quote', 'client_rate': str(RATE_PER_USD[buy] / RATE_PER_USD[sell]),
                'sell_amount': str(sell_amount), 'buy_amount': buy_amount}

    def submit(self, request_id, plan, details):
        self.submits.append((request_id, plan['invoice'], plan['route']))
        kind = 'card' if plan['route'] == 'CARD' else 'transfer'
        return {'id': 'demo-%s-%d' % (kind, len(self.submits)), 'status': 'ACTIVE' if kind == 'card' else 'PROCESSING',
                'request_id': request_id}

    def convert(self, request_id, funding, quote_id):
        return {'id': 'demo-conversion', 'status': 'SETTLED', 'request_id': request_id}

    def find(self, request_id, plan):
        return None

    def find_conversion(self, request_id):
        return None

    def freeze_card(self, card_id):
        return {'id': card_id, 'status': 'INACTIVE'}


def details(name, ccy, country, **bank):
    return {'beneficiary': {'entity_type': 'COMPANY', 'company_name': name,
                            'bank_details': {'account_currency': ccy, 'bank_country_code': country, 'account_name': name, **bank}},
            'transfer_methods': ['LOCAL']}


def email(sender, subject, body, received_at, mid, reply_to=None):
    return {'from': sender, 'reply_to': reply_to, 'subject': subject, 'body': body, 'received_at': received_at,
            'message_id': '<%s@demo.trustrail>' % mid}


def seed(db):
    if Path(db).exists():
        raise ValueError('demo seed needs a new ledger path')
    now = [datetime.fromisoformat('2026-10-01T09:00:00+00:00')]
    ledger, sandbox = Ledger(db, clock=lambda: now[0]), DemoSandbox()

    def at(iso):
        now[0] = datetime.fromisoformat(iso)

    def ingest(m):
        at(m['received_at'].replace('Z', '+00:00'))
        return inbox.ingest(ledger, m, env={})

    def plan_approve(invoice, *, loss_cap, execute=False, **kw):
        p = ledger.plan(invoice, sandbox, loss_cap=loss_cap, floor='0', **kw)
        ledger.approve(invoice, p['digest'], actor=OPERATOR)
        return ledger.execute(invoice, sandbox) if execute else p

    ledger.set_meta('dataset', 'demo')
    ledger.set_meta('company_name', 'Lantern & Loom Imports')
    ledger.set_meta('environment_label', 'Demo · offline stand-in, no Airwallex calls')
    suppliers = [
        ('atelier', details('Atelier Brandt GmbH', 'EUR', 'DE', iban='DE89370400440532013000'), False,
         'atelier-brandt.de', 'Atelier Brandt', '2026-10-01T10:00:00+00:00', '2026-10-15T10:00:00+00:00'),
        ('pennine', details('Pennine Weaving Ltd', 'GBP', 'GB', bank_name='Barclays', account_number='50133816',
                            account_routing_type1='sort_code', account_routing_value1='204577'), False,
         'pennine-weaving.co.uk', 'Pennine Weaving', '2026-10-22T10:00:00+00:00', '2026-10-30T10:00:00+00:00'),
        ('marlow', details('Marlow Glassworks', 'GBP', 'GB', bank_name='Lloyds', account_number='31926819',
                           account_routing_type1='sort_code', account_routing_value1='309950'), False,
         'marlow-glassworks.co.uk', 'Marlow Glassworks', '2026-10-28T10:00:00+00:00', None),
        ('foshan', details('Foshan Hongyu Ceramics', 'CNY', 'CN', account_number='6222020200112233445'), True,
         'hongyu-ceramics.cn', 'Foshan Hongyu', None, None),
    ]
    for ident, d, accepts_card, domain, short, validated, trusted in suppliers:
        at('2026-10-01T09:00:00+00:00')
        ledger.add_supplier(ident, d, accepts_card=accepts_card)
        ledger.set_contact(ident, domain, short)
        if validated:
            at(validated)
            ledger.validate_supplier(ident, sandbox)
        if trusted:
            at(trusted)
            ledger.attest_delivery(ident, reviewer=OPERATOR, evidence_ref='DEMO_DELIVERY_ATTESTATION')
    out = {}
    # Earlier invoices: not in the latest four emails.
    out['marlow_invoice'] = ingest(email('Marlow Glassworks <accounts@marlow-glassworks.co.uk>', 'Invoice MG-0093',
                                         'Invoice MG-0093\nTotal due: GBP 6,000.00\nThank you for your order.',
                                         '2026-11-06T14:02:00Z', 'mg-0093'))['action']
    plan_approve('marlow:MG-0093', loss_cap='2000', execute=True)
    out['pennine_invoice'] = ingest(email('Pennine Weaving <accounts@pennine-weaving.co.uk>', 'Invoice PW-2291',
                                          'Invoice PW-2291\nTotal due: GBP 8,250.00\nThank you for your order.',
                                          '2026-11-07T11:20:00Z', 'pw-2291'))['action']
    plan_approve('pennine:PW-2291', loss_cap='9000')
    # Today, in the order the agent saw them.
    out['atelier_invoice'] = ingest(email('Atelier Brandt <rechnung@atelier-brandt.de>', 'Invoice AB-1180',
                                          'Invoice AB-1180\nTotal due: EUR 12,400.00\nThank you for your order.',
                                          '2026-11-09T08:15:00Z', 'ab-1180'))['action']
    plan_approve('atelier:AB-1180', loss_cap='13000', execute=True)
    out['foshan_invoice'] = ingest(email('Foshan Hongyu Ceramics <sales@hongyu-ceramics.cn>', 'Invoice HY-0417',
                                         'Invoice HY-0417\nTotal due: CNY 48,600.00\nWe accept card payments.',
                                         '2026-11-09T09:31:00Z', 'hy-0417'))['action']
    ledger.plan('foshan:HY-0417', sandbox, loss_cap='60000', floor='0', fund_floor='0')
    out['marlow_shipment'] = ingest(email('Marlow Glassworks <accounts@marlow-glassworks.co.uk>', 'Shipment dispatched: order MG-0093',
                                          'Your order MG-0093 has shipped. Bill of lading attached, tracking number 7781-2291.',
                                          '2026-11-09T09:58:00Z', 'mg-0093-ship'))['action']
    out['pennine_bank_email'] = ingest(email(
        'Pennine Weaving <accounts@pennine-weavlng.co.uk>', 'Our bank details have moved',
        'Hello, our bank details have moved to Monzo.\nBank: Monzo\nSort code: 04-00-04\nAccount number: 81239921\n'
        'Please pay today to avoid delay.\nPennine Weaving Ltd', '2026-11-09T10:42:00Z', 'pennine-bank',
        reply_to='Pennine Accounts <pennine.accounts@mail-desk.test>'))['action']
    at('2026-11-09T10:43:00+00:00')
    ledger.record_balances(sandbox.balances())
    return out
