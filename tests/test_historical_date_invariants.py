"""Date/identity regressions using synthetic HTTP results and isolated ledgers.

No browser, account, real ledger, or network transport is used. The simulated
clock crosses 2026-11-02/03 to distinguish a calendar rule from data coverage.
"""
from contextlib import contextmanager, closing
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from quota_guard.browser_service import BrowserService, check_identity
from quota_guard.core import DataStore, GuardError, Person, SnapshotApplyError, TZ
from quota_guard.cost_parser import parse_cost_response
from quota_guard.platform_http import ConsoleCostReader, cost_url
from test_platform_http import Response


TARGET = '11111111-1111-4111-8111-111111111111'
OLD_TARGET = '22222222-2222-4222-8222-222222222222'
ACCOUNT = 'synthetic-stable-account'
NAME = 'Synthetic member'
KEYS = [{'name': NAME, 'tracking_id': TARGET}]


@contextmanager
def at_time(instant):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)

    with patch('quota_guard.browser_service.datetime', FixedDateTime), \
            patch('quota_guard.core.datetime', FixedDateTime):
        yield


def report(start, end, *, tracking=TARGET, amount='1.25', empty=False):
    url = cost_url(start, end)
    query = parse_qs(urlsplit(url).query)
    left, right = int(query['start'][0]), int(query['end'][0])
    series = [] if empty else [{
        'api_key': {'tracking_id': tracking, 'name': NAME},
        'model': 'synthetic-model',
        'buckets': [{'time': left, 'cost': amount}],
    }]
    payload = {'code': 0, 'data': {'biz_code': 0, 'biz_data': {
        'start': left, 'end': right, 'bucket': 86400,
        'data': [{'currency': 'CNY', 'series': series, 'function_series': []}],
    }}}
    return url, payload


class HistoricalDateInvariantsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.counter = 0

    def ledger(self, start):
        self.counter += 1
        store = DataStore(self.folder / ('ledger-' + str(self.counter)))
        store.initialize(1, '100', start)
        store.save_people([Person(NAME, Decimal('10'))])
        store.bind_keys(KEYS, ACCOUNT)
        return store

    def service(self, **report_options):
        reader = SimpleNamespace(prepare=Mock(), clear=Mock(),
            read=Mock(side_effect=lambda start, end: report(start, end, **report_options)))
        service = object.__new__(BrowserService)
        service._check_login_ready = Mock()
        service._keys = Mock(return_value=KEYS)
        service._stage = Mock()
        service._log = Mock()
        service.page = object()
        service._cost_reader = reader
        service._cost_reader_identity = ACCOUNT
        return service, reader

    def collect(self, service, start):
        return service._collect(start, account_id=ACCOUNT, known_keys={TARGET})

    def database_state(self, store):
        with closing(sqlite3.connect(store.db_path.as_uri() + '?mode=ro', uri=True)) as db:
            return {table: db.execute('SELECT * FROM ' + table + ' ORDER BY rowid').fetchall()
                    for table in ('meta', 'bindings', 'costs', 'snapshots', 'events')}

    def test_single_day_always_covers_utc_plus_eight_midnight_to_next_midnight(self):
        for day in ('2026-10-29', '2026-11-02', '2026-11-03'):
            with self.subTest(day=day):
                query = parse_qs(urlsplit(cost_url(day, day)).query)
                start, end = int(query['start'][0]), int(query['end'][0])
                self.assertEqual(end - start, 86400)
                self.assertEqual(query['tz'], ['28800'])
                self.assertEqual(datetime.fromtimestamp(start, TZ).isoformat(), day + 'T00:00:00+08:00')
                self.assertEqual(datetime.fromtimestamp(start, timezone.utc).hour, 16)
                self.assertEqual(datetime.fromtimestamp(end, TZ).hour, 0)

    def test_calendar_boundaries_include_each_selected_day_once(self):
        cases = (('2026-10-31', '2026-11-02', 3, '2026-11-03'),
                 ('2026-12-31', '2027-01-01', 2, '2027-01-02'),
                 ('2028-02-28', '2028-03-01', 3, '2028-03-02'))
        for start, end, days, exclusive_end in cases:
            with self.subTest(start=start, end=end):
                query = parse_qs(urlsplit(cost_url(start, end)).query)
                first, last = int(query['start'][0]), int(query['end'][0])
                self.assertEqual(last - first, days * 86400)
                self.assertEqual(datetime.fromtimestamp(last, TZ).date().isoformat(), exclusive_end)

    def test_changing_period_uses_same_authorization_but_sends_each_exact_query(self):
        transport = Mock(spec=['get'])
        transport.get.side_effect = lambda url, **_kwargs: Response(url, body=b'{"code":0}')
        reader = ConsoleCostReader(SimpleNamespace(request=transport))
        reader._observe(SimpleNamespace(method='GET', url=cost_url('2026-11-02', '2026-11-02'),
            header_value=lambda name: 'Bearer synthetic-date-session' if name == 'authorization' else None))
        periods = (('2026-10-29', '2026-11-02'), ('2026-11-02', '2026-11-02'),
                   ('2026-10-29', '2026-11-03'))
        for start, end in periods:
            reader.read(start, end)
        self.assertEqual(transport.get.call_count, 3)
        self.assertEqual([call.args[0] for call in transport.get.call_args_list],
                         [cost_url(start, end) for start, end in periods])
        self.assertEqual({call.kwargs['headers']['Authorization'] for call in transport.get.call_args_list},
                         {'Bearer synthetic-date-session'})

    def test_utc_plus_eight_midnight_changes_query_end_without_changing_identity(self):
        service, reader = self.service()
        with at_time(datetime(2026, 11, 2, 23, 59, tzinfo=TZ)):
            before = self.collect(service, '2026-10-29')
        # UTC is still November 2 here; the application's business day is Nov 3.
        with at_time(datetime(2026, 11, 3, 0, 30, tzinfo=TZ)):
            after = self.collect(service, '2026-10-29')
        self.assertEqual((before['end_date'], after['end_date']), ('2026-11-02', '2026-11-03'))
        self.assertEqual(before['account_id'], after['account_id'])
        self.assertEqual(before['keys'], after['keys'])
        self.assertEqual(before['totals'], after['totals'])
        self.assertNotEqual(before['source_id'], after['source_id'])
        self.assertEqual([call.args for call in reader.read.call_args_list],
                         [('2026-10-29', '2026-11-02'), ('2026-10-29', '2026-11-03')])
        reader.clear.assert_not_called()

    def test_nonempty_matching_fees_apply_for_history_and_today_on_both_simulated_days(self):
        for day in (2, 3):
            today = f'2026-11-{day:02d}'
            for start in ('2026-10-29', today):
                with self.subTest(today=today, start=start), at_time(datetime(2026, 11, day, 12, tzinfo=TZ)):
                    store = self.ledger(start)
                    service, _ = self.service()
                    observation = self.collect(service, start)
                    result = store.apply_snapshot_result(observation['totals'], observation['source_id'],
                                                         observation['start_date'], observation['end_date'])
                    self.assertTrue(result.fees_current)
                    self.assertEqual(result.rows[0]['spent'], Decimal('1.25'))
                    self.assertTrue(result.rows[0]['complete'])
                    self.assertEqual(store.load().account_id, ACCOUNT)
                    self.assertEqual(store.active_tracking_ids(), {TARGET})

    def test_changing_start_creates_new_period_with_same_identity_and_no_inherited_fees(self):
        with at_time(datetime(2026, 11, 3, 12, tzinfo=TZ)):
            original = self.ledger('2026-10-29')
            original.apply_snapshot_result({TARGET: Decimal('2')}, 'historical-source', '2026-10-29', '2026-11-02')
            before = self.database_state(original)
            successor = original.update_start_date('2026-11-03', self.folder / 'successor')
            self.assertEqual(successor.load().account_id, ACCOUNT)
            self.assertEqual(successor.active_tracking_ids(), {TARGET})
            self.assertEqual(check_identity(KEYS, successor.load().account_id, successor.active_tracking_ids()), ACCOUNT)
            self.assertIsNone(successor.read_evaluation()[0]['spent'])
            self.assertEqual(successor.current_snapshot_id(), '')
            accepted = successor.apply_snapshot_result({TARGET: Decimal('1')}, 'new-period-source',
                                                       '2026-11-03', '2026-11-03')
            self.assertEqual(accepted.rows[0]['spent'], Decimal('1'))
            self.assertEqual(self.database_state(original), before)

    def test_empty_history_and_today_remain_unknown_without_inventing_a_snapshot(self):
        with at_time(datetime(2026, 11, 3, 12, tzinfo=TZ)):
            for start in ('2026-10-29', '2026-11-03'):
                with self.subTest(start=start):
                    store = self.ledger(start)
                    before = self.database_state(store)
                    service, _ = self.service(empty=True)
                    observation = self.collect(service, start)
                    self.assertEqual(observation['reason_code'], 'NO_USAGE_DATA')
                    self.assertIsNone(observation['totals'])
                    self.assertEqual(observation['source_id'], '')
                    self.assertEqual(observation['account_id'], ACCOUNT)
                    self.assertIsNone(store.read_evaluation()[0]['spent'])
                    self.assertEqual(self.database_state(store), before)

    def test_historical_costs_for_same_name_old_id_cannot_replace_new_id_amount(self):
        with at_time(datetime(2026, 11, 3, 12, tzinfo=TZ)):
            store = self.ledger('2026-10-29')
            store.apply_snapshot_result({TARGET: Decimal('1')}, 'accepted-source', '2026-10-29', '2026-11-02')
            before = self.database_state(store)
            url, payload = report('2026-10-29', '2026-11-03', tracking=OLD_TARGET, amount='9')
            totals = parse_cost_response(payload, url, '2026-10-29', '2026-11-03')
            self.assertEqual(totals, {OLD_TARGET: Decimal('9')})
            with self.assertRaises(SnapshotApplyError) as caught:
                store.apply_snapshot_result(totals, 'old-generation-source', '2026-10-29', '2026-11-03')
            self.assertEqual(caught.exception.reason_code, 'NO_BOUND_COST_MATCH')
            self.assertEqual(caught.exception.persistence_state, 'not_written')
            self.assertEqual(self.database_state(store), before)

    def test_same_name_new_generation_cannot_silently_replace_binding_on_a_new_day(self):
        with at_time(datetime(2026, 11, 2, 12, tzinfo=TZ)):
            store = self.ledger('2026-10-29')
        before = self.database_state(store)
        with at_time(datetime(2026, 11, 3, 12, tzinfo=TZ)):
            with self.assertRaises(GuardError):
                store.bind_keys([{'name': NAME, 'tracking_id': OLD_TARGET}], ACCOUNT)
        self.assertEqual(self.database_state(store), before)

    def test_period_mismatch_is_rejected_separately_from_account_identity(self):
        with at_time(datetime(2026, 11, 3, 12, tzinfo=TZ)):
            store = self.ledger('2026-10-29')
            before = self.database_state(store)
            with self.assertRaises(SnapshotApplyError) as caught:
                store.apply_snapshot_result({TARGET: Decimal('1')}, 'wrong-period', '2026-11-03', '2026-11-03')
            self.assertEqual(caught.exception.reason_code, 'PERIOD_CONFIG_MISMATCH')
            self.assertEqual(store.load().account_id, ACCOUNT)
            self.assertEqual(self.database_state(store), before)

    def test_model_dimension_does_not_replace_nested_api_key_tracking_id(self):
        url, payload = report('2026-10-29', '2026-11-02')
        series = payload['data']['biz_data']['data'][0]['series']
        series[0]['model'] = OLD_TARGET
        totals = parse_cost_response(payload, url, '2026-10-29', '2026-11-02')
        self.assertEqual(totals, {TARGET: Decimal('1.25')})
        self.assertNotIn(OLD_TARGET, totals)


if __name__ == '__main__':
    unittest.main()
