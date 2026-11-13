"""Creation-to-current-date collection with synthetic HTTP only."""
from contextlib import nullcontext
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from quota_guard.browser_service import BrowserService
from quota_guard.core import TZ
from quota_guard.lifetime_costs import creation_periods, read_lifetime_costs
from quota_guard.platform_http import CostReadError, cost_url
from quota_guard.platform_keys import KeyReadError
import test_platform_keys as key_fixtures


A = '00000000-0000-0000-0000-000000000001'
B = '00000000-0000-0000-0000-000000000002'
C = '00000000-0000-0000-0000-000000000003'
NOW = datetime(2026, 11, 10, 12, tzinfo=TZ).timestamp()


def stamp(value):
    return int(datetime.fromisoformat(value).replace(tzinfo=TZ).timestamp())


def payload(start, end, keys, *, missing=(), malformed=False):
    url = cost_url(start, end, tracking_ids=keys)
    query = parse_qs(urlparse(url).query)
    begin, finish = int(query['start'][0]), int(query['end'][0])
    group = {'currency': 'CNY', 'series': [], 'function_series': []}
    for key in keys:
        if key in missing:
            continue
        for category, amount in (('series', '1.1'), ('function_series', '0.2')):
            group[category].append({'api_key': {'tracking_id': key}, 'model': category,
                                   'buckets': [{'time': begin, 'cost': amount}]})
    if malformed:
        group['other_series'] = []
    return url, {'code': 0, 'data': {'biz_code': 0, 'biz_data': {
        'start': begin, 'end': finish, 'bucket': 86400, 'data': [group]}}}


class LifetimeReadTests(unittest.TestCase):
    def reader(self, **overrides):
        reader = Mock()
        reader.read.side_effect = lambda start, end, tracking_ids: payload(start, end, tracking_ids, **overrides)
        return reader

    def test_creation_utc8_day_is_independent_of_machine_timezone(self):
        # 16:00 UTC is next-day midnight in the ledger timezone.
        origin = int(datetime.fromisoformat('2026-11-01T16:00:00+00:00').timestamp())
        self.assertEqual(creation_periods({A: origin}, '2026-11-10', NOW)[A]['start_date'], '2026-11-02')

    def test_future_bool_missing_and_invalid_end_rejected(self):
        for origins, end in (({A: True}, '2026-11-10'), ({A: int(NOW) + 1}, '2026-11-10'),
                             ({A: 0}, '2026-11-10'), ({}, '2026-11-10'),
                             ({A: stamp('2026-11-02')}, '2026-11-09')):
            with self.subTest(origins=origins, end=end), self.assertRaises(CostReadError):
                creation_periods(origins, end, NOW)

    def test_different_creation_days_group_and_segment_without_overlap(self):
        periods = creation_periods({A: stamp('2026-09-10'), B: stamp('2026-09-10'),
                                    C: stamp('2026-11-08')}, '2026-11-10', NOW)
        reader = self.reader()
        totals, digest = read_lifetime_costs(reader, periods)
        self.assertEqual(totals, {A: Decimal('3.9'), B: Decimal('3.9'), C: Decimal('1.3')})
        self.assertEqual([(call.args, call.kwargs['tracking_ids']) for call in reader.read.call_args_list], [
            (('2026-09-10', '2026-10-09'), [A, B]),
            (('2026-10-10', '2026-11-08'), [A, B]),
            (('2026-11-09', '2026-11-10'), [A, B]),
            (('2026-11-08', '2026-11-10'), [C])])
        self.assertRegex(digest, r'^[0-9a-f]{64}$')

    def test_missing_one_segment_never_publishes_partial_key_total(self):
        periods = creation_periods({A: stamp('2026-10-01'), B: stamp('2026-10-01')}, '2026-11-10', NOW)
        reader = self.reader()
        reader.read.side_effect = lambda start, end, tracking_ids: payload(
            start, end, tracking_ids, missing=[A] if start == '2026-10-01' else [])
        totals, _ = read_lifetime_costs(reader, periods)
        self.assertEqual(totals, {B: Decimal('2.6')})

    def test_empty_segment_is_unknown_not_zero(self):
        periods = creation_periods({A: stamp('2026-10-01')}, '2026-11-10', NOW)
        with self.assertRaises(CostReadError) as caught:
            read_lifetime_costs(self.reader(missing=[A]), periods)
        self.assertEqual(caught.exception.reason_code, 'LIFETIME_PERIOD_INCOMPLETE')

    def test_malformed_function_categories_reject_entire_read(self):
        periods = creation_periods({A: stamp('2026-11-02')}, '2026-11-10', NOW)
        with self.assertRaises(CostReadError) as caught:
            read_lifetime_costs(self.reader(malformed=True), periods)
        self.assertEqual(caught.exception.reason_code, 'CHARGE_TYPE_UNSUPPORTED')

    def test_limit_is_checked_before_any_http_request(self):
        periods = creation_periods({A: stamp('2000-01-01')}, '2026-11-10', NOW)
        reader = self.reader()
        with self.assertRaises(CostReadError) as caught:
            read_lifetime_costs(reader, periods)
        self.assertEqual(caught.exception.reason_code, 'LIFETIME_QUERY_LIMIT')
        reader.read.assert_not_called()

    def test_budget_timeout_does_not_publish_partially_read_totals(self):
        periods = creation_periods({A: stamp('2026-11-02')}, '2026-11-10', NOW)
        reader = self.reader()
        with patch('quota_guard.lifetime_costs.time.monotonic', side_effect=[0, 46]), self.assertRaises(CostReadError) as caught:
            read_lifetime_costs(reader, periods, started_monotonic=0)
        self.assertEqual(caught.exception.reason_code, 'LIFETIME_QUERY_TIMEOUT')
        self.assertEqual(reader.read.call_count, 1)

    def test_source_digest_commits_to_creation_metadata(self):
        periods = creation_periods({A: stamp('2026-11-02')}, '2026-11-10', NOW)
        changed = creation_periods({A: stamp('2026-11-02') + 1}, '2026-11-10', NOW)
        self.assertNotEqual(read_lifetime_costs(self.reader(), periods)[1],
                            read_lifetime_costs(self.reader(), changed)[1])


