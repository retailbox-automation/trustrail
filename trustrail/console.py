"""View model for the payment-desk console (design variant C). Formatting and selection only, no money decisions.

`build(ledger)` and `build_replay(replay_state)` return the same shape: page-level `slots` (one string per
data-slot in static/index.html), structured lists for the parts the page renders as rows, and one `hero` per
invoice that can be shown in the top panel ("stopped" or "card").
"""
from datetime import datetime
from decimal import Decimal
import json
import re

from .core import CARD_CHECKS, digest
from .policy import DEPOSIT_SHARE

SYMBOL = {'EUR': '€', 'GBP': '£', 'CNY': '¥', 'USD': '$'}
UNKNOWN = '—'
# Run bookkeeping in evidence files (step headings, screenshots, preflight); not sandbox steps.
BOOKKEEPING = {'step', 'shot', 'preflight'}
# Data-slots the page fills from structure rather than one flat string (kept for the contract test).
COMPUTED = ()
# Measured sandbox behaviour shown on a card plan (live/demo); replay shows what its own evidence recorded.
SANDBOX_CHECKS = {'decline_over_limit': ('LIMIT_EXCEEDED', 'docs/2026-11-09-c-awx/live-20261109T1533Z (CNY 103.01 on a 103.00 card)'),
                  'decline_other_currency': ('CURRENCY_NOT_ALLOWED', 'docs/2026-11-09-c-awx/live-20261109T1533Z (USD on a CNY card)'),
                  'decline_second_charge': ('LIMIT_EXCEEDED', 'docs/2026-10-20-trustrail-kb/spike (second charge after the cap was used)')}
# Wording that names where an action went: a demo ledger never claims a sandbox call it did not make.
NOTES = {'live': {'checks_title': 'Checked in the Airwallex sandbox', 'card_issued': 'Card issued in the sandbox.',
                  'sent': 'Sent to the sandbox.'},
         'demo': {'checks_title': 'Measured earlier in the Airwallex sandbox',
                  'card_issued': 'Card issued by the offline stand-in (demo).',
                  'sent': 'Sent to the offline stand-in (demo), no Airwallex call.'},
         'replay': {'checks_title': 'Recorded in the Airwallex sandbox', 'card_issued': 'Card issued in the sandbox (recorded).',
                    'sent': 'Replay is read-only.'}}
# Rows of the card panel when this card's own sandbox checks are recorded: label and the design's slot.
CHECK_ROWS = {'over_limit': ('Amount over the limit', 'decline_over_limit'),
              'other_currency': ('Other currency', 'decline_other_currency'),
              'invoice_amount': ('Invoice amount', 'decline_second_charge')}
SIGNALS = [  # display order and wording on the stopped panel
    ('lookalike_domain', 'Lookalike domain: {email_mid} vs {onfile_mid}'),
    ('display_name_spoof', 'Display name copies the supplier, sender is elsewhere'),
    ('reply_to_mismatch', 'Reply-To differs from sender'),
    ('urgency_pressure', 'Urgency: ‘{quote}’'),
    ('new_account_vs_on_file', 'New account, never paid before'),
    ('instruction_like_text', 'Instructions aimed at the AI inside the email'),
    ('message_id_reused', 'Message-ID reused with different content'),
    ('confusable_characters', 'Look-alike characters in the account number'),
    ('hidden_characters', 'Hidden characters in the email'),
    ('invalid_iban_checksum', 'IBAN checksum does not add up'),
    ('amount_mismatch', 'Amount differs from the open invoice'),
    ('unicode_domain', 'Sender domain uses non-Latin characters'),
    ('proposal_incomplete', 'New details are incomplete or unusable'),
]


def money_text(ccy, amount):
    return '%s%s' % (SYMBOL.get(ccy, ccy + ' '), '{:,.2f}'.format(Decimal(str(amount))))


def plain_amount(amount):
    return '{:,.2f}'.format(Decimal(str(amount)))


def short_digest(d):
    return '%s…%s' % (d[:4], d[-3:]) if d else ''


def when(iso, with_date=True):
    try:
        d = datetime.fromisoformat(iso.replace('Z', '+00:00'))
    except (AttributeError, ValueError):
        return iso or ''
    return '%s %d, %d · %s' % (d.strftime('%b'), d.day, d.year, d.strftime('%H:%M')) if with_date else d.strftime('%H:%M')


