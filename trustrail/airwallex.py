"""Airwallex sandbox-only REST boundary. No redirects, no automatic mutation retries."""
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .policy import LocalRefusal, money

BASE = 'https://api-demo.airwallex.com/api/v1'
LANE = 'CT0511-AWX'
# Wallet a card authorization bills, as measured in this sandbox (EUR 05.11, CNY 20.10/09.11).
CARD_BILLING = {'EUR': 'EUR', 'CNY': 'USD'}
TRANSFER_STATUSES = ('PROCESSING', 'SENT', 'PAID', 'FAILED', 'CANCELLED')
UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')
SAFE = {'process_result', 'type', 'subtype', 'id', 'card_id', 'card_status', 'status', 'failure_reason', 'transaction_type',
        'transaction_id', 'card_transaction_id', 'request_id', 'quote_id', 'conversion_id',
        'beneficiary_id', 'client_rate', 'buy_currency', 'sell_currency', 'buy_amount',
        'sell_amount', 'transfer_amount', 'transfer_currency', 'transaction_amount',
        'transaction_currency', 'billing_amount', 'billing_currency', 'code',
        'account_name_match_result', 'valid_to_at', 'usage', 'conversion_date', 'currency_pair',
        'amount_beneficiary_receives', 'amount_payer_pays', 'fee_amount', 'fee_currency', 'fee_paid_by',
        'source_amount', 'source_currency', 'transfer_method', 'updated_at', 'created_at', 'failure_type'}


def sanitize(obj):
    if isinstance(obj, list):
        return [sanitize(x) for x in obj]
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items() if k in SAFE and not isinstance(v, (dict, list))}
    return obj


def log_call(method, path, body, status, response):
    """Optional record of each sandbox request (TRUSTRAIL_CALL_LOG): one JSON line, whitelisted fields only.
    Headers, tokens and nested objects (beneficiary, controls) are never written."""
    target = os.environ.get('TRUSTRAIL_CALL_LOG')
    if not target:
        return
    if isinstance(response, dict):
        out = sanitize(response)
        if isinstance(response.get('items'), list):
            out['items'] = [sanitize(x) for x in response['items'] if isinstance(x, dict)]
        errors = (response.get('details') or {}).get('errors') if isinstance(response.get('details'), dict) else None
        if isinstance(errors, list):
            out['errors'] = [{k: e.get(k) for k in ('source', 'code') if isinstance(e, dict) and isinstance(e.get(k), str)}
                             for e in errors]
    elif isinstance(response, list):
        out = {'items': [sanitize(x) for x in response if isinstance(x, dict)]}
    else:
        out = response if isinstance(response, str) and re.fullmatch(r'[A-Z_]{1,40}', response) else None
    line = {'ts': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%fZ'), 'method': method, 'path': path, 'http': status,
            'request': sanitize(body) if isinstance(body, dict) else None, 'response': out}
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, 'a') as f:
        f.write(json.dumps(line, allow_nan=False, default=str) + '\n')
        f.flush()
        os.fsync(f.fileno())
    os.chmod(target, 0o600)


class APIError(RuntimeError):
    def __init__(self, status, code='unknown'):
        self.status = status
        self.code = code if isinstance(code, str) and re.fullmatch(r'[A-Za-z0-9_]{1,80}', code) else 'unknown'
        super().__init__(f'Airwallex HTTP {status} code={self.code}; remote outcome requires review')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def transport(method, url, headers, body):
    data = json.dumps(body, allow_nan=False).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        'Content-Type': 'application/json', **headers})
    try:
        with urllib.request.build_opener(NoRedirect()).open(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b'{}', parse_float=Decimal)
    except urllib.error.HTTPError as e:
        try:
            parsed = json.loads(e.read() or b'{}', parse_float=Decimal)
        except (ValueError, UnicodeDecodeError):
            parsed = {}
        return e.code, parsed


