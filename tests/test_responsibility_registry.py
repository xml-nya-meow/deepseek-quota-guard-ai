"""Offline durable registry tests; every artifact stays in workspace TEMP.

The concurrency workers normally exit; no network, browser, model or real
Windows-user registry is used. State corruption touches only synthetic files.
"""
from dataclasses import replace
from contextlib import closing
from hashlib import sha256
import json
import multiprocessing
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from quota_guard.responsibility_registry import (
    Receipt, RegistryError, ResponsibilityRegistry, validate_receipt,
)
from tests.responsibility_registry_fixture import plan_for, complete_plan


TEMP_ROOT = Path(__file__).resolve().parents[2] / 'TEMP'
TARGET = sha256(b'synthetic target').hexdigest()
OTHER = sha256(b'synthetic other target').hexdigest()


def competing_claim(directory, ledger_id, ready, go, results):
    registry = ResponsibilityRegistry(directory, timeout_seconds=.1)
    ready.put(ledger_id)
    if not go.wait(10):
        results.put({'error': 'test barrier timed out'})
        return
    try:
        receipt = registry.claim(ledger_id, uuid4().hex, TARGET, expected_checkpoint=0)
        results.put({'receipt': receipt.to_dict()})
    except RegistryError as exc:
        results.put({'error': exc.reason_code})


