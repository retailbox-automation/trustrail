"""Offline replay of recorded, sanitized sandbox evidence for judges: no network, no keys.

Every replayed step and object carries "replayed": true. Objects are derived only from fields that
are present in the evidence files; nothing is synthesized, so every ID shown was returned by the
Airwallex sandbox during a recorded run.
"""
import json
from pathlib import Path

MAX_FILE = 5_000_000


def files(root):
    root = Path(root)
    if not root.is_dir():
        raise ValueError('replay needs an evidence directory')
    return sorted(p for p in root.rglob('*.jsonl') if p.is_file() and p.stat().st_size <= MAX_FILE)


def walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from walk(v)


def read_steps(root):
    steps, errors = [], 0
    for path in files(root):
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                errors += 1
                continue
            if isinstance(row, dict) and {'at', 'step', 'data'} <= set(row):
                steps.append({'at': row['at'], 'step': row['step'], 'outcome': row.get('outcome', 'OBSERVED'),
                              'file': str(path.relative_to(root)), 'data': row['data'], 'replayed': True})
    if not steps:
        raise ValueError('no recorded evidence steps found')
    steps.sort(key=lambda s: s['at'])
    return steps, errors


def load(root):
    steps, errors = read_steps(root)
    found = {'cards': {}, 'authorizations': {}, 'quotes': {}, 'conversions': {}, 'transfers': {}}
    invoices, balances, audit = {}, None, []
    for s in steps:
        tag = {'at': s['at'], 'source_step': s['step'], 'replayed': True}
        if s['step'] in ('ledger_audit', 'audit') and isinstance(s['data'], list):
            audit = s['data']
        if s['step'] == 'balances_after' and isinstance(s['data'], dict):
            balances = {'at': s['at'], 'data': {k: v for k, v in s['data'].items() if v not in ('0.00', 0)}, 'replayed': True}
        for d in walk(s['data']):
            if 'conversion_id' in d:
                found['conversions'][d['conversion_id']] = {**{k: d.get(k) for k in (
                    'conversion_id', 'status', 'buy_currency', 'buy_amount', 'sell_currency', 'sell_amount',
                    'client_rate', 'quote_id', 'request_id')}, **tag}
            elif 'quote_id' in d and 'client_rate' in d:
                found['quotes'][d['quote_id']] = {**{k: d.get(k) for k in (
                    'quote_id', 'client_rate', 'buy_currency', 'buy_amount', 'sell_currency', 'sell_amount')}, **tag}
            if 'process_result' in d:
                key = d.get('card_transaction_id') or d.get('transaction_id') or s['step']
                found['authorizations'][key] = {**{k: d.get(k) for k in (
                    'card_transaction_id', 'card_id', 'transaction_amount', 'transaction_currency', 'process_result',
                    'failure_reason', 'billing_amount', 'billing_currency')}, **tag}
            elif 'card_id' in d and 'card_status' in d:
                found['cards'][d['card_id']] = {'card_id': d['card_id'], 'card_status': d['card_status'],
                                                'request_id': d.get('request_id'), **tag}
            if 'id' in d and 'transfer_currency' in d:
                found['transfers'][d['id']] = {**{k: d.get(k) for k in (
                    'id', 'status', 'request_id', 'transfer_amount', 'transfer_currency', 'transfer_method',
                    'fee_amount', 'fee_currency')}, **tag}
            if 'invoice' in d and 'state' in d and isinstance(d.get('plan'), dict):
                invoices[d['invoice']] = {**{k: d.get(k) for k in ('invoice', 'state', 'request_id', 'result', 'conversion')},
                                          'plan': d['plan'], **tag}
                r = d.get('result') or {}
                if d['plan'].get('route') == 'CARD' and r.get('id'):
                    found['cards'].setdefault(r['id'], {'card_id': r['id'], 'card_status': r.get('status'),
                                                        'request_id': d.get('request_id'), **tag})
    # A run's summary.json records each invoice's final state at finish (e.g. a transfer stopped by an email,
    # which no later step repeats); it overrides the last state seen in the steps.
    summaries = sorted(p for p in Path(root).rglob('summary.json') if p.is_file() and p.stat().st_size <= MAX_FILE)
    for path in summaries:
        try:
            summary = json.loads(path.read_text())
        except ValueError:
            errors += 1
            continue
        for d in (summary.get('invoices') or []) if isinstance(summary, dict) else []:
            if isinstance(d, dict) and 'invoice' in d and 'state' in d and isinstance(d.get('plan'), dict):
                invoices[d['invoice']] = {**{k: d.get(k) for k in ('invoice', 'state', 'request_id', 'result', 'conversion')},
                                          'plan': d['plan'], 'at': summary.get('finished'),
                                          'source_step': 'final state (%s)' % path.relative_to(root), 'replayed': True}
    plans = [{'invoice': i['invoice'], 'method': i['plan'].get('route'), 'amount': i['plan'].get('amount'),
              'cap': i['plan'].get('cap'), 'currency': i['plan'].get('currency'), 'status': i['state'],
              'funding': i['plan'].get('funding'), 'digest': i['plan'].get('digest'), 'replayed': True}
             for i in invoices.values()]
    used = {s['file'] for s in steps} | {str(p.relative_to(root)) for p in summaries if any(
        str(i.get('source_step', '')).endswith('(%s)' % p.relative_to(root)) for i in invoices.values())}
    return {'replayed': True, 'source': sorted(used), 'parse_errors': errors,
            'suppliers': [], 'invoices': list(invoices.values()), 'plans': plans, 'holds': [], 'inbox': [],
            'balances': balances, 'audit': [{**a, 'replayed': True} for a in audit[-50:] if isinstance(a, dict)],
            'steps': steps,
            'objects': {k: list(v.values()) for k, v in found.items()},
            'note': 'Recorded sandbox run. Supplier bank details were never recorded; synthetic suppliers only.'}


def demo(root, out):
    state = load(root)
    out.write('REPLAYED sandbox evidence (replayed: true). No network, no keys, nothing is live.\n')
    for s in state['steps']:
        bits = []
        for d in walk(s['data']):
            for k in ('process_result', 'failure_reason', 'status', 'card_status'):
                if isinstance(d.get(k), str):
                    bits.append('%s=%s' % (k, d[k]))
        out.write('%s  %-44s %-9s %s\n' % (s['at'][:19] + 'Z', s['step'][:44], s['outcome'], ' '.join(dict.fromkeys(bits))[:90]))
    summary = {'replayed': True, 'source': state['source'], 'parse_errors': state['parse_errors'], **state['objects']}
    out.write(json.dumps(summary, indent=1) + '\n')
    return 0
