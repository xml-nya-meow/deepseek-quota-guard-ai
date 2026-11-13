"""Closed reason codes across adapter, preparation and IPC; synthetic payloads."""
import copy
import json
import unittest

import test_platform_keys as fixtures
import test_http_revocation_workflow as orchestration
from quota_guard.browser_process import _worker_failure
from quota_guard.core import GuardError
from quota_guard.diagnostics import safe_fields
from quota_guard.platform_keys import KeyReadError
from quota_guard.revocation_permit import (DeletePermit, KEY_READ_REASONS,
                                          KeyInspectionError, RevocationNotSubmitted)


class KeyReadDiagnosticTests(unittest.TestCase):
    setUp = fixtures.PlatformKeyTests.setUp
    target = fixtures.PlatformKeyTests.target
    replace_list = fixtures.PlatformKeyTests.replace_list

    def test_field_failures_report_structure_without_value_and_preserve_no_write(self):
        marker = 'private-fixture-marker'
        cases = [('name', '', 'KEY_NAME_INVALID'),
                 ('tracking_id', marker, 'KEY_TRACKING_ID_INVALID'),
                 ('sensitive_id', marker, 'KEY_REDACTED_ID_INVALID'),
                 ('created_at', marker, 'KEY_CREATED_AT_INVALID')]
        for field, value, code in cases:
            with self.subTest(field=field):
                payload = copy.deepcopy(self.fixture['list_response'])
                payload['data']['biz_data']['api_keys'][0][field] = value
                self.replace_list(payload)
                with self.assertRaises(KeyReadError) as caught:
                    self.target()
                self.assertEqual(caught.exception.reason_code, code)
                event, data = self.events[-1]
                self.assertEqual(event, 'key_http_read_rejected')
                self.assertEqual(data['key_field'], field)
                self.assertEqual(data['count'], 1)
                self.assertEqual(data['value_type'], 'str')
                self.assertTrue(data['field_present'])
                self.assertIsNone(self.manager._issued)
                self.assertTrue(self.list_response.disposed)
                self.assertNotIn(marker, json.dumps(self.events))
                self.assertNotIn(marker, str(caught.exception))
        self.api.post.assert_not_called()

    def test_missing_field_is_distinguished_from_null_without_logging_response(self):
        for present in (False, True):
            payload = copy.deepcopy(self.fixture['list_response'])
            row = payload['data']['biz_data']['api_keys'][0]
            if present:
                row['sensitive_id'] = None
            else:
                del row['sensitive_id']
            self.replace_list(payload)
            with self.assertRaises(KeyReadError):
                self.target()
            data = safe_fields(self.events[-1][1])
            self.assertEqual(data['field_present'], present)
            self.assertEqual(data['value_type'], 'none')
            self.assertEqual(data['key_field'], 'sensitive_id')

    def test_http_auth_type_json_and_business_failures_have_distinct_codes(self):
        rejected = copy.deepcopy(self.fixture['list_response'])
        rejected['data']['biz_code'] = 1
        cases = [({'status': 401}, None, 'KEY_HTTP_AUTH_REQUIRED'),
                 ({'status': 500}, None, 'KEY_HTTP_FAILED'),
                 ({'headers': {'content-type': 'text/html'}}, None, 'KEY_RESPONSE_TYPE_INVALID'),
                 ({'body': b'{"code":0,"code":0}'}, None, 'KEY_RESPONSE_JSON_INVALID'),
                 ({}, rejected, 'KEY_RESPONSE_BUSINESS_INVALID'),
                 ({'headers': {'content-type': 'application/json', 'content-length': '1000001'}},
                  None, 'KEY_RESPONSE_SIZE_INVALID')]
        for options, payload, code in cases:
            with self.subTest(code=code):
                self.manager._observe(self.request)
                self.replace_list(payload, **options)
                with self.assertRaises(KeyReadError) as caught:
                    self.target()
                self.assertEqual(caught.exception.reason_code, code)
                self.assertTrue(self.list_response.disposed)
                self.assertIsNone(self.manager._issued)
        self.api.post.assert_not_called()

    def test_changed_http_identity_is_a_list_mismatch_not_unknown_field_error(self):
        self.fixture['list_response']['data']['biz_data']['api_keys'][1]['name'] = 'changed synthetic'
        self.replace_list()
        with self.assertRaises(KeyReadError) as caught:
            self.target()
        self.assertEqual(caught.exception.reason_code, 'KEY_LIST_IDENTITY_MISMATCH')
        self.assertEqual(self.events[-1][1]['key_count'], 3)
        self.api.post.assert_not_called()

    def test_case_variant_duplicate_uuid_is_rejected_before_any_write(self):
        rows = self.fixture['list_response']['data']['biz_data']['api_keys']
        rows[0]['tracking_id'] = 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
        rows[1]['tracking_id'] = rows[0]['tracking_id'].upper()
        self.replace_list()
        with self.assertRaises(KeyReadError) as caught:
            self.manager.read_keys()
        self.assertEqual(caught.exception.reason_code, 'KEY_LIST_DUPLICATE_IDENTITY')
        self.api.post.assert_not_called()

    def test_diagnostic_enums_reject_injected_field_names_and_boolean_values(self):
        value = safe_fields({'key_field': 'arbitrary-private-field', 'field_present': 'private'})
        self.assertNotIn('key_field', value)
        self.assertNotIn('field_present', value)

    def test_new_codes_survive_inspection_and_pre_send_ipc_but_not_post_gate_classification(self):
        permit = DeletePermit()
        for code in KEY_READ_REASONS:
            with self.subTest(code=code):
                inspect = _worker_failure(KeyInspectionError(reason_code=code), 'inspect_revoke', None)
                self.assertEqual(inspect['reason_code'], code)
                failed = _worker_failure(RevocationNotSubmitted(reason_code=code), 'revoke', permit)
                self.assertEqual(failed['reason_code'], code)
        permit.ready.set()
        failed = _worker_failure(RevocationNotSubmitted(reason_code='KEY_HTTP_FAILED'), 'revoke', permit)
        self.assertEqual(failed['error_type'], 'GuardError')
        self.assertNotIn('reason_code', failed)


class KeyReadPreparationTests(unittest.TestCase):
    setUp = orchestration.HttpRevocationWorkflowTests.setUp
    browser_submit = orchestration.HttpRevocationWorkflowTests.browser_submit

    def test_precise_read_reason_reaches_read_only_preflight_without_permit_or_post(self):
        for code in KEY_READ_REASONS:
            with self.subTest(code=code):
                self.manager.read_target.side_effect = KeyReadError(code)
                with self.assertRaises(KeyInspectionError) as caught:
                    self.service._inspect_revoke('Same name', orchestration.TARGET)
                self.assertEqual(caught.exception.reason_code, code)
                self.assertIn(code, str(caught.exception))
                self.manager.submit_once.assert_not_called()
                self.assertEqual(self.dispatches, 0)
