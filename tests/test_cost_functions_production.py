"""Production parser/worker tests using synthetic costs and isolated ledgers."""
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from quota_guard.cost_parser import NoUsageData, parse_cost_response
from quota_guard.core import DataStore, Person
from quota_guard.platform_http import CostReadError
import test_cost_functions_offline as fixture
import test_cost_read_diagnostics as read_fixture


class FunctionProductionTests(unittest.TestCase):
    def setUp(self):
        self.sample = fixture.OfflineFunctionCostTests()

    def parse(self, payload):
        return parse_cost_response(payload, self.sample.url(payload),
                                   fixture.START_DATE, fixture.START_DATE)

    def test_function_only_and_mixed_reports_use_actual_costs(self):
        function = self.sample.leaf('0.005', model='synthetic-function')
        only = self.sample.report(functions=[function])
        mixed = self.sample.report([self.sample.leaf('0.001')], [function])
        self.assertEqual(self.parse(only), {fixture.KEY: Decimal('0.005')})
        self.assertEqual(self.parse(mixed), {fixture.KEY: Decimal('0.006')})

    def test_equal_leaf_in_two_categories_counts_both_without_usage_estimation(self):
        leaf = self.sample.leaf('0.005')
        payload = self.sample.report([leaf], [deepcopy(leaf)])
        data = payload['data']['biz_data']
        data.update(total_cost='99999', request_count=99999)
        data['data'][0]['function_series'][0]['buckets'][0]['usage'] = {'FUNCTION_CALL': 99999}
        self.assertEqual(self.parse(payload), {fixture.KEY: Decimal('0.010')})

    def test_function_category_labels_are_not_guessed_from_a_price_table(self):
        payload = self.sample.report(functions=[
            self.sample.leaf('0.005', model='synthetic-category-a'),
            self.sample.leaf('0.007', model='synthetic-category-b'),
        ])
        self.assertEqual(self.parse(payload), {fixture.KEY: Decimal('0.012')})

    def test_invalid_function_leaf_rejects_whole_report_not_model_only_fallback(self):
        mutations = (
            lambda leaf: leaf['api_key'].update(tracking_id=None),
            lambda leaf: leaf['api_key'].clear(),
            lambda leaf: leaf.pop('model'),
            lambda leaf: leaf.update(model=''),
            lambda leaf: leaf.update(model='   '),
            lambda leaf: leaf['buckets'][0].pop('cost'),
            lambda leaf: leaf['buckets'][0].update(cost='-1'),
            lambda leaf: leaf['buckets'][0].update(cost='NaN'),
            lambda leaf: leaf['buckets'][0].update(cost=True),
            lambda leaf: leaf['buckets'][0].update(cost='0.0000000000000000001'),
            lambda leaf: leaf['buckets'][0].update(time=fixture.START + 1),
            lambda leaf: leaf['buckets'][0].update(time=fixture.START + fixture.DAY),
            lambda leaf: leaf['buckets'][0].update(time=True),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(case=index):
                payload = self.sample.report([self.sample.leaf('0.001')],
                                             [self.sample.leaf('0.005')])
                mutate(payload['data']['biz_data']['data'][0]['function_series'][0])
                with self.assertRaises(CostReadError):
                    self.parse(payload)

    def test_missing_function_details_rejected_when_functions_are_declared(self):
        payload = self.sample.report([self.sample.leaf('0.001')])
        del payload['data']['biz_data']['data'][0]['function_series']
        with self.assertRaises(CostReadError) as caught:
            self.parse(payload)
        self.assertEqual(caught.exception.reason_code, 'SCHEMA_UNSUPPORTED')

    def test_wrong_function_metadata_or_series_type_is_not_ignored(self):
        for field in ('functions', 'function_series'):
            for value in ({}, None, '', False, 0):
                with self.subTest(field=field, value=value):
                    payload = self.sample.report([self.sample.leaf('0.001')])
                    data = payload['data']['biz_data']
                    (data if field == 'functions' else data['data'][0])[field] = value
                    with self.assertRaises(CostReadError):
                        self.parse(payload)
        for value in ([None], [{}], [''], ['   '], [False]):
            with self.subTest(metadata=value):
                payload = self.sample.report([self.sample.leaf('0.001')])
                payload['data']['biz_data']['functions'] = value
                with self.assertRaises(CostReadError):
                    self.parse(payload)

    def test_unknown_additional_charge_series_is_rejected(self):
        payload = self.sample.report([self.sample.leaf('0.001')])
        payload['data']['biz_data']['data'][0]['future_series'] = [self.sample.leaf('0.5')]
        with self.assertRaises(CostReadError) as caught:
            self.parse(payload)
        self.assertEqual(caught.exception.reason_code, 'CHARGE_TYPE_UNSUPPORTED')

    def test_duplicate_function_buckets_rejected_within_and_across_groups(self):
        for across_groups in (False, True):
            with self.subTest(across_groups=across_groups):
                payload = self.sample.report(functions=[self.sample.leaf('0.005')])
                data = payload['data']['biz_data']
                if across_groups:
                    data['data'].append(deepcopy(data['data'][0]))
                else:
                    buckets = data['data'][0]['function_series'][0]['buckets']
                    buckets.append(deepcopy(buckets[0]))
                with self.assertRaises(CostReadError) as caught:
                    self.parse(payload)
                self.assertEqual(caught.exception.reason_code, 'DUPLICATE_COST_BUCKET')

    def test_currency_and_period_guards_also_cover_function_only_reports(self):
        payload = self.sample.report(functions=[self.sample.leaf('0.005')])
        payload['data']['biz_data']['data'][0]['currency'] = 'USD'
        with self.assertRaises(CostReadError) as caught:
            self.parse(payload)
        self.assertEqual(caught.exception.reason_code, 'CURRENCY_UNSUPPORTED')
        payload = self.sample.report(functions=[self.sample.leaf('0.005')])
        payload['data']['biz_data']['end'] -= 1
        with self.assertRaises(CostReadError) as caught:
            self.parse(payload)
        self.assertEqual(caught.exception.reason_code, 'PERIOD_INCOMPLETE')

    def test_empty_functions_remain_unknown_not_zero(self):
        payload = self.sample.report()
        with self.assertRaises(NoUsageData):
            self.parse(payload)
        payload = self.sample.report(functions=[self.sample.leaf('0')])
        payload['data']['biz_data']['data'][0]['function_series'][0]['buckets'] = []
        with self.assertRaises(NoUsageData):
            self.parse(payload)

    def test_combined_cost_crosses_threshold_and_repeated_snapshot_is_not_added(self):
        payload = self.sample.report([self.sample.leaf('0.001')], [self.sample.leaf('0.005')])
        totals = self.parse(payload)
        with tempfile.TemporaryDirectory() as temp:
            store = DataStore(Path(temp))
            store.initialize(1, '100', fixture.START_DATE)
            store.save_people([Person('Fixture person', Decimal('0.005'))])
            store.bind_keys([{'name': 'Fixture person', 'tracking_id': fixture.KEY}], 'fixture-account')
            store.apply_snapshot(totals, 'function-production-first', fixture.START_DATE, fixture.START_DATE)
            store.apply_snapshot(totals, 'function-production-first', fixture.START_DATE, fixture.START_DATE)
            row = store.evaluate()[0]
            self.assertEqual(row['spent'], Decimal('0.006'))
            self.assertEqual(row['status'], 'triggered')
            self.assertEqual(sum(event['kind'] == 'snapshot' for event in store.events()), 1)
            self.assertFalse(any(event['kind'] == 'action' for event in store.events()))

    def test_unbound_function_cost_is_not_charged_to_known_person(self):
        payload = self.sample.report([self.sample.leaf('0.001')],
                                     [self.sample.leaf('10', key=fixture.OTHER_KEY)])
        totals = self.parse(payload)
        self.assertEqual(totals[fixture.OTHER_KEY], Decimal('10'))
        with tempfile.TemporaryDirectory() as temp:
            store = DataStore(Path(temp))
            store.initialize(1, '100', fixture.START_DATE)
            store.save_people([Person('Fixture person', Decimal('0.005'))])
            store.bind_keys([{'name': 'Fixture person', 'tracking_id': fixture.KEY}], 'fixture-account')
            store.apply_snapshot(totals, 'unbound-function', fixture.START_DATE, fixture.START_DATE)
            row = store.evaluate()[0]
            self.assertEqual(row['spent'], Decimal('0.001'))
            self.assertEqual(row['status'], 'ok')

    def test_worker_routes_valid_function_only_costs_to_actual_parser(self):
        payload = read_fixture.report(())
        data = payload['data']['biz_data']
        data['functions'] = ['synthetic-function']
        data['data'][0]['function_series'] = [{
            'api_key': {'tracking_id': read_fixture.KEY_A}, 'model': 'synthetic-function',
            'buckets': [{'time': data['start'], 'cost': '0.005'}],
        }]
        result, events, _reader = read_fixture.CollectionCoverageTests().collect(payload)
        self.assertEqual(result['reason_code'], 'OK')
        self.assertEqual(result['totals'], {read_fixture.KEY_A: Decimal('0.005')})
        self.assertEqual(result['coverage_summary']['matched_platform_count'], 1)
        fields = next(fields for event, fields in events if event == 'costs_verified')
        self.assertEqual(fields['bucket_count'], 0)
        self.assertEqual(fields['function_bucket_count'], 1)
        self.assertEqual(fields['function_bucket_first_time'], data['start'])

    def test_worker_routes_mixed_costs_without_falling_back_on_malformed_function(self):
        payload = read_fixture.report()
        data = payload['data']['biz_data']
        data['functions'] = ['synthetic-function']
        data['data'][0]['function_series'] = [{
            'api_key': {'tracking_id': read_fixture.KEY_A}, 'model': 'synthetic-function',
            'buckets': [{'time': data['start'], 'cost': '0.005'}],
        }]
        result, _events, _reader = read_fixture.CollectionCoverageTests().collect(payload)
        self.assertEqual(result['totals'], {read_fixture.KEY_A: Decimal('0.005012')})
        del data['data'][0]['function_series'][0]['buckets'][0]['cost']
        result, events, _reader = read_fixture.CollectionCoverageTests().collect(payload)
        self.assertIsNone(result['totals'])
        self.assertEqual(result['reason_code'], 'SCHEMA_UNSUPPORTED')
        self.assertNotIn('coverage_summary', result)
        self.assertFalse(any(event == 'cost_identity_coverage' for event, _fields in events))


if __name__ == '__main__':
    unittest.main()
