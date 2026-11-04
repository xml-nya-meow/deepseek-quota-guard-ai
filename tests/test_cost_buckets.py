"""Synthetic regression cases for complete, precisely aggregated cost reports."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from quota_guard.browser_service import parse_cost_response
from quota_guard.core import GuardError


TZ = timezone(timedelta(hours=8))
DAY = 86400
KEY = '10000000-0000-4000-8000-000000000001'


class CostBucketTests(unittest.TestCase):
    def report(self, *, days=1, bucket='1d'):
        start = int(datetime(2026, 10, 1, tzinfo=TZ).timestamp())
        return {'code': 0, 'data': {'biz_code': 0, 'biz_data': {
            'start': start, 'end': start + days * DAY, 'bucket': bucket,
            'models': ['synthetic-model'], 'functions': [], 'data': [{
                'currency': 'CNY', 'function_series': [], 'series': [{
                    'api_key': {'tracking_id': KEY, 'name': 'Synthetic Person',
                                'sensitive_id': 'synthetic-masked-id',
                                'valid': True, 'key_type': 'NORMAL'},
                    'model': 'synthetic-model',
                    'buckets': [{'time': start + day * DAY, 'cost': '0'}
                                for day in range(days)],
                }],
            }],
        }}}

    def parse(self, payload, start_date='2026-10-01', end_date='2026-10-01'):
        data = payload['data']['biz_data']
        url = ('https://platform.deepseek.com/api/v0/usage/by_api_key/cost'
               f"?start={data['start']}&end={data['end']}&tz=28800")
        return parse_cost_response(payload, url, start_date, end_date)

    def test_numeric_and_legacy_daily_buckets_use_utc8_midnight(self):
        """Accept local-midnight daily buckets, without assuming UTC midnight."""
        for granularity in (86400, '1d'):
            with self.subTest(bucket=granularity):
                payload = self.report(bucket=granularity)
                series = payload['data']['biz_data']['data'][0]['series'][0]
                self.assertNotEqual(series['buckets'][0]['time'] % DAY, 0)
                series['buckets'][0]['cost'] = '0.0000120000000000'
                self.assertEqual(self.parse(payload), {KEY: Decimal('0.0000120000000000')})

    def test_numeric_and_legacy_hourly_buckets_include_last_hour(self):
        """Count all valid hours, including the final complete hour of the day."""
        for granularity in (3600, '1h'):
            with self.subTest(bucket=granularity):
                payload = self.report(bucket=granularity)
                data = payload['data']['biz_data']
                buckets = [{'time': data['start'] + hour * 3600, 'cost': '0'}
                           for hour in range(24)]
                buckets[0]['cost'], buckets[-1]['cost'] = '0.1', '0.2'
                data['data'][0]['series'][0]['buckets'] = buckets
                self.assertEqual(self.parse(payload), {KEY: Decimal('0.3')})

    def test_unknown_or_noninteger_granularity_is_rejected(self):
        """Do not reinterpret a changed bucket unit or Python numeric equality."""
        for granularity in (True, False, 86400.0, 3600.0, Decimal('86400'),
                            0, 60, 86401, '86400', '3600', '1w', None, {}, []):
            with self.subTest(bucket=repr(granularity)):
                with self.assertRaises(GuardError):
                    self.parse(self.report(bucket=granularity))

    def test_misaligned_bucket_is_rejected(self):
        """A timestamp inside the range cannot stand for a shifted billing day."""
        for granularity, offset in (('1d', 1), ('1d', 8 * 3600), ('1h', 1800)):
            with self.subTest(bucket=granularity, offset=offset):
                payload = self.report(bucket=granularity)
                payload['data']['biz_data']['data'][0]['series'][0]['buckets'][0]['time'] += offset
                with self.assertRaises(GuardError):
                    self.parse(payload)

    def test_bucket_timestamp_must_be_integer(self):
        """Reject changed timestamp types instead of accepting float equality."""
        for stamp in (True, False, 1790784000.0, Decimal('1790784000'), '1790784000'):
            with self.subTest(timestamp=repr(stamp)):
                payload = self.report()
                payload['data']['biz_data']['data'][0]['series'][0]['buckets'][0]['time'] = stamp
                with self.assertRaises(GuardError):
                    self.parse(payload)

    def test_bucket_outside_response_is_rejected(self):
        """Reject both a pre-range bucket and the exclusive ending boundary."""
        for offset in (-DAY, DAY):
            with self.subTest(offset=offset):
                payload = self.report()
                payload['data']['biz_data']['data'][0]['series'][0]['buckets'][0]['time'] += offset
                with self.assertRaises(GuardError):
                    self.parse(payload)

    def test_bucket_must_finish_inside_response(self):
        """An extra partial bucket cannot be silently ignored as out of scope."""
        payload = self.report()
        data = payload['data']['biz_data']
        data['data'][0]['series'][0]['buckets'].append({'time': data['end'], 'cost': '9'})
        data['end'] += DAY // 2
        with self.assertRaises(GuardError):
            self.parse(payload)

    def test_response_must_cover_entire_selected_period(self):
        """A nearly complete response must not replace full-period spending."""
        payload = self.report()
        payload['data']['biz_data']['end'] -= 1
        with self.assertRaises(GuardError):
            self.parse(payload)

    def test_nonempty_or_nonlist_function_series_is_rejected(self):
        """Unknown function charges must not disappear behind ordinary costs."""
        for functions in ([{'synthetic_function': 'unknown-shape'}], {}, '', None, False, 0):
            with self.subTest(function_series=repr(functions)):
                payload = self.report()
                payload['data']['biz_data']['data'][0]['function_series'] = functions
                with self.assertRaises(GuardError):
                    self.parse(payload)

    def test_legacy_report_without_function_series_remains_supported(self):
        """Retain the already supported response shape with no function field."""
        payload = self.report()
        del payload['data']['biz_data']['data'][0]['function_series']
        self.assertEqual(self.parse(payload), {KEY: Decimal(0)})

    def test_nonlist_series_cannot_hide_behind_valid_group(self):
        """An empty wrong-type group must not yield an apparently complete sum."""
        for series in ({}, '', None, False, 0):
            with self.subTest(series=repr(series)):
                payload = self.report()
                payload['data']['biz_data']['data'].append({
                    'currency': 'CNY', 'series': series, 'function_series': []})
                with self.assertRaises(GuardError):
                    self.parse(payload)

    def test_nonlist_buckets_cannot_hide_behind_valid_model(self):
        """Do not lose a model's costs by iterating an empty wrong-type value."""
        for buckets in ({}, '', None, False, 0):
            with self.subTest(buckets=repr(buckets)):
                payload = self.report()
                series = payload['data']['biz_data']['data'][0]['series']
                extra = deepcopy(series[0])
                extra.update(model='synthetic-second-model', buckets=buckets)
                series.append(extra)
                with self.assertRaises(GuardError):
                    self.parse(payload)

    def test_thirty_day_report_preserves_two_tiny_daily_charges(self):
        """Explicit zero days must not round away two small nonzero charges."""
        payload = self.report(days=30, bucket=86400)
        buckets = payload['data']['biz_data']['data'][0]['series'][0]['buckets']
        for day in (28, 29):
            buckets[day]['cost'] = '0.0000060000000000'
        totals = self.parse(payload, end_date='2026-10-30')
        self.assertEqual(totals, {KEY: Decimal('0.0000120000000000')})
        self.assertIsInstance(totals[KEY], Decimal)

    def test_selected_two_days_exclude_other_days_in_covering_response(self):
        """A larger valid response cannot add charges outside the chosen dates."""
        payload = self.report(days=30, bucket=86400)
        buckets = payload['data']['biz_data']['data'][0]['series'][0]['buckets']
        buckets[0]['cost'] = '100'
        buckets[27]['cost'] = '200'
        buckets[28]['cost'] = '0.0000060000000000'
        buckets[29]['cost'] = '0.0000060000000000'
        self.assertEqual(self.parse(payload, '2026-10-29', '2026-10-30'),
                         {KEY: Decimal('0.0000120000000000')})

    def test_same_key_multiple_models_are_added(self):
        """Distinct model charges at one timestamp must both count for the Key."""
        payload = self.report(bucket=86400)
        data = payload['data']['biz_data']
        series = data['data'][0]['series']
        series[0]['buckets'][0]['cost'] = '0.0000070000000000'
        extra = deepcopy(series[0])
        extra['model'] = 'synthetic-second-model'
        extra['buckets'][0]['cost'] = '0.0000050000000000'
        data['models'].append(extra['model'])
        series.append(extra)
        self.assertEqual(self.parse(payload), {KEY: Decimal('0.0000120000000000')})


if __name__ == '__main__':
    unittest.main()
