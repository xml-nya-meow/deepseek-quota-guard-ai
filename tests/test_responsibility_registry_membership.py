"""Registry-side F4 protocol tests with synthetic local callback witnesses.

Real SQLite transactions and normal child processes exercise serialization.
The JSON witness files below are not real ledgers; the separate membership and
Gate suites verify actual account/history/anchor evidence. Artifacts are kept.
"""
from contextlib import closing
from copy import deepcopy
from hashlib import sha256
import json
import multiprocessing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from quota_guard.responsibility_registry import (
    ResponsibilityRegistry, RegistryError, validate_membership_plan,
)
from tests.responsibility_registry_fixture import plan_for, complete_plan


TEMP_ROOT = Path(__file__).resolve().parents[2] / 'TEMP'
TARGET = sha256(b'synthetic membership target').hexdigest()
OTHER = sha256(b'synthetic membership other').hexdigest()


def compete_membership(directory, plan_id, worker_id, ready, go, results):
    registry = ResponsibilityRegistry(directory, timeout_seconds=1)
    callbacks = []
    ready.put(worker_id)
    if not go.wait(10):
        results.put({'error': 'barrier timeout'})
        return
    try:
        complete = registry.execute_membership_plan(plan_id,
            preflight=lambda _: callbacks.append('preflight'),
            apply=lambda _: callbacks.append('apply'),
            verify=lambda _: callbacks.append('verify'))
        results.put({'worker': worker_id, 'complete': complete, 'callbacks': callbacks})
    except RegistryError as exc:
        results.put({'worker': worker_id, 'error': exc.reason_code, 'callbacks': callbacks})