def date_dm(iso):
    try:
        return datetime.fromisoformat(iso.replace('Z', '+00:00')).strftime('%d.%m')
    except (AttributeError, ValueError):
        return ''


def date_only(iso):
    try:
        d = datetime.fromisoformat(iso.replace('Z', '+00:00'))
    except (AttributeError, ValueError):
        return ''
    return '%s %d, %d' % (d.strftime('%b'), d.day, d.year)


def sort_code(value):
    v = re.sub(r'\D', '', value or '')
    return '-'.join([v[0:2], v[2:4], v[4:6]]) if len(v) == 6 else (value or '')


def last4(value):
    return '••••' + str(value)[-4:] if value else ''


def domain_marks(email_domain, onfile_domain):
    """Split two domains into common prefix, differing middle and common suffix at '-' / '.' boundaries."""
    a, b = email_domain or '', onfile_domain or ''
    p = 0
    while p < min(len(a), len(b)) and a[p] == b[p]:
        p += 1
    cut = max(a.rfind('-', 0, p), a.rfind('.', 0, p)) + 1
    s = 0
    while s < min(len(a), len(b)) - cut and a[-1 - s] == b[-1 - s]:
        s += 1
    tail = a[len(a) - s:]
    dot = tail.find('.')
    suffix = tail[dot:] if dot >= 0 else ''
    return [a[:cut], a[cut:len(a) - len(suffix)], suffix], [b[:cut], b[cut:len(b) - len(suffix)], suffix]


def bank(details):
    return (details.get('beneficiary') or {}).get('bank_details') or {}


def method_lines(plan, ccy):
    route = (plan or {}).get('route')
    if route == 'CARD':
        return ['Single-use card,', 'limit ' + money_text(ccy, plan['cap'])]
    if route == 'DEPOSIT':
        return ['Deposit to verified account,', 'balance after shipment']
    if route == 'TRANSFER':
        return ['Local transfer (SEPA)' if ccy == 'EUR' else 'Local transfer']
    return ['Not planned']


def status_of(inv, tier):
    """(text, queue class, timeline pill class, rail node, rank in queue, rank on the rail)."""
    plan, state = inv.get('plan') or {}, inv['state']
    route, remote = plan.get('route'), (inv.get('result') or {}).get('status')
    if state == 'BLOCKED' or (tier in ('HOLD', 'HOLD_BANK_CHANGE') and state in ('DRAFT', 'APPROVED')):
        return 'Stopped', 'red', 'red', 'barrier', 2, 2
    if state == 'SUBMITTED' and route == 'DEPOSIT':
        return 'Awaiting shipment proof', 'orange', 'gray', 'ring_gray', 3, 3
    if state == 'SUBMITTED' and route == 'CARD':
        return 'Card issued', 'green', 'green', 'check', 1, 0
    if state == 'SUBMITTED':
        return {'PAID': 'Paid', 'SENT': 'Sent'}.get(remote, 'Sent, processing'), 'green', 'green', 'check', 1, 0
    if state == 'APPROVED':
        return 'Approved, not sent', 'blue', 'blue', 'ring_blue', 0, 1
    if state == 'DRAFT' and plan:
        return 'Ready for approval', 'blue', 'blue', 'ring_blue', 0, 1
    if state in ('UNKNOWN', 'SUBMITTING', 'CONVERTING'):
        return 'Outcome unknown, reconcile', 'orange', 'gray', 'ring_gray', 4, 4
    return 'Not planned', 'orange', 'gray', 'ring_gray', 5, 5


TRUST = {'TRUSTED': ('Trusted', 'trusted'), 'VALIDATED': ('Verified', 'verified'), 'NEW': ('New', 'new'),
         'HOLD': ('Trust revoked', 'revoked'), 'HOLD_BANK_CHANGE': ('Trust revoked', 'revoked')}


def short_name(supplier):
    return supplier.get('name') or supplier['id']


def full_name(supplier, details):
    return (details.get('beneficiary') or {}).get('company_name') or supplier.get('name') or supplier['id']