class ResponsibilityRegistryTests(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='registry-', dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.registry = ResponsibilityRegistry(self.folder / 'responsibility', timeout_seconds=.05)
        self.ids = [uuid4().hex, uuid4().hex]
        self.paths = [self.folder / 'X', self.folder / 'Y']
        for path in self.paths:
            path.mkdir()

    def initialize(self, *, finish=True, operations=()):
        self.plan = plan_for(self.ids, self.paths, operations=operations)
        state = self.registry.initialize(expected_ledger_ids=self.ids, membership_plan=self.plan)
        self.epoch = state['epoch']
        if finish:
            complete_plan(self.registry, self.plan)

    def claim(self, *, ledger=0, operation=None, checkpoint=0, target=TARGET):
        return self.registry.claim(self.ids[ledger], operation or uuid4().hex, target,
                                   expected_checkpoint=checkpoint)

    def test_no_constructor_creation_and_explicit_scope_is_not_ready_until_finished(self):
        self.assertFalse(self.registry.directory.exists())
        with self.assertRaises(RegistryError):
            self.registry.snapshot()
        self.initialize(finish=False)
        state = self.registry.snapshot()
        self.assertEqual(state['phase'], 'initializing')
        with self.assertRaises(RegistryError):
            self.registry.register_ledger(self.ids[0], self.paths[0])
        with self.assertRaises(RegistryError):
            self.registry.finish_initialization()
        with self.assertRaises(RegistryError) as caught:
            self.claim()
        self.assertEqual(caught.exception.reason_code, 'REGISTRY_INITIALIZING')
        complete_plan(self.registry, self.plan)
        self.assertEqual(self.registry.snapshot()['phase'], 'ready')
        self.assertEqual(self.claim().generation, 1)

    def test_claim_is_durable_and_another_ledger_or_same_operation_cannot_reclaim(self):
        self.initialize()
        receipt = self.claim()
        self.assertEqual(validate_receipt(receipt.to_dict()), receipt)
        reopened = ResponsibilityRegistry(self.registry.directory)
        self.assertEqual(reopened.receipt_for(self.ids[0], receipt.operation_id), receipt)
        self.assertEqual(reopened.open_obligations()[0]['phase'], 'declared')
        self.assertEqual(reopened.registered_ledgers()[0 if self.ids[0] < self.ids[1] else 1]['checkpoint'], 1)
        for ledger, operation, checkpoint in ((1, uuid4().hex, 0), (0, receipt.operation_id, 1)):
            with self.subTest(ledger=ledger), self.assertRaises(RegistryError):
                self.claim(ledger=ledger, operation=operation, checkpoint=checkpoint)
        self.registry.mark_possible_send(receipt)
        self.assertEqual(reopened.open_obligations()[0]['phase'], 'possible_send')
        self.assertEqual(len(reopened.open_obligations()), 1)

    def test_exact_receipt_release_is_idempotent_and_cannot_clear_new_slot(self):
        self.initialize()
        old = self.claim()
        self.registry.mark_possible_send(old)
        self.assertTrue(self.registry.release(old))
        new = self.claim(ledger=1)
        self.assertEqual(new.generation, old.generation + 1)
        self.assertFalse(self.registry.release(old))
        self.assertEqual(self.registry.open_obligations()[0]['receipt'], new.to_dict())
        for stale in (replace(old, generation=new.generation), replace(new, ledger_id=old.ledger_id),
                      replace(new, operation_id=old.operation_id), replace(new, target_hash=OTHER),
                      replace(new, epoch=uuid4().hex)):
            with self.subTest(stale=stale), self.assertRaises(RegistryError):
                self.registry.release(stale)
        self.assertEqual(self.registry.open_obligations()[0]['receipt'], new.to_dict())

    def test_possible_send_cannot_be_reclassified_as_not_submitted(self):
        self.initialize()
        receipt = self.claim()
        self.registry.mark_possible_send(receipt)
        self.registry.mark_possible_send(receipt)  # Same stage, no additional grant.
        for resolution in ('not_submitted', 'present_verified', 'timeout', 'process_exited'):
            with self.subTest(resolution=resolution), self.assertRaises(RegistryError):
                self.registry.release(receipt, resolution=resolution)
        self.assertEqual(self.registry.open_obligations()[0]['phase'], 'possible_send')
        self.registry.release(receipt)
        with self.assertRaises(RegistryError):
            self.registry.mark_possible_send(receipt)

    def test_declared_without_send_can_settle_not_submitted_with_exact_receipt(self):
        self.initialize()
        receipt = self.claim()
        self.assertTrue(self.registry.release(receipt, resolution='not_submitted'))
        self.assertEqual(self.registry.open_obligations(), [])
        next_receipt = self.claim(checkpoint=receipt.generation)
        self.assertGreater(next_receipt.generation, receipt.generation)

    def test_multiple_old_obligations_import_before_ready_and_all_block_new_claim(self):
        self.initialize(operations=[(self.ids[0], 'synthetic-old-one', TARGET),
                                    (self.ids[1], 'synthetic-old-two', OTHER)])
        first = self.registry.receipt_for(self.ids[0], 'synthetic-old-one')
        second = self.registry.receipt_for(self.ids[1], 'synthetic-old-two')
        self.assertNotEqual(first.generation, second.generation)
        self.assertEqual(len(self.registry.open_obligations()), 2)
        self.registry.release(first)
        with self.assertRaises(RegistryError) as caught:
            self.claim(checkpoint=first.generation)
        self.assertEqual(caught.exception.reason_code, 'REGISTRY_OCCUPIED')
        self.registry.release(second)
        self.assertEqual(self.claim(checkpoint=first.generation).generation, 3)
        with self.assertRaises(RegistryError):
            self.registry.register_unresolved(self.ids[1], 'another-old', OTHER,
                                              expected_checkpoint=second.generation)

    def test_paths_epoch_checkpoints_and_duplicate_ledger_copies_do_not_rebind(self):
        self.initialize()
        self.registry.check_ledger(self.ids[0], self.paths[0], self.epoch, 0)
        for ledger, path, epoch, checkpoint in (
            (self.ids[0], self.paths[1], self.epoch, 0),
            (self.ids[0], self.paths[0], uuid4().hex, 0),
            (self.ids[0], self.paths[0], self.epoch, 1),
            (self.ids[0], self.paths[0], self.epoch, None),
            (uuid4().hex, self.paths[0], self.epoch, 0),
        ):
            with self.subTest(ledger=ledger, checkpoint=checkpoint), self.assertRaises(RegistryError):
                self.registry.check_ledger(ledger, path, epoch, checkpoint)
        with self.assertRaises(RegistryError):
            self.registry.register_ledger(self.ids[0], self.paths[1])
        with self.assertRaises(RegistryError):
            self.registry.register_ledger(uuid4().hex, self.paths[0])
        receipt = self.claim()
        with self.assertRaises(RegistryError):
            self.registry.check_ledger(self.ids[0], self.paths[0], self.epoch, 0)
        self.registry.check_ledger(self.ids[0], self.paths[0], self.epoch, receipt.generation)
        self.registry.check_ledger(self.ids[1], self.paths[1], self.epoch, 0)

    def test_missing_database_or_whole_registry_directory_cannot_initialize_over_anchor(self):
        self.initialize()
        self.registry.db_path.unlink()
        self.registry.directory.rmdir()
        self.assertTrue(self.registry.anchor_path.exists(), 'Anchor is outside the lost DB directory.')
        with self.assertRaises(RegistryError):
            self.registry.snapshot()
        with self.assertRaises(RegistryError):
            self.registry.initialize(expected_ledger_ids=self.ids)
        self.assertFalse(self.registry.directory.exists())

    def test_missing_anchor_cannot_be_silently_recreated(self):
        self.initialize()
        self.registry.anchor_path.unlink()
        with self.assertRaises(RegistryError):
            self.claim()
        with self.assertRaises(RegistryError):
            self.registry.initialize(expected_ledger_ids=self.ids)
        self.assertFalse(self.registry.anchor_path.exists())

    def test_corrupt_database_or_schema_denies_even_when_no_open_obligation(self):
        self.initialize()
        original = self.registry.db_path.read_bytes()
        for corrupt in (b'not a database', b''):
            with self.subTest(size=len(corrupt)):
                self.registry.db_path.write_bytes(corrupt)
                with self.assertRaises(RegistryError):
                    self.claim()
        self.registry.db_path.write_bytes(original)
        with closing(sqlite3.connect(self.registry.db_path)) as db, db:
            db.execute('PRAGMA user_version=999')
        with self.assertRaises(RegistryError):
            self.registry.snapshot()

    def test_old_empty_database_rollback_and_old_anchor_rollback_both_deny(self):
        self.initialize()
        old_db = self.registry.db_path.read_bytes()
        old_anchor = self.registry.anchor_path.read_bytes()
        self.claim()
        latest_db = self.registry.db_path.read_bytes()
        self.registry.db_path.write_bytes(old_db)
        with self.assertRaises(RegistryError):
            self.registry.snapshot()
        self.registry.db_path.write_bytes(latest_db)
        self.registry.anchor_path.write_bytes(old_anchor)
        with self.assertRaises(RegistryError):
            self.registry.snapshot()

    def test_anchor_publish_failure_before_and_after_replace_does_not_admit_claim(self):
        self.initialize()
        original = self.registry._publish_anchor
        with patch.object(self.registry, '_publish_anchor', side_effect=OSError('synthetic prewrite failure')):
            with self.assertRaises(RegistryError):
                self.claim()
        self.assertEqual(self.registry.open_obligations(), [])
        def publish_then_fail(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError('synthetic crash cut after external anchor')
        with patch.object(self.registry, '_publish_anchor', side_effect=publish_then_fail):
            with self.assertRaises(RegistryError):
                self.claim()
        with self.assertRaises(RegistryError):
            self.registry.snapshot()
        with self.assertRaises(RegistryError):
            self.claim(ledger=1)

    def test_busy_database_is_bounded_and_fail_closed_then_positive_control_can_claim(self):
        self.initialize()
        db = sqlite3.connect(self.registry.db_path, isolation_level=None)
        try:
            db.execute('BEGIN IMMEDIATE')
            started = time.monotonic()
            with self.assertRaises(RegistryError) as caught:
                self.claim()
            elapsed = time.monotonic() - started
            self.assertEqual(caught.exception.reason_code, 'REGISTRY_BUSY')
            self.assertLess(elapsed, 1)
        finally:
            db.rollback()
            db.close()
        self.assertEqual(self.registry.open_obligations(), [])
        self.assertEqual(self.claim().generation, 1)

    def test_reader_lock_rejects_before_anchor_changes_and_release_allows_claim(self):
        self.initialize()
        anchor_before = self.registry.anchor_path.read_bytes()
        database_before = self.registry.db_path.read_bytes()
        reader = sqlite3.connect(self.registry.db_path, isolation_level=None)
        try:
            self.assertEqual(reader.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
            reader.execute('BEGIN')
            # BEGIN alone does not acquire SHARED. Actually reading a table
            # reproduces the rollback-journal reader that could block COMMIT.
            reader.execute('SELECT generation FROM metadata').fetchone()
            self.assertTrue(reader.in_transaction)
            started = time.monotonic()
            with self.assertRaises(RegistryError) as caught:
                self.claim()
            self.assertEqual(caught.exception.reason_code, 'REGISTRY_BUSY')
            self.assertLess(time.monotonic() - started, 1)
            self.assertEqual(self.registry.anchor_path.read_bytes(), anchor_before,
                             'An ordinary reader must not advance the external anchor.')
            self.assertEqual(self.registry.db_path.read_bytes(), database_before)
        finally:
            reader.rollback()
            reader.close()
        self.assertEqual(self.registry.open_obligations(), [])
        self.assertEqual(self.claim().generation, 1)

    def test_registry_read_reserves_pair_before_reading_external_anchor(self):
        self.initialize()
        other = ResponsibilityRegistry(self.registry.directory, timeout_seconds=.05)
        original_read = self.registry._read_anchor
        outcomes = []
        def read_while_writer_attempts():
            # A deterministic barrier at the external read: the registry's
            # SQLite reservation must already prevent a write between its two
            # sources. The competing writer has a separate real connection.
            try:
                other.claim(self.ids[1], uuid4().hex, OTHER, expected_checkpoint=0)
                outcomes.append('writer_claimed')
            except RegistryError as exc:
                outcomes.append(exc.reason_code)
            return original_read()
        with patch.object(self.registry, '_read_anchor', side_effect=read_while_writer_attempts):
            snapshot = self.registry.snapshot()
        self.assertEqual(outcomes, ['REGISTRY_BUSY'])
        self.assertEqual(snapshot['generation'], 0)
        self.assertEqual(snapshot['open_obligations'], [])
        self.assertEqual(self.claim(ledger=1).generation, 1)

    def test_two_processes_claim_one_shared_slot_atomically(self):
        self.initialize()
        context = multiprocessing.get_context('spawn')
        ready, results, go = context.Queue(), context.Queue(), context.Event()
        processes = [context.Process(target=competing_claim,
            args=(self.registry.directory, ledger, ready, go, results)) for ledger in self.ids]
        try:
            for process in processes:
                process.start()
            self.assertEqual({ready.get(timeout=10), ready.get(timeout=10)}, set(self.ids))
            go.set()
            outcomes = [results.get(timeout=10), results.get(timeout=10)]
            self.assertEqual(sum('receipt' in result for result in outcomes), 1)
            loser = next(result for result in outcomes if 'error' in result)
            self.assertIn(loser['error'], {'REGISTRY_BUSY', 'REGISTRY_OCCUPIED'})
            self.assertEqual(len(self.registry.open_obligations()), 1)
            self.assertEqual(self.registry.snapshot()['generation'], 1)
        finally:
            go.set()
            for process in processes:
                process.join(timeout=10)
                self.assertFalse(process.is_alive())
                self.assertEqual(process.exitcode, 0)
            for queue in (ready, results):
                queue.close()
                queue.join_thread()

    def test_receipt_validation_rejects_credentials_unknown_fields_and_invalid_numbers(self):
        value = Receipt(uuid4().hex, uuid4().hex, 'synthetic-op', TARGET, 1).to_dict()
        invalid = [value | {'authorization': 'synthetic-private'}, value | {'generation': True},
                   value | {'generation': 0}, value | {'generation': 2**63},
                   value | {'operation_id': 'sk-synthetic-private'}, value | {'target_hash': 'raw-tracking-id'}]
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(RegistryError):
                validate_receipt(item)

    def test_enrollment_blocks_claims_until_declared_new_member_is_registered_and_finished(self):
        self.initialize()
        identity, path = uuid4().hex, self.folder / 'Z'
        path.mkdir()
        with self.assertRaises(RegistryError):
            self.registry.register_ledger(identity, path)
        before = self.registry.registered_ledgers()
        plan = plan_for([identity], [path], snapshot=self.registry.snapshot())
        result = self.registry.begin_membership_plan(plan)
        self.assertEqual([m['ledger_id'] for m in result['payload']['members']], sorted([*self.ids, identity]))
        self.assertEqual(self.registry.snapshot()['phase'], 'initializing')
        self.assertEqual(self.registry.registered_ledgers(), before)
        with self.assertRaises(RegistryError):
            self.claim()
        with self.assertRaises(RegistryError):
            self.registry.finish_initialization()
        with self.assertRaises(RegistryError):
            self.registry.begin_enrollment(uuid4().hex)
        with self.assertRaises(RegistryError):
            self.registry.register_ledger(identity, path)
        with self.assertRaises(RegistryError):
            self.registry.claim(identity, uuid4().hex, TARGET, expected_checkpoint=0)
        complete_plan(self.registry, plan)
        receipt = self.registry.claim(identity, uuid4().hex, TARGET, expected_checkpoint=0)
        self.assertEqual(receipt.generation, 1)
        self.assertEqual(receipt.ledger_id, identity)

    def test_existing_member_and_open_responsibility_cannot_begin_enrollment(self):
        self.initialize()
        original = self.registry.snapshot()
        with self.assertRaises(RegistryError):
            self.registry.begin_enrollment(self.ids[0])
        self.assertEqual(self.registry.snapshot(), original)
        receipt = self.claim()
        identity, path = uuid4().hex, self.folder / 'Z'
        path.mkdir()
        for possible in (False, True):
            if possible:
                self.registry.mark_possible_send(receipt)
            before = self.registry.snapshot()
            plan = plan_for([identity], [path], snapshot=before)
            with self.assertRaises(RegistryError) as caught:
                self.registry.begin_membership_plan(plan)
            self.assertEqual(caught.exception.reason_code, 'REGISTRY_OCCUPIED')
            self.assertEqual(self.registry.snapshot(), before)
        self.registry.release(receipt)
        plan = plan_for([identity], [path], snapshot=self.registry.snapshot())
        self.registry.begin_membership_plan(plan)
        self.assertEqual(self.registry.snapshot()['phase'], 'initializing')


if __name__ == '__main__':
    unittest.main()
