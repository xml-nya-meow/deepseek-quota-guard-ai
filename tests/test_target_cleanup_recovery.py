"""Fault injection for cleanup ownership; never opens an account or browser."""
import unittest
from unittest.mock import patch

import test_target_lock as locks
import test_workflow as workflow
from quota_guard.core import GuardError
from quota_guard.target_lock import TargetLock, TargetLockCleanupError, _name


class InitialCleanupTests(unittest.TestCase):
    def test_two_phase_acquisition_retains_initial_rollback_failure(self):
        backend = locks.FakeBackend()
        backend.fail_acquire.add(_name('tracking_Z'))
        backend.fail_release.add(_name('tracking_A'))
        with patch('quota_guard.target_lock._WindowsMutexBackend', return_value=backend):
            owner = TargetLock()
        try:
            with self.assertRaises(GuardError):
                owner.update({'tracking_A', 'tracking_Z'})
            self.assertEqual(owner.tracking_ids, {'tracking_A'})
            with self.assertRaises(GuardError):
                owner.covers({'tracking_A'})
            with self.assertRaises(GuardError):
                owner.update({'tracking_A'})
            with self.assertRaises(GuardError):
                owner.close()
            self.assertTrue(backend.held)
        finally:
            backend.fail_release.clear()
            owner.close()
        self.assertFalse(backend.held)
        owner.close()

    def test_legacy_constructor_transfers_failed_cleanup_owner_in_exception(self):
        backend = locks.FakeBackend()
        backend.fail_acquire.add(_name('tracking_Z'))
        backend.fail_release.add(_name('tracking_A'))
        with patch('quota_guard.target_lock._WindowsMutexBackend', return_value=backend):
            with self.assertRaises(TargetLockCleanupError) as caught:
                TargetLock({'tracking_A', 'tracking_Z'})
        owner = caught.exception.cleanup_owner
        self.assertEqual(owner.tracking_ids, {'tracking_A'})
        backend.fail_release.clear()
        owner.close()
        self.assertFalse(backend.held)

    def test_conflicting_handle_close_failure_never_releases_competing_owner(self):
        backend = locks.WindowsMutexBackendTests().backend(error=183)
        backend.api.CloseHandle.return_value = False
        with patch('quota_guard.target_lock._WindowsMutexBackend', return_value=backend):
            owner = TargetLock()
        try:
            with self.assertRaises(GuardError):
                owner.update({'tracking_A'})
            self.assertEqual(owner.tracking_ids, {'tracking_A'})
            with self.assertRaises(GuardError):
                owner.covers({'tracking_A'})
            backend.api.ReleaseMutex.assert_not_called()
            self.assertFalse(owner._handles['tracking_A'].owned)
        finally:
            backend.api.CloseHandle.return_value = True
            owner.close()
        backend.api.ReleaseMutex.assert_not_called()
        self.assertFalse(owner.tracking_ids)

    def test_transient_conflict_close_failure_cleans_reference_but_never_grants(self):
        backend = locks.WindowsMutexBackendTests().backend(error=183)
        backend.api.CloseHandle.side_effect = [False, True]
        with patch('quota_guard.target_lock._WindowsMutexBackend', return_value=backend):
            owner = TargetLock()
        with self.assertRaises(GuardError):
            owner.update({'tracking_A'})
        self.assertFalse(owner.tracking_ids)
        self.assertFalse(owner.covers({'tracking_A'}))
        backend.api.ReleaseMutex.assert_not_called()
        owner.close()


class WindowCleanupTests(unittest.TestCase):
    setUp = workflow.WorkflowTests.setUp
    tearDown = workflow.WorkflowTests.tearDown
    pump = workflow.WorkflowTests.pump

    def test_gui_keeps_cleanup_owner_before_first_acquisition_and_retries_close(self):
        backend = locks.FakeBackend()
        backend.fail_acquire.add(_name('tracking_Z'))
        backend.fail_release.add(_name('tracking_A'))
        keys = [{'tracking_id': 'tracking_A'}, {'tracking_id': 'tracking_Z'}]
        try:
            with patch('quota_guard.target_lock._WindowsMutexBackend', return_value=backend):
                with self.assertRaises(GuardError):
                    self.app._claim_targets(keys)
            owner = self.app.target_lock
            self.assertIsNotNone(owner)
            self.assertFalse(self.app._close_local_locks())
            self.assertTrue(self.app.cleanup_failed)
            self.assertIs(self.app.target_lock, owner)
            self.assertFalse(self.app.automatic)
        finally:
            backend.fail_release.clear()
        self.assertTrue(self.app._close_local_locks())
        self.assertFalse(backend.held)
