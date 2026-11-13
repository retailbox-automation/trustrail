import io
import json
import tempfile
import unittest
from pathlib import Path
from trustrail.cli import main


class CLITests(unittest.TestCase):
    def test_local_commands_persist_invoice_and_redact_details(self):
        # Dropping persistence or exposing supplier bank payload breaks this scenario.
        with tempfile.TemporaryDirectory() as d:
            db = str(Path(d) / 'ledger.db')
            def run(action, data):
                out = io.StringIO()
                rc = main(['--db', db, action], stdin=io.StringIO(json.dumps(data)), stdout=out)
                self.assertEqual(rc, 0, out.getvalue())
                return json.loads(out.getvalue())
            run('supplier', {'id': 's', 'details': {'bank': 'DO-NOT-PRINT'}, 'accepts_card': True})
            run('invoice', {'id': 'i', 'supplier': 's', 'amount': '1.00', 'currency': 'EUR'})
            result = run('inspect', {'id': 'i'})
            self.assertEqual(result['state'], 'DRAFT')
            self.assertEqual(result['invoice'], 'i')
            self.assertNotIn('DO-NOT-PRINT', json.dumps(run('audit', {})))

    def test_invalid_money_and_probe_without_opt_in_fail_without_network(self):
        with tempfile.TemporaryDirectory() as d:
            out = io.StringIO()
            rc = main(['--db', str(Path(d)/'db'), 'invoice'], stdin=io.StringIO('{"id":"i","supplier":"s","amount":"NaN","currency":"EUR"}'), stdout=out)
            self.assertEqual(rc, 2)
            with self.assertRaises(SystemExit) as cm:
                main(['--db', str(Path(d)/'db'), 'probe'])
            self.assertEqual(cm.exception.code, 2)


    def test_sandbox_key_comes_only_from_env(self):
        from unittest import mock
        from trustrail import cli
        with mock.patch.dict('os.environ', {}, clear=True):
            with self.assertRaises(ValueError):
                cli.default_provider()  # no env: refuse, there is no other source
        with mock.patch.dict('os.environ', {'AIRWALLEX_CLIENT_ID': 'judge-id'}, clear=True):
            with self.assertRaises(ValueError):
                cli.default_provider()  # partial env: refuse
        env = {'AIRWALLEX_CLIENT_ID': 'judge-id', 'AIRWALLEX_API_KEY': 'judge-key', 'AIRWALLEX_ACCOUNT_ID': 'acct_judge'}
        with mock.patch.dict('os.environ', env, clear=True), mock.patch.object(cli, 'Airwallex') as aw:
            cli.default_provider()
        aw.assert_called_once_with('judge-id', 'judge-key', 'acct_judge')


if __name__ == '__main__': unittest.main()
