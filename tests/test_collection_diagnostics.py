from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

import test_browser_process as process_fixtures

from quota_guard import __version__
from quota_guard.build_identity import diagnostic_build_identity, source_build_identity
from quota_guard.browser_process import IsolatedBrowserService
from quota_guard.browser_service import BrowserService
from quota_guard.diagnostics import DiagnosticLog, safe_fields


class CollectionDiagnosticTests(unittest.TestCase):
    def test_phase_and_coverage_are_readable_without_raw_credentials(self):
        fields = dict(operation_id='a' * 32, stage='snapshot_validation',
                      reason_code='NO_BOUND_COST_MATCH', persistence_state='not_written',
                      export_state='not_started', bound_total_count=2, active_managed_count=1,
                      fee_tracking_count=2, matched_total_count=0, matched_active_count=0,
                      matched_historical_count=0, active_missing_count=1,
                      observed_source_id='b' * 64, effective_snapshot_id='c' * 64)
        self.assertEqual(safe_fields(fields), fields)
        with tempfile.TemporaryDirectory() as temp:
            log = DiagnosticLog(temp)
            log.emit('collection_application_failed', **fields,
                     payload={'raw-secret-key': 'private-response'},
                     authorization='Bearer private-authorization')
            raw = log.path.read_text(encoding='utf-8')
            for value in ('raw-secret-key', 'private-response', 'private-authorization'):
                self.assertNotIn(value, raw)
            data = json.loads(raw)['data']
            for key, value in fields.items():
                self.assertEqual(data[key], value)

    def test_source_identity_distinguishes_uncommitted_runtime_changes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'quota_guard').mkdir()
            (root / 'templates').mkdir()
            for name in ('main.py', 'build.py', 'requirements.lock', 'templates/人员额度模板.xlsx',
                         'quota_guard/example.py'):
                (root / name).write_bytes(b'first')
            with patch('quota_guard.build_identity.subprocess.run', return_value=SimpleNamespace(stdout='a' * 40)):
                first = source_build_identity(root)
                (root / 'quota_guard/example.py').write_bytes(b'changed')
                second = source_build_identity(root)
            self.assertEqual(first['build_commit'], second['build_commit'])
            self.assertNotEqual(first['build_id'], second['build_id'])
            self.assertEqual(second['app_version'], __version__)

    def test_frozen_metadata_is_bounded_and_validated(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / 'BUILD-INFO.json'
            valid = dict(app_version=__version__, build_commit='a' * 40,
                         build_id='code-sha256-' + 'b' * 64)
            with patch('quota_guard.build_identity.sys.frozen', True, create=True), \
                    patch('quota_guard.build_identity.sys.executable', str(root / 'app.exe')):
                for content in ('bad json', 'x' * 65537,
                                json.dumps({**valid, 'build_id': 'private-unvalidated-value'}),
                                json.dumps({**valid, 'app_version': 'old'})):
                    path.write_text(content, encoding='utf-8')
                    result = diagnostic_build_identity()
                    self.assertEqual(result['build_id'], 'unverified')
                path.write_text(json.dumps(valid), encoding='utf-8')
                self.assertEqual(diagnostic_build_identity(), {**valid, 'diagnostic_schema_version': 3})

    def test_operation_id_joins_worker_and_controller_diagnostics(self):
        operation = 'a' * 32
        worker = BrowserService.__new__(BrowserService)
        worker.diagnostics = Mock()
        worker._diagnostic_operation_id = operation
        worker._log('costs_verified', reason_code='OK')
        self.assertEqual(worker.diagnostics.emit.call_args.kwargs['operation_id'], operation)
        controller = IsolatedBrowserService.__new__(IsolatedBrowserService)
        controller._logger = Mock()
        controller._active = SimpleNamespace(action='collect', number=1, kwargs={'operation_id': operation})
        controller._worker_pid, controller._generation, controller._session = 123, 1, 'local'
        controller._log('browser_action_finished', success=True)
        self.assertEqual(controller._logger.emit.call_args.kwargs['operation_id'], operation)
        controller._active.kwargs['operation_id'] = 'private-unvalidated-value'
        controller._log('browser_action_finished', success=True)
        self.assertNotIn('operation_id', controller._logger.emit.call_args.kwargs)

    def test_operation_id_survives_real_result_and_reset_clearing_active_task(self):
        fixture = process_fixtures.ExpiredDeliveryTests()
        for outcome in ('result', 'reset'):
            with self.subTest(outcome=outcome):
                service, future, _queued = fixture.pending_service()
                service._active.kwargs['operation_id'] = 'a' * 32
                service._generation = 1
                service._logger = Mock()
                service._log = IsolatedBrowserService._log.__get__(service)
                if outcome == 'result':
                    fixture.deliver(service, monotonic=101, wall=1001)
                    event = 'browser_action_finished'
                else:
                    service._abort('synthetic interruption')
                    event = 'browser_process_reset'
                self.assertIsNone(service._active)
                self.assertTrue(future.done())
                calls = [call for call in service._logger.emit.call_args_list if call.args[0] == event]
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0].kwargs['operation_id'], 'a' * 32)


if __name__ == '__main__':
    unittest.main()
