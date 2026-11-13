"""Explicit sandbox test-money probe, never a production payment entrypoint.

Creates two tagged EUR cards and one EUR LOCAL transfer. Reuses the existing
cardholder. Writes each sanitized result immediately; finally freezes only cards
created in this invocation. Synthetic supplier/delivery attestations are labeled.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import uuid

from .airwallex import APIError, BASE, LANE, sanitize
from .core import Ledger


def synthetic_details():
    # Public checksum-valid example IBAN, synthetic company/address; sandbox only.
    return {'beneficiary': {'entity_type': 'COMPANY', 'company_name': LANE + ' Synthetic Supplier',
        'bank_details': {'account_currency': 'EUR', 'bank_country_code': 'DE',
            'account_name': LANE + ' Synthetic Supplier', 'iban': 'DE89370400440532013000'},
        'address': {'country_code': 'DE', 'city': 'Berlin', 'street_address': '1 Synthetic Street', 'postcode': '10115'}},
        'transfer_methods': ['LOCAL']}


def run(db_path, evidence_dir, api):
    root = Path(evidence_dir)
    root.mkdir(parents=True, exist_ok=False)  # rerun must not silently create more money movements
    if Path(db_path).exists():
        raise ValueError('probe requires a new ledger; use inspect/reconcile for an existing one')
    ledger = Ledger(db_path)
    events, created, failures = [], [], []
    summary = {'base': BASE, 'account_id': api.account_id, 'test_money_only': True,
               'issuing': 'BLOCKED', 'transfer': 'BLOCKED', 'fx': 'BLOCKED',
               'cny_live': 'BLOCKED_INDIRECT_USD_FLOOR_NOT_IMPLEMENTED', 'cleanup': 'BLOCKED'}

    def event(name, body, *, outcome='OBSERVED'):
        item = {'at': datetime.now(timezone.utc).isoformat(), 'step': name, 'outcome': outcome, 'data': body}
        events.append(item)
        with (root/'events.jsonl').open('a') as f:
            f.write(json.dumps(item, allow_nan=False) + '\n')
            f.flush()
            os.fsync(f.fileno())
        os.chmod(root/'events.jsonl', 0o600)
        print(name, outcome, flush=True)
        return body

    def call(name, method, path, body=None):
        try:
            r = api.call(method, path, body)
            event(name, sanitize(r))
            return r
        except Exception as e:
            failures.append(name)
            event(name, {'exception': type(e).__name__,
                         **({'http': e.status, 'code': e.code} if isinstance(e, APIError) else {})}, outcome='BLOCKED')
            raise

    def create_card(invoice, amount):
        ledger.add_invoice(invoice, LANE+'-card-supplier', amount, 'EUR')
        p = ledger.plan(invoice, api, loss_cap='10', floor='100')
        ledger.approve(invoice, p['digest'], actor=LANE+' sandbox authorization')
        event(invoice+'_plan', p)
        r = ledger.execute(invoice, api)
        event(invoice+'_execute', r)
        if r['state'] != 'SUBMITTED' or not r['result'].get('id'):
            raise ValueError('card outcome unknown; reconcile before any new operation')
        cid = r['result']['id']
        created.append(cid)
        current = call(invoice+'_readback', 'GET', '/issuing/cards/'+cid)
        if current.get('metadata', {}).get('lane') != LANE or current.get('request_id') != r['request_id']:
            raise ValueError('card ownership readback mismatch')
        if current.get('card_status') != 'ACTIVE':
            raise ValueError('card not active')
        return cid

    try:
        event('balances_before', api.balances())
        config = call('issuing_config', 'GET', '/issuing/config')
        event('funding_config', {'primary_currency': config.get('primary_currency'),
                                'enable_auto_conversion': config.get('enable_auto_conversion')})
        holders = call('existing_cardholders', 'GET', '/issuing/cardholders')
        match = [x for x in holders.get('items', [])
                 if x.get('email') == 'spike+trustrail@retailbox-automation.com' and x.get('status') == 'READY']
        if len(match) != 1:
            raise ValueError('existing sandbox cardholder not uniquely READY')
        api.cardholder_id = match[0].get('cardholder_id') or match[0].get('id')
        event('cardholder_reused', {'cardholder_id': api.cardholder_id})
        ledger.add_supplier(LANE+'-card-supplier', synthetic_details(), accepts_card=True)
        try:
            active = create_card(LANE+'-eur-card-5', '5.00')
            results = []
            for name, amt, ccy, want_reason, want_result in [
                    ('over_limit', '5.01', 'EUR', 'LIMIT_EXCEEDED', 'DECLINED'),
                    ('wrong_currency', '1.00', 'USD', 'CURRENCY_NOT_ALLOWED', 'DECLINED'),
                    ('allowed', '5.00', 'EUR', None, 'APPROVED')]:
                r = call(name, 'POST', '/simulation/issuing/create',
                         {'card_id': active, 'transaction_amount': amt, 'transaction_currency': ccy})
                results.append(r.get('process_result') == want_result and r.get('failure_reason') == want_reason)
            frozen = create_card(LANE+'-eur-card-freeze', '1.00')
            call('freeze_before_simulation', 'POST', '/issuing/cards/'+frozen+'/update', {'card_status': 'INACTIVE'})
            r = call('inactive', 'POST', '/simulation/issuing/create',
                     {'card_id': frozen, 'transaction_amount': '1.00', 'transaction_currency': 'EUR'})
            results.append(r.get('failure_reason') == 'CARD_INACTIVE' and r.get('process_result') == 'DECLINED')
            summary['issuing'] = 'PASS' if all(results) else 'FAIL'
        except Exception as e:
            event('issuing_phase', {'exception': type(e).__name__}, outcome='BLOCKED')

        details = synthetic_details()
        try:
            ledger.add_supplier(LANE+'-transfer-supplier', details, accepts_card=False)
            ledger.validate_supplier(LANE+'-transfer-supplier', api)
            event('beneficiary_validate', {'status': 'OK'})
            verification = call('verify_account', 'POST', '/beneficiaries/verify_account',
                                {'entity_type': 'COMPANY', 'transfer_method': 'LOCAL',
                                 'bank_details': details['beneficiary']['bank_details']})
            event('name_check', {'code': verification.get('code'),
                                'account_name_match_result': verification.get('details', {}).get('account_name_match_result')})
            # Explicit synthetic attestation, not an unverified shipping-doc boolean.
            ledger.attest_delivery(LANE+'-transfer-supplier', reviewer=LANE+' sandbox operator',
                                   evidence_ref='SYNTHETIC_DELIVERY_FOR_SANDBOX_EXERCISE_ONLY')
            invoice = LANE+'-eur-transfer-1'
            ledger.add_invoice(invoice, LANE+'-transfer-supplier', '1.00', 'EUR')
            p = ledger.plan(invoice, api, loss_cap='10', floor='100')
            ledger.approve(invoice, p['digest'], actor=LANE+' sandbox authorization')
            event('transfer_plan', p)
            first = ledger.execute(invoice, api)
            event('transfer_execute', first)
            repeat = Ledger(db_path).execute(invoice, api)
            event('transfer_repeat_without_remote_write', repeat)
            if first['state'] == 'SUBMITTED':
                final = ledger.reconcile(invoice, api)
                event('transfer_reconcile', final)
                summary['transfer_status'] = final['result']['status']
                summary['transfer'] = 'PASS_SUBMITTED_NOT_SETTLED'
                if final['result']['status'] in ('PAID', 'SETTLED'):
                    summary['transfer'] = 'PASS_REMOTE_' + final['result']['status']
            else:
                summary['transfer'] = 'BLOCKED_UNKNOWN_RECONCILE_ONLY'
        except Exception as e:
            event('transfer_phase', {'exception': type(e).__name__}, outcome='BLOCKED')
        try:
            quote = call('fx_quote', 'POST', '/fx/quotes/create',
                         {'buy_currency': 'EUR', 'sell_currency': 'USD', 'buy_amount': '2.00', 'validity': 'MIN_1'})
            if not quote.get('quote_id') or not quote.get('client_rate'):
                raise ValueError('quote response missing quote_id/client_rate')
            # EUR is already funded: no conversion is needed for the EUR LOCAL transfer.
            summary['fx'] = 'PASS_QUOTE_ONLY_CONVERSION_NOT_REQUIRED'
            summary['quote_id'] = quote['quote_id']
        except Exception as e:
            event('fx_phase', {'exception': type(e).__name__}, outcome='BLOCKED')
    except Exception as e:
        event('probe_prerequisite', {'exception': type(e).__name__}, outcome='BLOCKED')
    finally:
        cleanup_ok = True
        for cid in created:
            try:
                card = call('cleanup_ownership_'+cid, 'GET', '/issuing/cards/'+cid)
                if card.get('metadata', {}).get('lane') != LANE:
                    raise ValueError('not our tagged card; do not mutate')
                if card.get('card_status') != 'CLOSED':
                    call('cleanup_freeze_'+cid, 'POST', '/issuing/cards/'+cid+'/update', {'card_status': 'INACTIVE'})
                after = call('cleanup_verify_'+cid, 'GET', '/issuing/cards/'+cid)
                if after.get('card_status') not in ('INACTIVE', 'CLOSED'):
                    raise ValueError('cleanup readback not inactive/closed')
            except Exception as e:
                cleanup_ok = False
                event('cleanup_failure_'+cid, {'exception': type(e).__name__}, outcome='BLOCKED')
        summary['cleanup'] = 'PASS' if cleanup_ok else 'BLOCKED'
        summary['created_card_ids'] = created
        summary['api_error_steps'] = failures
        summary['api_errors'] = len(failures)
        try:
            event('balances_after', api.balances())
            event('ledger_audit', ledger.audit())
        except Exception as e:
            event('postcheck', {'exception': type(e).__name__}, outcome='BLOCKED')
        summary['at'] = datetime.now(timezone.utc).isoformat()
        (root/'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
        os.chmod(root/'summary.json', 0o600)
    return summary
