"""Requested roster -> repeated HTTP filters -> strict results -> real ledger.

All HTTP is mocked or routed to synthetic responses; no real platform access.
"""
from decimal import Decimal
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

import test_collection as collection
import test_workflow as workflow
from quota_guard.core import GuardError, Person
from quota_guard.cost_parser import parse_cost_response
from quota_guard.platform_http import ConsoleCostReader, CostReadError, cost_url, KEY_LIST_URL
from test_platform_http import Response
from test_key_table import TARGET, SURVIVOR, document, key_row


class ScopedCostContractTests(unittest.TestCase):
    def test_official_repeated_filter_has_only_explicit_ids(self):
        query = parse_qs(urlparse(cost_url('2026-10-29', '2026-10-30',
                                         tracking_ids=[SURVIVOR, TARGET])).query)
        self.assertEqual(set(query), {'start', 'end', 'tz', 'api_key_tracking_id'})
        self.assertEqual(query['api_key_tracking_id'], sorted([TARGET, SURVIVOR]))

    def test_invalid_or_empty_scope_cannot_fall_back_to_account_query(self):
        for scope in ([], (), '', [TARGET, TARGET], [True], ['sk-' + 'a' * 32],
                      [TARGET + '&another=value'], None):
            if scope is None:
                continue  # None is the explicitly retained legacy adapter API.
            with self.subTest(scope_type=type(scope).__name__), self.assertRaises(CostReadError):
                cost_url('2026-10-29', '2026-10-30', tracking_ids=scope)

    def test_transport_rejects_empty_scope_before_get(self):
        api = Mock()
        reader = ConsoleCostReader(SimpleNamespace(request=api))
        reader._authorization = 'Bearer synthetic-session'
        with self.assertRaises(CostReadError):
            reader.read('2026-10-29', '2026-10-30', tracking_ids=[])
        api.get.assert_not_called()

    def test_parser_rejects_missing_changed_or_duplicate_request_scope(self):
        payload = collection.cost_payload(collection.START, '1', '2')
        for url in (cost_url('2026-10-29', '2026-10-30'),
                    cost_url('2026-10-29', '2026-10-30', tracking_ids=[SURVIVOR]),
                    cost_url('2026-10-29', '2026-10-30', tracking_ids=[TARGET])
                    + '&api_key_tracking_id=' + TARGET):
            with self.subTest(url_shape=len(url)), self.assertRaises(CostReadError) as caught:
                parse_cost_response(payload, url, '2026-10-29', '2026-10-30', tracking_ids=[TARGET])
            self.assertEqual(caught.exception.reason_code, 'COST_SCOPE_MISMATCH')

    def test_out_of_scope_model_and_function_never_enter_totals(self):
        for category in ('series', 'function_series'):
            payload = collection.cost_payload(collection.START, '1', '999')
            group = payload['data']['biz_data']['data'][0]
            foreign = group['series'].pop()
            group[category].append(foreign)
            url = cost_url('2026-10-29', '2026-10-30', tracking_ids=[TARGET])
            with self.subTest(category=category), self.assertRaises(CostReadError) as caught:
                parse_cost_response(payload, url, '2026-10-29', '2026-10-30', tracking_ids=[TARGET])
            self.assertEqual(caught.exception.reason_code, 'COST_SCOPE_MISMATCH')

    def test_scoped_bootstrap_accepts_only_exact_console_list_get(self):
        reader = ConsoleCostReader(SimpleNamespace(request=Mock()))
        for url, method, bearer in ((KEY_LIST_URL + '?x=1', 'GET', 'Bearer synthetic'),
                                     (KEY_LIST_URL, 'POST', 'Bearer synthetic'),
                                     (KEY_LIST_URL, 'GET', 'Bearer sk-synthetic-model')):
            reader._observe_key_list(SimpleNamespace(url=url, method=method,
                                                      header_value=lambda _name: bearer))
            self.assertIsNone(reader._authorization)
        reader._observe_key_list(SimpleNamespace(url=KEY_LIST_URL, method='GET',
                                                 header_value=lambda _name: 'Bearer synthetic'))
        self.assertEqual(reader._authorization, 'Bearer synthetic')