def build(ledger):
    meta = ledger.meta()
    with ledger.tx() as c:
        sup_rows = {r['id']: dict(r) for r in c.execute('SELECT * FROM suppliers')}
        validated_at = {}
        for r in c.execute("SELECT subject, at FROM audit WHERE event='DETAILS_VALIDATED' ORDER BY seq"):
            validated_at[r['subject']] = r['at']
        superseded = {}
        for s in sup_rows.values():
            if s['pending_old']:
                superseded[s['id']] = ledger.on_file_details(c, s)
    views = {s['id']: s for s in ledger.suppliers()}
    checks = ledger.card_checks()
    items = [dict(r) for r in ledger.inbox_items()]
    for it in items:
        it['fields'] = json.loads(it['fields'])
        it['signals'] = json.loads(it['signals'])
    rows = []
    for inv in ledger.invoices():
        v, details = views[inv['supplier']], json.loads(sup_rows[inv['supplier']]['details'])
        text, qcls, pill, node, qrank, rrank = status_of(inv, v['tier'])
        plan = inv.get('plan') or {}
        rows.append({'invoice': inv['invoice'], 'supplier_id': v['id'], 'supplier': full_name(v, details), 'short': short_name(v),
                     'country': bank(details).get('bank_country_code', ''), 'invoice_no': inv['invoice'].split(':', 1)[-1],
                     'currency': inv['currency'], 'amount': money_text(inv['currency'], plan['amount'] if plan.get('route') == 'DEPOSIT' else inv['amount']),
                     'total': money_text(inv['currency'], inv['amount']) if plan.get('route') == 'DEPOSIT' else None,
                     'method': method_lines(plan, inv['currency']), 'card_limit': money_text(inv['currency'], plan['cap']) if plan.get('route') == 'CARD' else None,
                     'trust': TRUST[v['tier']][0], 'trust_class': TRUST[v['tier']][1], 'tier': v['tier'],
                     'status': text, 'status_class': qcls, 'pill_class': pill, 'node': node, 'qrank': qrank, 'rrank': rrank,
                     'kind': 'stopped' if text == 'Stopped' else ('card' if plan.get('route') == 'CARD' else 'other'),
                     'state': inv['state'], 'route': plan.get('route')})
    queue = sorted(rows, key=lambda r: (r['qrank'], r['invoice']))
    timeline = sorted(rows, key=lambda r: (r['rrank'], r['invoice']))
    slots = {'company_name': meta.get('company_name', 'TrustRail'),
             'environment_label': meta.get('environment_label', 'Airwallex sandbox · test money')}
    snap = ledger.last_balances()
    for ccy in ('USD', 'EUR', 'GBP', 'CNY'):
        value = (snap or {}).get('data', {}).get(ccy)
        slots['balance_' + ccy.lower()] = plain_amount(value) if value is not None else '—'
    fill_lists(slots, queue, timeline)
    ladder = ladder_items(rows)
    fill_ladder(slots, ladder)
    activity = []
    for it in sorted(items, key=lambda x: x['received_at'], reverse=True)[:4]:
        activity.append({'time': when(it['received_at'], with_date=False), 'text': activity_text(it, rows, views)})
    for n, a in enumerate(activity, 1):
        slots['act_%d_time' % n], slots['act_%d_text' % n] = a['time'], a['text']
    heroes = {}
    for r in rows:
        inv = next(i for i in ledger.invoices() if i['invoice'] == r['invoice'])
        if r['kind'] == 'stopped':
            heroes[r['invoice']] = stopped_hero(r, inv, views[r['supplier_id']], json.loads(sup_rows[r['supplier_id']]['details']),
                                                superseded.get(r['supplier_id']), items, validated_at.get(r['supplier_id']))
        elif r['kind'] == 'card':
            heroes[r['invoice']] = with_checks(card_hero(r, inv, {k: v[0] for k, v in SANDBOX_CHECKS.items()}),
                                               checks.get(r['invoice']))
    default = next((r['invoice'] for r in queue if r['kind'] == 'stopped'), None) or next(
        (r['invoice'] for r in queue if r['kind'] == 'card'), None)
    is_demo = meta.get('dataset') == 'demo'
    return {'demo': is_demo, 'replayed': False, 'notes': NOTES['demo' if is_demo else 'live'], 'slots': slots, 'queue': strip(queue),
            'timeline': strip(timeline), 'ladder': ladder, 'activity': activity, 'heroes': heroes, 'default_invoice': default,
            'sandbox_checks': [{'slot': k, 'code': v[0], 'evidence': v[1]} for k, v in SANDBOX_CHECKS.items()],
            'activity_title': 'latest 4 supplier emails'}


