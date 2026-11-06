"""Sparse roster -> scoped HTTP -> real ledger/Tk poll -> exact fake deletion.

Only session readiness and transport are replaced; no platform network access.
"""
from datetime import datetime, timedelta
from decimal import Decimal
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

import test_workflow as workflow
from test_platform_http import Response
from quota_guard.browser_service import BrowserService, key_fingerprint
from quota_guard.core import Person, TZ
from quota_guard.platform_http import ConsoleCostReader


IDS = {name: f'00000000-0000-0000-0000-{index:012d}'
       for index, name in enumerate(('a', 'b', 'c', 'skipped', 'unlisted'), 1)}


class SparseManagedRowsTests(unittest.TestCase):
    setUp = workflow.WorkflowTests.setUp
    tearDown = workflow.WorkflowTests.tearDown
    pump = workflow.WorkflowTests.pump

    def configure_sparse_roster(self):
        self.people = ([Person('a', Decimal(1)), Person('b', Decimal(1))]
                       + [Person('', Decimal(-1)) for _ in range(11)]
                       + [Person('c', Decimal('0.2'))])
        self.assertEqual(len(self.people), 14)
        self.store.save_people(self.people, allow_unassigned=True)
        self.app.load_folder()
        saved_people = self.store.read_people()
        self.assertEqual(saved_people, self.people)
        self.assertEqual([(person.name, person.amount) for person in saved_people[2:-1]],
                         [('', Decimal(-1))] * 11)
        # Platform order deliberately differs from workbook order. c is first
        # remotely and last locally; skipped/unlisted entries stay identity only.
        self.keys = [{'name': name, 'tracking_id': IDS[name]}
                     for name in ('c', 'skipped', 'b', 'unlisted', 'a')]
        self.browser = workflow.ControlledBrowser()
        self.app.browser = self.browser
        self.http_queries = []

    def assert_empty_rows_are_unmanaged(self):
        empty_rows = self.app.rows[2:-1]
        self.assertEqual(len(empty_rows), 11)
        share = self.store.load().share
        self.assertGreater(share, 0)
        for row in empty_rows:
            self.assertEqual(row['name'], '')
            self.assertEqual(row['amount'], Decimal(-1))
            self.assertEqual(row['status'], 'unassigned')
            self.assertEqual(row['tracking_id'], '')
            self.assertIsNone(row['spent'])
            self.assertEqual(row['share'], share)

    def complete_scoped_read(self, c_amount):
        action, args = self.browser.calls[-1]
        self.assertEqual(action, 'collect')
        self.assertEqual(args['managed_names'], ['a', 'b', 'c'])
        start = datetime.fromisoformat(self.start).replace(tzinfo=TZ)
        totals = {IDS['b']: Decimal('0.02'), IDS['c']: c_amount, IDS['a']: Decimal('0.01')}
        payload = {'code': 0, 'data': {'biz_code': 0, 'biz_data': {
            'start': int(start.timestamp()),
            'end': int((start + timedelta(days=1)).timestamp()),
            'bucket': 86400,
            'data': [{'currency': 'CNY', 'series': [
                {'api_key': {'tracking_id': tracking}, 'model': 'synthetic-model',
                 'buckets': [{'time': int(start.timestamp()), 'cost': str(amount)}]}
                for tracking, amount in totals.items()
            ], 'function_series': []}],
        }}}

        def fake_get(url, **_kwargs):
            query = parse_qs(urlparse(url).query)
            self.http_queries.append(query)
            self.assertEqual(query['api_key_tracking_id'], sorted(IDS[name] for name in ('a', 'b', 'c')))
            return Response(url, body=json.dumps(payload).encode())

        context = SimpleNamespace(request=SimpleNamespace(get=fake_get))
        service = object.__new__(BrowserService)
        service.context = context
        service.page = object()
        service._check_login_ready = Mock()
        service._keys = Mock(return_value=self.keys)
        service._stage = Mock()
        service._log = Mock()
        service._cost_reader = ConsoleCostReader(context)
        service._cost_reader._authorization = 'Bearer synthetic-session'
        service._cost_reader.prepare = Mock()
        service._cost_reader_identity = key_fingerprint(self.keys)
        result = service._collect(**args)
        self.assertEqual(result['reason_code'], 'OK')
        self.assertEqual(result['totals'], totals)
        self.assertEqual(result['requested_tracking_ids'], [IDS[name] for name in ('a', 'b', 'c')])
        self.browser.futures[-1].set_result(result)
        self.pump()

    def test_tail_c_crosses_limit_after_eleven_empty_gaps_and_only_c_is_dispatched(self):
        self.configure_sparse_roster()
        self.app.collect()
        self.complete_scoped_read(Decimal('0.19'))
        self.assertEqual(self.store.active_tracking_ids(), {IDS[name] for name in ('a', 'b', 'c')})
        self.assertEqual(self.app.rows[-1]['tracking_id'], IDS['c'])
        self.assertEqual(self.app.rows[-1]['status'], 'ok')
        self.assert_empty_rows_are_unmanaged()
        self.app.start()
        self.pump()
        self.assertTrue(self.app.automatic)
        self.assertEqual([action for action, _ in self.browser.calls], ['collect'])

        # Fire the real scheduled Tk callback, without waiting the ten seconds.
        timer = self.app.timer
        self.assertIsNotNone(timer)
        command = self.root.tk.call('after', 'info', timer)[0]
        self.root.tk.call('after', 'cancel', timer)
        self.root.tk.call(command)
        self.pump()
        self.complete_scoped_read(Decimal('0.20001'))
        self.assertEqual(len(self.http_queries), 2)
        self.assertEqual([action for action, _ in self.browser.calls], ['collect', 'collect', 'revoke'])
        _, args = self.browser.calls[-1]
        self.assertEqual((args['name'], args['tracking_id']), ('c', IDS['c']))
        self.assertEqual(self.store.latest_action(IDS['c']), 'pending')
        self.assertFalse(args['permit'].allowed)

        workflow.ready_with_inventory(self.app, args['permit'])
        self.pump()
        self.assertTrue(args['permit'].allowed)
        self.assertEqual(self.store.latest_action(IDS['c']), 'requested')
        evidence = self.store.latest_action_record(IDS['c'])['evidence']
        self.assertEqual(evidence['amount'], '0.2')
        self.assertEqual(evidence['spent'], '0.20001')
        self.browser.futures[-1].set_result({'absent': True})
        self.pump()
        self.assertEqual(self.store.latest_action(IDS['c']), 'absent_verified')
        actions = [event for event in self.store.events() if event['kind'] == 'action']
        self.assertEqual({(event['name'], event['tracking_id']) for event in actions}, {('c', IDS['c'])})
        self.assertEqual({event['status'] for event in actions}, {'pending', 'requested', 'absent_verified'})
        self.assertEqual(self.app.rows[-1]['status'], 'retired')
        self.assert_empty_rows_are_unmanaged()
        self.assertEqual([(person.name, person.amount) for person in self.store.read_people()[2:-1]],
                         [('', Decimal(-1))] * 11)
        self.assertEqual([action for action, _ in self.browser.calls].count('revoke'), 1)
        self.mocks[2].assert_called_once()  # Initial management confirmation only.
        self.mocks[1].assert_not_called()
        with self.store._connect() as db:
            self.assertEqual({row[0] for row in db.execute('SELECT tracking_id FROM costs')},
                             {IDS[name] for name in ('a', 'b', 'c')})
        for name in ('a', 'b', 'skipped', 'unlisted'):
            self.assertIsNone(self.store.latest_action(IDS[name]))


if __name__ == '__main__':
    unittest.main()