class ScopedBrowserCollectionTests(unittest.TestCase):
    setUpClass = classmethod(collection.CollectionTests.setUpClass.__func__)
    tearDownClass = classmethod(collection.CollectionTests.tearDownClass.__func__)
    setUp = collection.CollectionTests.setUp
    tearDown = collection.CollectionTests.tearDown

    def route_request(self, route):
        parsed = urlparse(route.request.url)
        if route.request.url == KEY_LIST_URL:
            route.fulfill(status=200, content_type='application/json', body='{}')
        elif parsed.path == '/api_keys':
            self.key_visits += 1
            markup = document(key_row() + key_row('Fixture survivor', SURVIVOR))
            markup += '<script>fetch(' + json.dumps(KEY_LIST_URL) + ', {headers:{Authorization:"Bearer synthetic-session"}}).catch(()=>{});</script>'
            route.fulfill(status=200, content_type='text/html; charset=utf-8', body=markup)
        else:
            collection.CollectionTests.route_request(self, route)

    def http_get(self, url, **kwargs):
        self.http_calls.append((url, kwargs))
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query['api_key_tracking_id'], sorted(self.expected_scope))
        self.assertEqual(set(query), {'start', 'end', 'tz', 'api_key_tracking_id'})
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer synthetic-session')
        self.assertEqual(kwargs['max_retries'], 0)
        return Response(url, body=json.dumps(self.selected_payload).encode())

    def read(self, names=('Fixture target',), ids=(TARGET,)):
        self.expected_scope = ids
        with patch('quota_guard.browser_service.datetime', collection.FixedDateTime):
            result = self.service._collect(collection.START_DATE, account_id='fixture-local-account',
                                           known_keys={TARGET, SURVIVOR}, managed_names=list(names))
        self.assertEqual(self.usage_visits, 0, 'Scoped bootstrap must not open global usage page')
        self.assertEqual(self.blocked_requests, [])
        return result

    def test_single_selected_key_uses_filtered_get_and_no_global_bootstrap(self):
        self.selected_payload['data']['biz_data']['data'][0]['series'].pop()
        result = self.read()
        self.assertEqual(result['requested_tracking_ids'], [TARGET])
        self.assertEqual(result['totals'], {TARGET: Decimal('42.000001')})
        self.assertEqual(result['keys'], self.keys)  # identity continuity only
        self.assertEqual(len(self.http_calls), 1)

    def test_multiple_selected_keys_share_one_filtered_get(self):
        result = self.read(('Fixture target', 'Fixture survivor'), (TARGET, SURVIVOR))
        self.assertEqual(set(result['totals']), {TARGET, SURVIVOR})
        self.assertEqual(len(self.http_calls), 1)

    def test_empty_selected_reply_is_unknown_and_retryable_not_binding_failure(self):
        self.selected_payload['data']['biz_data']['data'][0]['series'] = []
        result = self.read()
        self.assertIsNone(result['totals'])
        self.assertEqual(result['reason_code'], 'NO_SELECTED_USAGE_DATA')
        self.assertTrue(result['retryable'])
        self.assertEqual(len(self.http_calls), 1)

    def test_server_ignoring_filter_does_not_retry_account_wide(self):
        result = self.read()
        self.assertIsNone(result['totals'])
        self.assertEqual(result['reason_code'], 'COST_SCOPE_MISMATCH')
        self.assertFalse(result['retryable'])
        self.assertEqual(len(self.http_calls), 1)

    def test_absent_targets_send_no_cost_request(self):
        result = self.read(('Previously retired target',), ())
        self.assertEqual(result['reason_code'], 'NO_MANAGED_KEYS')
        self.assertEqual(self.http_calls, [])

    def test_explicit_empty_roster_never_reads_identity_or_cost(self):
        with patch.object(self.service, '_check_login_ready') as login, self.assertRaises(GuardError):
            self.service._collect(collection.START_DATE, managed_names=[])
        login.assert_not_called()
        self.assertEqual(self.http_calls, [])