def strip(rows):
    keep = ('invoice', 'supplier_id', 'supplier', 'short', 'country', 'invoice_no', 'amount', 'total', 'method', 'card_limit',
            'trust', 'trust_class', 'status', 'status_class', 'pill_class', 'node', 'kind', 'state', 'route', 'remote_id',
            'request_id', 'replayed')
    return [{k: r[k] for k in keep if k in r} for r in rows]


def fill_lists(slots, queue, timeline):
    for n, r in enumerate(queue, 1):
        slots.update({'q_%d_supplier' % n: r['supplier'], 'q_%d_country' % n: r['country'], 'q_%d_invoice' % n: r['invoice_no'],
                      'q_%d_amount' % n: r['amount'], 'q_%d_method' % n: ' '.join(r['method']),
                      'q_%d_trust' % n: r['trust'], 'q_%d_status' % n: r['status']})
        if r.get('card_limit'):
            slots['q_%d_card_limit' % n] = r['card_limit']
        if r.get('total'):
            slots['q_%d_total' % n] = r['total']
    for n, r in enumerate(timeline, 1):
        slots.update({'tl_%d_supplier' % n: r['short'], 'tl_%d_amount' % n: r['amount'], 'tl_%d_status' % n: r['status']})
        if r.get('total'):
            slots['tl_%d_total' % n] = r['total']


def ladder_items(rows):
    """One card per ladder position the design has: New (first), New (trust revoked), Verified, Trusted."""
    pick = lambda pred: next((r for r in sorted(rows, key=lambda r: (r['qrank'], r['invoice'])) if pred(r)), None)
    item = lambda r, note=None: r and {'invoice': r['invoice'], 'supplier': r['short'], 'amount': r['amount'],
                                       'total': r.get('total'), 'note': note}
    return {'new_1': item(pick(lambda r: r['tier'] == 'NEW')),
            'new_2': item(pick(lambda r: r['tier'] in ('HOLD', 'HOLD_BANK_CHANGE')), 'trust revoked'),
            'verified_1': item(pick(lambda r: r['tier'] == 'VALIDATED')),
            'trusted_1': item(pick(lambda r: r['tier'] == 'TRUSTED'))}


def fill_ladder(slots, ladder):
    for key, it in ladder.items():
        if not it:
            continue
        slots['ladder_%s_supplier' % key], slots['ladder_%s_amount' % key] = it['supplier'], it['amount']
        if it.get('total'):
            slots['ladder_%s_total' % key] = it['total']
        if it.get('note'):
            slots['ladder_%s_note' % key] = it['note']


def activity_text(it, rows, views):
    who = short_name(views[it['supplier']]) if it['supplier'] in views else 'Unknown sender'
    effect = (it['fields'].get('effect') or {}).get('invoice')
    inv = next((r for r in rows if r['invoice'] == effect), None)
    if it['action'] in ('SUPPLIER_HOLD', 'BANK_CHANGE_HOLD'):
        stopped = any(r['supplier_id'] == it['supplier'] and r['kind'] == 'stopped' for r in rows)
        return '%s: bank details email, %s' % (who, 'transfer stopped' if stopped else 'supplier on hold')
    if it['kind'] == 'shipment':
        return '%s: shipment notice, proof requested' % who
    if inv:
        done = {'CARD': 'card plan prepared', 'TRANSFER': 'transfer planned', 'DEPOSIT': 'deposit planned'}.get(inv['route'], 'recorded')
        if inv['state'] == 'SUBMITTED':
            done = {'CARD': 'card issued', 'TRANSFER': 'transfer sent', 'DEPOSIT': 'deposit sent'}.get(inv['route'], 'sent')
        return '%s: invoice %s, %s' % (who, inv['invoice_no'], done)
    if it['action'] == 'QUARANTINED':
        return '%s: email quarantined' % who
    return '%s: email recorded' % who


