"""Synthetic read diagnostics; never use a browser, account or network."""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from quota_guard.browser_service import BrowserService, TZ, cost_identity_coverage
from quota_guard.cost_parser import parse_cost_response
from quota_guard.diagnostics import DiagnosticLog, read_recent_diagnostics, safe_fields
from quota_guard.platform_http import ConsoleCostReader, cost_metadata, cost_url
from test_platform_http import Response


KEY_A = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
KEY_B = '11111111-2222-3333-4444-555555555555'
KEY_C = 'ffffffff-2222-3333-4444-555555555555'


def report(ids=(KEY_A,), *, first='2026-10-29', last='2026-11-02'):
    query = parse_qs(urlsplit(cost_url(first, last)).query)
    begin, end = int(query['start'][0]), int(query['end'][0])
    return {'code': 0, 'data': {'biz_code': 0, 'biz_data': {
        'start': begin, 'end': end, 'bucket': 86400,
        'data': [{'currency': 'CNY', 'function_series': [], 'series': [
            {'api_key': {'tracking_id': key}, 'model': 'synthetic-model',
             'buckets': [{'time': begin, 'cost': '0.000012'}]} for key in ids
        ]}],
    }}}


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 11, 2, 12, tzinfo=TZ)
        return value.astimezone(tz) if tz else value.replace(tzinfo=None)


class MetadataTests(unittest.TestCase):
    def test_message_content_and_unknown_fields_never_enter_metadata(self):
        payload = {'code': 0, 'msg': 'private-root-message', 'data': {
            'biz_code': 1, 'biz_msg': 'private-business-message-' * 100,
            'message': {'private-field-name': 'private-nested-value'},
        }, 'private-extra-name': 'private-extra-value'}
        fields = cost_metadata(payload)
        self.assertTrue(fields['response_message_present'])
        self.assertEqual(fields['response_message_type'], 'str')
        self.assertTrue(fields['business_message_present'])
        self.assertEqual(fields['business_message_type'], 'mixed')
        self.assertEqual(fields['business_message_length'], 1024)
        self.assertEqual(fields['biz_code'], 1)
        self.assertNotIn('private-', json.dumps(fields))
        self.assertEqual(safe_fields(fields), fields)

    def test_message_slots_distinguish_absent_null_and_non_text(self):
        absent = cost_metadata({'data': {}})
        self.assertFalse(absent['business_message_present'])
        self.assertEqual(absent['business_message_type'], 'missing')
        for value, expected in ((None, 'null'), (True, 'bool'), (1, 'int'),
                                ([], 'list'), ({}, 'dict'), (Decimal('1.0'), 'decimal')):
            with self.subTest(expected=expected):
                fields = cost_metadata({'data': {'biz_msg': value}})
                self.assertTrue(fields['business_message_present'])
                self.assertEqual(fields['business_message_type'], expected)
                self.assertEqual(fields['business_message_length'], 0)

    def test_bucket_summary_uses_numeric_times_not_costs_or_identity(self):
        payload = report()
        data = payload['data']['biz_data']
        data['bucket'] = '1d'
        begin = data['start']
        data['data'][0]['series'][0]['buckets'].extend([
            {'time': begin + 86400, 'cost': 'private-cost'},
            {'time': 'private-time'}, {'time': True}, {'time': -1},
            {'time': 10 ** 100}, {'private-field': 'private-value'},
        ])
        fields = cost_metadata(payload)
        self.assertEqual(fields['bucket_kind'], 'text')
        self.assertEqual(fields['bucket_seconds'], 86400)
        self.assertEqual(fields['bucket_first_time'], begin)
        self.assertEqual(fields['bucket_last_time'], begin + 86400)
        self.assertEqual(fields['bucket_count'], 7)
        for text in (KEY_A, 'private-', 'synthetic-model'):
            self.assertNotIn(text, json.dumps(fields))
        data['bucket'] = {'private-field': 'private-value'}
        fields = cost_metadata(payload)
        self.assertEqual(fields['bucket_kind'], 'unsupported')
        self.assertNotIn('bucket_seconds', fields)

    def test_log_whitelist_rejects_injected_values_and_preserves_preview(self):
        valid = dict(fee_id_source='api_key.tracking_id', key_id_source='tracking_id_column',
                     request_tz=28800, request_span_days=5, bucket_kind='integer',
                     bucket_seconds=86400, platform_key_count=2, matched_platform_count=0,
                     unmatched_fee_count=2, missing_platform_count=2,
                     uuid_casefold_match_count=1, case_only_match_count=1,
                     function_bucket_count=2, function_bucket_first_time=1793203200,
                     function_bucket_last_time=1793289600)
        self.assertEqual(safe_fields(valid), valid)
        for value in ('private-leak', {'private-leak': 'private-value'}, True, -1):
            with self.subTest(value=value):
                self.assertEqual(safe_fields(dict.fromkeys(valid, value)), {})
        with tempfile.TemporaryDirectory() as temp:
            log = DiagnosticLog(temp)
            log.emit('cost_identity_coverage', **valid)
            logged = json.loads(log.path.read_text(encoding='utf-8'))['data']
            self.assertEqual(logged, valid)
            preview = read_recent_diagnostics(Path(temp))
            self.assertIn('"matched_platform_count": 0', preview.text)
            self.assertIn('api_key.tracking_id', preview.text)

    def test_function_bucket_summary_does_not_change_model_bucket_meaning(self):
        payload = report()
        group = payload['data']['biz_data']['data'][0]
        begin = payload['data']['biz_data']['start']
        group['function_series'] = [{
            'api_key': {'tracking_id': 'private-function-id'}, 'model': 'private-category',
            'buckets': [{'time': begin + 86400, 'cost': 'private-cost'},
                        {'time': begin + 2 * 86400, 'cost': 'private-cost'},
                        {'time': 'private-time'}, {'time': True}],
        }]
        fields = cost_metadata(payload)
        self.assertEqual(fields['bucket_count'], 1)
        self.assertEqual(fields['bucket_first_time'], begin)
        self.assertEqual(fields['bucket_last_time'], begin)
        self.assertEqual(fields['function_series_count'], 1)
        self.assertEqual(fields['function_bucket_count'], 4)
        self.assertEqual(fields['function_bucket_first_time'], begin + 86400)
        self.assertEqual(fields['function_bucket_last_time'], begin + 2 * 86400)
        self.assertEqual(safe_fields(fields), fields)
        self.assertNotIn('private-', json.dumps(fields))


