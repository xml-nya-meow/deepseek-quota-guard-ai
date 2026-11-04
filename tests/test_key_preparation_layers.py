"""Layered identity/target behavior. Payloads are synthetic, never live proof."""
import copy
import json
import unittest
from unittest.mock import Mock

import test_platform_keys as fixtures
from quota_guard.core import GuardError
from quota_guard.diagnostics import safe_fields
from quota_guard.platform_keys import KeyReadError


class KeyPreparationLayerTests(unittest.TestCase):
    setUp = fixtures.PlatformKeyTests.setUp
    target = fixtures.PlatformKeyTests.target
    replace_list = fixtures.PlatformKeyTests.replace_list

    def reject(self, reason, action):
        with self.assertRaises(KeyReadError) as caught:
            action()
        self.assertEqual(caught.exception.reason_code, reason)
        self.assertIsNone(self.manager._issued)

    def test_unknown_bystander_identity_survives_but_swapping_it_into_target_refuses(self):
        rows = self.fixture['list_response']['data']['biz_data']['api_keys']
        rows[1].update(key_type='CASE06', sensitive_id='', created_at=None)
        self.replace_list()
        prepared = self.target()
        self.assertEqual(prepared.tracking_id, self.expected[0]['tracking_id'])
        other = self.expected[1]
        self.reject('KEY_TARGET_KIND_UNSUPPORTED', lambda: self.manager.read_target(
            other['name'], other['tracking_id'], self.expected))
        with self.assertRaises(GuardError):
            self.manager.submit_once(prepared, before_send=lambda: None)
        self.api.post.assert_not_called()

    def test_unknown_target_even_with_complete_parameters_never_gets_a_permit(self):
        rows = self.fixture['list_response']['data']['biz_data']['api_keys']
        for value in ('CASE06', 'USER', 'user', 'normal', 'NORMAL ', '', None, True, 1, [], {}, ['NORMAL']):
            with self.subTest(kind=repr(value)):
                rows[0]['key_type'] = value
                self.replace_list()
                self.reject('KEY_TARGET_KIND_UNSUPPORTED', self.target)
        rows[0].pop('key_type')
        self.replace_list()
        self.reject('KEY_TARGET_KIND_UNSUPPORTED', self.target)
        self.api.post.assert_not_called()

    def test_unknown_and_missing_bystander_types_do_not_fabricate_capability(self):
        rows = self.fixture['list_response']['data']['biz_data']['api_keys']
        for value in ('CASE06', 'USER', 'DSH ', None, True, [], {}, 5.2):
            with self.subTest(kind=repr(value)):
                rows[1]['key_type'] = value
                rows[1]['sensitive_id'] = None
                rows[1]['created_at'] = {'untrusted': 'private'}
                self.replace_list()
                self.assertEqual(self.target().tracking_id, self.expected[0]['tracking_id'])
                self.assertEqual(self.manager.read_keys(), self.expected)
                self.assertIsNone(self.manager._issued)
        rows[1].pop('key_type')
        self.replace_list()
        self.assertEqual(self.target().tracking_id, self.expected[0]['tracking_id'])
        self.api.post.assert_not_called()

    def test_non_target_ordinary_action_fields_are_not_required(self):
        rows = self.fixture['list_response']['data']['biz_data']['api_keys']
        for field in ('sensitive_id', 'created_at'):
            original = copy.deepcopy(rows[1])
            for value in ('', None, False, {}, [], -1):
                with self.subTest(field=field, value=repr(value)):
                    rows[1][field] = value
                    self.replace_list()
                    self.assertEqual(self.target().tracking_id, self.expected[0]['tracking_id'])
                    selected = self.expected[1]
                    self.reject('KEY_REDACTED_ID_INVALID' if field == 'sensitive_id' else 'KEY_CREATED_AT_INVALID',
                        lambda: self.manager.read_target(selected['name'], selected['tracking_id'], self.expected))
            rows[1] = original
        self.api.post.assert_not_called()

    def test_unknown_items_still_require_every_identity_and_full_set(self):
        for changed in ('name', 'id', 'duplicate', 'extra', 'missing'):
            payload = copy.deepcopy(self.fixture['list_response'])
            rows = payload['data']['biz_data']['api_keys']
            rows[1]['key_type'] = 'CASE06'
            if changed == 'name': rows[1]['name'] = 'wrong displayed identity'
            elif changed == 'id': rows[1]['tracking_id'] = 'not-a-full-id'
            elif changed == 'duplicate': rows[1]['tracking_id'] = rows[0]['tracking_id']
            elif changed == 'extra': rows.append(dict(rows[1], tracking_id='00000000-0000-0000-0000-000000000009'))
            else: rows.pop(1)
            self.replace_list(payload)
            with self.subTest(changed=changed), self.assertRaises(KeyReadError):
                self.target()
            self.assertIsNone(self.manager._issued)
        self.api.post.assert_not_called()

    def test_post_submit_unknown_survivor_metadata_does_not_hide_absence_or_allow_retry(self):
        target = self.target()
        self.manager.submit_once(target, before_send=lambda: None)
        rows = self.fixture['list_response']['data']['biz_data']['api_keys']
        rows.pop(0)
        rows[0].update(key_type='CASE06', sensitive_id='', created_at=None)
        rows[1].pop('sensitive_id')
        self.replace_list()
        self.assertEqual(self.manager.read_keys(), self.expected[1:])
        self.assertIsNone(self.manager._issued)
        with self.assertRaises(GuardError):
            self.manager.submit_once(target, before_send=lambda: None)
        self.api.post.assert_called_once()

    def test_identity_read_does_not_require_distinct_unused_redacted_fields(self):
        rows = self.fixture['list_response']['data']['biz_data']['api_keys']
        rows[2]['sensitive_id'] = rows[1]['sensitive_id']
        self.replace_list()
        self.assertEqual(self.manager.read_keys(), self.expected)
        self.assertEqual(self.target().tracking_id, rows[0]['tracking_id'])
        other = self.expected[1]
        self.reject('KEY_LIST_DUPLICATE_IDENTITY', lambda: self.manager.read_target(
            other['name'], other['tracking_id'], self.expected))

    def test_shape_report_joins_full_ids_and_does_not_leak_raw_values(self):
        rows = self.fixture['list_response']['data']['biz_data']['api_keys']
        rows[1].update(key_type='private-kind-marker', sensitive_id='private-secret-marker', created_at='private-time-marker')
        rows.reverse()
        self.replace_list()
        self.target()
        shapes = [data for event, data in self.events if event == 'key_http_entry_shape']
        selected = [data for data in shapes if data['is_selected_target']]
        bystander = [data for data in shapes if not data['is_selected_target']]
        self.assertEqual(selected[0]['http_row_ordinal'], 3)
        self.assertEqual(bystander[0]['http_row_ordinal'], 2)
        self.assertTrue(all(data['dom_identity_matched'] for data in shapes))
        self.assertFalse(bystander[0]['redacted_format_valid'])
        self.assertFalse(bystander[0]['created_at_valid'])
        for data in shapes:
            safe = safe_fields(data)
            self.assertEqual(safe['http_row_ordinal'], data['http_row_ordinal'])
            self.assertEqual(safe['is_selected_target'], data['is_selected_target'])
        serialized = json.dumps(self.events)
        for value in ('private-kind-marker', 'private-secret-marker', 'private-time-marker',
                      rows[1]['tracking_id'], rows[1]['name']):
            self.assertNotIn(value, serialized)

    def test_shape_diagnostics_are_bounded_and_always_include_selected_target(self):
        rows = self.fixture['list_response']['data']['biz_data']['api_keys']
        rows[:] = [dict(rows[0], key_type='CASE06', name=f'Fixture {i}',
                  tracking_id=f'00000000-0000-0000-0000-{i:012d}') for i in range(1, 51)]
        self.expected = [{'name': r['name'], 'tracking_id': r['tracking_id']} for r in rows]
        self.replace_list()
        last = self.expected[-1]
        self.reject('KEY_TARGET_KIND_UNSUPPORTED', lambda: self.manager.read_target(last['name'], last['tracking_id'], self.expected))
        shapes = [data for event, data in self.events if event == 'key_http_entry_shape']
        self.assertEqual(len(shapes), 21)
        self.assertEqual(shapes[-1]['http_row_ordinal'], 50)
        self.assertTrue(shapes[-1]['is_selected_target'])
        self.api.post.assert_not_called()

    def test_diagnostic_failure_cannot_issue_target(self):
        self.manager._emit = Mock(side_effect=OSError('private IO failure'))
        with self.assertRaises(GuardError): self.target()
        self.assertIsNone(self.manager._issued)
        self.api.post.assert_not_called()