class CreationMetadataTests(unittest.TestCase):
    setUp = key_fixtures.PlatformKeyTests.setUp
    replace_list = key_fixtures.PlatformKeyTests.replace_list

    def read(self, ids=(A,)):
        return self.manager.read_creation_times(self.expected, ids, observed_wall=NOW)

    def rows(self):
        return self.fixture['list_response']['data']['biz_data']['api_keys']

    def test_selected_origin_needs_no_redacted_secret_and_ignores_bystander_metadata(self):
        for row in self.rows():
            row.pop('sensitive_id', None)
        self.rows()[1]['created_at'] = None
        self.replace_list()
        self.assertEqual(self.read(), {A: self.rows()[0]['created_at']})
        self.api.post.assert_not_called()
        self.assertIsNone(self.manager._issued)
        self.assertTrue(self.list_response.disposed)

    def test_reordered_inventory_uses_tracking_id(self):
        expected = self.rows()[0]['created_at']
        self.rows().reverse()
        self.replace_list()
        self.assertEqual(self.read(), {A: expected})

    def test_dsh_bystander_does_not_require_action_fields(self):
        self.rows()[1].update(name='ignored raw app name', key_type='DSH', created_at=None)
        self.expected[1]['name'] = 'DeepSeek Harness App-managed'
        self.replace_list()
        self.assertEqual(set(self.read()), {A})

    def test_dsh_or_unknown_target_cannot_supply_lifetime_origin(self):
        for kind in ('DSH', 'FUTURE'):
            self.rows()[0]['key_type'] = kind
            if kind == 'DSH':
                self.expected[0]['name'] = 'DeepSeek Harness'
            else:
                self.expected[0]['name'] = self.rows()[0]['name']
            self.replace_list()
            with self.subTest(kind=kind), self.assertRaises(KeyReadError) as caught:
                self.read()
            self.assertEqual(caught.exception.reason_code, 'KEY_TARGET_KIND_UNSUPPORTED')

    def test_missing_or_future_origin_rejected(self):
        for origin in (True, None, '1793203200', 0, int(NOW) + 1):
            self.rows()[0]['created_at'] = origin
            self.replace_list()
            with self.subTest(origin=origin), self.assertRaises(KeyReadError) as caught:
                self.read()
            self.assertEqual(caught.exception.reason_code, 'KEY_CREATED_AT_INVALID')

    def test_identity_change_rejects_metadata(self):
        self.rows()[1]['name'] = 'changed'
        self.replace_list()
        with self.assertRaises(KeyReadError) as caught:
            self.read()
        self.assertEqual(caught.exception.reason_code, 'KEY_LIST_IDENTITY_MISMATCH')

    def test_readonly_bootstrap_never_falls_back_to_global_usage_page(self):
        self.manager.clear()
        page = Mock(spec=['context', 'url', 'goto', 'wait_for_timeout'])
        page.context, page.url = self.context, 'https://platform.deepseek.com/api_keys'
        self.context.on, self.context.remove_listener = Mock(), Mock()
        clock = iter(range(100))
        with patch('quota_guard.platform_keys.time.monotonic', side_effect=lambda: next(clock)), \
                patch('quota_guard.platform_keys.dismiss_optional_notice', return_value=False), \
                self.assertRaises(Exception) as caught:
            self.manager.prepare(page, allow_cost_fallback=False)
        self.assertEqual(caught.exception.reason_code, 'KEY_LIST_REQUEST_NOT_OBSERVED')
        self.assertEqual([call.args[0] for call in page.goto.call_args_list],
                         ['https://platform.deepseek.com/api_keys'])
        self.api.get.assert_not_called()
        self.api.post.assert_not_called()


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls.fromtimestamp(NOW, tz)


