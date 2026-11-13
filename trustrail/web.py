"""Local operator API for a future UI. Loopback only; a random session token guards every POST.

Not a public service: the token is printed once at start to the operator's terminal. Requests with a
foreign Host (DNS rebinding) or a foreign Origin are refused. Execution reaches only the sandbox
adapter (production base URL is rejected in code) and only for an approved, current plan.
"""
import base64
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import secrets
import sys

import os

from . import console, inbox
from .core import Ledger

MAX_BODY = 1_000_000
STATIC = Path(__file__).parent / 'static'
STATIC_FILES = {'/': 'index.html', '/index.html': 'index.html'}
ASSETS = {'/card-tile-bg.png': ('card-tile-bg.png', 'image/png')}
CSP = ("default-src 'self'; script-src %s; style-src 'unsafe-inline' https://fonts.googleapis.com; "
       "font-src https://fonts.gstatic.com; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
       "base-uri 'none'; form-action 'none'")


class Funding:
    """Plan-time boundary: balances (snapshot recorded for the UI and agents), card billing, FX quote. No writes."""
    def __init__(self, provider):
        self.provider, self.last = provider, None

    def balances(self):
        # Recorded by the caller after the plan transaction ends (a nested write would self-deadlock).
        self.last = self.provider.balances()
        return self.last

    def card_billing(self, ccy):
        return self.provider.card_billing(ccy)

    def quote(self, sell, buy, amount):
        return self.provider.quote(sell, buy, amount)


def state(ledger):
    suppliers, invoices = ledger.suppliers(), ledger.invoices()
    items = [inbox.view(r) for r in ledger.inbox_items()]
    plans = [{'invoice': i['invoice'], 'supplier': i['supplier'], 'method': i['plan']['route'],
              'amount': i['plan']['amount'], 'cap': i['plan']['cap'], 'currency': i['plan']['currency'],
              'status': i['state'], 'digest': i['plan']['digest'], 'funding': i['plan'].get('funding'),
              'remote': i['result']} for i in invoices if i['plan']]
    holds = []
    for s in suppliers:
        if s['tier'] not in ('HOLD', 'HOLD_BANK_CHANGE'):
            continue
        cause = [x for x in items if x['supplier'] == s['id'] and x['action'] in ('BANK_CHANGE_HOLD', 'SUPPLIER_HOLD')]
        holds.append({'supplier': s['id'], 'kind': 'BANK_CHANGE_PENDING' if s['pending_bank_change'] else 'EMAIL_HOLD',
                      'since': s['hold_since'], 'reasons': cause[-1]['signals'] if cause else [],
                      'message_id': cause[-1]['message_id'] if cause else None, 'causes': s.get('hold_causes', []),
                      'blocked_invoices': [i['invoice'] for i in invoices
                                           if i['supplier'] == s['id'] and i['state'] == 'BLOCKED']})
    return {'suppliers': suppliers, 'invoices': invoices, 'plans': plans, 'holds': holds, 'inbox': items,
            'balances': ledger.last_balances(), 'audit': ledger.audit(50), 'replayed': False,
            'console': console.build(ledger)}


def inline_script_hash(html):
    m = re.search(r'<script>(.*?)</script>', html, re.S)
    return "'sha256-%s'" % base64.b64encode(hashlib.sha256(m.group(1).encode()).digest()).decode() if m else "'none'"


