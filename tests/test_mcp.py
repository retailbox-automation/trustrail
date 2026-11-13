"""MCP stdio round trip in a real subprocess: agent tools exist, money tools do not."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_inbox import NEW_IBAN, mail, seed

ROOT = Path(__file__).resolve().parent.parent


def rpc(db, messages, env=None):
    lines = ''.join(json.dumps(m) + '\n' for m in messages) + 'not json\n'
    run = subprocess.run([sys.executable, '-m', 'trustrail', '--db', db, 'mcp'], input=lines, capture_output=True,
                         text=True, timeout=30, cwd=ROOT, env=env)
    assert run.returncode == 0, run.stderr
    return [json.loads(x) for x in run.stdout.splitlines() if x.strip()]


def call(i, name, args=None):
    return {'jsonrpc': '2.0', 'id': i, 'method': 'tools/call', 'params': {'name': name, 'arguments': args or {}}}


class MCPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / 'ledger.sqlite3')
        self.ledger, _ = seed(self.db)
        self.ledger.record_balances({'EUR': '100000.00', 'GBP': '100000.00'})

    def test_round_trip_lists_only_safe_tools_and_runs_them(self):
        out = rpc(self.db, [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
             'params': {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 't', 'version': '0'}}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            call(3, 'list_suppliers'),
            call(4, 'ingest_email', mail()),
            call(5, 'propose_plan', {'invoice': 'acme:INV-2002', 'loss_cap': '500', 'floor': '100'}),
            call(6, 'list_payment_queue'),
            call(7, 'get_audit', {'limit': 5}),
            call(8, 'approve', {'invoice': 'acme:INV-2002'}),
            {'jsonrpc': '2.0', 'id': 9, 'method': 'no/such'},
        ])
        by_id = {m.get('id'): m for m in out}
        self.assertEqual(by_id[1]['result']['protocolVersion'], '2025-06-18')
        self.assertIn('tools', by_id[1]['result']['capabilities'])
        names = {t['name'] for t in by_id[2]['result']['tools']}
        self.assertEqual(names, {'list_suppliers', 'list_payment_queue', 'get_audit', 'ingest_email', 'propose_plan'})
        tokens = {t for n in names for t in n.split('_')}
        self.assertFalse(tokens & {'approve', 'execute', 'pay', 'confirm', 'release', 'transfer', 'convert'})
        self.assertTrue(all(t['inputSchema']['type'] == 'object' for t in by_id[2]['result']['tools']))
        suppliers = json.loads(by_id[3]['result']['content'][0]['text'])
        self.assertNotIn('DE89370400440532013000', by_id[3]['result']['content'][0]['text'])
        self.assertEqual({s['id'] for s in suppliers}, {'acme', 'brit', 'shen'})
        self.assertEqual(json.loads(by_id[4]['result']['content'][0]['text'])['action'], 'PENDING_INVOICE')
        plan = json.loads(by_id[5]['result']['content'][0]['text'])
        self.assertEqual((plan['route'], plan['amount']), ('TRANSFER', '980.00'))
        queue = {q['invoice']: q for q in json.loads(by_id[6]['result']['content'][0]['text'])}
        self.assertEqual(queue['acme:INV-2002']['state'], 'DRAFT')  # proposed, never approved by the agent
        self.assertEqual(len(json.loads(by_id[7]['result']['content'][0]['text'])), 5)
        self.assertEqual(by_id[8]['error']['code'], -32602)
        self.assertEqual(by_id[9]['error']['code'], -32601)
        self.assertEqual(by_id[None]['error']['code'], -32700)
        self.assertEqual(self.ledger.inspect('acme:INV-2002')['state'], 'DRAFT')

    def test_bank_change_email_via_mcp_holds_and_agent_cannot_replan(self):
        out = rpc(self.db, [
            call(1, 'ingest_email', mail(subject='new bank details', message_id='<b-9@acme-tools.de>',
                                        body='We changed our bank. New IBAN: ' + NEW_IBAN)),
            call(2, 'propose_plan', {'invoice': 'acme:INV-1001', 'loss_cap': '500', 'floor': '100'}),
            call(3, 'ingest_email', {'from': 'x@y.z'}),
        ])
        self.assertEqual(json.loads(out[0]['result']['content'][0]['text'])['action'], 'BANK_CHANGE_HOLD')
        self.assertTrue(out[1]['result']['isError'])
        self.assertTrue(out[2]['result']['isError'])
        self.assertEqual(self.ledger.inspect('acme:INV-1001')['state'], 'BLOCKED')

    def test_plan_without_balance_snapshot_is_refused_not_fetched(self):
        db = str(Path(self.tmp.name) / 'fresh.sqlite3')
        seed(db)
        out = rpc(db, [call(1, 'propose_plan', {'invoice': 'acme:INV-1001', 'loss_cap': '500', 'floor': '100'})])
        self.assertTrue(out[0]['result']['isError'])
        self.assertIn('snapshot', out[0]['result']['content'][0]['text'])


if __name__ == '__main__':
    unittest.main()
