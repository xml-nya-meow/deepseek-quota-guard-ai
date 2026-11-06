"""Offline recovery inventory validation at the permit and process boundaries.

The spawned worker is the synthetic IPC fixture, never BrowserService. Its
revoke action only exchanges messages; no browser, credential or HTTP exists.
"""
from collections import deque
from concurrent.futures import Future
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock
from uuid import uuid4

from quota_guard.browser_process import IsolatedBrowserService, _Task
from quota_guard.core import GuardError
from quota_guard.revocation_permit import DeletePermit
from quota_guard.target_lock import TargetLock
from tests.test_browser_process import fixture_worker


TARGET = 'synthetic-inventory-target'
CONTROL = 'synthetic-inventory-control'
OTHER = 'synthetic-inventory-other'


def inventory():
    return {'target_tracking_id': TARGET, 'known_tracking_ids': [TARGET, CONTROL]}


class PreparedInventoryPermitTests(unittest.TestCase):
    def test_bound_target_property_cannot_be_reassigned(self):
        permit = DeletePermit(target_tracking_id=TARGET)
        with self.assertRaises(AttributeError):
            permit.target_tracking_id = OTHER
        self.assertEqual(permit.target_tracking_id, TARGET)
        with self.assertRaises(GuardError):
            permit.set_prepared_inventory(OTHER, [OTHER, CONTROL])
        self.assertFalse(permit.ready.is_set())
        self.assertFalse(permit.allowed)

    def test_inventory_is_detached_immutable_and_never_creates_permission(self):
        permit = DeletePermit(target_tracking_id=TARGET)
        known = [TARGET, CONTROL]
        permit.set_prepared_inventory(TARGET, known)
        known.append(OTHER)
        received = permit.prepared_inventory(TARGET)
        self.assertEqual(received, {'target_tracking_id': TARGET,
                                   'known_tracking_ids': sorted([TARGET, CONTROL])})
        received['known_tracking_ids'].append(OTHER)
        permit.set_prepared_inventory(TARGET, [CONTROL, TARGET])
        self.assertEqual(permit.prepared_inventory(TARGET)['known_tracking_ids'], sorted([TARGET, CONTROL]))
        for target, identities in ((TARGET, [TARGET, OTHER]), (OTHER, [OTHER, CONTROL])):
            with self.subTest(target=target), self.assertRaises(GuardError):
                permit.set_prepared_inventory(target, identities)
        self.assertFalse(permit.ready.is_set())
        self.assertFalse(permit.decided.is_set())
        self.assertFalse(permit.allowed)
        with self.assertRaises(GuardError):
            permit.export_grant()

    def test_missing_inventory_or_wrong_retrieval_target_is_rejected(self):
        permit = DeletePermit(target_tracking_id=TARGET)
        with self.assertRaises(GuardError):
            permit.prepared_inventory(TARGET)
        permit.set_prepared_inventory(TARGET, [TARGET, CONTROL])
        with self.assertRaises(GuardError):
            permit.prepared_inventory(OTHER)
        self.assertFalse(permit.ready.is_set())
        self.assertFalse(permit.allowed)

    def test_invalid_lists_do_not_install_partial_context_or_allow_submission(self):
        invalid = [None, 'not-an-inventory', [TARGET], [TARGET, TARGET], [CONTROL, OTHER],
                   [TARGET, 'sk-synthetic-not-a-tracking-id'], [TARGET, 'bad\nidentity'],
                   [TARGET, {'authorization': 'synthetic-private-value'}],
                   [TARGET] + [f'synthetic-control-{n:03}' for n in range(200)]]
        for known in invalid:
            with self.subTest(known=known):
                permit = DeletePermit(target_tracking_id=TARGET)
                with self.assertRaises(GuardError):
                    permit.set_prepared_inventory(TARGET, known)
                with self.assertRaises(GuardError):
                    permit.prepared_inventory(TARGET)
                self.assertFalse(permit.ready.is_set())
                self.assertFalse(permit.decided.is_set())
                self.assertFalse(permit.allowed)