def stopped_hero(r, inv, view, details, onfile_details, items, validated):
    by_key = {it['message_id']: it for it in items}
    open_causes = view.get('hold_causes') or []
    # The panel shows the newest open cause in full; every other open cause is listed so a release names them all.
    linked = [by_key.get(c['message_id']) for c in open_causes if by_key.get(c['message_id'])]
    cause = [it for it in items if it['supplier'] == r['supplier_id'] and it['action'] in ('SUPPLIER_HOLD', 'BANK_CHANGE_HOLD')]
    cause = (sorted(linked, key=lambda x: x['received_at']) or sorted(cause, key=lambda x: x['received_at']) or [None])[-1]
    onfile = bank(onfile_details or details)
    email_bank = (cause or {}).get('fields', {}).get('bank') or {}
    if view['pending_bank_change']:
        email_bank = {**email_bank, 'sort_code': bank(details).get('account_routing_value1'),
                      'account_number': bank(details).get('account_number'), 'iban': bank(details).get('iban')}
    sender = (cause or {}).get('fields', {}).get('sender') or ''
    email_dom, onfile_dom = domain_marks(sender.rsplit('@', 1)[-1], view.get('domain') or '')
    signals = (cause or {}).get('signals', [])
    ctx = {'email_mid': email_dom[1], 'onfile_mid': onfile_dom[1], 'quote': (cause or {}).get('fields', {}).get('urgency_quote', '')}
    texts = [t.format(**ctx) for key, t in SIGNALS if key in signals]
    causes = []
    for c in open_causes:
        it = by_key.get(c['message_id'])
        first = next((t for key, t in SIGNALS if key in c['reasons'] and '{' not in t), None) or ', '.join(c['reasons']) or 'held'
        label = ('email ‘%s’ %s' % (it['fields'].get('subject', ''), when(it['received_at'], with_date=False))) if it else 'hold set by a person'
        causes.append({'id': c['id'], 'at': c['at'], 'message_id': c['message_id'], 'label': label, 'reasons': c['reasons']})
        if not it or it is not cause:
            texts.append('Also held: %s · %s' % (label, first))
    method = method_lines(inv.get('plan'), inv['currency'])[0].rstrip(',')
    amount = money_text(inv['currency'], inv['amount'])
    noun = 'transfer' if (inv.get('plan') or {}).get('route') == 'TRANSFER' else 'payment'
    if 'lookalike_domain' in signals and 'new_account_vs_on_file' in signals:
        post = ' %s: the email came from a lookalike\ndomain and asks us to pay an account we have never paid.' % noun
    elif 'new_account_vs_on_file' in signals:
        post = ' %s: the email asks us to pay an account that is not the one\non file. A person must confirm it by phone.' % noun
    else:
        post = ' %s: the supplier is on hold until a person reviews it.' % noun
    slots = {'stop_supplier_name': r['supplier'], 'stop_invoice_id': r['invoice_no'], 'stop_amount': amount, 'stop_method': method,
             'stop_status': 'Stopped', 'stop_email_subject': (cause or {}).get('fields', {}).get('subject', ''),
             'stop_email_received_at': when((cause or {}).get('received_at', '')), 'stop_email_from': sender,
             'stop_domain_on_file': view.get('domain') or '', 'stop_agent_summary_amount': amount,
             'stop_agent_summary': 'I stopped the ' + amount + post.replace('\n', ' '),
             'onfile_verified_date': date_only(validated), 'onfile_domain': ''.join(onfile_dom), 'email_domain': ''.join(email_dom),
             # Unknown fields show a dash on both sides; a bank name is never inferred from a sort code.
             'onfile_bank': onfile.get('bank_name') or UNKNOWN,
             'onfile_sort_code': sort_code(onfile.get('account_routing_value1')) or UNKNOWN,
             'onfile_account': last4(onfile.get('account_number') or onfile.get('iban')) or UNKNOWN,
             'email_bank': (cause or {}).get('fields', {}).get('bank_name') or UNKNOWN,
             'email_sort_code': sort_code(email_bank.get('sort_code')) or UNKNOWN,
             'email_account': last4(email_bank.get('account_number') or email_bank.get('iban')) or UNKNOWN}
    for n, t in enumerate(texts[:4], 1):
        slots['stop_signal_%d' % n] = t
    return {'state': 'stopped', 'invoice': r['invoice'], 'supplier': r['supplier_id'], 'slots': slots, 'signals': texts,
            'causes': causes, 'cause_ids': [c['id'] for c in causes],
            'summary': {'pre': 'I stopped the ', 'amount': amount, 'post': post},
            'domain_marks': {'onfile': onfile_dom, 'email': email_dom},
            'pending_bank_change': view['pending_bank_change'], 'pending_old_digest': view.get('pending_old_digest'),
            'current_digest': view.get('current_digest'), 'change': view.get('change')}


