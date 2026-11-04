"""Offline category reports are not real-account acceptance or action permits."""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from quota_guard.browser_service import parse_cost_response as production_parse
from quota_guard.core import DataStore, Person, TZ
from quota_guard.cost_parser import (
    NoUsageData, OfflineFunctionCostCandidate,
    parse_cost_response_with_functions_offline,
)
from quota_guard.platform_http import CostReadError


KEY = '10000000-0000-4000-8000-000000000001'
OTHER_KEY = '10000000-0000-4000-8000-000000000002'
START_DATE = '2026-10-01'
START = int(datetime(2026, 10, 1, tzinfo=TZ).timestamp())
DAY = 86400


class OfflineFunctionCostTests(unittest.TestCase):
    def leaf(self, cost, *, key=KEY, model='synthetic-category', stamp=START):
        return {'api_key': {'tracking_id': key}, 'model': model,
                'buckets': [{'time': stamp, 'cost': cost}]}

    def report(self, ordinary=(), functions=()):
        return {'code': 0, 'data': {'biz_code': 0, 'biz_data': {
            'start': START, 'end': START + DAY, 'bucket': '1d',
            'models': ['synthetic-category'], 'functions': ['synthetic-function'],
            'data': [{'currency': 'CNY', 'series': list(ordinary),
                      'function_series': list(functions)}],
        }}}

    def url(self, payload):
        data = payload['data']['biz_data']
        return ('https://platform.deepseek.com/api/v0/usage/by_api_key/cost'
                f"?start={data['start']}&end={data['end']}&tz=28800")

    def parse(self, payload, *, expected=(KEY,), start=START_DATE, end=START_DATE):
        return parse_cost_response_with_functions_offline(
            payload, self.url(payload), start, end, expected_tracking_ids=expected)

    def test_model_only_matches_production_without_claiming_production_validation(self):
        payload = self.report([self.leaf('0.0000120000000000')])
        candidate = self.parse(payload)
        self.assertIsInstance(candidate, OfflineFunctionCostCandidate)
        self.assertEqual(candidate.totals, production_parse(
            payload, self.url(payload), START_DATE, START_DATE))
        self.assertEqual(candidate.category_totals['function_series'], {})
        self.assertIs(candidate.production_validated, False)
        self.assertEqual(candidate.missing_tracking_ids, frozenset())

    def test_function_only_with_empty_model_series_is_not_no_usage(self):
        candidate = self.parse(self.report(functions=[self.leaf('0.005')]))
        self.assertEqual(candidate.totals, {KEY: Decimal('0.005')})
        self.assertEqual(candidate.category_totals['series'], {})
        self.assertEqual(candidate.category_totals['function_series'], candidate.totals)
        self.assertIs(candidate.production_validated, False)

    def test_equal_amount_and_leaf_identity_in_different_categories_both_count(self):
        leaf = self.leaf('0.005')
        candidate = self.parse(self.report([leaf], [deepcopy(leaf)]))
        self.assertEqual(candidate.totals, {KEY: Decimal('0.010')})
        self.assertEqual(candidate.category_totals,
                         {'series': {KEY: Decimal('0.005')},
                          'function_series': {KEY: Decimal('0.005')}})

    def test_distinct_function_categories_with_equal_amount_both_count(self):
        candidate = self.parse(self.report(functions=[
            self.leaf('0.005', model='synthetic-search'),
            self.leaf('0.005', model='synthetic-fetch')]))
        self.assertEqual(candidate.totals, {KEY: Decimal('0.010')})

    def test_production_accepts_valid_functions_without_turning_helper_into_permit(self):
        payload = self.report(functions=[self.leaf('0.005')])
        candidate = self.parse(payload)
        self.assertIs(candidate.production_validated, False)
        for unused in range(2):
            self.assertEqual(production_parse(payload, self.url(payload), START_DATE, START_DATE),
                             {KEY: Decimal('0.005')})

    def test_duplicates_within_either_category_are_rejected_across_groups(self):
        for category in ('series', 'function_series'):
            with self.subTest(category=category):
                payload = self.report()
                group = payload['data']['biz_data']['data'][0]
                group[category] = [self.leaf('0.005')]
                payload['data']['biz_data']['data'].append(deepcopy(group))
                with self.assertRaises(CostReadError) as caught:
                    self.parse(payload)
                self.assertEqual(caught.exception.reason_code, 'DUPLICATE_COST_BUCKET')

    def test_duplicate_bucket_is_not_silently_deduplicated(self):
        leaf = self.leaf('0.005')
        leaf['buckets'].append(deepcopy(leaf['buckets'][0]))
        with self.assertRaises(CostReadError) as caught:
            self.parse(self.report(functions=[leaf]))
        self.assertEqual(caught.exception.reason_code, 'DUPLICATE_COST_BUCKET')

    def test_function_leaf_preserves_decimal_precision(self):
        candidate = self.parse(self.report(
            [self.leaf('0.000000000000000001')],
            [self.leaf('0.000000000000000002')]))
        self.assertEqual(candidate.totals, {KEY: Decimal('0.000000000000000003')})

    def test_function_costs_reject_bad_currency_key_amount_and_time(self):
        mutations = [
            ('currency', lambda g: g.update(currency='USD')),
            ('missing-key', lambda g: g['function_series'][0]['api_key'].clear()),
            ('short-key', lambda g: g['function_series'][0]['api_key'].update(tracking_id='short')),
            ('missing-category', lambda g: g['function_series'][0].pop('model')),
            ('empty-category', lambda g: g['function_series'][0].update(model='')),
            ('negative', lambda g: g['function_series'][0]['buckets'][0].update(cost='-1')),
            ('nan', lambda g: g['function_series'][0]['buckets'][0].update(cost='NaN')),
            ('precision', lambda g: g['function_series'][0]['buckets'][0].update(cost='0.0000000000000000001')),
            ('boolean-cost', lambda g: g['function_series'][0]['buckets'][0].update(cost=True)),
            ('missing-cost', lambda g: g['function_series'][0]['buckets'][0].pop('cost')),
            ('boolean-time', lambda g: g['function_series'][0]['buckets'][0].update(time=True)),
            ('unaligned', lambda g: g['function_series'][0]['buckets'][0].update(time=START + 1)),
            ('before', lambda g: g['function_series'][0]['buckets'][0].update(time=START - DAY)),
            ('after', lambda g: g['function_series'][0]['buckets'][0].update(time=START + DAY)),
        ]
        for label, mutate in mutations:
            with self.subTest(case=label):
                payload = self.report([self.leaf('0.001')], [self.leaf('0.005')])
                mutate(payload['data']['biz_data']['data'][0])
                with self.assertRaises(CostReadError):
                    self.parse(payload)

    def test_nonlist_categories_and_buckets_are_rejected(self):
        for value in ({}, '', None, False, 0):
            for field in ('series', 'function_series', 'buckets'):
                with self.subTest(field=field, value=value):
                    payload = self.report(functions=[self.leaf('0.005')])
                    group = payload['data']['biz_data']['data'][0]
                    if field == 'buckets':
                        group['function_series'][0]['buckets'] = value
                    else:
                        group[field] = value
                    with self.assertRaises(CostReadError):
                        self.parse(payload)

    def test_missing_model_series_is_not_inferred_from_valid_function_series(self):
        payload = self.report(functions=[self.leaf('0.005')])
        del payload['data']['biz_data']['data'][0]['series']
        with self.assertRaises(CostReadError) as caught:
            self.parse(payload)
        self.assertEqual(caught.exception.reason_code, 'SCHEMA_UNSUPPORTED')

    def test_empty_reports_remain_retryable_no_usage(self):
        empty_group = self.report()
        no_groups = self.report()
        no_groups['data']['biz_data']['data'] = []
        no_buckets = self.report(functions=[self.leaf('0')])
        no_buckets['data']['biz_data']['data'][0]['function_series'][0]['buckets'] = []
        for payload in (empty_group, no_groups, no_buckets):
            with self.subTest(payload=payload):
                with self.assertRaises(NoUsageData) as caught:
                    self.parse(payload)
                self.assertEqual(caught.exception.reason_code, 'NO_USAGE_DATA')
                self.assertTrue(caught.exception.retryable)

    def test_explicit_zero_keeps_missing_bound_key_unknown(self):
        candidate = self.parse(self.report(functions=[self.leaf('0')]),
                               expected=(KEY, OTHER_KEY))
        self.assertEqual(candidate.totals, {KEY: Decimal(0)})
        self.assertNotIn(OTHER_KEY, candidate.totals)
        self.assertEqual(candidate.missing_tracking_ids, frozenset({OTHER_KEY}))
        self.assertIs(candidate.production_validated, False)

    def test_unknown_key_is_rejected_without_spreading_cost_to_known_target(self):
        payload = self.report(functions=[self.leaf('0.005', key=OTHER_KEY)])
        with self.assertRaises(CostReadError) as caught:
            self.parse(payload)
        self.assertEqual(caught.exception.reason_code, 'KEY_ID_UNBOUND')

    def test_candidate_requires_explicit_unique_valid_bindings(self):
        payload = self.report(functions=[self.leaf('0.005')])
        for expected in (None, (), KEY, (KEY, KEY), ('short',), (True,), {}):
            with self.subTest(expected=expected):
                with self.assertRaises(CostReadError) as caught:
                    self.parse(payload, expected=expected)
                self.assertEqual(caught.exception.reason_code, 'KEY_ID_INVALID')

    def test_function_buckets_use_same_selected_period_and_timezone_rules(self):
        payload = self.report(functions=[self.leaf('0.005')])
        with self.assertRaises(CostReadError) as caught:
            parse_cost_response_with_functions_offline(payload,
                self.url(payload).replace('tz=28800', 'tz=0'), START_DATE, START_DATE,
                expected_tracking_ids=(KEY,))
        self.assertEqual(caught.exception.reason_code, 'TIMEZONE_MISMATCH')
        payload['data']['biz_data']['end'] -= 1
        with self.assertRaises(CostReadError) as caught:
            self.parse(payload)
        self.assertEqual(caught.exception.reason_code, 'PERIOD_INCOMPLETE')

    def test_function_hourly_buckets_exclude_days_outside_selected_period(self):
        payload = self.report(functions=[self.leaf('0.005')])
        data = payload['data']['biz_data']
        data.update(end=START + 2 * DAY, bucket=3600)
        data['data'][0]['function_series'][0]['buckets'] = [
            {'time': START, 'cost': '50'},
            {'time': START + DAY, 'cost': '0.005'},
            {'time': START + 2 * DAY - 3600, 'cost': '0.005'},
        ]
        candidate = self.parse(payload, start='2026-10-02', end='2026-10-02')
        self.assertEqual(candidate.totals, {KEY: Decimal('0.010')})

    def test_metadata_totals_and_usage_counts_are_not_added_again(self):
        payload = self.report(functions=[self.leaf('0.005')])
        data = payload['data']['biz_data']
        data.update(total_cost='999', request_count=5000, unrelated={'new_metadata': True})
        data['data'][0]['function_series'][0]['buckets'][0]['usage'] = {'FUNCTION_CALL': 100}
        self.assertEqual(self.parse(payload).totals, {KEY: Decimal('0.005')})

    def test_legacy_missing_function_field_keeps_model_only_behavior(self):
        payload = self.report([self.leaf('0.001')])
        del payload['data']['biz_data']['functions']
        del payload['data']['biz_data']['data'][0]['function_series']
        self.assertEqual(self.parse(payload).totals, {KEY: Decimal('0.001')})
        self.assertEqual(production_parse(payload, self.url(payload), START_DATE, START_DATE),
                         {KEY: Decimal('0.001')})

    def test_cumulative_candidate_replaces_existing_synthetic_snapshot(self):
        payload = self.report([self.leaf('0.001')], [self.leaf('0.005')])
        with tempfile.TemporaryDirectory() as temporary:
            store = DataStore(Path(temporary))
            store.initialize(1, '100', START_DATE)
            store.save_people([Person('Synthetic Person', Decimal(1))])
            store.bind_keys([{'name': 'Synthetic Person', 'tracking_id': KEY}], 'synthetic-account')
            # Explicit test-only adoption into an isolated ledger, never a runtime switch.
            first = self.parse(payload)
            store.apply_snapshot(first.totals, 'first', START_DATE, START_DATE)
            repeated = self.parse(payload)
            store.apply_snapshot(repeated.totals, 'first', START_DATE, START_DATE)
            payload['data']['biz_data']['data'][0]['function_series'][0]['buckets'][0]['cost'] = '0.010'
            later = self.parse(payload)
            store.apply_snapshot(later.totals, 'later', START_DATE, START_DATE)
            self.assertEqual(store.evaluate()[0]['spent'], Decimal('0.011'))
            self.assertFalse(any(event['kind'] == 'action' for event in store.events()))


if __name__ == '__main__':
    unittest.main()
