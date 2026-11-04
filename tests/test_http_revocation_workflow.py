"""Automatic HTTP orchestration, using in-memory fakes only."""
from contextlib import contextmanager
import unittest
from unittest.mock import Mock, patch

from quota_guard.browser_service import BrowserService, key_fingerprint
from quota_guard.core import GuardError
from quota_guard.revocation_permit import DeletePermit, KeyInspectionError, RevocationNotSubmitted

KEYS = [
    {'name': 'Same name', 'tracking_id': '00000000-0000-0000-0000-000000000001'},
    {'name': 'Same name', 'tracking_id': '00000000-0000-0000-0000-000000000002'},
]
TARGET = KEYS[0]['tracking_id']


class HttpRevocationWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.service = object.__new__(BrowserService)
        self.service.context = object()
        self.service.page = object()
        self.service.anchor = key_fingerprint(KEYS)
        self.service.known_keys = {key['tracking_id'] for key in KEYS}
        self.current = list(KEYS)
        self.service._keys = Mock(side_effect=lambda: list(self.current))
        self.service._revoke_dom_legacy = Mock(side_effect=AssertionError('No DOM fallback allowed'))
        self.manager = Mock()
        self.capturing = False
        @contextmanager
        def capture(page):
            self.assertIs(page, self.service.page)
            self.capturing = True
            try:
                yield
            finally:
                self.capturing = False
        self.manager.capture.side_effect = capture
        self.prepared = object()
        self.manager.read_target.return_value = self.prepared
        self.manager.read_keys.side_effect = lambda: list(self.current)
        self.dispatches = 0
        def submit(target, *, before_send):
            self.assertIs(target, self.prepared)
            before_send()
            self.dispatches += 1
            self.current = [KEYS[1]]
            return {'submitted': True}
        self.manager.submit_once.side_effect = submit
        self.factory = patch('quota_guard.platform_keys.ConsoleKeyManager', return_value=self.manager)
        self.factory.start()
        self.addCleanup(self.factory.stop)

    def permit(self, allowed=True):
        permit = DeletePermit()
        permit.decide(allowed)
        return permit

    def test_http_is_only_automatic_channel_and_requires_independent_absence(self):
        result = self.service._revoke('Same name', TARGET, self.permit())
        self.assertEqual(result, {'absent': True, 'present': False})
        self.assertEqual(self.dispatches, 1)
        self.manager.read_target.assert_called_once_with('Same name', TARGET, KEYS)
        self.manager.clear.assert_called_once()
        self.service._revoke_dom_legacy.assert_not_called()

    def test_first_required_list_navigation_is_inside_authorization_capture(self):
        observations = []
        def keys():
            observations.append(self.capturing)
            return list(self.current)
        self.service._keys.side_effect = keys
        self.service._revoke('Same name', TARGET, self.permit())
        self.assertEqual(observations, [True, True, False])
        self.assertFalse(self.capturing)

    def test_inspection_exercises_full_preparation_without_creating_permit_or_write(self):
        original_anchor, original_known = self.service.anchor, set(self.service.known_keys)
        observations = []
        def keys():
            observations.append(self.capturing)
            return list(self.current)
        self.service._keys.side_effect = keys
        with patch('quota_guard.browser_service.DeletePermit', side_effect=AssertionError('No inspection permit')):
            result = self.service._inspect_revoke('Same name', TARGET)
        self.assertEqual(result, {'preflight_passed': True, 'name': 'Same name',
                                  'tracking_id': TARGET, 'key_count': 2})
        self.assertEqual(observations, [True, True])
        self.manager.prepare.assert_called_once_with(self.service.page)
        self.manager.read_target.assert_called_once_with('Same name', TARGET, KEYS)
        self.manager.read_keys.assert_not_called()
        self.manager.submit_once.assert_not_called()
        self.manager.clear.assert_called_once()
        self.assertEqual(self.dispatches, 0)
        self.assertEqual(self.service.anchor, original_anchor)
        self.assertEqual(self.service.known_keys, original_known)
        self.service._revoke_dom_legacy.assert_not_called()

    def test_inspection_failure_at_each_read_stage_is_fixed_and_never_enters_submission(self):
        cases = (
            ('first_list', 'KEY_LIST_READ_FAILED'),
            ('authorization', 'KEY_AUTH_HEADER_MISSING'),
            ('target', 'KEY_TARGET_READ_FAILED'),
            ('account_recheck', 'KEY_ACCOUNT_RECHECK_FAILED'),
        )
        for phase, code in cases:
            with self.subTest(phase=phase):
                self.service._keys.side_effect = lambda: list(self.current)
                self.manager.prepare.side_effect = None
                self.manager.read_target.side_effect = None
                self.manager.clear.reset_mock()
                failure = GuardError('private SDK header or page value')
                if phase == 'first_list':
                    self.service._keys.side_effect = failure
                elif phase == 'authorization':
                    failure.reason_code = code
                    self.manager.prepare.side_effect = failure
                elif phase == 'target':
                    self.manager.read_target.side_effect = failure
                else:
                    self.service._keys.side_effect = [KEYS, failure]
                with self.assertRaises(KeyInspectionError) as caught:
                    self.service._inspect_revoke('Same name', TARGET)
                self.assertEqual(caught.exception.reason_code, code)
                self.assertNotIn('private SDK', str(caught.exception))
                self.assertFalse(self.capturing)
                self.manager.clear.assert_called_once()
                self.manager.submit_once.assert_not_called()
                self.assertEqual(self.service.anchor, key_fingerprint(KEYS))
                self.assertEqual(self.service.known_keys, {key['tracking_id'] for key in KEYS})

    def test_inspection_rejects_changed_account_or_last_key_without_relaxing_real_rules(self):
        for current, anchor, code in (([KEYS[1]], key_fingerprint(KEYS), 'KEY_ACCOUNT_CHANGED'),
                                      ([KEYS[0]], key_fingerprint([KEYS[0]]), 'KEY_LAST_TARGET')):
            with self.subTest(code=code):
                self.current, self.service.anchor = current, anchor
                with self.assertRaises(KeyInspectionError) as caught:
                    self.service._inspect_revoke('Same name', TARGET)
                self.assertEqual(caught.exception.reason_code, code)
        self.manager.prepare.assert_not_called()
        self.manager.read_target.assert_not_called()
        self.manager.submit_once.assert_not_called()

    def test_successful_inspection_cannot_be_reused_as_later_delete_authorization(self):
        self.service._inspect_revoke('Same name', TARGET)
        self.manager.prepare.side_effect = GuardError('Fresh preparation no longer succeeds')
        permit = DeletePermit()
        with self.assertRaises(RevocationNotSubmitted):
            self.service._revoke('Same name', TARGET, permit)
        self.assertEqual(self.manager.prepare.call_count, 2)
        self.assertEqual(self.manager.capture.call_count, 2)
        self.assertEqual(self.manager.clear.call_count, 2)
        self.assertFalse(permit.ready.is_set())
        self.manager.submit_once.assert_not_called()

    def test_shared_preparation_diagnostics_have_fixed_stage_timing_and_no_target_data(self):
        self.service._log = Mock()
        self.service._inspect_revoke('Same name', TARGET)
        completed = [call.kwargs for call in self.service._log.call_args_list
                     if call.args[0] == 'key_preparation_step' and call.kwargs.get('status') == 'passed']
        self.assertEqual([item['phase'] for item in completed],
                         ['key_list', 'authorization', 'target_http', 'account_recheck'])
        self.assertTrue(all(type(item['elapsed_ms']) is int and item['elapsed_ms'] >= 0 for item in completed))
        self.assertTrue(any(call.args[0] == 'key_preparation_completed' and call.kwargs['key_count'] == 2
                            for call in self.service._log.call_args_list))
        self.assertNotIn(TARGET, repr(self.service._log.call_args_list))
        self.assertNotIn('Same name', repr(self.service._log.call_args_list))

    def test_inspection_diagnostic_failure_returns_fixed_reason_and_clears_manager(self):
        self.service._log = Mock(side_effect=RuntimeError('private diagnostic failure'))
        with self.assertRaises(KeyInspectionError) as caught:
            self.service._inspect_revoke('Same name', TARGET)
        self.assertEqual(caught.exception.reason_code, 'KEY_DIAGNOSTIC_FAILED')
        self.assertNotIn('private diagnostic', str(caught.exception))
        self.manager.clear.assert_called_once()
        self.manager.submit_once.assert_not_called()

    def test_safe_preparation_reason_survives_without_exception_text(self):
        failure = GuardError('untrusted header value should never be forwarded')
        failure.reason_code = 'KEY_AUTH_HEADER_MISSING'
        self.manager.prepare.side_effect = failure
        permit = DeletePermit()
        with self.assertRaises(RevocationNotSubmitted) as raised:
            self.service._revoke('Same name', TARGET, permit)
        self.assertEqual(raised.exception.reason_code, 'KEY_AUTH_HEADER_MISSING')
        self.assertNotIn('untrusted header', str(raised.exception))
        self.assertFalse(self.capturing)
        self.assertFalse(permit.ready.is_set())
        self.manager.submit_once.assert_not_called()

    def test_preparation_error_is_definitely_not_submitted(self):
        self.manager.read_target.side_effect = GuardError('Synthetic contract mismatch')
        permit = DeletePermit()
        with self.assertRaises(RevocationNotSubmitted):
            self.service._revoke('Same name', TARGET, permit)
        self.assertFalse(permit.ready.is_set())
        self.manager.submit_once.assert_not_called()
        self.manager.clear.assert_called_once()
        self.service._revoke_dom_legacy.assert_not_called()

    def test_changed_account_between_mapping_and_permit_sends_nothing(self):
        self.service._keys.side_effect = [KEYS, [KEYS[1]]]
        permit = DeletePermit()
        with self.assertRaises(RevocationNotSubmitted):
            self.service._revoke('Same name', TARGET, permit)
        self.assertFalse(permit.ready.is_set())
        self.manager.submit_once.assert_not_called()

    def test_denied_permit_cancels_without_entering_write_adapter(self):
        result = self.service._revoke('Same name', TARGET, self.permit(False))
        self.assertEqual(result, {'cancelled': True})
        self.manager.submit_once.assert_not_called()
        self.manager.clear.assert_called_once()

    def test_expired_approved_permit_remains_uncertain_and_sends_nothing(self):
        permit = self.permit()
        with patch('quota_guard.revocation_permit._remaining', return_value=0):
            with self.assertRaises(GuardError) as raised:
                self.service._revoke('Same name', TARGET, permit)
        self.assertNotIsInstance(raised.exception, RevocationNotSubmitted)
        self.assertEqual(self.dispatches, 0)
        self.assertTrue(permit.ready.is_set())
        self.service._revoke_dom_legacy.assert_not_called()

    def test_server_response_does_not_replace_independent_verification(self):
        def submit(target, *, before_send):
            before_send()
            self.dispatches += 1
            return {'submitted': True}
        self.manager.submit_once.side_effect = submit
        with self.assertRaises(GuardError) as raised:
            self.service._revoke('Same name', TARGET, self.permit())
        self.assertNotIsInstance(raised.exception, RevocationNotSubmitted)
        self.assertEqual(self.dispatches, 1)
        self.service._revoke_dom_legacy.assert_not_called()

    def test_optimistic_dom_absence_cannot_override_fresh_http_presence(self):
        self.manager.read_keys.side_effect = lambda: list(KEYS)
        with self.assertRaisesRegex(GuardError, 'HTTP.*不一致') as raised:
            self.service._revoke('Same name', TARGET, self.permit())
        self.assertNotIsInstance(raised.exception, RevocationNotSubmitted)
        self.assertEqual(self.dispatches, 1)
        self.manager.submit_once.assert_called_once()
        self.service._revoke_dom_legacy.assert_not_called()

    def test_failed_post_submission_read_remains_unknown_without_retry(self):
        self.manager.read_keys.side_effect = GuardError('Synthetic read failure')
        with self.assertRaises(GuardError) as raised:
            self.service._revoke('Same name', TARGET, self.permit())
        self.assertNotIsInstance(raised.exception, RevocationNotSubmitted)
        self.assertEqual(self.dispatches, 1)
        self.manager.submit_once.assert_called_once()

    def test_lost_response_after_acceptance_is_not_retried(self):
        def submit(target, *, before_send):
            before_send()
            self.dispatches += 1
            self.current = [KEYS[1]]
            raise GuardError('Synthetic connection lost after acceptance')
        self.manager.submit_once.side_effect = submit
        with self.assertRaises(GuardError) as raised:
            self.service._revoke('Same name', TARGET, self.permit())
        self.assertNotIsInstance(raised.exception, RevocationNotSubmitted)
        self.assertEqual(self.dispatches, 1)
        self.manager.submit_once.assert_called_once()
        self.manager.clear.assert_called_once()
        self.service._revoke_dom_legacy.assert_not_called()

    def test_last_key_refuses_before_session_or_submission(self):
        self.current = [KEYS[0]]
        self.service.anchor = key_fingerprint(self.current)
        permit = DeletePermit()
        with self.assertRaises(RevocationNotSubmitted):
            self.service._revoke('Same name', TARGET, permit)
        self.manager.prepare.assert_not_called()
        self.assertFalse(permit.ready.is_set())

    def test_single_known_binding_presence_does_not_change_trust(self):
        self.service.known_keys = set()
        self.service.anchor = None
        result = self.service._verify_revoke('Same name', TARGET, {TARGET})
        self.assertEqual(result, {'absent': False, 'present': True})
        self.assertIsNone(self.service.anchor)
        self.assertEqual(self.service.known_keys, set())

    def test_positive_presence_requires_prior_identity_and_exact_name(self):
        for name, expected, current in (
            ('Changed name', {TARGET}, KEYS),
            ('Same name', {KEYS[1]['tracking_id']}, KEYS),
            ('Same name', {TARGET}, [KEYS[1]]),
        ):
            with self.subTest(name=name, expected=expected):
                self.current = current
                with self.assertRaises(GuardError):
                    self.service._verify_revoke(name, TARGET, expected)


if __name__ == '__main__':
    unittest.main()