class LifetimeServiceTests(unittest.TestCase):
    def setUp(self):
        self.service = object.__new__(BrowserService)
        self.service.context, self.service.page = SimpleNamespace(), Mock()
        self.keys = [{'name': 'Alpha', 'tracking_id': A}, {'name': 'Beta', 'tracking_id': B}]
        self.service._keys = Mock(return_value=self.keys)
        self.service._check_login_ready = Mock()
        self.manager = Mock()
        self.manager.capture.return_value = nullcontext()
        self.origins = {A: stamp('2026-10-29')}
        self.manager.read_creation_times.side_effect = lambda *args, **kwargs: dict(self.origins)
        self.reader = Mock()
        self.reader.read.side_effect = lambda start, end, tracking_ids: payload(start, end, tracking_ids)

    def collect(self, **overrides):
        with patch('quota_guard.platform_keys.ConsoleKeyManager', return_value=self.manager), \
                patch.object(self.service, '_create_cost_reader', return_value=self.reader), \
                patch('quota_guard.browser_service.datetime', FixedDateTime), \
                patch('quota_guard.browser_service.time.time', return_value=NOW):
            options = {'managed_names': ['Alpha'], 'fee_basis': 'key_lifetime'}
            options.update(overrides)
            return self.service._collect('2026-11-10', **options)

    def test_creation_origin_overrides_ledger_date_only_for_http_period(self):
        result = self.collect()
        self.reader.read.assert_called_once_with('2026-10-29', '2026-11-10', tracking_ids=[A])
        self.assertEqual(result['start_date'], '2026-11-10')
        self.assertEqual(result['key_periods'][A]['start_date'], '2026-10-29')
        self.assertEqual(result['totals'], {A: Decimal('1.3')})
        self.assertEqual(result['requested_tracking_ids'], [A])
        self.manager.prepare.assert_called_once_with(self.service.page, allow_cost_fallback=False)
        self.assertEqual(self.manager.read_creation_times.call_count, 2)
        self.manager.clear.assert_called_once()

    def test_post_read_creation_change_discards_all_fees_and_period_authority(self):
        self.manager.read_creation_times.side_effect = [self.origins, {A: self.origins[A] + 1}]
        result = self.collect()
        self.assertEqual(result['reason_code'], 'LIFETIME_IDENTITY_CHANGED')
        self.assertIsNone(result['totals'])
        self.assertEqual(result['key_periods'], {})

    def test_post_read_dom_change_discards_fees(self):
        self.service._keys.side_effect = [self.keys, [self.keys[0]]]
        result = self.collect()
        self.assertEqual(result['reason_code'], 'LIFETIME_IDENTITY_CHANGED')
        self.assertIsNone(result['totals'])

    def test_unsupported_target_does_not_query_costs(self):
        self.manager.read_creation_times.side_effect = KeyReadError('KEY_TARGET_KIND_UNSUPPORTED')
        result = self.collect()
        self.assertEqual(result['reason_code'], 'LIFETIME_SCOPE_UNSUPPORTED')
        self.assertEqual(result['key_periods'], {})
        self.reader.read.assert_not_called()

    def test_empty_costs_still_recheck_metadata_before_identity_only_result(self):
        self.reader.read.side_effect = lambda start, end, tracking_ids: payload(start, end, tracking_ids, missing=[A])
        result = self.collect()
        self.assertEqual(result['reason_code'], 'LIFETIME_PERIOD_INCOMPLETE')
        self.assertIsNone(result['totals'])
        self.assertEqual(set(result['key_periods']), {A})
        self.assertEqual(self.manager.read_creation_times.call_count, 2)

    def test_no_matching_managed_key_queries_no_metadata_or_cost(self):
        result = self.collect(managed_names=['Retired'])
        self.assertEqual(result['reason_code'], 'NO_MANAGED_KEYS')
        self.manager.prepare.assert_not_called()
        self.reader.read.assert_not_called()


if __name__ == '__main__':
    unittest.main()
