"""BrowserService + real HTTP parser; only transport and DOM are offline doubles."""
from contextlib import nullcontext
import unittest
from unittest.mock import Mock, patch

import test_platform_keys as fixtures
from quota_guard.browser_service import BrowserService, key_fingerprint
from quota_guard.core import GuardError
from quota_guard.revocation_permit import DeletePermit, KeyInspectionError, RevocationNotSubmitted


class LayeredRevocationFlowTests(unittest.TestCase):
    setUp = fixtures.PlatformKeyTests.setUp
    replace_list = fixtures.PlatformKeyTests.replace_list

    def service(self):
        service = object.__new__(BrowserService)
        service.context, service.page = self.context, object()
        service.anchor = key_fingerprint(self.expected)
        service.known_keys = {k['tracking_id'] for k in self.expected}
        self.current = list(self.expected)
        service._keys = Mock(side_effect=lambda: list(self.current))
        service._revoke_dom_legacy = Mock(side_effect=AssertionError('No DOM fallback'))
        self.manager.capture = Mock(side_effect=lambda page: nullcontext())
        self.manager.prepare = Mock()
        return service

    def test_readonly_preparation_passes_mixed_identity_without_post_or_permit(self):
        rows = self.fixture['list_response']['data']['biz_data']['api_keys']
        rows[1].update(key_type='CASE06', sensitive_id='', created_at=None)
        self.replace_list()
        service = self.service()
        with patch('quota_guard.platform_keys.ConsoleKeyManager', return_value=self.manager), \
                patch('quota_guard.browser_service.DeletePermit', side_effect=AssertionError('No permit')):
            result = service._inspect_revoke(**self.expected[0])
        self.assertTrue(result['preflight_passed'])
        self.assertEqual(result['key_count'], 3)
        self.assertIsNone(self.manager._issued)
        self.assertFalse(self.manager._headers)
        self.api.post.assert_not_called()

    def test_unknown_selected_item_is_not_submitted_even_with_granted_permit(self):
        rows = self.fixture['list_response']['data']['biz_data']['api_keys']
        rows[0]['key_type'] = 'CASE06'
        self.replace_list()
        service = self.service()
        permit = DeletePermit(); permit.decide(True)
        with patch('quota_guard.platform_keys.ConsoleKeyManager', return_value=self.manager):
            with self.assertRaises(RevocationNotSubmitted) as caught:
                service._revoke(**self.expected[0], permit=permit)
        self.assertEqual(caught.exception.reason_code, 'KEY_TARGET_KIND_UNSUPPORTED')
        self.assertFalse(permit.ready.is_set())
        self.api.post.assert_not_called()
        service._revoke_dom_legacy.assert_not_called()

    def test_actual_parser_post_followup_accepts_identity_of_unknown_empty_survivor(self):
        service = self.service()
        response = self.api.post.return_value
        def posted(*args, **kwargs):
            rows = self.fixture['list_response']['data']['biz_data']['api_keys']
            rows.pop(0)
            rows[0].update(key_type='CASE06', sensitive_id='', created_at=None)
            rows[1].pop('created_at')
            self.replace_list()
            self.current = self.expected[1:]
            return response
        self.api.post.side_effect = posted
        permit = DeletePermit(); permit.decide(True)
        with patch('quota_guard.platform_keys.ConsoleKeyManager', return_value=self.manager):
            result = service._revoke(**self.expected[0], permit=permit)
        self.assertEqual(result, {'absent': True, 'present': False})
        self.api.post.assert_called_once()
        self.assertEqual(self.api.get.call_count, 2)
        service._revoke_dom_legacy.assert_not_called()

    def test_post_submit_identity_difference_remains_unknown_not_not_submitted(self):
        service = self.service()
        response = self.api.post.return_value
        def posted(*args, **kwargs):
            rows = self.fixture['list_response']['data']['biz_data']['api_keys']
            rows.pop(0)
            rows[0].update(key_type='CASE06', sensitive_id='', name='Different HTTP identity')
            self.replace_list()
            self.current = self.expected[1:]
            return response
        self.api.post.side_effect = posted
        permit = DeletePermit(); permit.decide(True)
        with patch('quota_guard.platform_keys.ConsoleKeyManager', return_value=self.manager):
            with self.assertRaises(GuardError) as caught:
                service._revoke(**self.expected[0], permit=permit)
        self.assertNotIsInstance(caught.exception, RevocationNotSubmitted)
        self.api.post.assert_called_once()
        service._revoke_dom_legacy.assert_not_called()

    def mixed_console_list(self):
        """Four synthetic rows with the distinct HTTP and page orders from the capture."""
        data = self.fixture['list_response']['data']['biz_data']
        app = {'name': 'DeepSeek Harness', 'tracking_id': '00000000-0000-0000-0000-000000000004',
               'key_type': 'DSH', 'sensitive_id': '', 'created_at': 1700000000}
        a, b, target = data['api_keys']
        data['api_keys'][:] = [app, a, b, target]
        data['key_quota'] = {'total': {'used': 3, 'limit': 300}}
        self.expected = [{'name': item['name'], 'tracking_id': item['tracking_id']}
                         for item in (target, app, b, a)]
        self.expected[1]['name'] += 'App-managed'
        self.replace_list()
        return data, target, app

    def test_only_bound_normal_key_is_submitted_when_page_resorts_before_and_after_send(self):
        data, target, _ = self.mixed_console_list()
        selected = dict(self.expected[0])
        service = self.service()
        survivor_ids = {item['tracking_id'] for item in self.expected if item != selected}
        # First DOM, pre-send DOM recheck, and post-send DOM all differ in order.
        survivors = [item for item in reversed(self.expected) if item != selected]
        service._keys.side_effect = [self.expected, list(reversed(self.expected)), survivors]
        response = self.api.post.return_value

        def post(_url, **options):
            self.assertEqual(options['data'], {
                'action': 'delete', 'name': None, 'tracking_id': selected['tracking_id'],
                'redacted_key': target['sensitive_id'], 'created_at': target['created_at'],
            })
            data['api_keys'] = [r for r in reversed(data['api_keys']) if r['tracking_id'] != selected['tracking_id']]
            data['key_quota']['total']['used'] = 2
            self.replace_list()
            return response

        self.api.post.side_effect = post
        permit = DeletePermit()
        permit.decide(True)
        with patch('quota_guard.platform_keys.ConsoleKeyManager', return_value=self.manager):
            result = service._revoke(**selected, permit=permit)
        self.assertEqual(result, {'absent': True, 'present': False})
        self.assertEqual(service.known_keys, survivor_ids)
        self.api.post.assert_called_once()
        self.assertEqual(self.api.get.call_count, 2)
        service._revoke_dom_legacy.assert_not_called()

    def test_app_managed_key_refuses_inspection_and_action_even_with_complete_delete_fields(self):
        _, _, app = self.mixed_console_list()
        app.update(sensitive_id='sk-app*****0004', created_at=1700000000)
        self.replace_list()
        selected = self.expected[1]
        service = self.service()
        permit = DeletePermit()
        permit.decide(True)
        with patch('quota_guard.platform_keys.ConsoleKeyManager', return_value=self.manager):
            with self.assertRaises(KeyInspectionError) as inspect_failure:
                service._inspect_revoke(**selected)
            self.manager._observe(self.request)
            with self.assertRaises(RevocationNotSubmitted) as action_failure:
                service._revoke(**selected, permit=permit)
        self.assertEqual(inspect_failure.exception.reason_code, 'KEY_TARGET_NOT_DELETABLE')
        self.assertEqual(action_failure.exception.reason_code, 'KEY_TARGET_NOT_DELETABLE')
        self.assertFalse(permit.ready.is_set())
        self.api.post.assert_not_called()
