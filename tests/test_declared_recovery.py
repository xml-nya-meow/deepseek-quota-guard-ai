"""Recovery of an original never-authorized claim, using only synthetic files.

These are Gate/DataStore/SQLite integration checks, not browser or GUI tests.
All ledgers and the injected registry live in this development workspace.
"""
from contextlib import closing
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from quota_guard.core import DataStore, GuardError, Person, TZ
from quota_guard.declared_recovery import DeclaredRecoveryClosed, TOMBSTONE_PREFIX
from quota_guard.responsibility_gate import (
    ResponsibilityGate, _save_anchor, action_history, ledger_anchor, ledger_id,
)


TEMP_ROOT = Path(__file__).resolve().parents[2] / 'test-temp'
ACCOUNT = 'synthetic-declared-account'
TARGET = 'synthetic-declared-target'
OTHER = 'synthetic-declared-other'
CONTROL = 'synthetic-declared-control'
NAME = 'Synthetic original'
OTHER_NAME = 'Synthetic other'


class DeclaredRecoveryTests(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='declared-recovery-', dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.keys = [{'tracking_id': TARGET, 'name': NAME},
                     {'tracking_id': OTHER, 'name': OTHER_NAME},
                     {'tracking_id': CONTROL, 'name': 'Synthetic control'}]
        self.first = self.make_store('original', NAME)
        self.second = self.make_store('other', OTHER_NAME)
        self.gate = ResponsibilityGate(self.folder / 'shared' / 'responsibility')
        self.gate.initialize([self.first, self.second])

    def make_store(self, directory, name):
        store = DataStore(self.folder / directory)
        store.initialize(2, '100', datetime.now(TZ).date().isoformat())
        store.save_people([Person(name, Decimal(0))])
        store.bind_keys(self.keys, ACCOUNT)
        return store

    def claim(self, store=None, target=TARGET):
        return self.gate.claim(self.first if store is None else store, uuid4().hex, target)

    def pending(self, receipt, store=None, name=NAME, target=TARGET):
        store = self.first if store is None else store
        store.record_action(name, target, 'pending', 'synthetic pending',
                            operation_id=receipt.operation_id, responsibility=receipt.to_dict())

    def requested(self, receipt):
        self.first.record_action(NAME, TARGET, 'requested', 'synthetic requested',
            operation_id=receipt.operation_id,
            recovery_evidence={'schema': 1, 'account_id': ACCOUNT,
                'target_tracking_id': TARGET, 'known_tracking_ids': [TARGET, OTHER, CONTROL],
                'source': 'prepared_http_inventory_v1'})

    @staticmethod
    def local_snapshot(store):
        with store._connect() as db:
            return {
                'meta': list(db.execute('SELECT key,value FROM meta ORDER BY key')),
                'events': list(db.execute('SELECT * FROM events ORDER BY id')),
                'bindings': list(db.execute('SELECT * FROM bindings ORDER BY tracking_id')),
            }

    @staticmethod
    def recovery_events(store):
        with store._connect() as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT payload FROM events WHERE kind='responsibility_recovery' ORDER BY id")]

    def assert_still_owned(self, receipt):
        obligations = self.gate.registry.open_obligations()
        self.assertEqual(len(obligations), 1)
        self.assertEqual(obligations[0]['receipt'], receipt.to_dict())
        with self.assertRaises(GuardError):
            self.gate.claim(self.second, uuid4().hex, OTHER)

    def test_claim_only_recovery_keeps_original_operation_without_inventing_pending(self):
        receipt = self.claim()
        self.assertEqual(action_history(self.first), [])
        self.assertEqual(self.gate.recover_declared(self.first, receipt), 1)
        reopened = DataStore(self.first.folder)
        self.assertEqual(action_history(reopened), [])
        self.assertEqual(ledger_anchor(reopened)['checkpoint'], receipt.generation)
        self.assertEqual(reopened.unresolved_actions(), [])
        events = self.recovery_events(reopened)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['receipt'], receipt.to_dict())
        self.assertEqual(events[0]['origin'], 'claim_without_action')
        self.assertEqual(self.gate.registry.open_obligations(), [])
        with self.assertRaises(GuardError):
            self.gate.registry.mark_possible_send(receipt)
        self.gate.check(self.second)
        next_receipt = self.claim(self.second, OTHER)
        self.assertNotEqual(next_receipt.operation_id, receipt.operation_id)

    def test_requested_export_failure_preserves_original_rows_and_recovers_once(self):
        receipt = self.claim()
        self.pending(receipt)
        with patch.object(self.first, 'export_logs', side_effect=OSError('synthetic export failure')):
            with self.assertRaises(OSError):
                self.gate.possible_send(self.first, receipt,
                    write_requested=lambda: self.requested(receipt))
        before = action_history(self.first)
        self.assertEqual([row['status'] for row in before], ['pending', 'requested'])
        self.assertEqual(self.gate.registry.open_obligations()[0]['phase'], 'declared')
        self.assertEqual(self.gate.recover_declared(self.first, receipt), 1)
        after = action_history(self.first)
        self.assertEqual(after[:-1], before)
        self.assertEqual(after[-1]['status'], 'cancelled')
        self.assertEqual(after[-1]['operation_id'], receipt.operation_id)
        self.assertEqual(self.first.unresolved_actions(), [])
        snapshot = self.local_snapshot(self.first)
        self.assertEqual(self.gate.recover_declared(DataStore(self.first.folder), receipt), 0)
        self.assertEqual(self.local_snapshot(self.first), snapshot)
        self.assertEqual(len(self.recovery_events(self.first)), 1)
        self.gate.check(self.second)

    def test_paired_declared_backup_cannot_hide_local_possible_send_boundary(self):
        receipt = self.claim()
        database = self.gate.registry.db_path.read_bytes()
        anchor = self.gate.registry.anchor_path.read_bytes()
        self.pending(receipt)
        self.assertTrue(self.gate.possible_send(self.first, receipt,
            write_requested=lambda: self.requested(receipt)))
        self.assertEqual(self.gate.registry.open_obligations()[0]['phase'], 'possible_send')
        local_before = self.local_snapshot(self.first)
        for path, contents in ((self.gate.registry.db_path, database),
                               (self.gate.registry.anchor_path, anchor)):
            self.assertTrue(path.resolve().is_relative_to(self.folder))
            path.write_bytes(contents)
        self.assertEqual(self.gate.registry.open_obligations()[0]['phase'], 'declared')
        with self.assertRaises(GuardError):
            self.gate.recover_declared(DataStore(self.first.folder), receipt)
        self.assertEqual(self.local_snapshot(self.first), local_before)
        self.assertIsNone(self.gate.registry.recovery_for(receipt)['recovery_id'])
        self.assert_still_owned(receipt)

    def test_recovery_uses_ledger_prior_checkpoint_not_global_generation_minus_one(self):
        first_receipt = self.claim()
        self.pending(first_receipt)
        self.first.record_action(NAME, TARGET, 'not_submitted', 'synthetic unsent',
                                 operation_id=first_receipt.operation_id, continuation=True)
        self.assertTrue(self.gate.settle(self.first, first_receipt))
        for _ in range(2):
            other_receipt = self.claim(self.second, OTHER)
            self.pending(other_receipt, self.second, OTHER_NAME, OTHER)
            self.second.record_action(OTHER_NAME, OTHER, 'not_submitted', 'synthetic unsent',
                                      operation_id=other_receipt.operation_id, continuation=True)
            self.assertTrue(self.gate.settle(self.second, other_receipt))
        receipt = self.claim()
        context = self.gate.registry.recovery_for(receipt)
        self.assertEqual(context['prior_checkpoint'], first_receipt.generation)
        self.assertNotEqual(context['prior_checkpoint'], receipt.generation - 1)
        self.assertEqual(self.gate.recover_declared(self.first, receipt), 1)
        event = self.recovery_events(self.first)[-1]
        self.assertEqual(event['checkpoint_before'], first_receipt.generation)
        self.assertEqual(event['checkpoint_after'], receipt.generation)
        self.gate.check(self.second)

    def test_wrong_local_checkpoint_cannot_be_repaired_by_guessing(self):
        receipt = self.claim()
        anchor = ledger_anchor(self.first)
        _save_anchor(self.first, anchor['epoch'], receipt.generation + 1, anchor['roster_hash'])
        before = self.local_snapshot(self.first)
        with self.assertRaises(GuardError):
            self.gate.recover_declared(self.first, receipt)
        self.assertEqual(self.local_snapshot(self.first), before)
        self.assert_still_owned(receipt)

    def test_wrong_roster_cannot_be_repaired_as_an_orphan_claim(self):
        receipt = self.claim()
        anchor = ledger_anchor(self.first)
        _save_anchor(self.first, anchor['epoch'], anchor['checkpoint'], 'f' * 64)
        before = self.local_snapshot(self.first)
        with self.assertRaises(GuardError):
            self.gate.recover_declared(self.first, receipt)
        self.assertEqual(self.local_snapshot(self.first), before)
        self.assert_still_owned(receipt)

    def test_copied_ledger_cannot_recover_original_claim(self):
        receipt = self.claim()
        destination = self.folder / 'copied-original'
        shutil.copytree(self.first.folder, destination)
        copy = DataStore(destination)
        self.assertEqual(ledger_id(copy), ledger_id(self.first))
        original_before = self.local_snapshot(self.first)
        copy_before = self.local_snapshot(copy)
        with self.assertRaises(GuardError):
            self.gate.recover_declared(copy, receipt)
        self.assertEqual(self.local_snapshot(self.first), original_before)
        self.assertEqual(self.local_snapshot(copy), copy_before)
        self.assert_still_owned(receipt)

    def test_tombstone_rejects_every_old_callback_without_touching_new_operation(self):
        receipt = self.claim()
        self.pending(receipt)
        self.requested(receipt)
        self.assertEqual(self.gate.recover_declared(self.first, receipt), 1)
        next_receipt = self.claim()
        self.pending(next_receipt)
        before = self.local_snapshot(self.first)
        for status in ('pending', 'requested', 'prepared', 'confirmed', 'unknown_result',
                       'unknown', 'failed', 'absent_verified', 'present_verified',
                       'observation_failed', 'cancelled', 'not_submitted'):
            with self.subTest(status=status), self.assertRaises(DeclaredRecoveryClosed):
                self.first.record_action(NAME, TARGET, status, 'synthetic late callback',
                    operation_id=receipt.operation_id,
                    continuation=status not in {'pending', 'requested'},
                    responsibility=receipt.to_dict() if status == 'pending' else None)
            self.assertEqual(self.local_snapshot(self.first), before)
        self.assertEqual(self.first.latest_action_record(TARGET)['operation_id'], next_receipt.operation_id)
        with self.first._connect() as db:
            self.assertEqual(db.execute('SELECT active FROM bindings WHERE tracking_id=?', (TARGET,)).fetchone()[0], 1)

    def test_recovery_audit_transaction_failure_has_no_partial_tombstone_or_checkpoint(self):
        receipt = self.claim()
        before = self.local_snapshot(self.first)
        with patch.object(self.first, '_event', side_effect=OSError('synthetic recovery event failure')):
            with self.assertRaises(OSError):
                self.gate.recover_declared(self.first, receipt)
        self.assertEqual(self.local_snapshot(self.first), before)
        self.assertIsNotNone(self.gate.registry.recovery_for(receipt)['recovery_id'])
        self.assert_still_owned(receipt)
        with self.assertRaises(GuardError):
            self.gate.registry.mark_possible_send(receipt)
        self.assertEqual(self.gate.recover_declared(self.first, receipt), 1)
        self.assertEqual(len(self.recovery_events(self.first)), 1)

    def test_recovery_export_failure_reopens_and_completes_same_audit_without_duplicate(self):
        receipt = self.claim()
        self.pending(receipt)
        self.requested(receipt)
        with patch.object(self.first, 'export_logs', side_effect=OSError('synthetic recovery export failure')):
            with self.assertRaises(OSError):
                self.gate.recover_declared(self.first, receipt)
        self.assertEqual(self.first.latest_action(TARGET), 'cancelled')
        before = self.local_snapshot(self.first)
        self.assert_still_owned(receipt)
        reopened = DataStore(self.first.folder)
        self.assertEqual(self.gate.recover_declared(reopened, receipt), 1)
        self.assertEqual(self.local_snapshot(reopened), before)
        self.assertEqual(len(self.recovery_events(reopened)), 1)
        self.gate.check(self.second)

    def test_failure_after_cancelled_append_rolls_back_the_entire_recovery_transaction(self):
        receipt = self.claim()
        self.pending(receipt)
        self.requested(receipt)
        before = self.local_snapshot(self.first)
        real_event = self.first._event
        appended = []
        def fail_final_audit(db, kind, payload, *args, **kwargs):
            if kind == 'responsibility_recovery':
                raise OSError('synthetic failure after cancelled and tombstone writes')
            result = real_event(db, kind, payload, *args, **kwargs)
            if kind == 'action':
                appended.append(payload['status'])
            return result
        with patch.object(self.first, '_event', side_effect=fail_final_audit):
            with self.assertRaises(OSError):
                self.gate.recover_declared(self.first, receipt)
        self.assertEqual(appended, ['cancelled'], 'The failure must happen after the local terminal insert.')
        self.assertEqual(self.local_snapshot(self.first), before)
        self.assertEqual(self.first.latest_action(TARGET), 'requested')
        self.assert_still_owned(receipt)
        self.assertEqual(self.gate.recover_declared(self.first, receipt), 1)
        self.assertEqual([r['status'] for r in action_history(self.first)], ['pending', 'requested', 'cancelled'])

    def test_late_pending_between_preflight_and_fence_is_rechecked_by_local_transaction(self):
        from quota_guard.declared_recovery import check_local_recoverable
        receipt = self.claim()
        injected = []
        def pending_after_preflight(store, fence):
            check_local_recoverable(store, fence)
            # Inject exactly once before the recovery transaction. The final
            # post-export validation must read the tombstone without inserting
            # a second, deliberately rejected late callback.
            if not injected:
                self.pending(receipt)
                injected.append(True)
        with patch('quota_guard.declared_recovery.check_local_recoverable',
                   side_effect=pending_after_preflight):
            self.assertEqual(self.gate.recover_declared(self.first, receipt), 1)
        self.assertEqual([r['status'] for r in action_history(self.first)], ['pending', 'cancelled'])
        self.assertEqual(self.recovery_events(self.first)[0]['origin'], 'recorded_intent')
        with self.assertRaises(GuardError):
            self.gate.registry.mark_possible_send(receipt)
        self.gate.check(self.second)

    def test_upgraded_v1_claim_without_original_checkpoint_is_not_guessed_recoverable(self):
        receipt = self.claim()
        self.pending(receipt)
        self.requested(receipt)
        # Construct the exact old four-table schema from this closed synthetic
        # scope. Neither an actual registry nor an open SQLite file is changed.
        self.assertTrue(self.gate.registry.db_path.resolve().is_relative_to(self.folder))
        with closing(sqlite3.connect(self.gate.registry.db_path)) as db:
            with db:
                db.execute('DROP TABLE obligation_recovery')
                db.execute('DROP TABLE membership_plan')
                db.execute('UPDATE metadata SET schema=1')
                db.execute('PRAGMA user_version=1')
        anchor = json.loads(self.gate.registry.anchor_path.read_text(encoding='utf-8'))
        anchor['schema'] = 1
        self.gate.registry.anchor_path.write_text(json.dumps(anchor), encoding='utf-8')
        before = self.local_snapshot(self.first)
        self.assertTrue(self.gate.registry.upgrade_v1())
        context = self.gate.registry.recovery_for(receipt)
        self.assertIsNone(context['prior_checkpoint'])
        with self.assertRaises(GuardError):
            self.gate.recover_declared(self.first, receipt)
        self.assertEqual(self.local_snapshot(self.first), before)
        self.assertIsNone(self.gate.registry.recovery_for(receipt)['recovery_id'])
        self.assert_still_owned(receipt)

    def test_possible_send_cannot_be_reclassified_as_never_authorized(self):
        receipt = self.claim()
        self.pending(receipt)
        self.gate.possible_send(self.first, receipt, write_requested=lambda: self.requested(receipt))
        before = self.local_snapshot(self.first)
        with self.assertRaises(GuardError):
            self.gate.recover_declared(self.first, receipt)
        self.assertEqual(self.local_snapshot(self.first), before)
        with self.first._connect() as db:
            self.assertIsNone(db.execute('SELECT value FROM meta WHERE key=?',
                                        (TOMBSTONE_PREFIX + receipt.operation_id,)).fetchone())
        self.assert_still_owned(receipt)


if __name__ == '__main__':
    unittest.main()