class PreparedInventoryDeliveryTests(unittest.TestCase):
    def pending(self):
        service = object.__new__(IsolatedBrowserService)
        service._lock = threading.RLock()
        service._closed = service._interrupting = False
        service._cleanup_error = None
        service._session, service._worker_pid = 'synthetic-protocol-session', 123
        service._timeouts = {'revoke': 65}
        service._progress = {}
        service._log = Mock()
        service._kill_session = Mock()
        service._interrupt_waiters = []
        future = Future()
        future.set_running_or_notify_cancel()
        permit = DeletePermit(target_tracking_id=TARGET)
        task = _Task(1, 'revoke', {'tracking_id': TARGET}, future, permit,
                     started=time.monotonic(), started_wall=time.time())
        service._active = task
        queued = Future()
        service._pending = deque([_Task(2, 'collect', {}, queued)])
        return service, task, queued

    def deliver(self, service, context, *, task=1, session=None):
        session = service._session if session is None else session
        message = {'kind': 'permit_ready', 'session': session, 'task': task}
        if context != 'missing':
            message['prepared_inventory'] = context
        service._message(session, message)

    def assert_rejected(self, service, task, queued):
        self.assertIsNone(service._active)
        self.assertIsInstance(task.future.exception(timeout=0), GuardError)
        self.assertIsInstance(queued.exception(timeout=0), GuardError)
        self.assertFalse(task.permit_ready)
        self.assertFalse(task.permit.ready.is_set())
        self.assertTrue(task.permit.decided.is_set())
        self.assertFalse(task.permit.allowed)
        self.assertFalse(task.decision_sent)
        self.assertFalse(task.decision_allowed)
        service._kill_session.assert_called_once()
        with self.assertRaises(GuardError):
            task.permit.export_grant()

    def test_malformed_ipc_context_aborts_before_readiness_or_grant(self):
        invalid = ['missing', None, [], {},
                   inventory() | {'target_tracking_id': OTHER},
                   inventory() | {'authorization': 'synthetic-private-value'},
                   inventory() | {'known_tracking_ids': [TARGET, TARGET]},
                   inventory() | {'known_tracking_ids': [TARGET]},
                   inventory() | {'known_tracking_ids': [CONTROL, OTHER]},
                   inventory() | {'known_tracking_ids': [TARGET, 'sk-synthetic-credential']},
                   inventory() | {'known_tracking_ids': [TARGET] +
                                  [f'synthetic-control-{n:03}' for n in range(200)]}]
        for context in invalid:
            with self.subTest(context=context):
                service, task, queued = self.pending()
                self.deliver(service, context)
                self.assert_rejected(service, task, queued)
                self.assertNotIn('synthetic-private-value', str(task.future.exception()))
                self.assertNotIn('synthetic-private-value', str(service._log.call_args_list))

    def test_conflicting_existing_context_aborts_before_exposing_ready(self):
        service, task, queued = self.pending()
        task.permit.set_prepared_inventory(TARGET, [TARGET, CONTROL])
        self.deliver(service, inventory() | {'known_tracking_ids': [TARGET, OTHER]})
        self.assert_rejected(service, task, queued)
        self.assertEqual(task.permit.prepared_inventory(TARGET)['known_tracking_ids'], sorted([TARGET, CONTROL]))

    def test_matching_ready_is_idempotent_but_never_approves(self):
        service, task, queued = self.pending()
        self.deliver(service, inventory())
        self.deliver(service, inventory() | {'known_tracking_ids': [CONTROL, TARGET]})
        self.assertTrue(task.permit_ready)
        self.assertTrue(task.permit.ready.is_set())
        self.assertFalse(task.permit.decided.is_set())
        self.assertFalse(task.permit.allowed)
        self.assertFalse(task.future.done())
        self.assertFalse(queued.done())
        self.assertFalse(task.decision_sent)
        service._kill_session.assert_not_called()
        with self.assertRaises(GuardError):
            task.permit.export_grant()

    def test_conflict_after_ready_still_aborts_without_rewriting_historical_evidence(self):
        service, task, queued = self.pending()
        self.deliver(service, inventory())
        self.deliver(service, inventory() | {'known_tracking_ids': [TARGET, OTHER]})
        self.assertIsInstance(task.future.exception(timeout=0), GuardError)
        self.assertIsInstance(queued.exception(timeout=0), GuardError)
        self.assertTrue(task.permit.ready.is_set(), 'Readiness remains historical evidence, not permission.')
        self.assertTrue(task.permit.decided.is_set())
        self.assertFalse(task.permit.allowed)
        self.assertEqual(task.permit.prepared_inventory(TARGET)['known_tracking_ids'], sorted([TARGET, CONTROL]))
        service._kill_session.assert_called_once()

    def test_stale_session_or_task_cannot_install_context(self):
        service, task, queued = self.pending()
        self.deliver(service, inventory(), session='synthetic-old-session')
        self.deliver(service, inventory(), task=2)
        self.assertFalse(task.future.done())
        self.assertFalse(queued.done())
        self.assertFalse(task.permit.ready.is_set())
        self.assertFalse(task.permit.allowed)
        with self.assertRaises(GuardError):
            task.permit.prepared_inventory(TARGET)
        service._kill_session.assert_not_called()


class PreparedInventorySpawnTests(unittest.TestCase):
    def test_synthetic_worker_passes_inventory_to_parent_without_automatic_authorization(self):
        with tempfile.TemporaryDirectory() as folder:
            lock = TargetLock(['synthetic-prepared-protocol-' + uuid4().hex])
            service = IsolatedBrowserService(diagnostics_dir=Path(folder) / 'diagnostics',
                                             _worker_target=fixture_worker)
            try:
                service.submit('selftest').result(timeout=15)
                permit = DeletePermit(target_tracking_id=TARGET)
                result = service.submit('revoke', name='synthetic target', tracking_id=TARGET,
                    permit=permit, target_lock_names=lock.names, mode='permit')
                self.assertTrue(permit.ready.wait(timeout=10))
                self.assertEqual(permit.prepared_inventory(TARGET),
                    {'target_tracking_id': TARGET,
                     'known_tracking_ids': sorted([TARGET, 'synthetic-control'])})
                self.assertFalse(permit.decided.is_set())
                self.assertFalse(permit.allowed)
                self.assertFalse(result.done())
                with self.assertRaises(GuardError):
                    permit.export_grant()
                permit.decide(False)
                self.assertEqual(result.result(timeout=5), {'cancelled': True})
            finally:
                service.close()
                lock.close()


if __name__ == '__main__':
    unittest.main()
