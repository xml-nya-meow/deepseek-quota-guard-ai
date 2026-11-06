"""Real local ledger/registry coordination without Tk, browser or network.

Every synthetic ledger and registry lives under the workspace TEMP directory.
The tests do not open or initialize the actual Windows-user responsibility area.
"""
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from quota_guard.core import DataStore, GuardError, Person, TZ
from quota_guard.responsibility_gate import (
    ResponsibilityGate, action_history, ledger_anchor, ledger_id, target_hash,
)


TEMP_ROOT = Path(__file__).resolve().parents[2] / 'TEMP'
TARGET = 'synthetic-gate-target-one'
SECOND = 'synthetic-gate-target-two'
CONTROL = 'synthetic-gate-control'
NAME = 'Synthetic first'
SECOND_NAME = 'Synthetic second'
ACCOUNT = 'synthetic-gate-account'


class ResponsibilityGateTests(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='gate-', dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name)
        self.keys = [{'name': NAME, 'tracking_id': TARGET},
                     {'name': SECOND_NAME, 'tracking_id': SECOND},
                     {'name': 'Synthetic control', 'tracking_id': CONTROL}]
        self.first = self.make_store('X', NAME)
        self.second = self.make_store('Y', SECOND_NAME)
        self.gate = ResponsibilityGate(self.folder / 'shared' / 'responsibility')

    def make_store(self, directory, name):
        store = DataStore(self.folder / directory)
        store.initialize(2, '100', datetime.now(TZ).date().isoformat())
        store.save_people([Person(name, Decimal(0))])
        store.bind_keys(self.keys, ACCOUNT)
        return store

    def initialize(self, stores=None):
        self.gate.initialize([self.first, self.second] if stores is None else stores)

    def pending(self, receipt, store=None, *, name=NAME, target=TARGET):
        store = self.first if store is None else store
        store.record_action(name, target, 'pending', 'synthetic pending',
                            operation_id=receipt.operation_id, responsibility=receipt.to_dict())

    def requested(self, receipt):
        self.first.record_action(NAME, TARGET, 'requested', 'synthetic requested',
            operation_id=receipt.operation_id,
            recovery_evidence={'schema': 1, 'account_id': ACCOUNT,
                'target_tracking_id': TARGET, 'known_tracking_ids': [TARGET, SECOND, CONTROL],
                'source': 'prepared_http_inventory_v1'})

    def outcome(self, receipt, status='unknown_result', store=None, *, name=NAME, target=TARGET):
        store = self.first if store is None else store
        store.record_action(name, target, status, 'synthetic ' + status,
                            operation_id=receipt.operation_id, continuation=True)

    def begin(self, *, possible=True):
        receipt = self.gate.claim(self.first, uuid4().hex, TARGET)
        self.pending(receipt)
        if possible:
            self.gate.possible_send(self.first, receipt, write_requested=lambda: self.requested(receipt))
        else:
            self.requested(receipt)
        return receipt

    def test_order_is_shared_declared_then_atomic_local_receipt_then_requested_then_possible(self):
        self.initialize()
        receipt = self.gate.claim(self.first, uuid4().hex, TARGET)
        self.assertEqual(self.gate.registry.open_obligations()[0]['phase'], 'declared')
        self.assertEqual(action_history(self.first), [])
        self.assertEqual(ledger_anchor(self.first)['checkpoint'], 0)
        registered = next(item for item in self.gate.registry.registered_ledgers()
                          if item['ledger_id'] == ledger_id(self.first))
        self.assertEqual(registered['checkpoint'], receipt.generation)
        with self.assertRaises(GuardError):
            self.gate.possible_send(self.first, receipt, write_requested=lambda: None)
        self.pending(receipt)
        reopened = DataStore(self.first.folder)
        self.assertEqual(ledger_anchor(reopened)['checkpoint'], receipt.generation)
        self.assertEqual(reopened.latest_action_record(TARGET)['responsibility'], receipt.to_dict())
        with self.assertRaises(GuardError):
            self.gate.possible_send(reopened, receipt, write_requested=lambda: None)
        phases = []
        def write_requested():
            phases.append(self.gate.registry.open_obligations()[0]['phase'])
            self.requested(receipt)
            phases.append(self.gate.registry.open_obligations()[0]['phase'])
        self.assertTrue(self.gate.possible_send(reopened, receipt, write_requested=write_requested))
        self.assertEqual(phases, ['declared', 'declared'])
        self.assertEqual(self.gate.registry.open_obligations()[0]['phase'], 'possible_send')
        self.outcome(receipt)
        events = action_history(DataStore(self.first.folder))
        self.assertEqual([event['status'] for event in events], ['pending', 'requested', 'unknown_result'])
        self.assertTrue(all(event['responsibility'] == receipt.to_dict() for event in events))
        self.assertEqual(events[-1]['recovery_evidence']['known_tracking_ids'], sorted([TARGET, SECOND, CONTROL]))

    def test_pending_event_failure_rolls_back_receipt_and_checkpoint_but_shared_slot_remains(self):
        self.initialize()
        receipt = self.gate.claim(self.first, uuid4().hex, TARGET)
        with patch.object(self.first, '_event', side_effect=OSError('synthetic event insertion failure')):
            with self.assertRaises(OSError):
                self.pending(receipt)
        reopened = DataStore(self.first.folder)
        self.assertEqual(ledger_anchor(reopened)['checkpoint'], 0)
        self.assertEqual(action_history(reopened), [])
        self.assertEqual(self.gate.registry.open_obligations()[0]['receipt'], receipt.to_dict())
        with self.assertRaises(GuardError):
            self.gate.check(self.second)

    def test_pending_export_failure_keeps_atomic_receipt_and_checkpoint_without_send(self):
        self.initialize()
        receipt = self.gate.claim(self.first, uuid4().hex, TARGET)
        with patch.object(self.first, 'export_logs', side_effect=OSError('synthetic pending export failure')):
            with self.assertRaises(OSError):
                self.pending(receipt)
        reopened = DataStore(self.first.folder)
        self.assertEqual(reopened.latest_action_record(TARGET)['responsibility'], receipt.to_dict())
        self.assertEqual(ledger_anchor(reopened)['checkpoint'], receipt.generation)
        with self.assertRaises(GuardError):
            self.gate.possible_send(reopened, receipt, write_requested=lambda: None)
        self.assertEqual(self.gate.registry.open_obligations()[0]['phase'], 'declared')

    def test_requested_export_failure_cannot_later_be_promoted_to_possible_send(self):
        self.initialize()
        receipt = self.gate.claim(self.first, uuid4().hex, TARGET)
        self.pending(receipt)
        with patch.object(self.first, 'export_logs', side_effect=OSError('synthetic requested export failure')):
            with self.assertRaises(OSError):
                self.gate.possible_send(self.first, receipt, write_requested=lambda: self.requested(receipt))
        reopened = DataStore(self.first.folder)
        self.assertEqual(reopened.latest_action_record(TARGET)['status'], 'requested')
        self.assertEqual(self.gate.registry.open_obligations()[0]['phase'], 'declared')
        retry = Mock()
        with self.assertRaises(GuardError):
            self.gate.possible_send(reopened, receipt, write_requested=retry)
        retry.assert_not_called()
        reopened.export_logs()
        with self.assertRaises(GuardError):
            self.gate.possible_send(reopened, receipt, write_requested=retry)
        retry.assert_not_called()
        self.assertEqual(self.gate.registry.open_obligations()[0]['phase'], 'declared')

    def test_context_changed_after_requested_keeps_declared_and_cannot_resume_saved_request(self):
        self.initialize()
        receipt = self.gate.claim(self.first, uuid4().hex, TARGET)
        self.pending(receipt)
        self.assertFalse(self.gate.possible_send(self.first, receipt,
            write_requested=lambda: self.requested(receipt), context_valid=lambda: False))
        self.assertEqual(self.first.latest_action_record(TARGET)['status'], 'requested')
        self.assertEqual(self.gate.registry.open_obligations()[0]['phase'], 'declared')
        retry = Mock()
        with self.assertRaises(GuardError):
            self.gate.possible_send(self.first, receipt, write_requested=retry)
        retry.assert_not_called()

    def test_wrong_target_or_operation_receipt_cannot_be_installed_on_pending(self):
        self.initialize()
        receipt = self.gate.claim(self.first, uuid4().hex, TARGET)
        for name, target, operation in ((NAME, TARGET, uuid4().hex), (SECOND_NAME, SECOND, receipt.operation_id)):
            with self.subTest(target=target), self.assertRaises(GuardError):
                self.first.record_action(name, target, 'pending', 'synthetic mismatched receipt',
                    operation_id=operation, responsibility=receipt.to_dict())
        self.assertEqual(action_history(self.first), [])
        self.assertEqual(ledger_anchor(self.first)['checkpoint'], 0)

    def test_original_receipt_cannot_be_replaced_on_requested_or_continuation(self):
        self.initialize()
        receipt = self.gate.claim(self.first, uuid4().hex, TARGET)
        self.pending(receipt)
        for status, continuation in (('requested', False), ('unknown_result', True)):
            with self.subTest(status=status), self.assertRaises(GuardError):
                self.first.record_action(NAME, TARGET, status, 'synthetic receipt backfill',
                    operation_id=receipt.operation_id, continuation=continuation,
                    responsibility=receipt.to_dict())
        self.assertEqual([row['status'] for row in action_history(self.first)], ['pending'])

    def test_unknown_and_present_remain_blocking_then_absence_settles_exact_old_operation(self):
        self.initialize()
        receipt = self.begin()
        self.outcome(receipt)
        with self.assertRaises(GuardError):
            self.gate.check(self.second)
        self.outcome(receipt, 'present_verified')
        self.assertFalse(self.gate.settle(self.first, receipt))
        with self.assertRaises(GuardError):
            self.gate.claim(self.second, uuid4().hex, SECOND)
        self.outcome(receipt, 'absent_verified')
        self.assertTrue(self.gate.settle(self.first, receipt))
        self.gate.check(self.second)
        next_receipt = self.gate.claim(self.second, uuid4().hex, SECOND)
        self.pending(next_receipt, self.second, name=SECOND_NAME, target=SECOND)
        self.assertFalse(self.gate.settle(self.first, receipt))
        self.assertEqual(self.gate.registry.open_obligations()[0]['receipt'], next_receipt.to_dict())

    def test_terminal_local_fact_precedes_shared_release_and_export_failure_leaves_slot(self):
        self.initialize()
        receipt = self.begin()
        self.outcome(receipt, 'absent_verified')
        self.assertEqual(len(self.gate.registry.open_obligations()), 1)
        with patch.object(self.first, 'export_logs', side_effect=OSError('synthetic terminal export failure')):
            with self.assertRaises(OSError):
                self.gate.settle(self.first, receipt)
        self.assertEqual(len(self.gate.registry.open_obligations()), 1)
        with self.assertRaises(GuardError):
            self.gate.check(self.second)
        self.gate.reconcile(DataStore(self.first.folder))
        self.assertEqual(self.gate.registry.open_obligations(), [])
        self.assertEqual([row['status'] for row in action_history(self.first)],
                         ['pending', 'requested', 'absent_verified'])

    def test_old_local_unknown_backup_blocks_even_with_same_checkpoint_and_registry_settled(self):
        self.initialize()
        receipt = self.begin()
        self.outcome(receipt)
        old_unknown = self.first.db_path.read_bytes()
        checkpoint = ledger_anchor(self.first)
        self.outcome(receipt, 'absent_verified')
        self.assertTrue(self.gate.settle(self.first, receipt))
        self.assertEqual(self.gate.registry.open_obligations(), [])
        self.first.db_path.write_bytes(old_unknown)
        reopened = DataStore(self.first.folder)
        self.assertEqual(ledger_anchor(reopened), checkpoint)
        self.assertEqual(reopened.latest_action_record(TARGET)['status'], 'unknown_result')
        with self.assertRaises(GuardError):
            self.gate.check(self.second)
        with self.assertRaises(GuardError):
            self.gate.claim(self.second, uuid4().hex, SECOND)
        self.assertEqual(self.gate.registry.open_obligations(), [])
        self.assertEqual(action_history(self.second), [])

    def test_missing_registry_is_not_implicitly_created_and_old_anchors_prevent_reinitialization(self):
        with self.assertRaises(GuardError):
            self.gate.check(self.first)
        self.assertFalse(self.gate.registry.directory.exists())
        self.initialize()
        self.gate.registry.db_path.rename(self.folder / 'saved-registry.sqlite3')
        with self.assertRaises(GuardError):
            self.gate.check(self.first)
        with self.assertRaises(GuardError):
            self.gate.initialize([self.first, self.second])
        self.assertFalse(self.gate.registry.db_path.exists())

    def test_declared_legacy_uncertainty_migrates_and_blocks_until_original_absence(self):
        operation = uuid4().hex
        self.first.record_action(NAME, TARGET, 'pending', 'synthetic legacy pending', operation_id=operation)
        self.first.record_action(NAME, TARGET, 'requested', 'synthetic legacy requested', operation_id=operation)
        self.first.record_action(NAME, TARGET, 'unknown_result', 'synthetic legacy unknown',
                                 operation_id=operation, continuation=True)
        self.initialize()
        obligation = self.gate.registry.open_obligations()[0]
        self.assertEqual(obligation['origin'], 'migration')
        self.assertEqual(obligation['receipt']['operation_id'], operation)
        self.assertEqual(obligation['receipt']['target_hash'], target_hash(TARGET))
        self.assertNotIn('responsibility', self.first.latest_action_record(TARGET),
                         'Migration must not invent an original pre-send receipt in old events.')
        with self.assertRaises(GuardError):
            self.gate.check(self.second)
        self.first.record_action(NAME, TARGET, 'absent_verified', 'synthetic legacy absence',
                                 operation_id=operation, continuation=True)
        self.assertTrue(self.gate.settle_operation(self.first, operation))
        self.gate.check(self.second)

    def test_unidentifiable_legacy_operation_rejects_initialization_before_registry_creation(self):
        self.first.record_action(NAME, TARGET, 'pending', 'synthetic unnumbered legacy')
        with self.assertRaises(GuardError):
            self.initialize()
        self.assertFalse(self.gate.registry.directory.exists())
        self.assertIsNone(ledger_anchor(self.first))

    def test_interrupted_migration_keeps_initializing_and_does_not_recreate_empty_scope(self):
        from quota_guard import membership_recovery
        operation = uuid4().hex
        self.first.record_action(NAME, TARGET, 'pending', 'synthetic old unresolved', operation_id=operation)
        original_history = action_history(self.first)
        apply_member = membership_recovery._apply_member
        applied = []
        def fail_second_member(plan, member):
            if applied:
                raise RuntimeError('synthetic membership interruption')
            apply_member(plan, member)
            applied.append(member['ledger_id'])
        with patch('quota_guard.membership_recovery._apply_member', side_effect=fail_second_member):
            with self.assertRaises(RuntimeError):
                self.initialize()
        state = self.gate.registry.snapshot()
        self.assertEqual(state['phase'], 'initializing')
        # A failed execution rolls back the shared import, but the original
        # operation manifest and successful local witness remain recoverable.
        self.assertEqual(state['open_obligations'], [])
        plan = self.gate.registry.membership_plan()
        self.assertEqual(plan['state'], 'pending')
        self.assertEqual([o['operation_id'] for o in plan['payload']['operations']], [operation])
        self.assertEqual(sum(ledger_anchor(s) is not None for s in (self.first, self.second)), 1)
        with self.assertRaises(GuardError):
            self.gate.initialize([self.first, self.second])
        self.assertEqual(self.gate.registry.snapshot(), state)
        reopened = ResponsibilityGate(self.gate.registry.directory)
        self.assertEqual(reopened.reconcile(DataStore(self.second.folder)), 0)
        self.assertEqual(reopened.registry.snapshot()['phase'], 'ready')
        self.assertEqual(reopened.registry.membership_plan()['plan_id'], plan['plan_id'])
        self.assertEqual(reopened.registry.open_obligations()[0]['receipt']['operation_id'], operation)
        self.assertEqual(action_history(self.first), original_history)
        with self.assertRaises(GuardError):
            reopened.check(self.second)

    def test_same_target_in_two_origin_ledgers_keeps_both_old_operations(self):
        # Different original ledgers are independent responsibilities, even if
        # the platform target is the same. Neither operation may disappear.
        self.second.save_people([Person(NAME, Decimal(0))])
        self.second.bind_keys(self.keys, ACCOUNT)
        operations = [uuid4().hex, uuid4().hex]
        for store, operation in zip((self.first, self.second), operations):
            store.record_action(NAME, TARGET, 'pending', 'synthetic legacy origin', operation_id=operation)
        self.initialize()
        imported = self.gate.registry.open_obligations()
        self.assertEqual({a['receipt']['operation_id'] for a in imported}, set(operations))
        self.assertEqual({a['receipt']['ledger_id'] for a in imported},
                         {ledger_id(self.first), ledger_id(self.second)})
        self.assertEqual({a['receipt']['target_hash'] for a in imported}, {target_hash(TARGET)})
        for store, operation in zip((self.first, self.second), operations):
            with self.assertRaises(GuardError):
                self.gate.check(store)
            store.record_action(NAME, TARGET, 'absent_verified', 'synthetic original verification',
                                operation_id=operation, continuation=True)
            self.assertTrue(self.gate.settle_operation(store, operation))
        self.assertEqual(self.gate.registry.open_obligations(), [])
        self.gate.check(self.first)

    def test_read_only_observation_does_not_create_shared_obligation_or_mutate_checked_scope(self):
        self.initialize()
        database = self.gate.registry.db_path.read_bytes()
        anchor = self.gate.registry.anchor_path.read_bytes()
        operation = uuid4().hex
        self.first.record_action(NAME, TARGET, 'pending', 'synthetic read-only inspection',
                                 operation_id=operation, read_only=True)
        self.first.record_action(NAME, TARGET, 'observation_failed', 'synthetic read-only unavailable',
                                 operation_id=operation, continuation=True)
        self.gate.check(self.first)
        self.assertEqual(self.gate.registry.open_obligations(), [])
        self.assertEqual(self.gate.registry.db_path.read_bytes(), database)
        self.assertEqual(self.gate.registry.anchor_path.read_bytes(), anchor)

    def test_new_ledger_autojoins_existing_ready_scope_without_an_action_or_claim(self):
        self.initialize([self.first])
        self.assertIsNone(ledger_anchor(self.second))
        self.gate.check(self.second)
        self.assertEqual(ledger_anchor(self.second)['checkpoint'], 0)
        self.assertEqual(len(self.gate.registry.registered_ledgers()), 2)
        self.assertEqual(self.gate.registry.open_obligations(), [])
        self.assertEqual(action_history(self.second), [])

    def test_historical_ledger_even_with_no_open_action_cannot_silently_autojoin(self):
        self.initialize([self.first])
        operation = uuid4().hex
        self.second.record_action(SECOND_NAME, SECOND, 'pending', 'synthetic old pending', operation_id=operation)
        self.second.record_action(SECOND_NAME, SECOND, 'not_submitted', 'synthetic old unsent',
                                  operation_id=operation, continuation=True)
        self.assertEqual(self.second.unresolved_actions(), [])
        with self.assertRaises(GuardError):
            self.gate.check(self.second)
        self.assertIsNone(ledger_anchor(self.second))
        self.assertEqual(len(self.gate.registry.registered_ledgers()), 1)

    def test_old_paired_registry_backup_cannot_erase_later_unknown_member(self):
        self.initialize()
        old_database = self.gate.registry.db_path.read_bytes()
        old_anchor = self.gate.registry.anchor_path.read_bytes()
        old_roster = ledger_anchor(self.second)['roster_hash']
        third = self.make_store('Z', NAME)
        self.gate.check(third)
        self.assertNotEqual(ledger_anchor(self.second)['roster_hash'], old_roster)
        receipt = self.gate.claim(third, uuid4().hex, TARGET)
        self.pending(receipt, third)
        def requested():
            third.record_action(NAME, TARGET, 'requested', 'synthetic third requested',
                operation_id=receipt.operation_id,
                recovery_evidence={'schema': 1, 'account_id': ACCOUNT,
                    'target_tracking_id': TARGET, 'known_tracking_ids': [TARGET, SECOND, CONTROL],
                    'source': 'prepared_http_inventory_v1'})
        self.gate.possible_send(third, receipt, write_requested=requested)
        self.outcome(receipt, store=third)
        self.gate.registry.db_path.write_bytes(old_database)
        self.gate.registry.anchor_path.write_bytes(old_anchor)
        self.assertEqual(self.gate.registry.open_obligations(), [], 'This is the paired old idle snapshot.')
        self.assertEqual(len(self.gate.registry.registered_ledgers()), 2)
        self.assertEqual(third.latest_action_record(TARGET)['status'], 'unknown_result')
        with self.assertRaises(GuardError):
            self.gate.check(self.second)
        with self.assertRaises(GuardError):
            self.gate.claim(self.second, uuid4().hex, SECOND)
        self.assertEqual(action_history(self.second), [])

    def test_normal_settled_successor_joins_without_importing_a_second_old_operation(self):
        self.initialize()
        receipt = self.begin()
        self.outcome(receipt, 'absent_verified')
        self.assertTrue(self.gate.settle(self.first, receipt))
        successor = self.first.create_successor(self.folder / 'successor', keep_account=True)
        original = action_history(self.first)
        self.assertIsNone(ledger_anchor(successor))
        self.assertEqual(successor.latest_action_record(TARGET)['operation_id'], receipt.operation_id)
        self.gate.check(successor)
        self.assertIsNotNone(ledger_anchor(successor))
        self.assertEqual(len(self.gate.registry.registered_ledgers()), 3)
        self.assertEqual(self.gate.registry.open_obligations(), [])
        self.assertEqual(self.gate.registry.snapshot()['generation'], receipt.generation)
        self.assertIsNone(self.gate.registry.receipt_for(ledger_id(successor), receipt.operation_id))
        self.assertEqual(action_history(self.first), original)
        self.assertEqual([row['status'] for row in action_history(successor)], ['absent_verified'])


if __name__ == '__main__':
    unittest.main()