class Airwallex:
    def __init__(self, client_id, api_key, account_id, *, base=BASE, transport=transport, min_interval=1.05):
        if base != BASE:
            raise ValueError('only exact approved api-demo sandbox base is allowed')
        self.base, self.transport = base, transport
        self.interval, self.last = min_interval, 0
        self.lock = threading.Lock()
        self.token, self.cardholder_id = None, None
        login = self.call('POST', '/authentication/login', {}, headers={'x-client-id': client_id, 'x-api-key': api_key})
        self.token = login['token']
        account = self.call('GET', '/account')
        if (account.get('id') or account.get('account_id')) != account_id:
            raise ValueError('sandbox account identity mismatch; writes blocked')
        self.account_id = account_id

    def call(self, method, path, body=None, *, headers=None):
        if not isinstance(path, str) or not path.startswith('/') or path.startswith('//') or '..' in path or '#' in path:
            raise ValueError('relative sandbox API path required')
        with self.lock:
            wait = self.interval - (time.monotonic() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.monotonic()
            h = {'Authorization': 'Bearer ' + self.token} if self.token else {}
            st, response = self.transport(method, self.base + path, {**h, **(headers or {})}, body)
            log_call(method, path, body, st, response)
            if not 200 <= st < 300:
                raise APIError(st, response.get('code', 'unknown') if isinstance(response, dict) else 'unknown')
            return response

    def balances(self):
        rows = self.call('GET', '/balances/current')
        if isinstance(rows, dict):
            rows = rows['items']
        return {r['currency']: str(money(str(r['available_amount']), zero=True)) for r in rows}

    def validate(self, details):
        result = self.call('POST', '/beneficiaries/validate', details)
        # Live REST response (05.11) is JSON string "OK", not {status: OK}.
        return {'status': 'OK' if result == 'OK' else None}

    @staticmethod
    def card_billing(ccy):
        if ccy not in CARD_BILLING:
            raise LocalRefusal('card billing wallet for this currency is unmeasured; blocked')
        return CARD_BILLING[ccy]

    def submit(self, request_id, plan, details):
        """Checks before the request raise LocalRefusal (nothing sent). After the request, any odd
        response propagates as an ordinary error, which the ledger records as UNKNOWN (reconcile-only)."""
        try:
            route, path, body = self.prepare(request_id, plan, details)
        except (KeyError, TypeError, AttributeError) as e:
            raise LocalRefusal('incomplete plan or beneficiary details; nothing sent') from e
        result = self.call('POST', path, body)
        if route == 'CARD':
            return {'id': result.get('card_id') or result.get('id'), 'status': result.get('card_status'), 'request_id': request_id}
        return sanitize(result)

    def prepare(self, request_id, plan, details):
        """Pure: validates and builds the request; never performs I/O."""
        ccy = plan['currency']
        if plan['route'] == 'CARD':
            billing = self.card_billing(ccy)
            funding = plan.get('funding') or {}
            if billing != ccy and (funding.get('via'), funding.get('currency')) != ('CARD_BILLING', billing):
                raise LocalRefusal('indirectly billed card needs a plan with a checked billing-wallet floor')
            if not getattr(self, 'cardholder_id', None):
                raise LocalRefusal('explicit existing sandbox cardholder required')
            return 'CARD', '/issuing/cards/create', {
                'request_id': request_id, 'cardholder_id': self.cardholder_id,
                'created_by': LANE, 'form_factor': 'VIRTUAL', 'is_personalized': False,
                'program': {'purpose': 'COMMERCIAL'}, 'metadata': {'lane': LANE},
                'authorization_controls': {'allowed_transaction_count': 'SINGLE',
                    'allowed_currencies': [ccy], 'transaction_limits': {'currency': ccy, 'limits': [
                        {'amount': plan['cap'], 'interval': 'PER_TRANSACTION'},
                        {'amount': plan['cap'], 'interval': 'ALL_TIME'}]}}}
        if ccy not in ('EUR', 'GBP') or details.get('transfer_methods') != ['LOCAL']:
            raise LocalRefusal('only EUR and GBP LOCAL payouts are measured; no unbudgeted SWIFT fee')
        bank = details['beneficiary']['bank_details']
        if bank['account_currency'] != ccy:
            raise LocalRefusal('beneficiary currency does not match invoice')
        if ccy == 'GBP' and not (bank.get('account_routing_type1') == 'sort_code' and bank.get('bank_country_code') == 'GB'
                                 and re.fullmatch(r'\d{6}', str(bank.get('account_routing_value1', '')))
                                 and re.fullmatch(r'\d{8}', str(bank.get('account_number', '')))):
            raise LocalRefusal('GBP LOCAL needs GB sort code (6 digits) and account number (8 digits)')
        return 'TRANSFER', '/transfers/create', {
            'request_id': request_id, 'reason': 'goods_purchased', 'reference': LANE,
            'transfer_currency': ccy, 'source_currency': ccy, 'transfer_amount': plan['amount'],
            'beneficiary': details['beneficiary'], 'transfer_method': 'LOCAL', 'metadata': {'lane': LANE}}

    def quote(self, sell, buy, buy_amount):
        """A rate quote is not a money movement; it fixes the sell side we compare against the approved cap."""
        r = self.call('POST', '/fx/quotes/create', {'buy_currency': buy, 'sell_currency': sell,
                                                    'buy_amount': str(money(buy_amount)), 'validity': 'MIN_1'})
        if not r.get('quote_id') or r.get('sell_amount') is None or r.get('sell_currency') not in (None, sell):
            raise ValueError('quote response missing quote_id/sell_amount')
        return {'quote_id': r['quote_id'], 'client_rate': str(r.get('client_rate')),
                'sell_amount': str(money(str(r['sell_amount']))), 'buy_amount': str(money(buy_amount))}

    def convert(self, request_id, funding, quote_id):
        r = self.call('POST', '/fx/conversions/create', {
            'request_id': request_id, 'buy_currency': funding['buy_currency'], 'sell_currency': funding['currency'],
            'buy_amount': str(money(funding['buy_amount'])), 'quote_id': quote_id})
        out = sanitize(r)
        out.update(id=r.get('conversion_id'), status=r.get('status'), request_id=r.get('request_id'))
        return out

    def find_conversion(self, request_id):
        # Contract of the list filter is unmeasured; we only accept an exact request_id match.
        rows = self.call('GET', '/fx/conversions?' + urllib.parse.urlencode({'request_id': request_id}))
        matches = [r for r in (rows.get('items', []) if isinstance(rows, dict) else []) if r.get('request_id') == request_id]
        if len(matches) != 1:
            return None
        out = sanitize(matches[0])
        out.update(id=matches[0].get('conversion_id'), status=matches[0].get('status'), request_id=request_id)
        return out

    def freeze_card(self, card_id):
        """Human action: set one of our own tagged cards INACTIVE, then read it back."""
        if not isinstance(card_id, str) or not UUID.fullmatch(card_id):
            raise LocalRefusal('card uuid required')
        card = self.call('GET', '/issuing/cards/' + card_id)
        if (card.get('metadata') or {}).get('lane') != LANE:
            raise LocalRefusal('not a card issued by TrustRail; not touched')
        updated = card.get('card_status') not in ('CLOSED', 'INACTIVE')
        if updated:
            self.call('POST', '/issuing/cards/' + card_id + '/update', {'card_status': 'INACTIVE'})
        after = self.call('GET', '/issuing/cards/' + card_id)
        # `updated` False: the card was already closed or inactive before the person acted; nothing was changed.
        return {'id': card_id, 'status': after.get('card_status'), 'before': card.get('card_status'), 'updated': updated}

    def simulate_transfer(self, transfer_id, next_status):
        """Sandbox-only documented simulation: one valid hop per call (PROCESSING -> SENT -> PAID)."""
        if not isinstance(transfer_id, str) or not UUID.fullmatch(transfer_id) or next_status not in TRANSFER_STATUSES:
            raise ValueError('transfer uuid and a documented next_status required')
        return sanitize(self.call('POST', '/simulation/transfers/' + transfer_id + '/transition', {'next_status': next_status}))

    def find(self, request_id, plan):
        # No retry after an empty list; absence is not proof of failure.
        path = '/issuing/cards' if plan['route'] == 'CARD' else '/transfers'
        rows = self.call('GET', path + '?' + urllib.parse.urlencode({'request_id': request_id}))
        matches = [r for r in rows.get('items', []) if r.get('request_id') == request_id]
        if len(matches) != 1:
            return None
        r = matches[0]
        result = sanitize(r)
        if plan['route'] == 'CARD':
            result = {'id': r.get('card_id') or r.get('id'), 'status': r.get('card_status'), 'request_id': request_id}
        return result
