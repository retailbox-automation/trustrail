"""Agent entry point: MCP over stdio (JSON-RPC 2.0, one message per line), standard library only.

Deliberately absent: approve, execute, pay, confirm bank details, release a hold. The agent can read,
ingest supplier email and propose a plan; a person approves and executes elsewhere. This process never
loads Airwallex credentials: plans use the ledger's last recorded balance snapshot and every check is
repeated against live balances when a person executes.
"""
import json
import sys

from . import inbox
from .core import Ledger

VERSIONS = ('2025-06-18', '2025-03-26', '2024-11-05')
STR = {'type': 'string'}
EMAIL = {'type': 'object', 'additionalProperties': False, 'required': ['from', 'subject', 'body', 'received_at', 'message_id'],
         'properties': {'from': STR, 'reply_to': {'type': ['string', 'null']}, 'subject': STR, 'body': STR,
                        'received_at': STR, 'message_id': STR}}
TOOLS = [
    {'name': 'list_suppliers', 'description': 'Suppliers with trust tier, hold state and masked on-file/proposed bank details.',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'list_payment_queue', 'description': 'Invoices with plan (card/deposit/transfer), state and remote result.',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'get_audit', 'description': 'Most recent audit events (fingerprints only).',
     'inputSchema': {'type': 'object', 'properties': {'limit': {'type': 'integer', 'minimum': 1, 'maximum': 500}},
                     'additionalProperties': False}},
    {'name': 'ingest_email', 'description': 'Classify one supplier email. It can create a pending invoice or a hold; '
     'it can never approve, confirm or pay.', 'inputSchema': EMAIL},
    {'name': 'propose_plan', 'description': 'Propose a payment plan (card, deposit or transfer) for a person to approve. '
     'Amounts are decimal text in major units.',
     'inputSchema': {'type': 'object', 'additionalProperties': False, 'required': ['invoice', 'loss_cap', 'floor'],
                     'properties': {'invoice': STR, 'loss_cap': STR, 'floor': STR, 'fund_from': STR, 'fund_floor': STR}}},
]


class SnapshotBalances:
    """Read-only stand-in for the remote boundary: last recorded balances, nothing else."""
    def __init__(self, ledger):
        snap = ledger.last_balances()
        if not snap:
            raise ValueError('no balance snapshot recorded; a person must refresh balances first')
        self.data = snap['data']

    def balances(self):
        return dict(self.data)

    def card_billing(self, ccy):
        from .airwallex import Airwallex
        return Airwallex.card_billing(ccy)

    def quote(self, *a):
        raise ValueError('live FX quote needed; propose this plan from the operator console')


def tool(ledger, name, args, env):
    if name == 'list_suppliers':
        return ledger.suppliers()
    if name == 'list_payment_queue':
        return ledger.invoices()
    if name == 'get_audit':
        limit = args.get('limit', 50)
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError('limit must be an integer 1..500')
        return ledger.audit(limit)
    if name == 'ingest_email':
        return inbox.ingest(ledger, args, env=env)
    if name == 'propose_plan':
        if set(args) - {'invoice', 'loss_cap', 'floor', 'fund_from', 'fund_floor'}:
            raise ValueError('unexpected arguments')
        return ledger.plan(args.get('invoice'), SnapshotBalances(ledger), loss_cap=args.get('loss_cap'),
                           floor=args.get('floor'), fund_from=args.get('fund_from'), fund_floor=args.get('fund_floor'),
                           keep_approved=True)  # an agent never resets a person's approval
    raise KeyError(name)


def handle(ledger, msg, env):
    if not isinstance(msg, dict) or msg.get('jsonrpc') != '2.0' or not isinstance(msg.get('method'), str):
        return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'invalid request'}}
    ident, method, params = msg.get('id'), msg['method'], msg.get('params') or {}
    if 'id' not in msg:
        return None  # notification
    ok = lambda result: {'jsonrpc': '2.0', 'id': ident, 'result': result}
    err = lambda code, text: {'jsonrpc': '2.0', 'id': ident, 'error': {'code': code, 'message': text}}
    if method == 'initialize':
        asked = params.get('protocolVersion')
        return ok({'protocolVersion': asked if asked in VERSIONS else VERSIONS[0],
                   'capabilities': {'tools': {'listChanged': False}},
                   'serverInfo': {'name': 'trustrail', 'version': '0.2.0'},
                   'instructions': 'Read, ingest supplier email and propose plans. Approval, execution and bank-detail '
                                   'confirmation are human-only and are not exposed here.'})
    if method == 'ping':
        return ok({})
    if method == 'tools/list':
        return ok({'tools': TOOLS})
    if method == 'tools/call':
        name, args = params.get('name'), params.get('arguments') or {}
        if name not in {t['name'] for t in TOOLS} or not isinstance(args, dict):
            return err(-32602, 'unknown tool or invalid arguments')
        try:
            result = tool(ledger, name, args, env)
            return ok({'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}], 'isError': False})
        except ValueError as e:
            # Core/inbox errors are fixed strings without payload data.
            return ok({'content': [{'type': 'text', 'text': str(e)}], 'isError': True})
        except Exception:
            return ok({'content': [{'type': 'text', 'text': 'tool failed; nothing was approved or paid'}], 'isError': True})
    return err(-32601, 'method not found')


def serve(db, stdin=None, stdout=None, env=None):
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    ledger = Ledger(db)
    for line in stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'parse error'}}
        else:
            reply = handle(ledger, msg, env)
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + '\n')
            stdout.flush()
    return 0
