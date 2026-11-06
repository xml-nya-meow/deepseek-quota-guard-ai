"""Offline recovery fences against real SQLite files and normal child exits.

Synthetic artifacts stay in workspace TEMP for review; no cleanup, Tk, remote
platform, account, credential, or default Windows-user registry is accessed.
"""
from contextlib import closing
from dataclasses import replace
from hashlib import sha256
import json
import multiprocessing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from quota_guard.responsibility_registry import ResponsibilityRegistry, RegistryError, Receipt


TEMP_ROOT = Path(__file__).resolve().parents[2] / 'TEMP'
TARGET = sha256(b'synthetic recovery target').hexdigest()


def compete_recovery(directory, receipt, action, ready, go, results):
    registry = ResponsibilityRegistry(directory, timeout_seconds=1)
    ready.put(action)
    if not go.wait(10):
        results.put((action, 'barrier_timeout'))
        return
    try:
        if action == 'freeze':
            registry.freeze_declared(receipt)
        else:
            registry.mark_possible_send(receipt)
        results.put((action, 'ok'))
    except RegistryError as exc:
        results.put((action, exc.reason_code))


class ResponsibilityRegistryRecoveryTests(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.folder = Path(tempfile.mkdtemp(prefix='registry-recovery-', dir=TEMP_ROOT))
        self.registry = ResponsibilityRegistry(self.folder / 'responsibility', timeout_seconds=.05)
        self.ids = [uuid4().hex, uuid4().hex]
        self.paths = [self.folder / 'X', self.folder / 'Y']
        for path in self.paths:
            path.mkdir()

    def initialize(self, finish=True):
        self.registry.initialize(expected_ledger_ids=self.ids)
        for identity, path in zip(self.ids, self.paths):
            self.registry.register_ledger(identity, path)
        if finish:
            self.registry.finish_initialization()

    def claim(self, ledger=0, checkpoint=0):
        return self.registry.claim(self.ids[ledger], uuid4().hex, TARGET,
                                   expected_checkpoint=checkpoint)

    def make_v1_fixture(self):
        # All original tables retain exactly the v1 definitions. Removing the
        # added table and versions here creates a synthetic old-format pair;
        # production upgrade never removes data or manufactures old evidence.
        with closing(sqlite3.connect(self.registry.db_path)) as db, db:
            db.execute('DROP TABLE obligation_recovery')
            db.execute('UPDATE metadata SET schema=1')
            db.execute('PRAGMA user_version=1')
        anchor = json.loads(self.registry.anchor_path.read_text(encoding='utf-8'))
        anchor['schema'] = 1
        self.registry.anchor_path.write_text(json.dumps(anchor), encoding='utf-8')

    def pair(self):
        return self.registry.db_path.read_bytes(), self.registry.anchor_path.read_bytes()

    def test_prior_checkpoint_is_exact_ledger_checkpoint_not_global_generation_minus_one(self):
        self.initialize()
        first = self.claim()
        self.registry.release(first, resolution='not_submitted')
        second = self.claim(ledger=1)
        self.registry.release(second, resolution='not_submitted')
        third = self.claim(ledger=1, checkpoint=second.generation)
        self.registry.release(third, resolution='not_submitted')
        fourth = self.claim(checkpoint=first.generation)
        self.assertEqual(fourth.generation, 4)
        recovery = self.registry.recovery_for(fourth)
        self.assertEqual(recovery['prior_checkpoint'], 1)
        self.assertNotEqual(recovery['prior_checkpoint'], fourth.generation - 1)
        self.assertIsNone(recovery['recovery_id'])
        before = self.pair()
        self.assertEqual(self.registry.recovery_for(fourth), recovery)
        self.assertEqual(self.pair(), before)

    def test_manual_handoff_claim_durably_disables_declared_recovery_from_start(self):
        self.initialize()
        receipt = self.registry.claim(self.ids[0], uuid4().hex, TARGET,
            expected_checkpoint=0, allow_declared_recovery=False)
        reopened = ResponsibilityRegistry(self.registry.directory)
        context = reopened.recovery_for(receipt)
        self.assertEqual(context['receipt'], receipt.to_dict())
        self.assertIsNone(context['prior_checkpoint'])
        self.assertIsNone(context['recovery_id'])
        self.assertEqual(reopened.open_obligations()[0]['recovery'], context)
        # Disabling recovery does not erase the obligation or the actual
        # checkpoint. A platform absence remains an available settlement.
        self.assertEqual(reopened.check_ledger(self.ids[0], self.paths[0],
            receipt.epoch, receipt.generation)['checkpoint'], receipt.generation)
        with self.assertRaises(RegistryError):
            self.claim(ledger=1)
        self.assertTrue(reopened.release(receipt, resolution='absent_verified'))
        new = self.claim(checkpoint=receipt.generation)
        self.assertEqual(reopened.recovery_for(new)['prior_checkpoint'], receipt.generation)

    def test_recovery_flag_requires_exact_bool_and_invalid_flag_does_not_claim(self):
        self.initialize()
        before = self.pair()
        for value in (0, 1, None, '', 'false', [], {}):
            with self.subTest(value=value), self.assertRaises(RegistryError):
                self.registry.claim(self.ids[0], uuid4().hex, TARGET,
                    expected_checkpoint=0, allow_declared_recovery=value)
            self.assertEqual(self.pair(), before)
        receipt = self.registry.claim(self.ids[0], uuid4().hex, TARGET,
            expected_checkpoint=0, allow_declared_recovery=True)
        self.assertEqual(receipt.generation, 1)
        self.assertEqual(self.registry.recovery_for(receipt)['prior_checkpoint'], 0)

    def test_freeze_is_durable_idempotent_keeps_slot_and_denies_all_old_promotions(self):
        self.initialize()
        receipt = self.claim()
        recovery = self.registry.freeze_declared(receipt)
        self.assertEqual(recovery['receipt'], receipt.to_dict())
        self.assertEqual(recovery['prior_checkpoint'], 0)
        self.assertEqual(len(recovery['recovery_id']), 32)
        before = self.pair()
        reopened = ResponsibilityRegistry(self.registry.directory)
        self.assertEqual(reopened.freeze_declared(receipt), recovery)
        self.assertEqual(self.pair(), before)
        state = reopened.snapshot()
        self.assertEqual(state['open_obligations'][0]['phase'], 'declared')
        self.assertEqual(state['open_obligations'][0]['recovery'], recovery)
        for action in (lambda: reopened.mark_possible_send(receipt),
                       lambda: reopened.release(receipt),
                       lambda: reopened.release(receipt, resolution='not_submitted'),
                       lambda: self.claim(ledger=1)):
            with self.assertRaises(RegistryError):
                action()
        self.assertEqual(reopened.snapshot(), state)

    def test_finish_requires_exact_fence_and_stale_finish_never_clears_new_slot(self):
        self.initialize()
        old = self.claim()
        with self.assertRaises(RegistryError):
            self.registry.finish_declared_recovery(old, uuid4().hex)
        recovery = self.registry.freeze_declared(old)
        for wrong in (uuid4().hex, '', None, 'sk-synthetic'):
            with self.subTest(wrong=wrong), self.assertRaises(RegistryError):
                self.registry.finish_declared_recovery(old, wrong)
        for wrong in (replace(old, epoch=uuid4().hex), replace(old, ledger_id=self.ids[1]),
                      replace(old, operation_id='wrong-operation'), replace(old, target_hash='0' * 64)):
            with self.subTest(wrong=wrong), self.assertRaises(RegistryError):
                self.registry.finish_declared_recovery(wrong, recovery['recovery_id'])
        self.assertTrue(self.registry.finish_declared_recovery(old, recovery['recovery_id']))
        current = self.claim(ledger=1)
        before = self.pair()
        self.assertFalse(self.registry.finish_declared_recovery(old, recovery['recovery_id']))
        self.assertEqual(self.pair(), before)
        self.assertEqual(self.registry.open_obligations()[0]['receipt'], current.to_dict())
        for action in (lambda: self.registry.freeze_declared(old),
                       lambda: self.registry.mark_possible_send(old),
                       lambda: self.registry.release(old)):
            with self.assertRaises(RegistryError):
                action()

    def test_possible_send_remains_observation_only_even_without_execution_log(self):
        self.initialize()
        receipt = self.claim()
        self.registry.mark_possible_send(receipt)
        before = self.pair()
        with self.assertRaises(RegistryError):
            self.registry.freeze_declared(receipt)
        with self.assertRaises(RegistryError):
            self.registry.finish_declared_recovery(receipt, uuid4().hex)
        with self.assertRaises(RegistryError):
            self.registry.release(receipt, resolution='not_submitted')
        self.assertEqual(self.pair(), before)
        self.assertTrue(self.registry.release(receipt, resolution='absent_verified'))

    def test_imported_legacy_obligation_cannot_be_frozen(self):
        self.initialize(finish=False)
        receipt = self.registry.register_unresolved(self.ids[0], 'synthetic-legacy', TARGET,
                                                    expected_checkpoint=0)
        self.registry.finish_initialization()
        with self.assertRaises(RegistryError):
            self.registry.freeze_declared(receipt)
        self.assertEqual(self.registry.open_obligations()[0]['origin'], 'migration')

    def test_upgrade_is_explicit_preserves_receipts_and_unknown_prior_checkpoints(self):
        self.initialize()
        prior = self.claim()
        self.registry.release(prior, resolution='not_submitted')
        old = self.claim(ledger=1)
        before = self.registry.snapshot()
        self.make_v1_fixture()
        original = self.pair()
        reopened = ResponsibilityRegistry(self.registry.directory)
        for action in (reopened.snapshot, lambda: reopened.freeze_declared(old),
                       lambda: reopened.mark_possible_send(old)):
            with self.assertRaises(RegistryError) as caught:
                action()
            self.assertEqual(caught.exception.reason_code, 'REGISTRY_UPGRADE_REQUIRED')
        self.assertEqual(self.pair(), original)
        self.assertTrue(reopened.upgrade_v1())
        after = reopened.snapshot()
        self.assertEqual(after['schema'], 2)
        for field in ('epoch', 'generation', 'phase', 'ledgers'):
            self.assertEqual(after[field], before[field])
        self.assertEqual(reopened.receipt_for(old.ledger_id, old.operation_id), old)
        self.assertIsNone(reopened.recovery_for(old)['prior_checkpoint'])
        self.assertIsNone(reopened.recovery_for(prior)['prior_checkpoint'])
        stable = self.pair()
        self.assertFalse(reopened.upgrade_v1())
        self.assertEqual(self.pair(), stable)
        recovery = reopened.freeze_declared(old)
        self.assertIsNone(recovery['prior_checkpoint'])  # Gate must not guess this.
        reopened.finish_declared_recovery(old, recovery['recovery_id'])
        new = reopened.claim(old.ledger_id, uuid4().hex, TARGET, expected_checkpoint=old.generation)
        self.assertEqual(reopened.recovery_for(new)['prior_checkpoint'], old.generation)

    def test_upgrade_does_not_complete_partial_initialization(self):
        self.initialize(finish=False)
        self.make_v1_fixture()
        self.assertTrue(self.registry.upgrade_v1())
        self.assertEqual(self.registry.snapshot()['phase'], 'initializing')
        with self.assertRaises(RegistryError):
            self.claim()

    def test_upgrade_refuses_missing_or_inconsistent_pair_without_recreating_it(self):
        with self.assertRaises(RegistryError):
            self.registry.upgrade_v1()
        self.assertFalse(self.registry.directory.exists())
        self.initialize()
        self.make_v1_fixture()
        anchor = json.loads(self.registry.anchor_path.read_text(encoding='utf-8'))
        anchor['revision'] += 1
        self.registry.anchor_path.write_text(json.dumps(anchor), encoding='utf-8')
        before = self.pair()
        with self.assertRaises(RegistryError):
            self.registry.upgrade_v1()
        self.assertEqual(self.pair(), before)

    def test_upgrade_failure_before_anchor_publish_preserves_old_pair_for_retry(self):
        self.initialize()
        old = self.claim()
        self.make_v1_fixture()
        before = self.pair()
        with patch.object(self.registry, '_publish_anchor', side_effect=OSError('synthetic write failure')):
            with self.assertRaises(RegistryError):
                self.registry.upgrade_v1()
        self.assertEqual(self.pair(), before)
        self.assertTrue(self.registry.upgrade_v1())
        self.assertEqual(self.registry.receipt_for(old.ledger_id, old.operation_id), old)

    def test_upgrade_failure_after_anchor_publish_keeps_mismatch_blocked(self):
        self.initialize()
        self.make_v1_fixture()
        publish = self.registry._publish_anchor
        def publish_then_fail(*args, **kwargs):
            publish(*args, **kwargs)
            raise OSError('synthetic failure after external publication')
        with patch.object(self.registry, '_publish_anchor', side_effect=publish_then_fail):
            with self.assertRaises(RegistryError):
                self.registry.upgrade_v1()
        with closing(sqlite3.connect(self.registry.db_path)) as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
        self.assertEqual(json.loads(self.registry.anchor_path.read_text(encoding='utf-8'))['schema'], 2)
        before = self.pair()
        for action in (self.registry.snapshot, self.registry.upgrade_v1):
            with self.assertRaises(RegistryError) as caught:
                action()
            self.assertEqual(caught.exception.reason_code, 'REGISTRY_INVALID')
        self.assertEqual(self.pair(), before)

    def test_real_shared_reader_blocks_upgrade_before_any_publication(self):
        self.initialize()
        self.make_v1_fixture()
        before = self.pair()
        with closing(sqlite3.connect(self.registry.db_path, isolation_level=None)) as reader:
            reader.execute('BEGIN')
            reader.execute('SELECT * FROM metadata').fetchall()
            with self.assertRaises(RegistryError) as caught:
                self.registry.upgrade_v1()
            self.assertEqual(caught.exception.reason_code, 'REGISTRY_BUSY')
            self.assertEqual(self.pair(), before)
            reader.execute('ROLLBACK')
        self.assertTrue(self.registry.upgrade_v1())

    def test_missing_recovery_row_is_rejected(self):
        self.initialize()
        receipt = self.claim()
        with closing(sqlite3.connect(self.registry.db_path)) as db, db:
            db.execute('DELETE FROM obligation_recovery WHERE generation=?', (receipt.generation,))
        with self.assertRaises(RegistryError):
            self.registry.snapshot()

    def test_invalid_checkpoint_fence_and_unexpected_columns_are_rejected(self):
        self.initialize()
        receipt = self.claim()
        invalid_statements = (
            ('UPDATE obligation_recovery SET prior_checkpoint=99', ()),
            ('UPDATE obligation_recovery SET recovery_id=?', ('sk-synthetic',)),
            ('INSERT INTO obligation_recovery VALUES(9,NULL,NULL)', ()),
            ('ALTER TABLE obligation_recovery ADD COLUMN token TEXT', ()),
        )
        # Each corruption is confined to a fresh synthetic copy pair.
        original_db, original_anchor = self.pair()
        for index, (statement, arguments) in enumerate(invalid_statements):
            with self.subTest(statement=statement):
                directory = self.folder / ('invalid-' + str(index))
                directory.mkdir()
                registry = ResponsibilityRegistry(directory)
                registry.db_path.write_bytes(original_db)
                registry.anchor_path.write_bytes(original_anchor)
                with closing(sqlite3.connect(registry.db_path)) as db, db:
                    db.execute(statement, arguments)
                with self.assertRaises(RegistryError):
                    registry.recovery_for(receipt)

    def test_fence_cannot_coexist_with_possible_send_in_persisted_state(self):
        self.initialize()
        receipt = self.claim()
        self.registry.freeze_declared(receipt)
        with closing(sqlite3.connect(self.registry.db_path)) as db, db:
            db.execute("UPDATE obligations SET phase='possible_send'")
        with self.assertRaises(RegistryError):
            self.registry.snapshot()

    def test_freeze_and_promotion_compete_atomically_in_two_processes(self):
        self.initialize()
        receipt = self.claim()
        context = multiprocessing.get_context('spawn')
        ready, results, go = context.Queue(), context.Queue(), context.Event()
        processes = [context.Process(target=compete_recovery,
            args=(self.registry.directory, receipt.to_dict(), action, ready, go, results))
            for action in ('freeze', 'promote')]
        try:
            for process in processes:
                process.start()
            self.assertEqual({ready.get(timeout=10), ready.get(timeout=10)}, {'freeze', 'promote'})
            go.set()
            outcomes = dict([results.get(timeout=10), results.get(timeout=10)])
            self.assertEqual(list(outcomes.values()).count('ok'), 1)
            self.assertEqual(list(outcomes.values()).count('REGISTRY_TRANSITION_REJECTED'), 1)
            state = self.registry.open_obligations()[0]
            if outcomes['freeze'] == 'ok':
                self.assertEqual(state['phase'], 'declared')
                self.assertIsNotNone(state['recovery']['recovery_id'])
            else:
                self.assertEqual(state['phase'], 'possible_send')
                self.assertIsNone(state['recovery']['recovery_id'])
        finally:
            go.set()
            for process in processes:
                process.join(timeout=10)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)
            for queue in (ready, results):
                queue.close()
                queue.join_thread()

    def test_failed_boundary_or_recovery_callback_does_not_change_registry(self):
        self.initialize()
        receipt = self.claim()
        before = self.pair()
        calls = []
        def fail_mark():
            calls.append('mark')
            raise RuntimeError('synthetic local send witness write failed')
        def fail_freeze(context):
            calls.append(context)
            raise RuntimeError('synthetic recovery precheck failed')
        with self.assertRaises(RuntimeError):
            self.registry.mark_possible_send(receipt, before_mark=fail_mark)
        self.assertEqual(self.pair(), before)
        with self.assertRaises(RuntimeError):
            self.registry.freeze_declared(receipt, before_freeze=fail_freeze)
        self.assertEqual(self.pair(), before)
        self.assertEqual(calls[0], 'mark')
        self.assertEqual(calls[1]['receipt'], receipt.to_dict())
        self.assertEqual(calls[1]['prior_checkpoint'], 0)
        self.assertEqual(len(calls[1]['recovery_id']), 32)
        self.assertIsNone(self.registry.recovery_for(receipt)['recovery_id'])

    def test_fence_winner_never_calls_old_boundary_callback_or_accepts_context_mutation(self):
        self.initialize()
        receipt = self.claim()
        callback_ids = []
        def check_local(context):
            callback_ids.append(context['recovery_id'])
            context['receipt']['operation_id'] = 'mutated-callback-copy'
            context['recovery_id'] = uuid4().hex
            context['prior_checkpoint'] = 999
        recovery = self.registry.freeze_declared(receipt, before_freeze=check_local)
        self.assertEqual(recovery['receipt'], receipt.to_dict())
        self.assertEqual(recovery['recovery_id'], callback_ids[0])
        self.assertEqual(recovery['prior_checkpoint'], 0)
        calls = []
        with self.assertRaises(RegistryError):
            self.registry.mark_possible_send(receipt, before_mark=lambda: calls.append('called'))
        self.assertEqual(calls, [])

    def test_boundary_winner_is_not_replayed_and_recovery_precheck_is_not_called(self):
        self.initialize()
        receipt = self.claim()
        calls = []
        self.registry.mark_possible_send(receipt, before_mark=lambda: calls.append('witness'))
        self.registry.mark_possible_send(receipt, before_mark=lambda: calls.append('replayed'))
        with self.assertRaises(RegistryError):
            self.registry.freeze_declared(receipt, before_freeze=lambda _: calls.append('recovery'))
        self.assertEqual(calls, ['witness'])

    def test_callbacks_hold_same_sqlite_lock_in_both_race_directions(self):
        self.initialize()
        receipt = self.claim()
        other = ResponsibilityRegistry(self.registry.directory, timeout_seconds=.01)
        blocked = []
        def freeze_while_marking():
            try:
                other.freeze_declared(receipt, before_freeze=lambda _: blocked.append('unexpected-freeze'))
            except RegistryError as exc:
                blocked.append(exc.reason_code)
        self.registry.mark_possible_send(receipt, before_mark=freeze_while_marking)
        self.assertEqual(blocked, ['REGISTRY_BUSY'])
        self.registry.release(receipt, resolution='absent_verified')
        new = self.claim(ledger=1)
        def mark_while_freezing(_):
            try:
                other.mark_possible_send(new, before_mark=lambda: blocked.append('unexpected-mark'))
            except RegistryError as exc:
                blocked.append(exc.reason_code)
        self.registry.freeze_declared(new, before_freeze=mark_while_freezing)
        self.assertEqual(blocked, ['REGISTRY_BUSY', 'REGISTRY_BUSY'])


if __name__ == '__main__':
    unittest.main()
