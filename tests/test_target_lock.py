"""Offline target exclusion fixtures; no browser, account or platform calls."""
import multiprocessing
import os
import threading
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from quota_guard.core import GuardError
from quota_guard.target_lock import (TargetLock, TargetLockReferences, _Handle,
                                    _WindowsMutexBackend, _name, validate_lock_names)


class FakeBackend:
    def __init__(self):
        self.held = set()
        self.fail_acquire = set()
        self.fail_release = set()

    def acquire(self, name):
        if name in self.held or name in self.fail_acquire:
            raise GuardError("synthetic target conflict")
        self.held.add(name)
        return name

    def release(self, name):
        if name in self.fail_release:
            raise OSError("synthetic release failure")
        self.held.remove(name)


class TargetLockTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeBackend()
        self.backend_patch = patch("quota_guard.target_lock._WindowsMutexBackend", return_value=self.backend)
        self.backend_patch.start()
        self.addCleanup(self.backend_patch.stop)

    def lock(self, targets):
        lock = TargetLock(targets)
        self.addCleanup(lock.close)
        return lock

    def test_overlap_conflicts_even_on_same_thread_and_disjoint_sets_coexist(self):
        first = self.lock({"tracking_A", "tracking_B"})
        second = self.lock({"tracking_C"})
        with self.assertRaises(GuardError):
            TargetLock({"tracking_B", "tracking_D"})
        self.assertEqual(first.tracking_ids, {"tracking_A", "tracking_B"})
        self.assertTrue(second.covers({"tracking_C"}))
        self.assertNotIn(_name("tracking_D"), self.backend.held)

    def test_failed_batch_releases_previously_acquired_additions(self):
        owner = self.lock({"tracking_Z"})
        with self.assertRaises(GuardError):
            TargetLock({"tracking_A", "tracking_Z"})
        self.assertEqual(self.backend.held, {_name("tracking_Z")})
        self.assertTrue(owner.covers({"tracking_Z"}))
        self.lock({"tracking_A"})

    def test_failed_update_retains_old_protection_and_rolls_back_new_targets(self):
        first = self.lock({"tracking_A"})
        other = self.lock({"tracking_Z"})
        with self.assertRaises(GuardError):
            first.update({"tracking_B", "tracking_Z"})
        self.assertEqual(first.tracking_ids, {"tracking_A"})
        self.assertEqual(self.backend.held, {_name("tracking_A"), _name("tracking_Z")})
        self.assertTrue(other.covers({"tracking_Z"}))

    def test_update_deduplicates_and_close_is_idempotent(self):
        lock = self.lock(["tracking_A", "tracking_A"])
        lock.update({"tracking_A", "tracking_B"})
        self.assertTrue(lock.covers({"tracking_A", "tracking_B"}))
        lock.update({"tracking_B"})
        self.assertNotIn(_name("tracking_A"), self.backend.held)
        self.lock({"tracking_A"})
        lock.close()
        lock.close()
        with self.assertRaises(GuardError):
            lock.covers({"tracking_B"})
        with self.assertRaises(GuardError):
            lock.update({"tracking_B"})

    def test_empty_malformed_secret_or_shortened_targets_fail_before_acquisition(self):
        for values in ([], None, "tracking_A", ["short"], ["tracking...A"],
                       [" tracking_A"], ["tracking_A\n"], ["sk-syntheticsecret"],
                       ["SK-syntheticsecret"], [None], ["tracking_A", 123]):
            with self.subTest(values=values), self.assertRaises(GuardError):
                TargetLock(values)
        self.assertFalse(self.backend.held)
        lock = self.lock({"tracking_A"})
        with self.assertRaises(GuardError):
            lock.update([])
        with self.assertRaises(GuardError):
            lock.covers([])
        self.assertTrue(lock.covers({"tracking_A"}))

    def test_other_thread_cannot_check_extend_or_release_owner(self):
        lock = self.lock({"tracking_A"})
        errors = []

        def use_from_other_thread():
            for operation in (lambda: lock.covers({"tracking_A"}),
                              lambda: lock.update({"tracking_B"}), lock.close):
                try:
                    operation()
                except GuardError:
                    errors.append(True)

        worker = threading.Thread(target=use_from_other_thread)
        worker.start()
        worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(errors), 3)
        self.assertTrue(lock.covers({"tracking_A"}))

    def test_rollback_cleanup_failure_blocks_operations_until_explicit_close(self):
        lock = self.lock({"tracking_A"})
        self.backend.fail_acquire.add(_name("tracking_Z"))
        self.backend.fail_release.add(_name("tracking_B"))
        try:
            with self.assertRaisesRegex(GuardError, "回滚"):
                lock.update({"tracking_A", "tracking_B", "tracking_Z"})
            with self.assertRaises(GuardError):
                lock.covers({"tracking_A"})
            with self.assertRaises(GuardError):
                lock.update({"tracking_A"})
        finally:
            self.backend.fail_release.clear()
        lock.close()
        self.assertFalse(self.backend.held)

    def test_close_failure_preserves_handle_for_retry_and_blocks_coverage(self):
        lock = self.lock({"tracking_A"})
        self.backend.fail_release.add(_name("tracking_A"))
        try:
            with self.assertRaises(GuardError):
                lock.close()
            with self.assertRaises(GuardError):
                lock.covers({"tracking_A"})
            self.assertIn(_name("tracking_A"), self.backend.held)
        finally:
            self.backend.fail_release.clear()
        lock.close()
        self.assertFalse(self.backend.held)

    def test_global_names_depend_only_on_full_target(self):
        name = _name("tracking_A")
        self.assertTrue(name.startswith("Global\\DeepSeekQuotaGuard.VerifiedTarget.v1."))
        self.assertEqual(name, _name("tracking_A"))
        self.assertNotEqual(name, _name("tracking_B"))
        self.assertNotIn("tracking_A", name)

    def test_exported_names_require_live_owned_protection(self):
        lock = self.lock({"tracking_B", "tracking_A"})
        self.assertEqual(lock.names, tuple(sorted({_name("tracking_A"), _name("tracking_B")})))
        lock.close()
        with self.assertRaises(GuardError):
            _ = lock.names