def card_hero(r, inv, declines):
    plan = inv['plan']
    ccy, amount = inv['currency'], money_text(inv['currency'], inv['amount'])
    limit = money_text(ccy, plan['cap'])
    status = {'DRAFT': 'Ready for approval', 'APPROVED': 'Approved', 'SUBMITTED': 'Card issued', 'BLOCKED': 'Stopped'}.get(
        inv['state'], 'Outcome unknown')
    deposit = (Decimal(inv['amount']) * DEPOSIT_SHARE).quantize(Decimal('.01'))
    pre, post = ('New supplier with unverified bank details: I prepared a single-use card\ncapped at ',
                 ' for invoice %s. Nothing is paid until you approve.' % r['invoice_no'])
    if inv['state'] == 'APPROVED':
        pre, post = 'Approved: the single-use card\ncapped at ', ' for invoice %s is ready. Paying is a separate step.' % r['invoice_no']
    elif inv['state'] == 'SUBMITTED':
        pre, post = 'A person approved it: the single-use card\ncapped at ', ' for invoice %s was issued.' % r['invoice_no']
    slots = {'card_supplier_name': r['supplier'], 'card_invoice_id': r['invoice_no'], 'card_amount': amount,
             'card_method': 'Single-use card', 'card_status': status, 'card_limit': limit, 'card_invoice_base': amount,
             'card_fx_buffer': '3%' if ccy == 'CNY' else '0%', 'card_currency': ccy, 'card_limit_button': limit,
             'card_agent_summary_limit': limit, 'card_number_masked': '•••• •••• •••• ••••',
             'card_agent_summary': (pre + limit + post).replace('\n', ' '),
             'card_note': 'New supplier, bank details not yet verified. The first payment goes on a single-use Airwallex card capped '
                          'to this invoice: the supplier can take it, a fraudster cannot redirect it.',
             'deposit_amount': money_text(ccy, deposit), 'deposit_pct': '%d%%' % (DEPOSIT_SHARE * 100),
             'plan_id': short_digest(plan.get('digest')), **declines}
    state = inv['state']
    # Which buttons a person can use in this state; an issued card shows its state, not an approve button.
    actions = {'approve': state == 'DRAFT', 'pay': state == 'APPROVED', 'issued': state == 'SUBMITTED',
               'deposit': state in ('DRAFT', 'APPROVED', 'BLOCKED'), 'hold': True}
    return {'state': 'card', 'invoice': r['invoice'], 'supplier': r['supplier_id'], 'slots': slots, 'plan_digest': plan.get('digest'),
            'invoice_state': state, 'actions': actions,
            'summary': {'pre': pre, 'amount': limit, 'post': post}}


def with_checks(hero, rows):
    """A card shows its own recorded sandbox authorizations; without them, the earlier measured codes, labelled so."""
    if not rows:
        hero['checks_title'] = 'Measured earlier in the Airwallex sandbox'
        return hero
    latest = {r['label']: r for r in rows}
    hero['checks_title'] = 'Checked in the Airwallex sandbox · this card'
    hero['check_rows'] = []
    for label in CARD_CHECKS:
        if label not in latest:
            continue
        r, (text, slot) = latest[label], CHECK_ROWS[label]
        code = r['result'].get('failure_reason') or r['result']['process_result']
        hero['slots'][slot] = code
        hero['check_rows'].append({'label': text, 'slot': slot, 'code': code, 'ok': r['result']['process_result'] == 'APPROVED',
                                   'amount': money_text(r['currency'], r['amount']), 'at': r['at']})
    return hero


