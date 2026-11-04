"""Offline identity-only refresh: no cost requests, grants or live browsers."""
from collections import deque
from concurrent.futures import Future
from contextlib import nullcontext
from copy import deepcopy
from queue import Queue
import threading
import unittest
from unittest.mock import Mock, patch

from quota_guard.browser_process import (
    IsolatedBrowserService, _Task, _browser_worker, _identity_read_result, _worker_failure,
)
from quota_guard.browser_service import BrowserService, identity_read_args, key_fingerprint
from quota_guard.core import AccountMismatch, GuardError
from quota_guard.revocation_permit import DeletePermit, RevocationNotSubmitted


KEY_A = '00000000-0000-0000-0000-000000000001'
KEY_B = '00000000-0000-0000-0000-000000000002'
KEY_OTHER = '00000000-0000-0000-0000-000000000003'
KEYS = [{'name': 'Synthetic A', 'tracking_id': KEY_A},
        {'name': 'Synthetic B', 'tracking_id': KEY_B}]


class IdentityReadTests(unittest.TestCase):
    def setUp(self):
        network = patch('socket.socket', side_effect=AssertionError('Network is forbidden'))
        network.start()
        self.addCleanup(network.stop)
        self.service = object.__new__(BrowserService)
        self.service.anchor = 'stale-anchor'
        self.service.known_keys = {KEY_OTHER}
        self.service._check_login_ready = Mock()
        self.service._keys = Mock(return_value=deepcopy(KEYS))

    def test_new_identity_read_binds_only_complete_keys_without_costs_or_manager(self):
        with patch('quota_guard.browser_service.ConsoleCostReader', side_effect=AssertionError('No cost read')), \
                patch('quota_guard.platform_keys.ConsoleKeyManager', side_effect=AssertionError('No management request')):
            result = self.service._read_keys()
        self.assertEqual(result, {'keys': KEYS, 'account_id': key_fingerprint(KEYS)})
        self.assertEqual(self.service.anchor, key_fingerprint(KEYS))
        self.assertEqual(self.service.known_keys, {KEY_A, KEY_B})
        self.service._check_login_ready.assert_called_once_with()
        self.service._keys.assert_called_once_with()

    def test_existing_account_marker_survives_key_set_change_with_known_anchor(self):
        self.service._cost_reader = Mock()
        self.service._cost_reader_identity = 'stable-ledger-account'
        result = self.service._read_keys('stable-ledger-account', {KEY_A})
        self.assertEqual(result['account_id'], 'stable-ledger-account')
        self.service._cost_reader.clear.assert_not_called()
        self.assertEqual(self.service._cost_reader.method_calls, [])

    def test_read_for_another_identity_clears_old_cost_session_without_collecting(self):
        reader = self.service._cost_reader = Mock()
        self.service._cost_reader_identity = 'old-account'
        self.service._read_keys()
        reader.clear.assert_called_once_with()
        self.assertIsNone(self.service._cost_reader)
        self.assertIsNone(self.service._cost_reader_identity)

    def test_empty_unrelated_or_unanchored_lists_cannot_keep_old_session_anchor(self):
        for keys, known, error in (([], {KEY_A}, GuardError),
                                  (KEYS, {KEY_OTHER}, AccountMismatch),
                                  (KEYS, set(), AccountMismatch)):
            with self.subTest(keys=keys, known=known):
                self.service.anchor = 'stale-anchor'
                self.service.known_keys = {KEY_OTHER}
                self.service._keys.return_value = deepcopy(keys)
                with self.assertRaises(error):
                    self.service._read_keys('existing-account', known)
                self.assertIsNone(self.service.anchor)
                self.assertEqual(self.service.known_keys, set())

    def test_unfinished_login_or_list_failure_cannot_reuse_old_identity(self):
        for failing in ('_check_login_ready', '_keys'):
            with self.subTest(failing=failing):
                self.service.anchor = 'stale-anchor'
                self.service.known_keys = {KEY_OTHER}
                method = getattr(self.service, failing)
                method.side_effect = GuardError('Synthetic read failure')
                with self.assertRaises(GuardError):
                    self.service._read_keys()
                self.assertIsNone(self.service.anchor)
                self.assertEqual(self.service.known_keys, set())
                method.side_effect = None

    def test_one_key_can_be_read_but_original_last_target_guard_still_blocks_delete(self):
        self.service._keys.return_value = deepcopy(KEYS[:1])
        self.service.context, self.service.page = Mock(), Mock()
        self.service._read_keys()
        manager = Mock()
        manager.capture.return_value = nullcontext()
        with patch('quota_guard.platform_keys.ConsoleKeyManager', return_value=manager):
            with self.assertRaises(RevocationNotSubmitted) as raised:
                with self.service._prepared_http_target('Synthetic A', KEY_A):
                    self.fail('The last target must never reach submission preparation')
        self.assertEqual(raised.exception.reason_code, 'KEY_LAST_TARGET')
        manager.prepare.assert_not_called()
        manager.read_target.assert_not_called()
        manager.clear.assert_called_once_with()

    def test_worker_admits_read_keys_and_invalidates_anchor_after_late_notice_failure(self):
        for notice in (None, GuardError('Synthetic optional notice')):
            with self.subTest(notice=notice):
                service = object.__new__(BrowserService)
                service.queue = Queue()
                service.page = None
                future = Future()
                service.queue.put(('read_keys', {}, future))
                service.queue.put(None)
                def ready():
                    service.page = Mock()
                def read():
                    service.anchor, service.known_keys = 'bound-anchor', {KEY_A}
                    return {'keys': KEYS, 'account_id': key_fingerprint(KEYS)}
                with patch.object(service, '_ensure_browser', side_effect=ready), \
                        patch.object(service, '_read_keys', side_effect=read), \
                        patch.object(service, '_dispose_browser'), \
                        patch('quota_guard.browser_service.raise_optional_notice_error', side_effect=notice):
                    service._worker()
                if notice is None:
                    self.assertEqual(future.result()['keys'], KEYS)
                else:
                    self.assertIsInstance(future.exception(), GuardError)
                    self.assertIsNone(service.anchor)
                    self.assertEqual(service.known_keys, set())