class WindowsMutexBackendTests(unittest.TestCase):
    def backend(self, *, handle=123, error=0):
        backend = _WindowsMutexBackend.__new__(_WindowsMutexBackend)
        backend.api = Mock()
        backend.api.CreateMutexW.return_value = handle
        backend.api.ReleaseMutex.return_value = True
        backend.api.CloseHandle.return_value = True
        backend.ctypes = Mock()
        backend.ctypes.get_last_error.return_value = error
        return backend

    def test_new_global_object_is_created_with_initial_thread_ownership(self):
        backend = self.backend()
        result = backend.acquire(_name("tracking_A"))
        backend.ctypes.set_last_error.assert_called_once_with(0)
        backend.api.CreateMutexW.assert_called_once_with(None, True, _name("tracking_A"))
        self.assertEqual(result, _Handle(123))

    def test_existing_object_is_closed_without_releasing_other_owner(self):
        backend = self.backend(error=183)
        with self.assertRaises(GuardError):
            backend.acquire(_name("tracking_A"))
        backend.api.CloseHandle.assert_called_once_with(123)
        backend.api.ReleaseMutex.assert_not_called()

    def test_access_failure_does_not_fall_back_to_a_local_namespace(self):
        backend = self.backend(handle=None, error=5)
        with self.assertRaises(GuardError):
            backend.acquire(_name("tracking_A"))
        self.assertEqual(backend.api.CreateMutexW.call_count, 1)
        backend.api.CloseHandle.assert_not_called()

    def test_failed_release_keeps_owned_handle_for_explicit_retry(self):
        backend = self.backend()
        handle = _Handle(123)
        backend.api.ReleaseMutex.return_value = False
        with self.assertRaises(GuardError):
            backend.release(handle)
        self.assertTrue(handle.owned)
        backend.api.CloseHandle.assert_not_called()
        backend.api.ReleaseMutex.return_value = True
        backend.release(handle)
        self.assertFalse(handle.owned)

    def test_failed_close_retries_close_without_releasing_mutex_twice(self):
        backend = self.backend()
        handle = _Handle(123)
        backend.api.CloseHandle.return_value = False
        with self.assertRaises(GuardError):
            backend.release(handle)
        self.assertFalse(handle.owned)
        backend.api.CloseHandle.return_value = True
        backend.release(handle)
        backend.api.ReleaseMutex.assert_called_once_with(123)
        self.assertEqual(backend.api.CloseHandle.call_count, 2)

    def test_reference_only_opens_existing_object_and_never_takes_ownership(self):
        backend = self.backend()
        backend.api.OpenMutexW.return_value = 456
        handle = backend.open_reference(_name("tracking_A"))
        backend.api.OpenMutexW.assert_called_once_with(0x00100000, False, _name("tracking_A"))
        backend.close_reference(handle)
        backend.api.CloseHandle.assert_called_once_with(456)
        backend.api.CreateMutexW.assert_not_called()
        backend.api.ReleaseMutex.assert_not_called()
        backend.api.WaitForSingleObject.assert_not_called()

    def test_missing_reference_is_not_recreated(self):
        backend = self.backend()
        backend.api.OpenMutexW.return_value = None
        with self.assertRaises(GuardError):
            backend.open_reference(_name("tracking_A"))
        backend.api.CreateMutexW.assert_not_called()