def build_replay(state):
    """Same shape from recorded sandbox evidence. Every item is marked replayed; IDs come only from the evidence."""
    rows = []
    for inv in state.get('invoices', []):
        plan = inv.get('plan') or {}
        if not plan.get('currency'):
            continue
        tier = 'NEW' if plan.get('route') == 'CARD' else ('TRUSTED' if plan.get('route') == 'TRANSFER' else 'VALIDATED')
        full = {**inv, 'currency': plan['currency'], 'amount': plan.get('amount')}
        text, qcls, pill, node, qrank, rrank = status_of(full, tier)
        run = '05.11' if inv['invoice'].startswith('CT0511') else ('09.11' if inv['invoice'].startswith('CT0911') else '')
        short = re.sub(r'^(CT0511-AWX|CT0911)-', '', inv['invoice']).replace('-invoice-1', '').replace('-', ' ')
        short = re.sub(r'^(eur|gbp|cny|usd)\b', lambda m: m.group(1).upper(), short)
        if ':' in inv['invoice']:  # a console run: "<supplier>:<invoice number>", dated by its recorded step
            sup, number = inv['invoice'].split(':', 1)
            short, run = '%s %s' % (sup.capitalize(), number), (date_dm(inv.get('at')) or run)
        name = ('%s · %s' % (run, short)) if run else short  # the badge already says "Recorded sandbox evidence"
        remote = (inv.get('result') or {}).get('id')
        rows.append({'invoice': inv['invoice'], 'supplier_id': inv['invoice'], 'supplier': name, 'short': short, 'country': '',
                     'invoice_no': remote[:8] if remote else inv['invoice'][-8:], 'remote_id': remote,
                     'request_id': inv.get('request_id'),
                     'currency': plan['currency'], 'amount': money_text(plan['currency'], plan['amount']),
                     'total': None, 'method': method_lines(plan, plan['currency']),
                     'card_limit': money_text(plan['currency'], plan['cap']) if plan.get('route') == 'CARD' else None,
                     'trust': TRUST[tier][0], 'trust_class': TRUST[tier][1], 'tier': tier, 'status': text, 'status_class': qcls,
                     'pill_class': pill, 'node': node, 'qrank': qrank, 'rrank': rrank,
                     'kind': 'card' if plan.get('route') == 'CARD' else 'other', 'state': inv['state'], 'route': plan.get('route'),
                     'replayed': True})
    queue = sorted(rows, key=lambda r: (r['qrank'], r['invoice']))
    # Default: the latest recorded card (the 09.11 CNY card with its 3 % buffer) when present.
    cards = [r['invoice'] for r in queue if r['kind'] == 'card']
    default = next((i for i in cards if 'cny' in i), None) or (cards[0] if cards else None)
    timeline = sorted(rows, key=lambda r: (r['rrank'], r['invoice']))
    if default and len(timeline) > 1:
        # The design highlights the card plan in rail column 2; place the selected card there.
        timeline = [r for r in timeline if r['invoice'] != default]
        timeline.insert(1, next(r for r in rows if r['invoice'] == default))
    slots = {'company_name': 'TrustRail sandbox run', 'environment_label': 'Recorded sandbox evidence · replayed'}
    bal = (state.get('balances') or {}).get('data', {})
    for ccy in ('USD', 'EUR', 'GBP', 'CNY'):
        slots['balance_' + ccy.lower()] = plain_amount(bal[ccy]) if ccy in bal else '—'
    fill_lists(slots, queue, timeline)
    auth = state.get('objects', {}).get('authorizations', [])
    codes = [a.get('failure_reason') for a in auth if a.get('failure_reason')]
    declines = {'decline_over_limit': next((c for c in codes if c == 'LIMIT_EXCEEDED'), '—'),
                'decline_other_currency': next((c for c in codes if c == 'CURRENCY_NOT_ALLOWED'), '—'),
                'decline_second_charge': 'not recorded'}
    heroes = {}
    for r in rows:
        if r['kind'] == 'card':
            inv = next(i for i in state['invoices'] if i['invoice'] == r['invoice'])
            hero = card_hero(r, {**inv, 'currency': r['currency'], 'amount': inv['plan']['amount']}, declines)
            hero['replayed'] = True
            hero['remote'] = {'card_id': r.get('remote_id'), 'request_id': r.get('request_id')}
            heroes[r['invoice']] = hero
    steps = [s for s in state.get('steps', []) if s.get('outcome') == 'OBSERVED' and s.get('step') not in BOOKKEEPING][-4:][::-1]
    activity = [{'time': when(s['at'], with_date=False), 'text': 'Recorded step: %s' % s['step'], 'replayed': True} for s in steps]
    for n, a in enumerate(activity, 1):
        slots['act_%d_time' % n], slots['act_%d_text' % n] = a['time'], a['text']
    return {'demo': False, 'replayed': True, 'notes': NOTES['replay'], 'slots': slots, 'queue': strip(queue), 'timeline': strip(timeline),
            'ladder': {'new_1': None, 'new_2': None, 'verified_1': None, 'trusted_1': None}, 'activity': activity,
            'heroes': heroes, 'default_invoice': default, 'activity_title': 'latest 4 recorded sandbox steps',
            'sandbox_checks': [{'slot': k, 'code': v, 'evidence': 'replayed authorizations'} for k, v in declines.items()]}