class IdentityCoverageTests(unittest.TestCase):
    def test_uuid_case_comparison_does_not_modify_actual_keys_or_costs(self):
        keys = [{'name': 'Fixture A', 'tracking_id': KEY_A},
                {'name': 'Fixture B', 'tracking_id': KEY_B}]
        totals = {KEY_A.upper(): Decimal('1'), KEY_C: Decimal('2')}
        original = deepcopy((keys, totals))
        self.assertEqual(cost_identity_coverage(keys, totals), {
            'platform_key_count': 2, 'fee_tracking_count': 2,
            'matched_platform_count': 0, 'unmatched_fee_count': 2,
            'missing_platform_count': 2, 'uuid_casefold_match_count': 1,
            'case_only_match_count': 1,
        })
        self.assertEqual((keys, totals), original)

    def test_opaque_identifiers_are_not_case_folded(self):
        keys = [{'tracking_id': 'OpaqueKeyA'}, {'tracking_id': KEY_A}]
        totals = {'opaquekeya': Decimal('1'), KEY_A: Decimal('2')}
        fields = cost_identity_coverage(keys, totals)
        self.assertEqual(fields['matched_platform_count'], 1)
        self.assertEqual(fields['uuid_casefold_match_count'], 1)
        self.assertEqual(fields['case_only_match_count'], 0)

    def test_model_field_is_not_the_identity_source(self):
        payload = report()
        payload['data']['biz_data']['data'][0]['series'][0]['model'] = KEY_C
        totals = parse_cost_response(payload, cost_url('2026-10-29', '2026-11-02'),
                                     '2026-10-29', '2026-11-02')
        self.assertEqual(totals, {KEY_A: Decimal('0.000012')})
        fields = cost_identity_coverage([{'tracking_id': KEY_A}], totals)
        self.assertEqual(fields['matched_platform_count'], 1)