class FakeReferenceBackend:
    def __init__(self):
        self.known = {_name("tracking_A"), _name("tracking_B"), _name("tracking_Z")}
        self.held = set()
        self.opened = []
        self.fail_open = set()
        self.fail_close = set()

    def open_reference(self, name):
        if name not in self.known or name in self.fail_open:
            raise GuardError("synthetic reference failure")
        self.opened.append(name)
        self.held.add(name)
        return name

    def close_reference(self, name):
        if name in self.fail_close:
            raise OSError("synthetic close failure")
        self.held.remove(name)


class TargetLockReferenceTests(unittest.TestCase):
    def setUp(self):
        self.backend = FakeReferenceBackend()
        backend_patch = patch("quota_guard.target_lock._WindowsMutexBackend", return_value=self.backend)
        backend_patch.start()
        self.addCleanup(backend_patch.stop)
        self.references = TargetLockReferences()
        self.addCleanup(self.references.close)

    def test_only_fixed_nonempty_hashed_namespace_is_accepted(self):
        for names in ([], None, _name("tracking_A"), ["tracking_A"],
                      [_name("tracking_A").replace("Global\\", "Local\\")],
                      [_name("tracking_A") + "x"], [_name("tracking_A"), 1],
                      ["Global\\DeepSeekQuotaGuard.VerifiedTarget.v1." + "A" * 64]):
            with self.subTest(names=names), self.assertRaises(GuardError):
                self.references.retain(names)
        self.assertEqual(self.backend.opened, [])
        self.assertEqual(validate_lock_names([_name("tracking_A")] * 2), (_name("tracking_A"),))

    def test_retain_is_additive_idempotent_and_returns_complete_reference_set(self):
        first, second = _name("tracking_A"), _name("tracking_B")
        self.assertEqual(self.references.retain([first, first]), (first,))
        self.references.retain([second])
        self.assertEqual(self.references.retain([first]), tuple(sorted((first, second))))
        self.assertEqual(self.backend.opened.count(first), 1)
        self.references.close()
        self.references.close()
        self.assertFalse(self.backend.held)
        with self.assertRaises(GuardError):
            self.references.retain([first])

    def test_failed_batch_rolls_back_new_references_and_preserves_previous(self):
        held = _name("tracking_A")
        new, failure = sorted((_name("tracking_B"), _name("tracking_Z")))
        self.references.retain([held])
        self.backend.fail_open.add(failure)
        with self.assertRaises(GuardError):
            self.references.retain([new, failure])
        self.assertEqual(self.references.names, (held,))
        self.assertEqual(self.backend.held, {held})

    def test_failed_rollback_disables_acknowledgment_but_keeps_close_retry(self):
        new, failure = sorted((_name("tracking_B"), _name("tracking_Z")))
        self.backend.fail_open.add(failure)
        self.backend.fail_close.add(new)
        try:
            with self.assertRaisesRegex(GuardError, "回滚"):
                self.references.retain([new, failure])
            with self.assertRaises(GuardError):
                _ = self.references.names
            with self.assertRaises(GuardError):
                self.references.retain([_name("tracking_A")])
        finally:
            self.backend.fail_close.clear()
        self.references.close()
        self.assertFalse(self.backend.held)

    def test_failed_close_retains_reference_until_retry(self):
        name = _name("tracking_A")
        self.references.retain([name])
        self.backend.fail_close.add(name)
        try:
            with self.assertRaises(GuardError):
                self.references.close()
            self.assertIn(name, self.backend.held)
        finally:
            self.backend.fail_close.clear()
        self.references.close()
        self.assertFalse(self.backend.held)