class ScopedGuiCollectionTests(unittest.TestCase):
    setUp = workflow.WorkflowTests.setUp
    tearDown = workflow.WorkflowTests.tearDown
    pump = workflow.WorkflowTests.pump
    valid_collection = workflow.WorkflowTests.valid_collection

    def configure(self, people):
        self.store.save_people(people, allow_unassigned=True)
        self.app.load_folder()

    def read(self, result):
        self.app.browser = workflow.OfflineBrowser({'collect': result})
        self.app.collect()
        self.pump()

    def test_only_named_nonnegative_rows_are_sent_to_worker(self):
        self.configure([Person('测试甲', Decimal(0)), Person('测试乙', Decimal(-1)), Person('', Decimal(10))])
        self.read(self.valid_collection(totals={'test-key-a': Decimal('0.03')},
                                       requested_tracking_ids=['test-key-a']))
        self.assertEqual(self.app.browser.calls[0][1]['managed_names'], ['测试甲'])
        with self.store._connect() as db:
            self.assertEqual(list(db.execute('SELECT tracking_id FROM costs')), [('test-key-a',)])
        self.assertEqual(self.app.action_states['collect'], '成功')

    def test_no_managed_rows_skip_browser_even_with_blank_positive_rows(self):
        self.configure([Person('测试甲', Decimal(-1)), Person('', Decimal(0))])
        self.read(self.valid_collection())
        self.assertEqual(self.app.browser.calls, [])
        self.assertEqual(self.app.collection_reason_code, 'NO_MANAGED_KEYS')
        self.assertFalse(self.app.observing)

    def test_previously_bound_minus_one_keeps_history_without_refreshing_its_cost(self):
        self.store.bind_keys(self.keys, self.valid_collection()['account_id'])
        self.store.apply_snapshot({'test-key-a': Decimal(1), 'test-key-b': Decimal(5)},
                                   'prior', self.start, self.start)
        self.configure([Person('测试甲', Decimal(0)), Person('测试乙', Decimal(-1))])
        self.read(self.valid_collection(totals={'test-key-a': Decimal(2)},
                                       requested_tracking_ids=['test-key-a']))
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT amount,source_id FROM costs WHERE tracking_id=?',
                                        ('test-key-b',)).fetchone(), ('5', 'prior'))

    def test_selected_empty_allows_only_existing_zero_rule_and_no_snapshot(self):
        self.configure([Person('测试甲', Decimal(0)), Person('测试乙', Decimal(-1))])
        self.read(self.valid_collection(totals=None, requested_tracking_ids=['test-key-a'],
                                       reason_code='NO_SELECTED_USAGE_DATA', retryable=True))
        self.assertTrue(self.app._collection_is_current())
        self.assertFalse(self.app.collection_has_fees)
        self.assertTrue(self.app.session_ready)
        self.assertTrue(self.app.retry_collection)
        self.assertIn('不认定绑定错误', self.app.collection_summary_var.get())
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0], 0)
        self.assertEqual(self.app.rows[0]['status'], 'triggered')
        self.assertIsNone(self.app.rows[0]['spent'])

    def test_selected_empty_positive_limit_never_becomes_zero_or_delete(self):
        self.configure([Person('测试甲', Decimal(1)), Person('测试乙', Decimal(-1))])
        self.read(self.valid_collection(totals=None, requested_tracking_ids=['test-key-a'],
                                       reason_code='NO_SELECTED_USAGE_DATA', retryable=True))
        self.assertEqual(self.app.rows[0]['status'], 'unknown')
        self.assertIsNone(self.app.rows[0]['spent'])
        self.assertIsNone(self.app.delete_permit)

    def test_worker_cannot_add_skipped_key_to_confirmed_scope(self):
        self.configure([Person('测试甲', Decimal(0)), Person('测试乙', Decimal(-1))])
        self.read(self.valid_collection(requested_tracking_ids=['test-key-a', 'test-key-b']))
        with self.store._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM costs').fetchone()[0], 0)
        self.assertEqual(self.app.action_states['collect'], '读取期间设置已变化')
        self.assertFalse(self.app._collection_is_current())


if __name__ == '__main__':
    unittest.main()
