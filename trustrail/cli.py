"""CLI for a local sandbox ledger; human attestations are explicit, not inferred."""
import argparse
import json
import os
import subprocess
import sys
from .airwallex import Airwallex, APIError
from .core import Ledger


def from_env():
    """An Airwallex sandbox key from the environment; still pinned to the sandbox base URL."""
    names = ('AIRWALLEX_CLIENT_ID', 'AIRWALLEX_API_KEY', 'AIRWALLEX_ACCOUNT_ID')
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        raise ValueError('set AIRWALLEX_CLIENT_ID, AIRWALLEX_API_KEY and AIRWALLEX_ACCOUNT_ID')
    return Airwallex(*(os.environ[n] for n in names))


def default_provider():
    """Sandbox credentials come only from the environment; there is no other source."""
    return from_env()


def web_provider(db, default):
    """A demo ledger only ever talks to the offline DemoSandbox, never to the live sandbox."""
    if Ledger(db).meta().get('dataset') == 'demo':
        from .demo import DemoSandbox
        sandbox = DemoSandbox()
        return lambda: sandbox
    return default


def main(argv=None, *, stdin=None, stdout=None, provider_factory=default_provider):
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    parser = argparse.ArgumentParser(prog='trustrail', description='TrustRail: deterministic sandbox payment core with an '
                                     'email intake agent. Money decisions are code; a person approves.')
    parser.add_argument('--db', help='local SQLite ledger (required except for --replay)')
    parser.add_argument('action', choices=['supplier', 'contact', 'invoice', 'ingest', 'validate', 'plan', 'approve',
        'execute', 'reconcile', 'bank-change', 'confirm-bank-change', 'reject-bank-change', 'release-hold',
        'freeze-card', 'attest-delivery', 'inspect',
        'audit', 'probe', 'mcp', 'web', 'demo'])
    parser.add_argument('--sandbox-test-money', action='store_true')
    parser.add_argument('--evidence')
    parser.add_argument('--replay', help='sanitized evidence directory to replay offline (demo, web)')
    parser.add_argument('--seed', help='demo: create the offline demo world in this NEW ledger file (no keys, no network)')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args(argv)
    if args.action == 'demo' and args.seed:
        from . import demo
        try:
            result = {'demo_ledger': args.seed, 'emails': demo.seed(args.seed),
                      'next': 'trustrail --db %s web' % args.seed}
        except ValueError as e:
            result = {'error': str(e)}
        json.dump(result, stdout, indent=2)
        stdout.write('\n')
        return 0 if 'error' not in result else 2
    if args.action == 'demo' or (args.action == 'web' and args.replay):
        if not args.replay:
            parser.error('demo needs --seed <new ledger> or --replay <evidence directory>')
        from . import replay
        try:
            if args.action == 'demo':
                return replay.demo(args.replay, stdout)
            from .web import serve
            return serve(None, provider_factory=provider_factory, port=args.port, replay=replay.load(args.replay), out=stdout)
        except ValueError as e:
            json.dump({'error': str(e)}, stdout)
            stdout.write('\n')
            return 2
    if not args.db:
        parser.error('--db is required for this action')
    # A demo ledger never reaches env keys or Airwallex: every provider action uses the offline stand-in.
    if os.path.exists(args.db) and Ledger(args.db).meta().get('dataset') == 'demo':
        if args.action == 'probe':
            parser.error('probe never runs on a demo ledger; nothing was contacted')
        provider_factory = web_provider(args.db, provider_factory)
    if args.action == 'web':
        from .web import serve
        return serve(args.db, provider_factory=web_provider(args.db, provider_factory), port=args.port, out=stdout)
    if args.action == 'probe':
        if not args.sandbox_test_money or not args.evidence:
            parser.error('probe requires --sandbox-test-money and a new --evidence directory')
        if os.path.exists(args.evidence) or os.path.exists(args.db):
            parser.error('probe needs a new --evidence directory and a new --db; nothing was contacted')
    if args.action == 'mcp':
        from .mcp_server import serve
        return serve(args.db, stdin, stdout)
    try:
        if args.action == 'probe':
            from .probe import run
            result = run(args.db, args.evidence, provider_factory())
        else:
            data = json.load(stdin)
            ledger = Ledger(args.db)
            action = args.action
            if action == 'supplier':
                ledger.add_supplier(data['id'], data['details'], accepts_card=data['accepts_card'])
                result = {'created': 'supplier'}
            elif action == 'contact':
                ledger.set_contact(data['id'], data['domain'], data['name'])
                result = {'registered': data['id']}
            elif action == 'ingest':
                from .inbox import ingest
                result = ingest(ledger, data)
            elif action == 'reject-bank-change':
                ledger.reject_bank_change(data['id'], data['old_digest'], data['new_digest'], actor=data['actor'],
                                          reason_ref=data['reason_ref'])
                result = {'rejected': data['id']}
            elif action == 'release-hold':
                ledger.release_hold(data['id'], actor=data['actor'], reason_ref=data['reason_ref'], cause_ids=data.get('cause_ids'))
                result = {'released': data['id']}
            elif action == 'invoice':
                ledger.add_invoice(data['id'], data['supplier'], data['amount'], data['currency'])
                result = ledger.inspect(data['id'])
            elif action == 'inspect': result = ledger.inspect(data['id'])
            elif action == 'audit': result = ledger.audit()
            elif action == 'approve':
                ledger.approve(data['id'], data['digest'], actor=data['actor'])
                result = ledger.inspect(data['id'])
            elif action == 'bank-change': result = ledger.bank_change(data['id'], data['details'])
            elif action == 'confirm-bank-change':
                ledger.confirm_bank_change(data['id'], data['old_digest'], data['new_digest'], actor=data['actor'], callback_ref=data['callback_ref'])
                result = {'confirmed': data['id']}
            elif action == 'attest-delivery':
                ledger.attest_delivery(data['id'], reviewer=data['reviewer'], evidence_ref=data['evidence_ref'])
                result = {'attested': data['id']}
            else:
                provider = provider_factory()
                if action == 'validate':
                    ledger.validate_supplier(data['id'], provider)
                    result = {'validated': data['id']}
                elif action == 'freeze-card':
                    result = ledger.freeze_card(data['id'], provider, actor=data['actor'])
                elif action == 'plan':
                    result = ledger.plan(data['id'], provider, loss_cap=data['loss_cap'], floor=data['floor'],
                                         fund_from=data.get('fund_from'), fund_floor=data.get('fund_floor'))
                else:
                    provider.cardholder_id = data.get('cardholder_id')
                    result = getattr(ledger, action)(data['id'], provider)
        json.dump(result, stdout, indent=2)
        stdout.write('\n')
        return 0
    except (ValueError, KeyError, APIError, OSError, subprocess.SubprocessError):
        # A raw upstream exception or payload may contain bank/card/credential data.
        json.dump({'error': 'command failed; no automatic retry; inspect ledger and sanitized evidence'}, stdout)
        stdout.write('\n')
        return 2