def hold_target_process(connection, targets):
    """Spawn fixture: report acquisition, then wait for an explicit local exit."""
    lock = None
    try:
        lock = TargetLock(targets)
        connection.send({"locked": True})
        command = connection.recv()
        if command == "crash":
            os._exit(19)
    except GuardError:
        connection.send({"locked": False})
    finally:
        if lock is not None:
            lock.close()
        connection.close()


@unittest.skipUnless(os.name == "nt", "Windows global mutex integration")
class WindowsTargetLockTests(unittest.TestCase):
    def start_owner(self, targets):
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(target=hold_target_process, args=(child, targets))
        process.start()
        child.close()

        def cleanup():
            if process.is_alive():
                process.terminate()
            process.join(10)
            parent.close()

        self.addCleanup(cleanup)
        self.assertTrue(parent.poll(10), "Synthetic mutex child did not report readiness")
        self.assertEqual(parent.recv(), {"locked": True})
        return process, parent

    def test_real_same_thread_second_instance_cannot_use_mutex_recursion(self):
        target = "fixture_" + uuid4().hex
        with TargetLock({target}):
            with self.assertRaises(GuardError):
                TargetLock({target})
        with TargetLock({target}) as recovered:
            self.assertTrue(recovered.covers({target}))

    def test_other_process_conflict_rolls_back_whole_batch_and_normal_exit_releases(self):
        shared = "zz_fixture_" + uuid4().hex
        added = "aa_fixture_" + uuid4().hex
        process, connection = self.start_owner({shared})
        with self.assertRaises(GuardError):
            TargetLock({added, shared})
        with TargetLock({added}) as independent:
            self.assertTrue(independent.covers({added}))
        connection.send("close")
        process.join(10)
        self.assertEqual(process.exitcode, 0)
        with TargetLock({shared}) as recovered:
            self.assertTrue(recovered.covers({shared}))

    def test_crashed_owner_does_not_leave_a_persistent_lock(self):
        target = "fixture_" + uuid4().hex
        process, connection = self.start_owner({target})
        with self.assertRaises(GuardError):
            TargetLock({target})
        connection.send("crash")
        process.join(10)
        self.assertEqual(process.exitcode, 19)
        with TargetLock({target}) as recovered:
            self.assertTrue(recovered.covers({target}))

    def test_forcibly_terminated_owner_releases_kernel_handles(self):
        target = "fixture_" + uuid4().hex
        process, _ = self.start_owner({target})
        process.terminate()
        process.join(10)
        self.assertFalse(process.is_alive())
        with TargetLock({target}) as recovered:
            self.assertTrue(recovered.covers({target}))

    def test_reference_keeps_name_occupied_after_owner_releases_it(self):
        target = "fixture_" + uuid4().hex
        owner = TargetLock({target})
        self.addCleanup(owner.close)
        references = TargetLockReferences()
        self.addCleanup(references.close)
        references.retain(owner.names)
        owner.close()
        with self.assertRaises(GuardError):
            TargetLock({target})
        references.close()
        with TargetLock({target}) as recovered:
            self.assertTrue(recovered.covers({target}))

    def test_surviving_reference_blocks_new_owner_after_original_process_is_killed(self):
        target = "fixture_" + uuid4().hex
        process, _ = self.start_owner({target})
        references = TargetLockReferences()
        self.addCleanup(references.close)
        references.retain([_name(target)])
        process.terminate()
        process.join(10)
        self.assertFalse(process.is_alive())
        with self.assertRaises(GuardError):
            TargetLock({target})
        references.close()
        with TargetLock({target}) as recovered:
            self.assertTrue(recovered.covers({target}))


if __name__ == "__main__":
    unittest.main()