class ResponsibilityRegistryMembershipTests(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.folder = Path(tempfile.mkdtemp(prefix='registry-membership-', dir=TEMP_ROOT))
        self.registry = ResponsibilityRegistry(self.folder / 'responsibility', timeout_seconds=.05)
        self.ids = [uuid4().hex, uuid4().hex]
        self.paths = [self.folder / 'X', self.folder / 'Y']
        for path in self.paths:
            path.mkdir()

    def initialize(self, *, complete=False, operations=()):
        self.plan = plan_for(self.ids, self.paths, operations=operations)
        state = self.registry.initialize(expected_ledger_ids=self.ids, membership_plan=self.plan)
        if complete:
            complete_plan(self.registry, self.plan)
        return state

    def next_plan(self):
        identity, path = uuid4().hex, self.folder / ('member-' + uuid4().hex)
        path.mkdir()
        return plan_for([identity], [path], snapshot=self.registry.snapshot())

    def pair(self):
        return self.registry.db_path.read_bytes(), self.registry.anchor_path.read_bytes()

    def callbacks(self, *, fail_after=None, fail_verify=False):
        writes = []
        def read(member):
            path = self.folder / (member['ledger_id'] + '.witness.json')
            return None if not path.exists() else json.loads(path.read_text(encoding='utf-8'))
        def target(plan, member):
            return {'plan_id': plan['plan_id'], 'anchor': member['target_anchor']}
        def preflight(plan):
            for member in plan['members']:
                current = read(member)
                if current is not None and current != target(plan, member):
                    raise RuntimeError('synthetic witness is neither absent before nor exact target')
        def apply(plan):
            for member in plan['members']:
                if read(member) != target(plan, member):
                    path = self.folder / (member['ledger_id'] + '.witness.json')
                    path.write_text(json.dumps(target(plan, member)), encoding='utf-8')
                    writes.append(member['ledger_id'])
                    if fail_after is not None and len(writes) == fail_after:
                        raise RuntimeError('synthetic member write failure')
        def verify(plan):
            for member in plan['members']:
                if read(member) != target(plan, member):
                    raise RuntimeError('synthetic member not target')
            if fail_verify:
                raise RuntimeError('synthetic final validation failure')
        return {'preflight': preflight, 'apply': apply, 'verify': verify}, writes

    def test_complete_plan_is_durable_before_any_member_callback_and_old_shortcuts_deny(self):
        state = self.initialize()
        self.assertEqual(state['phase'], 'initializing')
        self.assertEqual(state['ledgers'], [])
        self.assertEqual(state['membership_plan']['payload'], self.plan)
        self.assertEqual(self.registry.membership_plan()['state'], 'pending')
        before = self.pair()
        for action in (lambda: self.registry.finish_initialization(),
                       lambda: self.registry.register_ledger(self.ids[0], self.paths[0]),
                       lambda: self.registry.register_unresolved(self.ids[0], 'legacy', TARGET, expected_checkpoint=0),
                       lambda: self.registry.begin_enrollment(uuid4().hex)):
            with self.assertRaises(RegistryError) as caught:
                action()
            self.assertEqual(caught.exception.reason_code, 'REGISTRY_PLAN_REQUIRED')
        self.assertEqual(self.pair(), before)
        callbacks, writes = self.callbacks()
        self.assertTrue(self.registry.execute_membership_plan(self.plan['plan_id'], **callbacks))
        self.assertEqual(len(writes), 2)
        self.assertEqual(self.registry.snapshot()['phase'], 'ready')

    def test_partial_local_apply_rolls_shared_rows_back_then_resumes_same_plan(self):
        self.initialize()
        before = self.registry.snapshot()
        callbacks, writes = self.callbacks(fail_after=1)
        with self.assertRaises(RuntimeError):
            self.registry.execute_membership_plan(self.plan['plan_id'], **callbacks)
        self.assertEqual(len(writes), 1)
        self.assertEqual(self.registry.snapshot(), before)
        reopened = ResponsibilityRegistry(self.registry.directory)
        callbacks, second_writes = self.callbacks()
        self.assertTrue(reopened.execute_membership_plan(self.plan['plan_id'], **callbacks))
        self.assertEqual(len(second_writes), 1)
        self.assertNotEqual(second_writes, writes)
        self.assertEqual(reopened.membership_plan()['state'], 'complete')

    def test_all_old_operations_keep_same_generations_after_partial_apply_retry(self):
        ops = [(self.ids[0], 'synthetic-old-A', TARGET),
               (self.ids[1], 'synthetic-old-B', OTHER)]
        self.initialize(operations=ops)
        planned = deepcopy(self.plan['operations'])
        callbacks, _ = self.callbacks(fail_after=1)
        with self.assertRaises(RuntimeError):
            self.registry.execute_membership_plan(self.plan['plan_id'], **callbacks)
        self.assertEqual(self.registry.snapshot()['generation'], 0)
        self.assertEqual(self.registry.open_obligations(), [])
        callbacks, _ = self.callbacks()
        self.registry.execute_membership_plan(self.plan['plan_id'], **callbacks)
        actual = self.registry.open_obligations()
        self.assertEqual(len(actual), 2)
        for old in planned:
            receipt = self.registry.receipt_for(old['ledger_id'], old['operation_id'])
            self.assertEqual(receipt.generation, old['generation'])
            self.assertEqual(receipt.target_hash, old['target_hash'])
            self.assertEqual(self.registry.recovery_for(receipt)['prior_checkpoint'], old['prior_checkpoint'])
        with self.assertRaises(RegistryError):
            self.registry.claim(self.ids[0], uuid4().hex, TARGET,
                expected_checkpoint=next(m['checkpoint'] for m in self.registry.registered_ledgers()
                                         if m['ledger_id'] == self.ids[0]))

    def test_same_target_across_two_ledgers_preserves_both_original_obligations(self):
        self.initialize(operations=[(self.ids[0], 'old-in-X', TARGET),
                                    (self.ids[1], 'old-in-Y', TARGET)])
        complete_plan(self.registry, self.plan)
        receipts = [self.registry.receipt_for(identity, operation)
                    for identity, operation in zip(self.ids, ['old-in-X', 'old-in-Y'])]
        self.assertEqual(len(self.registry.open_obligations()), 2)
        self.registry.release(receipts[0])
        with self.assertRaises(RegistryError):
            self.registry.claim(self.ids[0], uuid4().hex, OTHER,
                                expected_checkpoint=receipts[0].generation)
        self.assertEqual(self.registry.open_obligations()[0]['receipt'], receipts[1].to_dict())
        self.registry.release(receipts[1])
        self.assertEqual(self.registry.open_obligations(), [])

    def test_ambiguous_same_ledger_target_or_duplicate_operation_is_rejected_before_creation(self):
        cases = [[(self.ids[0], 'old-A', TARGET), (self.ids[0], 'old-B', TARGET)],
                 [(self.ids[0], 'same-op', TARGET), (self.ids[1], 'same-op', OTHER)]]
        for ops in cases:
            with self.subTest(ops=ops), self.assertRaises(RegistryError):
                self.registry.initialize(membership_plan=plan_for(self.ids, self.paths, operations=ops))
            self.assertFalse(self.registry.directory.exists())

    def test_completed_plan_replay_has_no_callback_and_old_plan_cannot_overwrite_new_plan(self):
        self.initialize(complete=True)
        calls = []
        callbacks = {key: lambda p: calls.append(p['plan_id']) for key in ('preflight', 'apply', 'verify')}
        before = self.pair()
        self.assertFalse(self.registry.execute_membership_plan(self.plan['plan_id'], **callbacks))
        self.assertEqual(calls, [])
        self.assertEqual(self.pair(), before)
        next_plan = self.next_plan()
        self.registry.begin_membership_plan(next_plan)
        current = self.pair()
        with self.assertRaises(RegistryError) as caught:
            self.registry.execute_membership_plan(self.plan['plan_id'], **callbacks)
        self.assertEqual(caught.exception.reason_code, 'REGISTRY_PLAN_STALE')
        self.assertEqual(calls, [])
        self.assertEqual(self.pair(), current)
        self.assertTrue(complete_plan(self.registry, next_plan))

    def test_preflight_and_final_verification_failures_never_publish_ready(self):
        self.initialize()
        before = self.registry.snapshot()
        calls = []
        def fail(_):
            raise RuntimeError('synthetic preflight failure')
        with self.assertRaises(RuntimeError):
            self.registry.execute_membership_plan(self.plan['plan_id'], preflight=fail,
                apply=lambda _: calls.append('apply'), verify=lambda _: calls.append('verify'))
        self.assertEqual(calls, [])
        self.assertEqual(self.registry.snapshot(), before)
        callbacks, writes = self.callbacks(fail_verify=True)
        with self.assertRaises(RuntimeError):
            self.registry.execute_membership_plan(self.plan['plan_id'], **callbacks)
        self.assertEqual(len(writes), 2)
        self.assertEqual(self.registry.snapshot(), before)
        callbacks, writes = self.callbacks()
        self.assertTrue(self.registry.execute_membership_plan(self.plan['plan_id'], **callbacks))
        self.assertEqual(writes, [])

    def test_mutating_callback_copy_cannot_change_following_phases_or_persisted_plan(self):
        self.initialize()
        seen = []
        def preflight(plan):
            plan['members'].clear()
            plan['plan_id'] = uuid4().hex
        def apply(plan):
            seen.append(len(plan['members']))
            plan['operations'].append({'unexpected': 'copy mutation'})
        def verify(plan):
            seen.append(len(plan['members']))
            self.assertEqual(plan, self.plan)
        self.registry.execute_membership_plan(self.plan['plan_id'],
            preflight=preflight, apply=apply, verify=verify)
        self.assertEqual(seen, [2, 2])
        self.assertEqual(self.registry.membership_plan()['payload'], self.plan)

    def test_real_reader_blocks_execute_before_callbacks_and_publication(self):
        self.initialize()
        calls = []
        before = self.pair()
        with closing(sqlite3.connect(self.registry.db_path, isolation_level=None)) as reader:
            reader.execute('BEGIN')
            reader.execute('SELECT * FROM membership_plan').fetchall()
            with self.assertRaises(RegistryError) as caught:
                self.registry.execute_membership_plan(self.plan['plan_id'],
                    preflight=lambda _: calls.append('preflight'),
                    apply=lambda _: calls.append('apply'), verify=lambda _: calls.append('verify'))
            self.assertEqual(caught.exception.reason_code, 'REGISTRY_BUSY')
            self.assertEqual(calls, [])
            self.assertEqual(self.pair(), before)
            reader.execute('ROLLBACK')
        self.assertTrue(complete_plan(self.registry, self.plan))

    def test_all_local_callbacks_hold_scope_lock_and_cannot_nest_another_registry_writer(self):
        self.initialize()
        other = ResponsibilityRegistry(self.registry.directory, timeout_seconds=.01)
        blocked = []
        def callback(_):
            try:
                complete_plan(other, self.plan)
            except RegistryError as exc:
                blocked.append(exc.reason_code)
        self.registry.execute_membership_plan(self.plan['plan_id'],
            preflight=callback, apply=callback, verify=callback)
        self.assertEqual(blocked, ['REGISTRY_BUSY'] * 3)

    def test_publish_failure_before_replace_retries_targets_but_after_replace_stays_blocked(self):
        self.initialize()
        before = self.pair()
        callbacks, writes = self.callbacks()
        with patch.object(self.registry, '_publish_anchor', side_effect=OSError('synthetic before publication')):
            with self.assertRaises(RegistryError):
                self.registry.execute_membership_plan(self.plan['plan_id'], **callbacks)
        self.assertEqual(len(writes), 2)
        self.assertEqual(self.pair(), before)
        publish = self.registry._publish_anchor
        def publish_then_fail(*args, **kwargs):
            publish(*args, **kwargs)
            raise OSError('synthetic after publication')
        callbacks, writes = self.callbacks()
        with patch.object(self.registry, '_publish_anchor', side_effect=publish_then_fail):
            with self.assertRaises(RegistryError):
                self.registry.execute_membership_plan(self.plan['plan_id'], **callbacks)
        self.assertEqual(writes, [])
        self.assertEqual(self.registry.db_path.read_bytes(), before[0])
        self.assertNotEqual(self.registry.anchor_path.read_bytes(), before[1])
        with self.assertRaises(RegistryError):
            self.registry.membership_plan()
        with self.assertRaises(RegistryError):
            complete_plan(self.registry, self.plan)

    def test_v2_ready_upgrade_preserves_recovery_fence_but_unplanned_initializing_cannot_finish(self):
        self.initialize(complete=True)
        receipt = self.registry.claim(self.ids[0], uuid4().hex, TARGET, expected_checkpoint=0)
        fence = self.registry.freeze_declared(receipt)
        with closing(sqlite3.connect(self.registry.db_path)) as db, db:
            db.execute('DROP TABLE membership_plan')
            db.execute('UPDATE metadata SET schema=2')
            db.execute('PRAGMA user_version=2')
        anchor = json.loads(self.registry.anchor_path.read_text(encoding='utf-8'))
        anchor['schema'] = 2
        self.registry.anchor_path.write_text(json.dumps(anchor), encoding='utf-8')
        self.assertTrue(self.registry.upgrade_schema())
        self.assertEqual(self.registry.recovery_for(receipt), fence)
        self.assertIsNone(self.registry.membership_plan())
        self.registry.finish_declared_recovery(receipt, fence['recovery_id'])
        with closing(sqlite3.connect(self.registry.db_path)) as db, db:
            db.execute("UPDATE metadata SET phase='initializing'")
        with self.assertRaises(RegistryError) as caught:
            self.registry.finish_initialization()
        self.assertEqual(caught.exception.reason_code, 'REGISTRY_PLAN_REQUIRED')
        with self.assertRaises(RegistryError):
            self.registry.execute_membership_plan(uuid4().hex,
                preflight=lambda _: None, apply=lambda _: None, verify=lambda _: None)
        self.assertEqual(self.registry.snapshot()['phase'], 'initializing')

    def test_strict_plan_fields_checkpoints_and_original_operation_survival(self):
        self.initialize(complete=True, operations=[(self.ids[0], 'old-A', TARGET)])
        bad = deepcopy(self.plan)
        bad['members'][0]['target_anchor']['checkpoint'] = True
        with self.assertRaises(RegistryError):
            validate_membership_plan(bad)
        plan = self.next_plan()
        bad = deepcopy(plan)
        existing = next(m for m in bad['members'] if m['before_anchor'] is not None
                        and m['before_anchor']['checkpoint'] == 1)
        existing['before_anchor']['checkpoint'] = True
        with self.assertRaises(RegistryError):
            validate_membership_plan(bad)
        with closing(sqlite3.connect(self.registry.db_path)) as db, db:
            db.execute('UPDATE obligations SET target_hash=?', (OTHER,))
        with self.assertRaises(RegistryError):
            self.registry.snapshot()

    def test_two_processes_execute_once_and_loser_never_writes_local_callbacks(self):
        self.initialize()
        context = multiprocessing.get_context('spawn')
        ready, results, go = context.Queue(), context.Queue(), context.Event()
        workers = [context.Process(target=compete_membership,
            args=(self.registry.directory, self.plan['plan_id'], i, ready, go, results)) for i in range(2)]
        try:
            for worker in workers:
                worker.start()
            self.assertEqual({ready.get(timeout=10), ready.get(timeout=10)}, {0, 1})
            go.set()
            outcomes = [results.get(timeout=10), results.get(timeout=10)]
            winner = [o for o in outcomes if o.get('complete') is True]
            self.assertEqual(len(winner), 1)
            self.assertEqual(winner[0]['callbacks'], ['preflight', 'apply', 'verify'])
            loser = next(o for o in outcomes if o.get('complete') is not True)
            self.assertEqual(loser['callbacks'], [])
            self.assertTrue(loser.get('complete') is False or loser.get('error') == 'REGISTRY_BUSY')
        finally:
            go.set()
            for worker in workers:
                worker.join(timeout=10)
                self.assertFalse(worker.is_alive())
                self.assertEqual(worker.exitcode, 0)
            for queue in (ready, results):
                queue.close()
                queue.join_thread()


if __name__ == '__main__':
    unittest.main()
