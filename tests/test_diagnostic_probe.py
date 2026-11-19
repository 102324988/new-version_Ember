"""Standalone unittest: no application imports, real database or provider calls."""
from contextlib import chdir
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from utils.diagnostic_probe import database_failure, probe


class DiagnosticProbeTests(unittest.TestCase):
    def environment(self):
        return {prefix + '_' + suffix: ('https://example.invalid/v1' if suffix == 'BASE_URL' else 'synthetic-value')
            for prefix in ('LARGE_LLM', 'SMALL_LLM', 'EMBEDDING') for suffix in ('API_KEY', 'BASE_URL', 'MODEL')}

    def run_probe(self, error=None, network=False):
        cursor = MagicMock()
        cursor.fetchone.return_value = (True,)
        connection = MagicMock()
        connection.cursor.return_value.__enter__.return_value = cursor
        connect = MagicMock(return_value=connection, side_effect=error)
        driver = SimpleNamespace(connect=connect)
        with patch.dict(os.environ, self.environment(), clear=True), patch.dict(sys.modules, {'psycopg2': driver}):
            results = probe(network=network)
        return results, connect, cursor

    def test_password_error_is_specific_but_never_leaks_connection_text(self):
        error = RuntimeError('password authentication failed secret-password postgres://private:user@host')
        error.pgcode = '28P01'
        result = database_failure(error)
        self.assertEqual(result['status'], 'FAIL')
        self.assertIn('认证失败', result['detail'])
        self.assertNotIn('secret-password', json.dumps(result))

    def test_database_checks_use_read_only_transactions_and_only_select(self):
        results, connect, cursor = self.run_probe()
        self.assertIn('default_transaction_read_only=on', connect.call_args.kwargs['options'])
        self.assertTrue(all(call.args[0].startswith('SELECT') for call in cursor.execute.call_args_list))
        self.assertTrue(any(row['check'] == 'database' and row['status'] == 'PASS' for row in results))

    def test_default_does_not_contact_model_or_network_endpoints(self):
        with patch('socket.create_connection') as connect:
            self.run_probe()
            connect.assert_not_called()

    def test_network_failure_is_classified_without_raw_exception(self):
        with patch('socket.create_connection', side_effect=TimeoutError('private-endpoint-secret')):
            results, _, _ = self.run_probe(network=True)
        failures = [row for row in results if row['check'].endswith(':network')]
        self.assertEqual(len(failures), 3)
        self.assertTrue(all(row['status'] == 'FAIL' for row in failures))
        self.assertNotIn('private-endpoint-secret', json.dumps(results))

    def test_invalid_state_file_is_reported_without_its_contents(self):
        with tempfile.TemporaryDirectory(prefix='ember-diagnostic-test-') as folder, chdir(folder):
            Path('config').mkdir()
            Path('config/state.json').write_text('private-chat-fragment', encoding='utf-8')
            results, _, _ = self.run_probe()
        self.assertTrue(any(row['check'] == 'config/state.json' and row['status'] == 'FAIL' for row in results))
        self.assertNotIn('private-chat-fragment', json.dumps(results))

    def test_database_disconnect_does_not_prevent_other_checks(self):
        results, _, _ = self.run_probe(error=RuntimeError('private socket endpoint'))
        self.assertTrue(any(row['check'] == 'database' and row['status'] == 'FAIL' for row in results))
        self.assertTrue(any(row['check'] == 'python' for row in results))
        self.assertNotIn('private socket endpoint', json.dumps(results))

    def test_structured_model_failure_logs_are_classified_without_body_export(self):
        with tempfile.TemporaryDirectory(prefix='ember-diagnostic-log-') as folder, chdir(folder):
            Path('data/logs').mkdir(parents=True)
            Path('data/logs/system.log').write_text('Model request failed code=timeout private-fragment\nModel request failed code=connection private-fragment\n', encoding='utf-8')
            results, _, _ = self.run_probe()
        self.assertTrue(any(x['check'] == 'log:model-timeout' for x in results))
        self.assertTrue(any(x['check'] == 'log:model-connection' for x in results))
        self.assertNotIn('private-fragment',json.dumps(results))


if __name__ == '__main__':
    unittest.main()
