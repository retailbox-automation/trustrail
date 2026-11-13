"""Judge replay: recorded sanitized sandbox evidence, no keys, no network, every record marked replayed."""
import http.client
import io
import json
import re
import threading
import unittest
from pathlib import Path
from unittest import mock

from trustrail import replay, web
from trustrail.cli import main

ROOT = Path(__file__).resolve().parent.parent
EVIDENCE = ROOT / 'docs'
UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}')


def no_network(*a, **k):
    raise AssertionError('replay must not touch the network')


class ReplayTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch('urllib.request.OpenerDirector.open', no_network)
        patcher.start()
        self.addCleanup(patcher.stop)

    def evidence_text(self):
        return ''.join(p.read_text() for p in replay.files(EVIDENCE))

    def test_state_marks_everything_replayed_and_ids_come_from_evidence(self):
        state = replay.load(EVIDENCE)
        self.assertTrue(state['replayed'])
        objects = state['objects']
        for kind in ('cards', 'authorizations', 'quotes', 'transfers'):
            with self.subTest(kind=kind):
                self.assertTrue(objects[kind])
                self.assertTrue(all(o['replayed'] is True for o in objects[kind]))
        self.assertTrue(all(s['replayed'] is True for s in state['steps']))
        transfers = {t['id']: t for t in objects['transfers']}
        self.assertEqual(transfers['51765bab-b30a-4a25-b70d-4ae6ad9a0f9b']['status'], 'SENT')
        reasons = {a['failure_reason'] for a in objects['authorizations']}
        self.assertTrue({'LIMIT_EXCEEDED', 'CURRENCY_NOT_ALLOWED', 'CARD_INACTIVE'} <= reasons)
        text = self.evidence_text()
        served = set(UUID.findall(json.dumps(objects)))
        self.assertTrue(served)
        self.assertEqual([x for x in served if x not in text], [])

    def test_missing_or_empty_directory_is_an_error_not_an_empty_demo(self):
        with self.assertRaises(ValueError):
            replay.load(ROOT / 'no-such-dir')
        with self.assertRaises(ValueError):
            replay.load(ROOT / 'trustrail' / 'static')

    def test_cli_demo_replay_needs_no_db_or_keys(self):
        out = io.StringIO()
        rc = main(['demo', '--replay', str(EVIDENCE)], stdout=out,
                  provider_factory=lambda: (_ for _ in ()).throw(AssertionError('no provider in replay')))
        self.assertEqual(rc, 0)
        lines = out.getvalue().splitlines()
        self.assertIn('replayed', lines[0].lower())
        summary = json.loads(out.getvalue()[out.getvalue().index('{'):])
        self.assertTrue(summary['replayed'])
        self.assertIn('51765bab-b30a-4a25-b70d-4ae6ad9a0f9b', {t['id'] for t in summary['transfers']})

    def test_web_replay_serves_recorded_state_and_refuses_writes(self):
        srv = web.make_server(None, provider_factory=no_network, replay=replay.load(EVIDENCE))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        c = http.client.HTTPConnection('127.0.0.1', srv.server_address[1], timeout=10)
        c.request('GET', '/api/state')
        r = c.getresponse()
        self.assertEqual(r.status, 200)  # replay is read-only, so its recorded state needs no token
        r.read()
        c.request('GET', '/api/state', headers={'X-TrustRail-Token': srv.token})
        r = c.getresponse()
        state = json.loads(r.read())
        self.assertTrue(state['replayed'])
        self.assertTrue(state['audit'] and all(a['replayed'] is True for a in state['audit']))
        c.request('POST', '/api/execute', body=b'{"invoice":"x"}',
                  headers={'Content-Type': 'application/json', 'X-TrustRail-Token': srv.token})
        r = c.getresponse()
        self.assertEqual((r.status, json.loads(r.read())['replayed']), (403, True))
        c.close()


if __name__ == '__main__':
    unittest.main()