class IdentityProtocolTests(unittest.TestCase):
    def setUp(self):
        network = patch('socket.socket', side_effect=AssertionError('Network is forbidden'))
        network.start()
        self.addCleanup(network.stop)

    def args(self):
        return {'account_id': 'stable-account', 'known_keys': (KEY_A,)}

    def result(self):
        return {'keys': deepcopy(KEYS), 'account_id': 'stable-account'}

    def pending_service(self):
        service = object.__new__(IsolatedBrowserService)
        service._lock = threading.RLock()
        service._closed = service._interrupting = False
        service._cleanup_error = None
        service._session, service._worker_pid = 'synthetic-session', 123
        service._timeouts = {'read_keys': 45}
        service._progress = {}
        service._log = Mock()
        service._kill_session = Mock()
        service._interrupt_waiters = []
        service._pending = deque()
        service._wake, service._counter = Mock(), 0
        future = Future()
        future.set_running_or_notify_cancel()
        service._active = _Task(1, 'read_keys', self.args(), future, started=100, started_wall=1000)
        return service, future

    def deliver(self, service, *, result=None, ok=True, session='synthetic-session', task=1,
                monotonic=101, wall=1001, **extra):
        with patch('quota_guard.browser_process.time.monotonic', return_value=monotonic), \
                patch('quota_guard.browser_process.time.time', return_value=wall):
            service._message(session, {'kind': 'result', 'session': session, 'task': task,
                                      'ok': ok, 'result': self.result() if result is None else result, **extra})

    def test_read_request_accepts_only_identity_inputs_and_copies_known_keys(self):
        self.assertEqual(identity_read_args({}), {'account_id': '', 'known_keys': ()})
        service, _ = self.pending_service()
        known = {KEY_A}
        pending = service.submit('read_keys', account_id='stable-account', known_keys=known)
        known.clear()
        task = service._pending[-1]
        self.assertIs(task.future, pending)
        self.assertEqual(task.kwargs, self.args())
        self.assertIsNone(task.permit)
        self.assertEqual(task.target_lock_names, ())
        for bad in ({'permit': None}, {'permit': DeletePermit()}, {'target_lock_names': ()},
                    {'start_date': '2026-11-01'}, {'known_keys': KEY_A}, {'known_keys': [KEY_A, KEY_A]},
                    {'known_keys': ['masked...id']}, {'account_id': None}, {'account_id': ' padded '},
                    {'name': 'Synthetic A'}, {'tracking_id': KEY_A}):
            with self.subTest(bad=bad):
                rejected = service.submit('read_keys', **bad)
                self.assertIsInstance(rejected.exception(), GuardError)
                self.assertEqual(len(service._pending), 1)

    def test_result_accepts_only_public_identity_and_rechecks_account_continuity(self):
        value = self.result()
        safe = _identity_read_result(value, self.args())
        value['keys'][0]['name'] = 'Changed after return'
        self.assertEqual(safe['keys'], KEYS)
        bad_values = [
            {**self.result(), 'totals': {}}, {**self.result(), 'absent': True},
            {**self.result(), 'authorization': 'private-value'},
            {**self.result(), 'account_id': 'wrong-account'}, {'keys': [], 'account_id': 'stable-account'},
            {'keys': [KEYS[0], KEYS[0]], 'account_id': 'stable-account'},
            {'keys': [{**KEYS[0], 'sensitive_id': 'private-value'}], 'account_id': 'stable-account'},
            {'keys': [{'name': 'Synthetic', 'tracking_id': 'short-id'}], 'account_id': 'stable-account'},
            {'keys': [{'name': ' Synthetic ', 'tracking_id': KEY_A}], 'account_id': 'stable-account'},
        ]
        for bad in bad_values:
            with self.subTest(bad=bad), self.assertRaises(GuardError):
                _identity_read_result(bad, self.args())
        with self.assertRaises(AccountMismatch):
            _identity_read_result(self.result(), {'account_id': 'stable-account', 'known_keys': [KEY_OTHER]})
        fresh = {'keys': KEYS, 'account_id': key_fingerprint(KEYS)}
        self.assertEqual(_identity_read_result(fresh, {}), fresh)

    def test_unrelated_duplicate_names_preserve_the_full_identity_list(self):
        keys = deepcopy(KEYS) + [{'name': 'Synthetic B', 'tracking_id': KEY_OTHER}]
        value = {'keys': keys, 'account_id': 'stable-account'}
        self.assertEqual(_identity_read_result(value, self.args()), value)
        # Parent delivery must retain both unrelated rows. Only the eventual
        # selected name is required to be unique by the UI/ledger binding.
        service, future = self.pending_service()
        self.deliver(service, result=value)
        self.assertEqual(future.result(), value)
        fresh = {'keys': keys, 'account_id': key_fingerprint(keys)}
        self.assertEqual(_identity_read_result(fresh, {}), fresh)

    def test_child_rejects_extra_input_before_service_and_extra_output_before_ipc(self):
        for kwargs, value, calls, success in (
            (self.args(), self.result(), 1, True),
            ({**self.args(), 'permit': None}, self.result(), 0, False),
            ([], self.result(), 0, False),
            (self.args(), {**self.result(), 'totals': {}}, 1, False),
        ):
            with self.subTest(kwargs=kwargs, value=value):
                connection = Mock()
                connection.recv.side_effect = [
                    {'kind': 'start', 'session': 'synthetic-session'},
                    {'kind': 'action', 'session': 'synthetic-session', 'task': 1,
                     'action': 'read_keys', 'kwargs': kwargs}, EOFError(),
                ]
                connection.poll.return_value = True
                service = Mock()
                service.progress = {}
                future = Future(); future.set_result(value)
                service.submit.return_value = future
                with patch('quota_guard.browser_service.BrowserService', return_value=service), \
                        patch('quota_guard.browser_process.DeletePermit', side_effect=AssertionError('No grant')):
                    _browser_worker(connection)
                self.assertEqual(service.submit.call_count, calls)
                messages = [call.args[0] for call in connection.send.call_args_list]
                results = [message for message in messages if message['kind'] == 'result']
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0]['ok'], success)
                self.assertFalse(any(message['kind'] == 'permit_ready' for message in messages))
                if success:
                    self.assertEqual(results[0]['result'], self.result())
                else:
                    self.assertNotIn('result', results[0])

    def test_parent_only_accepts_current_matching_read_only_result(self):
        service, future = self.pending_service()
        self.deliver(service, session='stale-session')
        self.deliver(service, task=2)
        self.assertFalse(future.done())
        self.deliver(service)
        self.assertEqual(future.result(), self.result())
        for field, value in (('permit', DeletePermit()), ('permit_ready', True),
                             ('decision_sent', True), ('decision_allowed', True),
                             ('target_lock_names', ('unexpected-lock',))):
            with self.subTest(field=field):
                service, future = self.pending_service()
                setattr(service._active, field, value)
                self.deliver(service)
                self.assertIsInstance(future.exception(), GuardError)
        service, future = self.pending_service()
        self.deliver(service, result={**self.result(), 'collection_started_wall': 1001})
        self.assertIsInstance(future.exception(), GuardError)

    def test_late_identity_result_cannot_revive_expired_session(self):
        service, future = self.pending_service()
        self.deliver(service, monotonic=146, wall=1046)
        self.assertIsInstance(future.exception(), GuardError)
        service._kill_session.assert_called_once()

    def test_identity_failures_never_transfer_sensitive_text_or_submission_status(self):
        for exc in (AccountMismatch('private-value'), GuardError('private-value'),
                    RevocationNotSubmitted(), RuntimeError('private-value')):
            with self.subTest(exc=exc):
                result = _worker_failure(exc, 'read_keys', None)
                self.assertNotIn('private-value', str(result))
                self.assertNotEqual(result['error_type'], 'RevocationNotSubmitted')
                service, future = self.pending_service()
                self.deliver(service, ok=False, error_type=result['error_type'], message='private-value')
                self.assertNotIn('private-value', str(future.exception()))
                self.assertEqual(isinstance(future.exception(), AccountMismatch), isinstance(exc, AccountMismatch))
