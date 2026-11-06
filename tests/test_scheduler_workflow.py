"""Actual Tk/SQLite management callbacks with a synthetic, permit-aware platform.

Only Tk timer delays are advanced by the driver. Real candidate selection,
eligibility, requested-event persistence, permit consumption and result callbacks
run normally. No browser, HTTP, real account or paid model is used.
"""
from concurrent.futures import Future
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

import test_workflow as fixtures
from quota_guard.browser_service import key_fingerprint
from quota_guard.core import DataStore, GuardError, Person, TZ
from isolated_app import App


class PermitFuture(Future):
    def __init__(self, browser, args):
        super().__init__()
        self.browser, self.args = browser, args

    def done(self):
        if not super().done():
            permit = self.args['permit']
            if permit.decided.is_set() and not permit.allowed:
                self.set_result({'cancelled': True})
            elif permit.allowed and self.browser.auto_complete:
                try:
                    self.browser.finish(self)
                except Exception as exc:
                    self.set_exception(exc)
        return super().done()


class SyntheticPlatform:
    """A shrinking identity inventory with one unbound, ordinary control target."""
    def __init__(self, store, keys, start, *, totals=None, auto_complete=True,
                 prepare_evidence=True):
        self.store, self.keys, self.start = store, list(keys), start
        self.account = key_fingerprint(keys)
        self.totals = totals
        self.auto_complete = auto_complete
        self.prepare_evidence = prepare_evidence
        self.auto_collect = True
        self.calls, self.revoke_futures, self.collection_futures = [], [], []
        self.submitted, self.requested_records = [], []
        self.collection_number = 0

    def result(self, args):
        self.collection_number += 1
        names = set(args['managed_names'])
        wanted = [entry['tracking_id'] for entry in self.keys if entry['name'] in names]
        totals = None if self.totals is None else {key: self.totals[key] for key in wanted if key in self.totals}
        result = dict(keys=list(reversed(self.keys)), account_id=self.account, totals=totals,
                      start_date=self.start, end_date=self.start,
                      source_id=f'synthetic-collection-{self.collection_number}' if totals is not None else '',
                      requested_tracking_ids=wanted,
                      collection_started_wall=time.time(), collection_started_monotonic=time.monotonic())
        if totals is None:
            result.update(reason_code='NO_USAGE_DATA', message='synthetic no usage', retryable=True)
        return result

    def submit(self, action, **args):
        self.calls.append((action, args))
        if action == 'collect':
            future = Future()
            self.collection_futures.append((future, args))
            if self.auto_collect:
                future.set_result(self.result(args))
            return future
        if action != 'revoke':
            raise AssertionError(f'unexpected synthetic action {action}')
        target = args['tracking_id']
        known = {entry['tracking_id'] for entry in self.keys}
        assert target in known and len(known) >= 2
        assert target not in self.submitted
        assert self.store.latest_action(target) == 'pending'
        future = PermitFuture(self, args)
        self.revoke_futures.append(future)
        if self.prepare_evidence:
            args['permit'].set_prepared_inventory(target, known)
        args['permit'].ready.set()
        return future

    def finish(self, future, *, unknown=False):
        target, permit = future.args['tracking_id'], future.args['permit']
        assert permit.allowed and target not in self.submitted
        record = self.store.latest_action_record(target)
        assert record['status'] == 'requested' and record['operation_id']
        evidence = record['recovery_evidence']
        assert evidence['target_tracking_id'] == target
        assert evidence['account_id'] == self.account
        assert set(evidence['known_tracking_ids']) == {entry['tracking_id'] for entry in self.keys}
        assert evidence['source'] == 'prepared_http_inventory_v1'
        permit.consume_for_click()
        self.submitted.append(target)
        self.requested_records.append(record)
        if unknown:
            future.set_exception(GuardError('synthetic response lost after submission'))
        else:
            self.keys = [entry for entry in self.keys if entry['tracking_id'] != target]
            future.set_result({'absent': True})

    def close(self):
        pass


