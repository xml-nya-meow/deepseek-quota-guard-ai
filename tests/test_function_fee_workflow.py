"""Real parser -> GUI -> ledger/permit integration using synthetic function fees.

All ledgers are temporary and every browser operation is a test double. A fake
revoke waits for the real durable UI permit; no platform requests are issued.
"""
from concurrent.futures import Future
from decimal import Decimal
from hashlib import sha256
import json
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit

import test_workflow as fixtures
from quota_guard.core import GuardError, Person
from quota_guard.cost_parser import parse_cost_response
from quota_guard.platform_http import CostReadError, cost_url


class FunctionFeeWorkflowTests(unittest.TestCase):
    tearDown = fixtures.WorkflowTests.tearDown
    pump = fixtures.WorkflowTests.pump
    valid_collection = fixtures.WorkflowTests.valid_collection

    def setUp(self):
        fixtures.WorkflowTests.setUp(self)
        self.store.save_people([Person('测试甲', Decimal('0.01')), Person('测试乙', Decimal('80'))])
        self.app.load_folder()

    def payload(self, *, model_cost=None, function_cost=None, function_key='test-key-a'):
        query = parse_qs(urlsplit(cost_url(self.start, self.start)).query)
        first, last = int(query['start'][0]), int(query['end'][0])

        def leaf(amount, key, category):
            return {'api_key': {'tracking_id': key, 'name': '测试甲'},
                    'model': category, 'buckets': [{'time': first, 'cost': amount}]}

        return {'code': 0, 'data': {'biz_code': 0, 'biz_data': {
            'start': first, 'end': last, 'bucket': '1d',
            'data': [{'currency': 'CNY',
                      'series': [] if model_cost is None else [leaf(model_cost, 'test-key-a', 'synthetic-model')],
                      'function_series': [] if function_cost is None else [leaf(function_cost, function_key, 'synthetic-search')]}],
        }}}

    def observation(self, payload):
        totals = parse_cost_response(payload, cost_url(self.start, self.start), self.start, self.start)
        source = sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        return self.valid_collection(totals=totals, source_id=source, reason_code='OK')

    def read(self, observation, browser=None):
        browser = browser or fixtures.OfflineBrowser({'collect': observation})
        self.app.browser = browser
        self.app.collect()
        self.pump()
        return browser

    def table_rows(self, table):
        with self.store._connect() as db:
            return db.execute('SELECT * FROM ' + table + ' ORDER BY rowid').fetchall()

    def test_model_plus_function_crosses_positive_quota_and_grants_only_after_durable_request(self):
        observation = self.observation(self.payload(model_cost='0.006', function_cost='0.005'))
        self.assertEqual(observation['totals'], {'test-key-a': Decimal('0.011')})
        browser = fixtures.OfflineBrowser({'collect': observation})
        ordinary_submit = browser.submit
        observed, workers, permits = [], [], []

        def submit(action, **kwargs):
            if action != 'revoke':
                return ordinary_submit(action, **kwargs)
            browser.calls.append((action, kwargs))
            future = Future()
            permit = kwargs['permit']
            permits.append(permit)

            def worker():
                try:
                    permit.ready.set()
                    if not permit.decided.wait(3):
                        raise RuntimeError('Synthetic worker received no decision')
                    record = self.store.latest_action_record('test-key-a')
                    observed.append({'allowed': permit.allowed, 'status': record['status'],
                                     'spent': record['evidence']['spent'],
                                     'snapshot': record['evidence']['snapshot_id'],
                                     'consumed': bool(permit.consume_for_click())})
                    if not observed[-1]['allowed'] or not observed[-1]['consumed']:
                        raise RuntimeError('Synthetic revoke was not authorized')
                    future.set_result({'absent': True})
                except Exception as exc:
                    future.set_exception(exc)

            thread = threading.Thread(target=worker, daemon=True)
            workers.append(thread)
            thread.start()
            return future

        browser.submit = submit
        try:
            self.read(observation, browser)
            row = next(row for row in self.app.rows if row['tracking_id'] == 'test-key-a')
            self.assertEqual(row['spent'], Decimal('0.011'))
            self.assertEqual(row['status'], 'triggered')
            self.assertTrue(row['complete'])
            self.app.start()
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                self.pump()
                if self.store.latest_action('test-key-a') == 'absent_verified':
                    break
            self.assertEqual(observed, [{'allowed': True, 'status': 'requested', 'spent': '0.011',
                                         'snapshot': observation['source_id'], 'consumed': True}])
            self.assertEqual([action for action, _ in browser.calls], ['collect', 'revoke'])
            self.assertEqual(self.store.latest_action('test-key-a'), 'absent_verified')
            self.assertIsNone(self.store.latest_action('test-key-b'))
        finally:
            for permit in permits:
                permit.decide(False)
            for worker in workers:
                worker.join(3)

    def test_function_only_observations_are_valid_and_duplicate_cumulative_reads_do_not_add(self):
        payload = self.payload(function_cost='0.005')
        first = self.observation(payload)
        self.assertEqual(first['totals'], {'test-key-a': Decimal('0.005')})
        self.read(first)
        before = self.table_rows('costs')
        self.read(self.observation(payload))
        self.assertTrue(self.app.collection_has_fees)
        self.assertTrue(self.app._collection_is_current())
        row = next(row for row in self.app.rows if row['tracking_id'] == 'test-key-a')
        self.assertEqual(row['spent'], Decimal('0.005'))
        self.assertEqual(row['status'], 'ok')
        self.assertEqual(self.table_rows('costs'), before)
        self.assertEqual(len(self.table_rows('snapshots')), 1)
        self.assertEqual(len([event for event in self.store.events() if event['kind'] == 'snapshot']), 1)
        self.assertFalse(any(event['kind'] == 'action' for event in self.store.events()))

    def test_malformed_function_fee_rejects_model_subtotal_and_revokes_old_positive_authority(self):
        # Leave an older, above-quota amount visible. A failed new read must not
        # use either that amount or the otherwise valid model subtotal to act.
        self.read(self.observation(self.payload(model_cost='0.02')))
        costs, snapshots = self.table_rows('costs'), self.table_rows('snapshots')
        with self.assertRaises(CostReadError) as caught:
            self.observation(self.payload(model_cost='0.03', function_cost='not-an-amount'))
        self.assertEqual(caught.exception.reason_code, 'AMOUNT_INVALID')
        rejected = self.valid_collection(totals=None, source_id='',
            reason_code=caught.exception.reason_code, retryable=False, message=str(caught.exception))
        browser = self.read(rejected)
        self.assertTrue(self.app.session_ready)
        self.assertFalse(self.app.collection_has_fees)
        self.assertEqual(self.table_rows('costs'), costs)
        self.assertEqual(self.table_rows('snapshots'), snapshots)
        row = next(row for row in self.app.rows if row['tracking_id'] == 'test-key-a')
        self.assertEqual(row['spent'], Decimal('0.02'))
        with self.assertRaises(GuardError):
            self.app._current_revoke_target(row)
        self.app.start()
        self.pump()
        self.assertEqual([action for action, _ in browser.calls], ['collect'])
        self.assertFalse(any(event['kind'] == 'action' for event in self.store.events()))
        self.assertIsNone(self.app.delete_permit)

    def test_same_name_other_tracking_function_fees_never_belong_to_current_target(self):
        observation = self.observation(self.payload(function_cost='0.04', function_key='old-test-key-a'))
        self.assertEqual(observation['totals'], {'old-test-key-a': Decimal('0.04')})
        browser = self.read(observation)
        self.assertTrue(self.app.session_ready)
        self.assertFalse(self.app.collection_has_fees)
        self.assertEqual(self.table_rows('costs'), [])
        self.assertEqual(self.table_rows('snapshots'), [])
        self.assertEqual(self.store.active_tracking_ids(), {'test-key-a', 'test-key-b'})
        self.assertIn('费用与绑定不匹配', self.app.action_states['collect'])
        self.app.start()
        self.pump()
        self.assertEqual([action for action, _ in browser.calls], ['collect'])
        self.assertFalse(any(event['kind'] == 'action' for event in self.store.events()))


if __name__ == '__main__':
    unittest.main()