class CollectionCoverageTests(unittest.TestCase):
    def collect(self, payload, *, first='2026-10-29'):
        events = []
        keys = [{'name': 'Fixture A', 'tracking_id': KEY_A},
                {'name': 'Fixture B', 'tracking_id': KEY_B}]
        api = Mock(spec=['get'])
        api.get.return_value = Response(cost_url(first, '2026-11-02'),
                                       body=json.dumps(payload).encode())
        service = object.__new__(BrowserService)
        service.context = SimpleNamespace(request=api)
        service.page = object()
        service._keys = Mock(return_value=keys)
        service._check_login_ready = Mock()
        service._stage = Mock()
        service._log = lambda event, **fields: events.append((event, safe_fields(fields)))
        reader = ConsoleCostReader(service.context, emit=service._log)
        reader._authorization = 'Bearer synthetic-session'
        reader.prepare = Mock()
        service._cost_reader = reader
        service._cost_reader_identity = 'fixture-account'
        with patch('quota_guard.browser_service.datetime', FixedDateTime):
            result = service._collect(first, account_id='fixture-account',
                                      known_keys=(KEY_A, KEY_B))
        api.get.assert_called_once()
        self.assertEqual(service._keys.call_count, 2)
        self.assertTrue(api.get.return_value.disposed)
        self.assertNotIn('synthetic-session', json.dumps(events))
        return result, events, reader

    def test_unmatched_fees_stay_intact_for_strict_ledger_validation(self):
        result, events, _reader = self.collect(report((KEY_C,)))
        self.assertEqual(result['totals'], {KEY_C: Decimal('0.000012')})
        self.assertEqual(result['reason_code'], 'OK')
        self.assertEqual(result['coverage_summary']['matched_platform_count'], 0)
        self.assertEqual(result['coverage_summary']['unmatched_fee_count'], 1)
        coverage = [fields for event, fields in events if event == 'cost_identity_coverage']
        self.assertEqual(len(coverage), 1)
        self.assertEqual(coverage[0]['fee_id_source'], 'api_key.tracking_id')
        self.assertEqual(coverage[0]['key_id_source'], 'tracking_id_column')
        self.assertNotIn(KEY_C, json.dumps(events))
        started = next(fields for event, fields in events if event == 'cost_http_started')
        self.assertEqual(started['request_tz'], 28800)
        self.assertEqual(started['request_span_days'], 5)

    def test_empty_report_does_not_invent_coverage_or_zero_totals(self):
        result, events, _reader = self.collect(report((), first='2026-11-02'), first='2026-11-02')
        self.assertEqual(result['reason_code'], 'NO_USAGE_DATA')
        self.assertIsNone(result['totals'])
        self.assertNotIn('coverage_summary', result)
        self.assertTrue(result['retryable'])
        self.assertFalse(any(event == 'cost_identity_coverage' for event, _fields in events))
        started = next(fields for event, fields in events if event == 'cost_http_started')
        self.assertEqual(started['request_span_days'], 1)

    def test_business_rejection_is_not_mislabeled_as_login_failure(self):
        payload = {'code': 0, 'data': {'biz_code': 1, 'biz_msg': 'private-business-reason'}}
        result, events, reader = self.collect(payload)
        self.assertEqual(result['reason_code'], 'PLATFORM_REJECTED')
        self.assertIsNone(result['totals'])
        self.assertFalse(result['retryable'])
        self.assertIn('具体业务原因尚未确认', result['message'])
        self.assertNotIn('登录', result['message'])
        self.assertNotIn('private-business-reason', json.dumps((result, events)))
        self.assertIsNone(reader._authorization)  # Existing session policy remains.
        rejected = next(fields for event, fields in events if event == 'cost_collection_rejected')
        self.assertTrue(rejected['business_message_present'])
        self.assertEqual(rejected['business_message_type'], 'str')


if __name__ == '__main__':
    unittest.main()
