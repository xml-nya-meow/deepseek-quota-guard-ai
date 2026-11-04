"""Diagnose local target conflicts without confusing them with fee failures.

All ledgers are temporary, all targets are synthetic, and the browser is an
offline double.  Native mutex tests create and release only unique test names.
"""
from decimal import Decimal
import multiprocessing
import os
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import test_workflow as fixtures
from quota_guard.core import GuardError, Person
from quota_guard.target_lock import TargetLock, TargetLockError


class CollectionTargetConflictTests(unittest.TestCase):
    tearDown = fixtures.WorkflowTests.tearDown
    pump = fixtures.WorkflowTests.pump
    valid_collection = fixtures.WorkflowTests.valid_collection

    def setUp(self):
        fixtures.WorkflowTests.setUp(self)
        self.target = 'fixture_' + uuid4().hex
        self.other = 'fixture_' + uuid4().hex
        self.keys = [
            {'name': '测试甲', 'tracking_id': self.target},
            {'name': '测试乙', 'tracking_id': self.other},
        ]
        self.store.save_people([Person('测试甲', Decimal(2)),
                                Person('测试乙', Decimal(-1)),
                                Person('', Decimal(0))], allow_unassigned=True)
        self.app.load_folder()
        self.emitted = []
        real_diagnostic = self.app._diagnostic

        def record(event, **fields):
            self.emitted.append((event, fields))
            real_diagnostic(event, **fields)

        self.app._diagnostic = record

    def result(self, *, empty=False, reason_code=None):
        values = {'requested_tracking_ids': [self.target],
                  'totals': None if empty else {self.target: Decimal('0.25')},
                  'reason_code': reason_code or ('NO_SELECTED_USAGE_DATA' if empty else 'OK'),
                  'retryable': True}
        if empty:
            values['source_id'] = ''
        return self.valid_collection(**values)

    def read(self, result):
        if not isinstance(self.app.browser, fixtures.OfflineBrowser):
            self.app.browser = fixtures.OfflineBrowser({'collect': result})
        else:
            self.app.browser.results['collect'] = result
        self.app.collect()
        self.pump()

    def event(self, name):
        return [fields for event, fields in self.emitted if event == name][-1]

    def assert_no_application(self):
        with self.store._connect() as db:
            for table in ('bindings', 'costs', 'snapshots'):
                self.assertEqual(db.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0], 0)
        self.assertFalse(self.app.automatic)
        self.assertFalse(self.app.observing)
        self.assertFalse(self.app.busy)
        self.assertIsNone(self.app.current_action)
        self.assertIsNone(self.app.delete_permit)
        self.assertIsNone(self.app.collection_context)
        self.assertFalse(self.app._collection_is_current())
        self.assertFalse(any(event['kind'] == 'action' for event in self.store.events()))
        self.assertTrue(all(action == 'collect' for action, _ in self.app.browser.calls))

    def assert_conflict(self, fee_reason):
        self.assert_no_application()
        failure = self.event('collection_application_failed')
        self.assertEqual(failure['reason_code'], 'TARGET_IN_USE')
        self.assertEqual(failure['stage'], 'target_lock_acquisition')
        self.assertEqual(failure['persistence_state'], 'not_written')
        self.assertEqual(self.app.action_states['collect'], '其他实例占用')
        self.assertEqual(self.event('collection_result_received')['reason_code'], fee_reason)
        self.assertIn('关闭', self.app.status_var.get())
        self.assertIn('重新读取', self.app.status_var.get())

    def test_verified_fees_conflict_then_same_app_recovers_without_login(self):
        permit = Mock()
        self.app.delete_permit = permit
        self.app.automatic = self.app.observing = True
        with TargetLock({self.target}):
            self.read(self.result())
            self.assert_conflict('OK')
        permit.decide.assert_called_with(False)
        self.read(self.result())
        self.assertEqual(self.app.action_states['collect'], '成功')
        self.assertTrue(self.app.session_ready)
        self.assertTrue(self.app.collection_has_fees)
        self.assertEqual([action for action, _ in self.app.browser.calls], ['collect', 'collect'])
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM bindings').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0], 1)

    def test_empty_fees_conflict_then_identity_read_recovers_without_zero_snapshot(self):
        with TargetLock({self.target}):
            self.read(self.result(empty=True))
            self.assert_conflict('NO_SELECTED_USAGE_DATA')
        self.read(self.result(empty=True))
        self.assertEqual(self.app.collection_reason_code, 'NO_SELECTED_USAGE_DATA')
        self.assertTrue(self.app.session_ready)
        self.assertTrue(self.app._collection_is_current())
        self.assertFalse(self.app.collection_has_fees)
        self.assertEqual([action for action, _ in self.app.browser.calls], ['collect', 'collect'])
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM costs').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0], 0)
        self.assertEqual(self.app.rows[0]['status'], 'unknown')

    def test_complete_list_protection_conflict_is_explained_even_for_skipped_fee_target(self):
        # Fee selection remains narrow, while existing whole-list exclusion is
        # still enforced; the user must see why this independent guard stopped.
        with TargetLock({self.other}):
            self.read(self.result())
            self.assert_conflict('OK')
        self.assertEqual(self.app.browser.calls[0][1]['managed_names'], ['测试甲'])

    def test_generic_target_lock_fault_does_not_accuse_another_instance(self):
        with patch.object(self.app, '_claim_targets', side_effect=GuardError('synthetic lock failure')):
            self.read(self.result())
        self.assert_no_application()
        failure = self.event('collection_application_failed')
        self.assertEqual(failure['reason_code'], 'TARGET_LOCK_FAILED')
        self.assertEqual(failure['stage'], 'target_lock_acquisition')
        self.assertEqual(failure['persistence_state'], 'not_written')
        self.assertEqual(self.app.action_states['collect'], '目标保护不可用')
        self.assertIn('未认定为其他实例占用', self.app.status_var.get())

    def test_unknown_received_reason_is_replaced_with_fixed_diagnostic_code(self):
        marker = 'UNTRUSTED_SYNTHETIC_REASON_CONTENT'
        self.read(self.result(reason_code=marker))
        self.assertEqual(self.event('collection_result_received')['reason_code'], 'UNKNOWN_COLLECTION_RESULT')
        self.assertNotIn(marker, repr(self.emitted))


