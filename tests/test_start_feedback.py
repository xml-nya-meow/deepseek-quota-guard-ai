"""Production GUI startup refusal is visible without weakening responsibility.

Every ledger/registry is isolated. The fake browser returns synthetic fees and
never contacts a platform. Tests intentionally do not pre-initialize the registry.
"""
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from quota_guard.browser_service import key_fingerprint
from quota_guard.core import DataStore, Person, TZ, GuardError
from quota_guard.responsibility_gate import ResponsibilityGate
from quota_guard.responsibility_registry import RegistryError
from quota_guard.target_lock import TargetLockError
from test_responsibility_workflow import OfflineResponsibilityApp


class StartFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.store = DataStore(self.base / 'ledger')
        self.store.initialize(1, '100', datetime.now(TZ).date().isoformat())
        self.store.save_people([Person('Synthetic Start', Decimal(20))])
        self.target = 'synthetic-start-' + uuid4().hex
        keys = [{'name': 'Synthetic Start', 'tracking_id': self.target}]
        self.directory = self.base / 'shared' / 'responsibility'
        self.driver = OfflineResponsibilityApp(self.store.folder, self.directory,
            keys, self.target, key_fingerprint(keys))
        self.app = self.driver.app
        self.events = []
        emit = self.app._diagnostic

        def record(event, **fields):
            self.events.append({'event': event, **fields})
            emit(event, **fields)
        self.app._diagnostic = record

    def tearDown(self):
        self.driver.close(fixture_cleanup=True)
        self.temporary.cleanup()

    def collect(self):
        self.app.collect()
        self.driver.until(lambda: not self.app.busy)
        self.assertTrue(self.app.session_ready)
        self.assertTrue(self.app._collection_is_current())
        self.assertTrue(self.app.collection_has_fees)

    def initialize(self):
        ResponsibilityGate(self.directory).initialize([self.app.store])

    def assert_blocked(self, stage, reason):
        self.assertFalse(self.app.automatic)
        self.assertFalse(self.app.observing)
        events = [item for item in self.events if item['event'] == 'start_blocked']
        self.assertEqual(events[-1], {'event': 'start_blocked', 'stage': stage, 'reason_code': reason})
        self.assertIn(reason, self.app.status_var.get())
        self.assertEqual(self.app.buttons['start'].cget('text'), '全自动管理：未开启')
        self.assertEqual(self.driver.browser.sends, 0)
        self.assertEqual(self.driver.browser.consumptions, 0)

    def test_missing_registry_is_visible_after_successful_read_and_never_created(self):
        self.collect()
        self.app.start()
        self.assert_blocked('shared_responsibility', 'REGISTRY_MISSING')
        showerror = self.driver.mocks[1]
        showerror.assert_called_once()
        text = showerror.call_args.args[1]
        self.assertIn('建立本机责任范围', text)
        self.assertIn('如果从未建立过', text)
        self.assertIn('如果以前建立过', text)
        self.assertIn('恢复原登记', text)
        self.assertIn('不要重新创建或清空', text)
        self.assertFalse(self.directory.exists())
        self.assertFalse(self.directory.with_name('responsibility.anchor.json').exists())
        self.assertEqual(self.driver.browser.calls, ['collect'])
        self.assertEqual(self.app.store.unresolved_actions(), [])
        self.driver.mocks[2].assert_not_called()

    def test_explicit_scope_then_start_clears_refusal_and_preserves_normal_confirmation(self):
        self.collect()
        self.app.start()
        self.assert_blocked('shared_responsibility', 'REGISTRY_MISSING')
        self.initialize()
        self.driver.mocks[1].reset_mock()
        self.app.start()
        self.assertTrue(self.app.automatic)
        self.assertTrue(self.app.observing)
        self.assertEqual(self.app.buttons['start'].cget('text'), '全自动管理：已开启')
        self.driver.mocks[1].assert_not_called()
        self.driver.mocks[2].assert_called_once()
        self.assertIn({'event': 'start_completed', 'stage': 'activation', 'reason_code': 'OK'}, self.events)
        self.assertEqual(self.driver.browser.calls, ['collect'])
        self.assertEqual(self.driver.browser.sends, 0)

    def test_session_and_stale_fees_have_distinct_diagnostics_before_registry(self):
        self.app.start()
        self.assert_blocked('identity', 'SESSION_NOT_VERIFIED')
        self.collect()
        self.app.collection_observed_wall = time.time() - 400
        self.app.collection_observed_monotonic = time.monotonic() - 400
        self.app.start()
        self.assert_blocked('collection', 'COLLECTION_NOT_CURRENT')
        self.assertFalse(self.directory.exists())
        self.driver.mocks[1].assert_not_called()
        self.assertEqual(self.driver.browser.calls, ['collect'])

    def test_busy_start_reports_reason_without_workbook_or_platform_work(self):
        self.app.busy = True
        try:
            with patch.object(self.app, 'check_people') as check:
                self.app.start()
            check.assert_not_called()
            self.assert_blocked('precondition', 'OPERATION_BUSY')
            self.assertEqual(self.driver.browser.calls, [])
        finally:
            self.app.busy = False

    def test_no_managed_people_reports_workbook_gate_and_keeps_skip_rule(self):
        self.app.store.save_people([Person('Synthetic Start', Decimal(-1))])
        self.app.start()
        self.assert_blocked('workbook', 'WORKBOOK_NOT_READY')
        self.assertEqual(self.driver.browser.calls, [])
        self.assertFalse(self.directory.exists())

    def test_known_target_conflict_reports_target_gate_without_registry_prompt(self):
        self.collect()
        with patch.object(self.app, '_claim_targets', side_effect=TargetLockError()):
            self.app.start()
        self.assert_blocked('target_lock', 'TARGET_IN_USE')
        self.driver.mocks[1].assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_invalid_registry_is_not_presented_as_new_scope_setup(self):
        self.collect()
        self.initialize()
        database = self.directory / 'responsibility.sqlite3'
        before = database.read_bytes()
        with patch.object(self.app.responsibility_gate, 'reconcile', side_effect=RegistryError('REGISTRY_INVALID')):
            self.app.start()
        self.assert_blocked('shared_responsibility', 'REGISTRY_INVALID')
        self.driver.mocks[1].assert_called_once()
        self.assertNotIn('如果从未建立过', self.driver.mocks[1].call_args.args[1])
        self.assertEqual(database.read_bytes(), before)

    def test_shared_failure_diagnostics_do_not_emit_private_exception_contents(self):
        self.collect()
        private = 'synthetic-private-ledger-and-operation-detail'
        with patch.object(self.app.responsibility_gate, 'reconcile', side_effect=GuardError(private)):
            self.app.start()
        self.assert_blocked('shared_responsibility', 'RESPONSIBILITY_CHECK_FAILED')
        self.assertNotIn(private, repr(self.events))

    def test_cancelled_confirmation_and_mutation_do_not_start_management(self):
        self.collect()
        self.initialize()
        with patch('quota_guard.ui.messagebox.askyesno', return_value=False):
            self.app.start()
        self.assert_blocked('confirmation', 'START_CANCELLED')

        def stop_during_dialog(*_args, **_kwargs):
            self.app.stop(quiet=True)
            return True
        with patch('quota_guard.ui.messagebox.askyesno', side_effect=stop_during_dialog):
            self.app.start()
        self.assert_blocked('confirmation', 'START_CONTEXT_CHANGED')
        self.assertEqual(self.driver.browser.calls, ['collect'])


if __name__ == '__main__':
    unittest.main()