class Handler(BaseHTTPRequestHandler):
    server_version = 'TrustRail'
    sys_version = ''

    def log_message(self, fmt, *args):
        # Method, path without query, status; never bodies or headers.
        sys.stderr.write('%s %s\n' % (self.command, self.path.split('?')[0][:120]))

    def send_json(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.common_headers()
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def common_headers(self):
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')

    def allowed_origins(self):
        port = self.server.server_address[1]
        return {'127.0.0.1:%d' % port, 'localhost:%d' % port}

    def host_ok(self):
        return self.headers.get('Host') in self.allowed_origins()

    def token_ok(self):
        token = self.headers.get('X-TrustRail-Token') or ''
        return hmac.compare_digest(token.encode(), self.server.token.encode())

    def do_GET(self):
        if not self.host_ok():
            return self.send_json(403, {'error': 'foreign host'})
        path = self.path.split('?')[0]
        if path == '/api/state':
            # Replay is read-only (every write is refused), so its recorded state needs no token.
            if self.server.replay is None and not self.token_ok():
                return self.send_json(403, {'error': 'session token required'})
            return self.send_json(200, self.server.state())
        if path in ASSETS:
            name, ctype = ASSETS[path]
            data = (STATIC / name).read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.common_headers()
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        name = STATIC_FILES.get(path)
        if not name:
            return self.send_json(404, {'error': 'not found'})
        # The page never contains the session token: the operator opens the link printed in the terminal
        # (http://127.0.0.1:PORT/#t=<token>); the fragment is never sent to the server.
        html = (STATIC / name).read_text()
        body = html.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Security-Policy', CSP % inline_script_hash(html))
        self.common_headers()
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self.host_ok():
            return self.send_json(403, {'error': 'foreign host'})
        origin = self.headers.get('Origin')
        if origin is not None and origin.removeprefix('http://') not in self.allowed_origins():
            return self.send_json(403, {'error': 'foreign origin'})
        if not self.token_ok():
            return self.send_json(403, {'error': 'session token required'})
        if (self.headers.get('Content-Type') or '').split(';')[0].strip() != 'application/json':
            return self.send_json(415, {'error': 'application/json required'})
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            return self.send_json(400, {'error': 'bad length'})
        if length > MAX_BODY:
            return self.send_json(413, {'error': 'body too large'})
        try:
            data = json.loads(self.rfile.read(length) or b'null')
        except ValueError:
            return self.send_json(400, {'error': 'invalid JSON'})
        if not isinstance(data, dict):
            return self.send_json(400, {'error': 'JSON object required'})
        route = ROUTES.get(self.path.split('?')[0])
        if route is None:
            return self.send_json(404, {'error': 'not found'})
        if self.server.replay is not None:
            return self.send_json(403, {'error': 'replay is read-only', 'replayed': True})
        try:
            return self.send_json(200, route(self.server, data))
        except (ValueError, KeyError) as e:
            # Core errors are fixed strings; a missing key names only the field.
            return self.send_json(400, {'error': str(e) if isinstance(e, ValueError) else 'missing field %s' % e})
        except Exception:
            return self.send_json(502, {'error': 'operation failed; no automatic retry; inspect ledger'})


def text(data, key):
    value = data[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError('%s must be nonempty text' % key)
    return value


def r_inbox(srv, d):
    return inbox.ingest(srv.ledger, d)


def r_plan(srv, d):
    invoice = text(d, 'invoice')
    previous = (srv.ledger.inspect(invoice).get('plan') or {})
    if d.get('method') not in (None, 'deposit'):
        raise ValueError('method must be deposit or omitted')
    # A re-plan from the console reuses the caps the plan was made with; it never invents new limits.
    caps = {k: d.get(k, previous.get(k)) for k in ('loss_cap', 'floor')}
    fund_floor = d.get('fund_floor', (previous.get('funding') or {}).get('fund_floor'))
    if caps['loss_cap'] is None or caps['floor'] is None:
        raise ValueError('loss_cap and floor required for a first plan')
    funding = Funding(srv.provider_factory())
    try:
        return srv.ledger.plan(invoice, funding, loss_cap=caps['loss_cap'], floor=caps['floor'],
                               fund_from=d.get('fund_from'), fund_floor=fund_floor, no_card=d.get('method') == 'deposit')
    finally:
        if funding.last is not None:
            srv.ledger.record_balances(funding.last)


def r_approve(srv, d):
    srv.ledger.approve(text(d, 'invoice'), text(d, 'digest'), actor=text(d, 'actor'))
    return srv.ledger.inspect(d['invoice'])


def r_execute(srv, d):
    provider = srv.provider_factory()
    cardholder = d.get('cardholder_id') or os.environ.get('TRUSTRAIL_CARDHOLDER_ID')
    if cardholder is not None:
        provider.cardholder_id = cardholder if isinstance(cardholder, str) and cardholder.strip() else text(d, 'cardholder_id')
    view = srv.ledger.execute(text(d, 'invoice'), provider)
    if view.get('state') == 'SUBMITTED':
        # Money moved: the top bar shows a fresh balances read (a GET), not the snapshot taken at planning.
        try:
            srv.ledger.record_balances(provider.balances())
        except Exception as e:
            view = {**view, 'balances_snapshot': 'not refreshed (%s)' % type(e).__name__}
    return view


def r_confirm(srv, d):
    srv.ledger.confirm_bank_change(text(d, 'supplier'), text(d, 'old_digest'), text(d, 'new_digest'),
                                   actor=text(d, 'actor'), callback_ref=text(d, 'callback_ref'))
    return srv.ledger.supplier_view(d['supplier'])


def r_reject(srv, d):
    srv.ledger.reject_bank_change(text(d, 'supplier'), text(d, 'old_digest'), text(d, 'new_digest'),
                                  actor=text(d, 'actor'), reason_ref=text(d, 'reason_ref'))
    return srv.ledger.supplier_view(d['supplier'])


def r_freeze(srv, d):
    return srv.ledger.freeze_card(text(d, 'invoice'), srv.provider_factory(), actor=text(d, 'actor'))


def r_hold(srv, d):
    srv.ledger.hold(text(d, 'supplier'), actor=text(d, 'actor'), reason_ref=text(d, 'reason_ref'))
    return srv.ledger.supplier_view(d['supplier'])


def r_keep_hold(srv, d):
    srv.ledger.keep_hold(text(d, 'supplier'), actor=text(d, 'actor'), reason_ref=text(d, 'reason_ref'))
    return srv.ledger.supplier_view(d['supplier'])


def r_release(srv, d):
    srv.ledger.release_hold(text(d, 'supplier'), actor=text(d, 'actor'), reason_ref=text(d, 'reason_ref'),
                            cause_ids=d.get('cause_ids'))
    return srv.ledger.supplier_view(d['supplier'])


ROUTES = {'/api/inbox': r_inbox, '/api/plan': r_plan, '/api/approve': r_approve, '/api/execute': r_execute,
          '/api/confirm-bank-details': r_confirm, '/api/reject-bank-details': r_reject,
          '/api/release-hold': r_release, '/api/freeze-card': r_freeze, '/api/hold': r_hold, '/api/keep-hold': r_keep_hold}


def make_server(db, *, provider_factory, host='127.0.0.1', port=0, token=None, replay=None):
    if host != '127.0.0.1':
        raise ValueError('loopback only')
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    srv.token = token or secrets.token_urlsafe(32)
    srv.provider_factory, srv.replay = provider_factory, replay
    srv.ledger = Ledger(db) if replay is None else None
    if replay is not None and 'console' not in replay:
        replay = {**replay, 'console': console.build_replay(replay)}
    srv.state = (lambda: state(srv.ledger)) if replay is None else (lambda: replay)
    return srv


def banner(srv):
    url = 'http://127.0.0.1:%d/#t=%s' % (srv.server_address[1], srv.token)
    if srv.replay is not None:
        mode = 'replay, read-only'
    elif srv.ledger.meta().get('dataset') == 'demo':
        mode = 'demo ledger, offline sandbox stand-in, no keys'
    else:
        mode = 'live ledger, Airwallex sandbox'
    return ('TrustRail console (%s): open %s\n'
            'The link carries the session token in its fragment; the page keeps it in memory only.\n') % (mode, url)


def serve(db, *, provider_factory, port=8765, replay=None, out=None):
    out = out or sys.stdout
    srv = make_server(db, provider_factory=provider_factory, port=port, replay=replay)
    out.write(banner(srv))
    out.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0