def _hold_synthetic_target(connection, target):
    """Spawn fixture, bounded by an explicit stop sent by this test only."""
    lock = None
    try:
        lock = TargetLock({target})
        connection.send('locked')
        connection.recv()
    finally:
        if lock is not None:
            lock.close()
        connection.close()


@unittest.skipUnless(os.name == 'nt', 'Windows native target mutex diagnostics')
class NativeTargetConflictReasonTests(unittest.TestCase):
    def test_same_thread_conflict_has_dedicated_reason_and_releases(self):
        target = 'fixture_' + uuid4().hex
        with TargetLock({target}):
            with self.assertRaises(TargetLockError) as caught:
                TargetLock({target})
            self.assertEqual(caught.exception.reason_code, 'TARGET_IN_USE')
        with TargetLock({target}) as recovered:
            self.assertTrue(recovered.covers({target}))

    def test_child_owner_conflict_has_dedicated_reason_and_recovers_after_exit(self):
        target = 'fixture_' + uuid4().hex
        context = multiprocessing.get_context('spawn')
        parent, child = context.Pipe()
        process = context.Process(target=_hold_synthetic_target, args=(child, target))
        process.start()
        child.close()

        def cleanup():
            if process.is_alive():
                process.terminate()  # Only this test's own synthetic child.
            process.join(10)
            parent.close()

        self.addCleanup(cleanup)
        self.assertTrue(parent.poll(10), 'Synthetic owner did not report readiness')
        self.assertEqual(parent.recv(), 'locked')
        with self.assertRaises(TargetLockError) as caught:
            TargetLock({target})
        self.assertEqual(caught.exception.reason_code, 'TARGET_IN_USE')
        parent.send('close')
        process.join(10)
        self.assertEqual(process.exitcode, 0)
        with TargetLock({target}) as recovered:
            self.assertTrue(recovered.covers({target}))


if __name__ == '__main__':
    unittest.main()