class SchedulerWorkflowTests(unittest.TestCase):
    tearDown = fixtures.WorkflowTests.tearDown

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name).resolve()
        self.start = datetime.now(TZ).date().isoformat()
        self.store = DataStore(self.folder)
        self.store.initialize(100, '10000', self.start)
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root, self.folder)
        self.patches = [patch('quota_guard.ui.messagebox.showinfo'),
                        patch('quota_guard.ui.messagebox.showerror'),
                        patch('quota_guard.ui.messagebox.askyesno', return_value=True)]
        self.mocks = [item.start() for item in self.patches]
        self.root.update_idletasks()

    def drive(self, predicate, *, steps=2000):
        """Invoke the real scheduled control callbacks, omitting their wall wait."""
        if predicate():
            return
        for _ in range(steps):
            eligible = []
            for timer in self.root.tk.call('after', 'info'):
                command = self.root.tk.call('after', 'info', timer)[0]
                if str(command).endswith(('poll', 'advance', '_drain_pending')):
                    eligible.append((timer, command))
            if not eligible:
                break
            # Preserve registration order rather than Tcl's reverse inventory.
            timer, command = min(eligible, key=lambda item: int(str(item[0]).split('#')[1]))
            self.root.tk.call('after', 'cancel', timer)
            self.root.tk.call(command)
            self.root.update_idletasks()
            if predicate():
                return
        self.fail('scheduled workflow did not reach expected state: ' + self.app.status_var.get())

    def setup_platform(self, people, *, totals=None, auto_complete=True, prepare_evidence=True):
        self.store.save_people(people)
        self.app.load_folder()
        keys = [{'name': person.name, 'tracking_id': f'sg-target-{index:03d}'}
                for index, person in enumerate(people) if person.name]
        keys.append({'name': 'synthetic-unbound-control', 'tracking_id': 'sg-control-999'})
        self.platform = SyntheticPlatform(self.store, keys, self.start, totals=totals,
                                          auto_complete=auto_complete, prepare_evidence=prepare_evidence)
        self.app.browser = self.platform
        self.app.collect()
        self.drive(lambda: not self.app.busy)
        self.assertTrue(self.app._collection_is_current())
        self.assertTrue(self.app.session_ready)
        return keys

    def start_and_wait_for_grant(self):
        self.app.start()
        self.drive(lambda: bool(self.platform.revoke_futures)
                   and self.platform.revoke_futures[-1].args['permit'].allowed)
        return self.platform.revoke_futures[-1]

    def test_hundred_targets_reversed_roster_continue_without_per_key_approval(self):
        # IDs are assigned before the worksheet is reversed; no row ordinal can
        # explain the selected ascending identity order.
        people = [Person(f'synthetic-{index:03d}', Decimal(0)) for index in range(100)]
        self.store.save_people(people)
        keys = [{'name': person.name, 'tracking_id': f'sg-target-{index:03d}'}
                for index, person in enumerate(people)]
        keys.append({'name': 'synthetic-unbound-control', 'tracking_id': 'sg-control-999'})
        self.store.save_people(list(reversed(people)))
        self.app.load_folder()
        self.platform = SyntheticPlatform(self.store, keys, self.start)
        self.app.browser = self.platform
        self.app.collect()
        self.drive(lambda: not self.app.busy)
        self.app.start()
        self.drive(lambda: len(self.platform.submitted) == 100 and not self.app.busy
                   and self.app.candidate_queue.summary()['count'] == 0)
        expected = [f'sg-target-{index:03d}' for index in range(100)]
        self.assertEqual(self.platform.submitted, expected)
        self.assertEqual([action for action, _ in self.platform.calls], ['collect'] + ['revoke'] * 100)
        self.assertEqual(len(self.platform.requested_records), 100)
        self.assertEqual(len({item['operation_id'] for item in self.platform.requested_records}), 100)
        self.assertEqual(len(self.platform.requested_records[0]['recovery_evidence']['known_tracking_ids']), 101)
        self.assertEqual(len(self.platform.requested_records[-1]['recovery_evidence']['known_tracking_ids']), 2)
        self.assertEqual(self.platform.keys, [keys[-1]])
        self.assertEqual(self.store.unresolved_actions(), [])
        self.assertTrue(all(self.store.latest_action(key) == 'absent_verified' for key in expected))
        self.mocks[2].assert_called_once()
        self.mocks[1].assert_not_called()
        self.assertTrue(self.app.automatic)
        self.assertIsNotNone(self.app.timer)  # Interval starts only after backlog.

    def test_repeated_complete_refresh_keeps_continuous_candidate_age(self):
        self.setup_platform([Person('synthetic-a', Decimal(1)), Person('synthetic-b', Decimal(1))],
                            totals={'sg-target-000': Decimal(1), 'sg-target-001': Decimal(2)})
        with patch.object(self.app, '_execute_revoke') as execute:
            self.app.start()
            self.drive(lambda: execute.call_count == 1)
            before = self.app.candidate_queue.snapshot()
            self.app.collect()
            self.drive(lambda: execute.call_count == 2)
            after = self.app.candidate_queue.snapshot()
        self.assertEqual({item['tracking_id']: item['first_confirmed_monotonic'] for item in before},
                         {item['tracking_id']: item['first_confirmed_monotonic'] for item in after})
        self.assertEqual([action for action, _ in self.platform.calls], ['collect', 'collect'])
        self.assertEqual(self.store.unresolved_actions(), [])

    def test_expired_evidence_refreshes_before_next_target_and_keeps_age(self):
        self.setup_platform([Person('synthetic-a', Decimal(0)), Person('synthetic-b', Decimal(0))],
                            auto_complete=False)
        first = self.start_and_wait_for_grant()
        before = {item['tracking_id']: item['first_confirmed_monotonic']
                  for item in self.app.candidate_queue.snapshot()}
        self.app.collection_observed_wall -= 300
        self.app.collection_observed_monotonic -= 300
        self.platform.auto_collect = False
        self.platform.finish(first)
        self.drive(lambda: len(self.platform.collection_futures) == 2)
        self.assertEqual([action for action, _ in self.platform.calls], ['collect', 'revoke', 'collect'])
        self.assertFalse(self.app._collection_is_current())
        self.assertEqual(self.app.candidate_queue.snapshot()[1]['first_confirmed_monotonic'], before['sg-target-001'])
        fresh, args = self.platform.collection_futures[-1]
        fresh.set_result(self.platform.result(args))
        self.drive(lambda: len(self.platform.revoke_futures) == 2
                   and self.platform.revoke_futures[-1].args['permit'].allowed)
        after = self.app.candidate_queue.snapshot()
        self.assertEqual(len(after), 1)
        self.assertEqual(after[0]['tracking_id'], 'sg-target-001')
        self.assertEqual(after[0]['first_confirmed_monotonic'], before['sg-target-001'])
        self.platform.finish(self.platform.revoke_futures[-1])
        self.drive(lambda: not self.app.busy)

    def test_stop_after_grant_keeps_late_success_but_does_not_start_next_target(self):
        self.setup_platform([Person('synthetic-a', Decimal(0)), Person('synthetic-b', Decimal(0))],
                            auto_complete=False)
        first = self.start_and_wait_for_grant()
        operation = self.store.latest_action_record('sg-target-000')['operation_id']
        self.app.stop(quiet=True)
        self.platform.finish(first)
        self.drive(lambda: not self.app.busy)
        self.root.update()  # Any queued stale advance must honor its generation.
        self.assertEqual([action for action, _ in self.platform.calls], ['collect', 'revoke'])
        latest = self.store.latest_action_record('sg-target-000')
        self.assertEqual(latest['operation_id'], operation)
        self.assertEqual(latest['status'], 'absent_verified')
        self.assertIn('recovery_evidence', latest)
        self.assertFalse(self.app.automatic)
        self.assertIsNone(self.store.latest_action('sg-target-001'))

    def test_unknown_submission_preserves_operation_and_blocks_remaining_queue(self):
        self.setup_platform([Person('synthetic-a', Decimal(1)), Person('synthetic-b', Decimal(1))],
                            totals={'sg-target-000': Decimal(1), 'sg-target-001': Decimal(1)},
                            auto_complete=False)
        first = self.start_and_wait_for_grant()
        operation = self.store.latest_action_record('sg-target-000')['operation_id']
        self.platform.finish(first, unknown=True)
        self.drive(lambda: not self.app.busy)
        self.assertFalse(self.app.automatic)
        unresolved = self.store.unresolved_actions()
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0]['operation_id'], operation)
        self.assertEqual(unresolved[0]['status'], 'unknown_result')
        self.assertIn('recovery_evidence', unresolved[0])
        self.app.start()
        self.root.update()
        self.assertEqual([action for action, _ in self.platform.calls], ['collect', 'revoke'])
        self.assertEqual(self.platform.submitted, ['sg-target-000'])

    def test_zero_skip_and_positive_unknown_keep_existing_rules(self):
        self.setup_platform([Person('synthetic-zero', Decimal(0)),
                             Person('synthetic-skip', Decimal(-1)),
                             Person('synthetic-positive', Decimal(1))])
        self.assertEqual(self.platform.calls[0][1]['managed_names'], ['synthetic-zero', 'synthetic-positive'])
        self.app.start()
        self.drive(lambda: self.platform.submitted == ['sg-target-000'] and not self.app.busy
                   and self.app.candidate_queue.summary()['count'] == 0)
        self.assertIsNone(self.store.latest_action('sg-target-001'))
        self.assertIsNone(self.store.latest_action('sg-target-002'))
        self.assertEqual(next(item for item in self.app.rows if item['name'] == 'synthetic-skip')['status'], 'skipped')
        self.platform.totals = {'sg-target-002': Decimal('1.00001')}
        self.app.collect()
        self.drive(lambda: self.platform.submitted == ['sg-target-000', 'sg-target-002'] and not self.app.busy)
        self.assertIsNone(self.store.latest_action('sg-target-001'))
        self.mocks[2].assert_called_once()

    def test_readiness_without_recovery_inventory_never_gets_requested_or_permission(self):
        self.setup_platform([Person('synthetic-zero', Decimal(0))], prepare_evidence=False)
        self.app.start()
        self.drive(lambda: bool(self.platform.revoke_futures) and not self.app.busy)
        future = self.platform.revoke_futures[0]
        self.assertFalse(future.args['permit'].allowed)
        self.assertEqual(self.platform.submitted, [])
        actions = [item for item in self.store.events() if item['kind'] == 'action']
        self.assertNotIn('requested', [item['status'] for item in actions])
        self.assertFalse(self.app.automatic)


if __name__ == '__main__':
    unittest.main()
