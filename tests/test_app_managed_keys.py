"""Offline DSH/NORMAL contract regressions; every HTTP request is a fixture mock."""
import copy
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import test_platform_keys as fixtures
from quota_guard.core import GuardError
from quota_guard.diagnostics import safe_fields
from quota_guard.platform_keys import DeleteTarget, KeyReadError


class AppManagedKeyTests(unittest.TestCase):
    def setUp(self):
        # Reuse fixture construction without importing/inheriting its TestCase:
        # unittest must discover only this module's new regression cases.
        fixtures.PlatformKeyTests.setUp(self)
        self.rows = self.fixture['list_response']['data']['biz_data']['api_keys']
        self.rows.insert(0, {
            'key_type': 'DSH',
            'tracking_id': '00000000-0000-0000-0000-000000000004',
            'name': 'raw-app-owned-name',
            'sensitive_id': '',
        })
        self.expected.insert(0, {
            'name': 'DeepSeek HarnessApp-managed',
            'tracking_id': self.rows[0]['tracking_id'],
        })
        self.replace_list()

    def replace_list(self, payload=None, **kwargs):
        fixtures.PlatformKeyTests.replace_list(self, payload, **kwargs)

    def target(self, expected=None):
        user = self.expected[1]
        return self.manager.read_target(user['name'], user['tracking_id'],
            self.expected if expected is None else expected)

    def assert_read_error(self, reason, action):
        with self.assertRaises(KeyReadError) as caught:
            action()
        self.assertEqual(caught.exception.reason_code, reason)
        return caught.exception

    def test_first_empty_dsh_row_allows_exact_single_user_submission(self):
        self.assertEqual(len(self.rows), 4)
        self.assertEqual(self.rows[0]['sensitive_id'], '')
        self.assertNotIn('created_at', self.rows[0])
        target = self.target()
        self.assertIsInstance(target, DeleteTarget)
        self.assertEqual(target.tracking_id, self.rows[1]['tracking_id'])
        before_send = Mock()
        self.assertEqual(self.manager.submit_once(target, before_send=before_send),
                         {'submitted': True})
        before_send.assert_called_once_with()
        self.api.post.assert_called_once()
        self.assertEqual(self.api.post.call_args.args, (self.fixture['delete_url'],))
        self.assertEqual(self.api.post.call_args.kwargs['data'], self.fixture['delete_body'])
        with self.assertRaises(GuardError):
            self.manager.submit_once(target, before_send=before_send)
        self.api.post.assert_called_once()
        self.assertTrue(self.list_response.disposed)

    def test_selecting_dsh_is_explicitly_not_deletable_and_never_posts(self):
        app = self.expected[0]
        self.assert_read_error('KEY_TARGET_NOT_DELETABLE', lambda:
            self.manager.read_target(app['name'], app['tracking_id'], self.expected))
        self.api.post.assert_not_called()

    def test_normal_user_named_deepseek_harness_remains_deletable(self):
        self.rows[1]['name'] = self.expected[1]['name'] = 'DeepSeek Harness'
        self.replace_list()
        target = self.target()
        self.assertIsInstance(target, DeleteTarget)
        self.manager.submit_once(target, before_send=lambda: None)
        self.api.post.assert_called_once()
        self.assertEqual(self.api.post.call_args.kwargs['data'], self.fixture['delete_body'])

    def test_official_whole_dsh_display_names_are_preserved_after_verification(self):
        for display in ('DeepSeek Harness', 'DeepSeek HarnessApp-managed',
                        'DeepSeek Harness App-managed', 'DeepSeek Harness   App-managed'):
            with self.subTest(display=display):
                self.expected[0]['name'] = display
                self.target()
                self.assertEqual(self.manager.read_keys(), self.expected)
        self.api.post.assert_not_called()

    def test_dsh_display_control_characters_stay_rejected_before_http(self):
        for separator in ('\t', '\n', '\r', '\x00'):
            with self.subTest(separator=repr(separator)):
                self.expected[0]['name'] = 'DeepSeek Harness' + separator + 'App-managed'
                self.assert_read_error('KEY_NAME_INVALID', self.target)
        self.api.get.assert_not_called()
        self.api.post.assert_not_called()

    def test_dsh_display_name_does_not_allow_prefix_suffix_or_other_labels(self):
        for display in ('DeepSeek Harness clone', 'DeepSeek HarnessApp-managed extra',
                        'Other DeepSeek Harness', 'Harness', 'DeepSeek HarnessApp-managedApp-managed'):
            with self.subTest(display=display):
                self.expected[0]['name'] = display
                self.assert_read_error('KEY_LIST_IDENTITY_MISMATCH', self.target)
        self.api.post.assert_not_called()

    def test_unverified_dsh_read_uses_official_name_not_raw_http_name(self):
        expected = copy.deepcopy(self.expected)
        expected[0]['name'] = 'DeepSeek Harness'
        self.assertEqual(self.manager.read_keys(), expected)
        self.api.post.assert_not_called()

    def test_dsh_does_not_require_user_name_or_delete_metadata(self):
        for missing in (('name',), ('sensitive_id',), ('name', 'sensitive_id')):
            with self.subTest(missing=missing):
                original = copy.deepcopy(self.rows[0])
                for field in missing:
                    self.rows[0].pop(field, None)
                self.replace_list()
                self.assertIsInstance(self.target(), DeleteTarget)
                self.rows[0] = original
        self.api.post.assert_not_called()

    def test_unknown_kind_cannot_borrow_dsh_display_mapping(self):
        for kind in ('UNKNOWN', 'dsh', 'DSH ', '', None, 1, True, []):
            with self.subTest(kind=kind):
                self.rows[0]['key_type'] = kind
                self.replace_list()
                self.assert_read_error('KEY_LIST_IDENTITY_MISMATCH', self.target)
        self.rows[0].pop('key_type')
        self.replace_list()
        self.assert_read_error('KEY_LIST_IDENTITY_MISMATCH', self.target)
        self.api.post.assert_not_called()

    def test_user_empty_or_missing_redacted_secret_is_never_treated_as_dsh(self):
        self.rows[1]['name'] = self.expected[1]['name'] = 'DeepSeek Harness'
        for value in ('', None, 'sk-this-is-a-complete-unmasked-secret'):
            with self.subTest(value=value):
                self.rows[1]['sensitive_id'] = value
                self.replace_list()
                self.assert_read_error('KEY_REDACTED_ID_INVALID', self.target)
        self.rows[1].pop('sensitive_id')
        self.replace_list()
        self.assert_read_error('KEY_REDACTED_ID_INVALID', self.target)
        self.api.post.assert_not_called()

    def test_user_creation_metadata_stays_strict_in_mixed_list(self):
        for value in (None, '', True, 0, -1, 1.5):
            with self.subTest(value=value):
                self.rows[1]['created_at'] = value
                self.replace_list()
                self.assert_read_error('KEY_CREATED_AT_INVALID', self.target)
        self.api.post.assert_not_called()

    def test_user_empty_secret_diagnostic_retains_kind_and_structure_only(self):
        self.rows[1]['sensitive_id'] = ''
        self.rows[1]['unused_private'] = 'fixture-private-never-log'
        self.replace_list()
        self.assert_read_error('KEY_REDACTED_ID_INVALID', self.target)
        event, data = self.events[-1]
        self.assertEqual(event, 'key_http_read_rejected')
        self.assertEqual(data['key_kind'], 'NORMAL')
        self.assertEqual(data['key_field'], 'sensitive_id')
        self.assertEqual(data['count'], 2)
        self.assertEqual(data['value_type'], 'str')
        self.assertEqual(data['value_length'], 0)
        self.assertTrue(data['field_present'])
        self.assertEqual(safe_fields(data)['key_kind'], 'NORMAL')
        serialized = json.dumps(self.events)
        self.assertNotIn('fixture-private-never-log', serialized)
        self.assertNotIn(self.rows[1]['tracking_id'], serialized)
        self.assertNotIn(self.rows[1]['name'], serialized)
        self.api.post.assert_not_called()

    def test_unknown_kind_diagnostics_do_not_echo_arbitrary_json_values(self):
        marker = 'fixture-private-kind-never-log'
        values = ((marker, 'str'), ([marker], 'list'),
                  ({'private': marker}, 'dict'), (None, 'none'),
                  (True, 'bool'), (91347, 'int'), (1.25, 'float'))
        for value, value_type in values:
            with self.subTest(value_type=value_type):
                self.events.clear()
                self.rows[1]['key_type'] = value
                self.replace_list()
                error = self.assert_read_error('KEY_TARGET_KIND_UNSUPPORTED', self.target)
                event, data = self.events[-1]
                self.assertEqual(event, 'key_http_read_rejected')
                self.assertEqual(data['key_kind'], 'unknown')
                self.assertEqual(data['key_field'], 'key_type')
                self.assertEqual(data['value_type'], value_type)
                self.assertEqual(data['count'], 2)
                self.assertTrue(data['field_present'])
                self.assertEqual(safe_fields(data)['key_kind'], 'unknown')
                self.assertNotIn(marker, json.dumps(self.events))
                self.assertNotIn(marker, str(error))
        self.rows[1].pop('key_type')
        self.replace_list()
        self.assert_read_error('KEY_TARGET_KIND_UNSUPPORTED', self.target)
        data = self.events[-1][1]
        self.assertEqual(data['key_kind'], 'unknown')
        self.assertFalse(data['field_present'])
        self.api.post.assert_not_called()

    def test_diagnostic_kind_whitelist_drops_untrusted_values(self):
        for kind in ('NORMAL', 'DSH', 'unknown', 'USER'):
            with self.subTest(accepted=kind):
                self.assertEqual(safe_fields({'key_kind': kind}), {'key_kind': kind})
        for value in ('private-kind', 'USER ', 'dsh', None, True, 1, 1.5,
                      ['USER'], {'kind': 'DSH'}):
            with self.subTest(rejected=repr(value)):
                self.assertNotIn('key_kind', safe_fields({'key_kind': value}))

    def test_every_dsh_requires_a_full_valid_uuid(self):
        for tracking_id in ('', None, '00000000-0000...', 'not-a-uuid'):
            with self.subTest(tracking_id=tracking_id):
                self.rows[0]['tracking_id'] = tracking_id
                self.replace_list()
                self.assert_read_error('KEY_TRACKING_ID_INVALID', self.target)
        self.api.post.assert_not_called()

    def test_duplicate_uuid_across_dsh_and_user_is_rejected(self):
        self.rows[0]['tracking_id'] = self.rows[1]['tracking_id']
        self.replace_list()
        self.assert_read_error('KEY_LIST_DUPLICATE_IDENTITY', self.target)
        self.api.post.assert_not_called()

    def test_duplicate_user_redacted_secret_stays_rejected(self):
        self.rows[2]['sensitive_id'] = self.rows[1]['sensitive_id']
        self.replace_list()
        self.assert_read_error('KEY_LIST_DUPLICATE_IDENTITY', self.target)
        self.api.post.assert_not_called()

    def test_missing_extra_or_changed_user_in_expected_full_list_is_rejected(self):
        changed = copy.deepcopy(self.expected)
        changed[3]['name'] = 'Changed ordinary user'
        extra = self.expected + [{'name': 'Extra ordinary user',
            'tracking_id': '00000000-0000-0000-0000-000000000005'}]
        for expected in (self.expected[1:], self.expected[:-1], extra, changed):
            with self.subTest(expected=expected):
                self.assert_read_error('KEY_LIST_IDENTITY_MISMATCH',
                    lambda: self.target(expected))
        self.api.post.assert_not_called()

    def test_dsh_same_name_with_different_uuid_cannot_match_dom(self):
        self.rows[0]['tracking_id'] = '00000000-0000-0000-0000-000000000005'
        self.replace_list()
        self.assert_read_error('KEY_LIST_IDENTITY_MISMATCH', self.target)
        self.api.post.assert_not_called()

    def test_successful_post_followup_keeps_dsh_display_and_other_survivors(self):
        target = self.target()
        self.manager.submit_once(target, before_send=lambda: None)
        del self.rows[1]
        self.replace_list()
        self.assertEqual(self.manager.read_keys(), [self.expected[0], *self.expected[2:]])
        self.api.post.assert_called_once()

    def test_clear_discards_verified_app_display_mapping(self):
        self.target()
        self.manager.clear()
        self.manager._observe(self.request)
        self.assertEqual(self.manager.read_keys()[0]['name'], 'DeepSeek Harness')
        self.api.post.assert_not_called()

    def test_new_capture_discards_verified_app_display_mapping(self):
        self.target()
        self.context.on = Mock()
        self.context.remove_listener = Mock()
        page = SimpleNamespace(context=self.context)
        with self.manager.capture(page):
            self.manager._observe(self.request)
            self.assertEqual(self.manager.read_keys()[0]['name'], 'DeepSeek Harness')
        self.context.remove_listener.assert_called_once()
        self.api.post.assert_not_called()

    def test_failed_reprepare_invalidates_prior_permit_and_app_display(self):
        old_target = self.target()
        changed = copy.deepcopy(self.expected)
        changed[2]['name'] = 'Mismatch'
        self.assert_read_error('KEY_LIST_IDENTITY_MISMATCH', lambda: self.target(changed))
        with self.assertRaises(GuardError):
            self.manager.submit_once(old_target, before_send=lambda: None)
        self.assertEqual(self.manager.read_keys()[0]['name'], 'DeepSeek Harness')
        self.api.post.assert_not_called()

    def test_failed_followup_read_does_not_retain_app_display_mapping(self):
        self.target()
        self.rows[2]['tracking_id'] = 'invalid-id'
        self.replace_list()
        self.assert_read_error('KEY_TRACKING_ID_INVALID', self.manager.read_keys)
        self.rows[2]['tracking_id'] = self.expected[2]['tracking_id']
        self.replace_list()
        self.assertEqual(self.manager.read_keys()[0]['name'], 'DeepSeek Harness')
        self.api.post.assert_not_called()

    def test_post_followup_rejects_unverified_new_app_managed_identity(self):
        target = self.target()
        self.manager.submit_once(target, before_send=lambda: None)
        del self.rows[1]
        self.rows.append({'key_type': 'DSH', 'sensitive_id': '',
            'tracking_id': '00000000-0000-0000-0000-000000000005'})
        self.replace_list()
        self.assert_read_error('KEY_LIST_IDENTITY_MISMATCH', self.manager.read_keys)
        self.api.post.assert_called_once()

    def test_post_followup_rejects_user_to_dsh_type_change(self):
        target = self.target()
        self.manager.submit_once(target, before_send=lambda: None)
        del self.rows[1]
        self.rows[1]['key_type'] = 'DSH'
        self.rows[1]['sensitive_id'] = ''
        self.replace_list()
        self.assert_read_error('KEY_LIST_IDENTITY_MISMATCH', self.manager.read_keys)
        self.api.post.assert_called_once()

    def test_post_followup_rejects_dsh_to_user_type_change(self):
        target = self.target()
        self.manager.submit_once(target, before_send=lambda: None)
        del self.rows[1]
        self.rows[0].update(key_type='NORMAL', name='DeepSeek Harness',
                            sensitive_id='sk-delta*****0004', created_at=1793203204)
        self.replace_list()
        self.assert_read_error('KEY_LIST_IDENTITY_MISMATCH', self.manager.read_keys)
        self.api.post.assert_called_once()


if __name__ == '__main__':
    unittest.main()
