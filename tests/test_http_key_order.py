"""Synthetic list ordering must never turn a roster position into a target ID."""
from itertools import permutations
import unittest

import test_platform_keys as fixtures
from quota_guard.platform_keys import KeyReadError


class HttpKeyOrderTests(unittest.TestCase):
    setUp = fixtures.PlatformKeyTests.setUp
    replace_list = fixtures.PlatformKeyTests.replace_list

    def test_normal_delete_fields_follow_bound_id_for_independent_http_and_page_orders(self):
        rows = self.fixture['list_response']['data']['biz_data']['api_keys']
        # Synthetic identities. Include a second ordinary Key with the same name.
        for row, name in zip(rows, ('target', 'other', 'target')):
            row.update(name=name, key_type='NORMAL')
        target_row = dict(rows[2])
        rows.append({'key_type': 'DSH', 'tracking_id': '00000000-0000-0000-0000-000000000004',
                     'sensitive_id': '', 'name': 'synthetic-app-managed'})
        self.fixture['list_response']['data']['biz_data']['key_quota'] = {'total': {'used': 3, 'limit': 300}}
        page_entries = [{'name': row['name'], 'tracking_id': row['tracking_id']} for row in rows]
        page_entries[-1]['name'] = 'DeepSeek HarnessApp-managed'
        for http_order in permutations(tuple(rows)):
            rows[:] = http_order
            self.replace_list()
            for page_order in permutations(page_entries):
                with self.subTest(http_ids=[r['tracking_id'] for r in rows],
                                  page_ids=[r['tracking_id'] for r in page_order]):
                    self.manager.clear()
                    self.manager._observe(self.request)
                    self.api.post.reset_mock()
                    target = self.manager.read_target('target', target_row['tracking_id'], page_order)
                    self.manager.submit_once(target, before_send=lambda: None)
                    self.api.post.assert_called_once()
                    self.assertEqual(self.api.post.call_args.kwargs['data'], {
                        'action': 'delete', 'name': None,
                        'tracking_id': target_row['tracking_id'],
                        'redacted_key': target_row['sensitive_id'],
                        'created_at': target_row['created_at'],
                    })

    def test_empty_field_on_unselected_user_does_not_block_c_and_selected_a_is_rejected(self):
        rows = self.fixture['list_response']['data']['biz_data']['api_keys']
        for row, name in zip(rows, ('a', 'b', 'c')):
            row['name'] = name
        self.expected = [{'name': row['name'], 'tracking_id': row['tracking_id']}
                         for row in reversed(rows)]
        target = self.expected[0]
        rows[0]['sensitive_id'] = ''
        self.replace_list()
        prepared = self.manager.read_target('c', target['tracking_id'], self.expected)
        self.assertEqual(prepared.tracking_id, target['tracking_id'])
        with self.assertRaises(KeyReadError) as caught:
            self.manager.read_target('a', rows[0]['tracking_id'], self.expected)
        self.assertEqual(caught.exception.reason_code, 'KEY_REDACTED_ID_INVALID')
        event, fields = self.events[-1]
        self.assertEqual(event, 'key_http_read_rejected')
        self.assertEqual(fields['http_row_ordinal'], 1)
        self.assertTrue(fields['is_selected_target'])
        self.assertEqual(fields['value_length'], 0)
        self.assertIsNone(self.manager._issued)
        self.api.post.assert_not_called()
