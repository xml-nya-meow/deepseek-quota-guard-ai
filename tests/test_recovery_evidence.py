"""Disk-backed offline recovery of a prepared, previously unbound survivor.

Platform inventory is a fixture. No browser, HTTP, model, or permit is created.
"""
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from quota_guard.browser_service import BrowserService
from quota_guard.core import DataStore, GuardError, Person, TZ
from quota_guard.recovery_evidence import RECOVERY_SOURCE, validate_recovery_evidence


TARGET = 'synthetic-target-001'
CONTROL = 'synthetic-survivor-002'
OTHER = 'synthetic-other-003'
NAME = '合成目标'
ACCOUNT = 'synthetic-account'


def recovery_value():
    return {'schema': 1, 'account_id': ACCOUNT, 'target_tracking_id': TARGET,
            'known_tracking_ids': [TARGET, CONTROL], 'source': RECOVERY_SOURCE}


class RecoveryEvidenceValidationTests(unittest.TestCase):
    def validate(self, value):
        return validate_recovery_evidence(value, target_tracking_id=TARGET, account_id=ACCOUNT)

    def test_canonical_copy_is_sorted_and_independent(self):
        value = recovery_value()
        normalized = self.validate(value)
        self.assertEqual(normalized['known_tracking_ids'], sorted([TARGET, CONTROL]))
        value['known_tracking_ids'].append(OTHER)
        self.assertNotIn(OTHER, normalized['known_tracking_ids'])

    def test_invalid_shape_identity_or_secret_is_rejected(self):
        invalid = [None, [], {}, recovery_value() | {'cookie': 'forbidden'},
                   recovery_value() | {'schema': True}, recovery_value() | {'schema': 2},
                   recovery_value() | {'source': 'current_unverified_list'},
                   recovery_value() | {'account_id': 'different-account'},
                   recovery_value() | {'target_tracking_id': OTHER},
                   recovery_value() | {'known_tracking_ids': [TARGET]},
                   recovery_value() | {'known_tracking_ids': [TARGET, TARGET]},
                   recovery_value() | {'known_tracking_ids': [CONTROL, OTHER]},
                   recovery_value() | {'known_tracking_ids': [TARGET, 'sk-not-a-tracking-id']},
                   recovery_value() | {'known_tracking_ids': [TARGET, 'SK-not-a-tracking-id']},
                   recovery_value() | {'known_tracking_ids': [TARGET, 'bad\nidentity']},
                   recovery_value() | {'known_tracking_ids': [TARGET] + [f'control-{n:04}' for n in range(200)]}]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(GuardError):
                self.validate(value)


class RecoveryEvidenceStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name) / 'ledger'
        self.store = DataStore(self.folder)
        self.store.initialize(2, '100', datetime.now(TZ).date().isoformat())
        self.store.save_people([Person(NAME, Decimal('0'))])
        self.keys = [{'name': NAME, 'tracking_id': TARGET},
                     {'name': 'DeepSeek Harness', 'tracking_id': CONTROL}]
        self.store.bind_keys(self.keys, ACCOUNT)
        self.operation = 'synthetic-original-operation'

    def record(self, status, *, operation=None, **kwargs):
        self.store.record_action(NAME, TARGET, status, 'synthetic ' + status,
            operation_id=self.operation if operation is None else operation, **kwargs)

    def begin(self):
        self.record('pending', evidence=self.store.action_evidence(NAME, TARGET))
        self.record('requested', recovery_evidence=recovery_value())

    def verify(self, known, current=None):
        if current is None:
            current = self.keys[1:]
        fake = SimpleNamespace(_keys=lambda: current, known_keys=set(), anchor=None)
        return BrowserService._verify_revoke(fake, NAME, TARGET, known, http_keys=current)

    def test_reopened_ledger_keeps_unbound_survivor_and_original_operation(self):
        self.begin()
        self.record('unknown_result', continuation=True)
        before = self.store.unresolved_actions()
        self.store = DataStore(self.folder)
        self.assertEqual(self.store.active_tracking_ids(), {TARGET})
        expected = self.store.recovery_tracking_ids(TARGET, self.operation)
        self.assertEqual(expected, {TARGET, CONTROL})
        self.assertEqual(self.verify(expected), {'absent': True, 'present': False})
        self.assertEqual(self.store.unresolved_actions(), before)
        self.record('absent_verified', continuation=True)
        self.assertEqual(self.store.unresolved_actions(), [])
        last = self.store.latest_action_record(TARGET)
        self.assertEqual(last['operation_id'], self.operation)
        self.assertEqual(last['recovery_evidence']['known_tracking_ids'], sorted(expected))

    def test_unbound_normal_survivor_follows_same_identity_contract(self):
        self.keys[1]['name'] = '未受管普通对照'
        self.begin()
        reopened = DataStore(self.folder)
        self.assertEqual(self.verify(reopened.recovery_tracking_ids(TARGET, self.operation)),
                         {'absent': True, 'present': False})

    def test_presence_never_clears_original_possible_submission(self):
        self.begin()
        self.record('unknown_result', continuation=True)
        expected = self.store.recovery_tracking_ids(TARGET, self.operation)
        self.assertEqual(self.verify(expected, self.keys), {'absent': False, 'present': True})
        self.record('present_verified', continuation=True)
        self.assertEqual(self.store.unresolved_actions()[0]['operation_id'], self.operation)
        self.assertEqual(self.store.recovery_tracking_ids(TARGET, self.operation), expected)
        with self.assertRaises(GuardError):
            self.record('pending', operation='would-duplicate-submission')

    def test_missing_or_unrelated_survivor_still_cannot_confirm_absence(self):
        self.begin()
        expected = self.store.recovery_tracking_ids(TARGET, self.operation)
        for current in ([], [{'name': '陌生对照', 'tracking_id': OTHER}]):
            with self.subTest(current=current), self.assertRaises(GuardError):
                self.verify(expected, current)
        self.assertEqual(len(self.store.unresolved_actions()), 1)

    def test_legacy_record_retains_existing_fallback_without_new_evidence(self):
        self.record('pending')
        self.record('requested')
        before = self.store.events()
        known = DataStore(self.folder).recovery_tracking_ids(TARGET, self.operation)
        self.assertEqual(known, {TARGET})
        with self.assertRaises(GuardError):
            self.verify(known)
        self.assertEqual(self.store.events(), before)
        self.assertNotIn('recovery_evidence', self.store.latest_action_record(TARGET))

    def test_explicit_operation_cannot_borrow_or_invent_another_context(self):
        self.begin()
        with self.assertRaises(GuardError):
            self.store.recovery_tracking_ids(TARGET, 'unknown-operation')
        with self.assertRaises(GuardError):
            self.store.recovery_tracking_ids(CONTROL, self.operation)
        # Absence of an operation remains the legacy read-only fallback.
        self.assertEqual(self.store.recovery_tracking_ids(TARGET, None), {TARGET})

    def test_context_cannot_be_created_outside_first_requested_transition(self):
        for status, kwargs in [('pending', {}), ('unknown_result', {'continuation': True}),
                               ('requested', {'continuation': True})]:
            with self.subTest(status=status), self.assertRaises(GuardError):
                self.record(status, recovery_evidence=recovery_value(), **kwargs)
        with self.assertRaises(GuardError):
            self.record('requested', recovery_evidence=recovery_value())
        self.record('pending', read_only=True)
        with self.assertRaises(GuardError):
            self.record('requested', recovery_evidence=recovery_value())

    def test_requested_context_is_immutable_and_cannot_regrant(self):
        self.begin()
        before = self.store.events()
        for value in (recovery_value(), recovery_value() | {'known_tracking_ids': [TARGET, OTHER]}, None):
            with self.subTest(value=value), self.assertRaises(GuardError):
                self.record('requested', recovery_evidence=value)
            self.assertEqual(self.store.events(), before)
        with self.assertRaises(GuardError):
            self.record('unknown_result', continuation=True, recovery_evidence=recovery_value())

    def test_legacy_possible_submission_cannot_be_backfilled_from_new_list(self):
        self.record('pending')
        self.record('requested')
        before = self.store.events()
        with self.assertRaises(GuardError):
            self.record('requested', recovery_evidence=recovery_value())
        self.assertEqual(self.store.events(), before)

    def test_invalid_context_rolls_back_requested_event(self):
        self.record('pending')
        before = self.store.events()
        with self.assertRaises(GuardError):
            self.record('requested', recovery_evidence=recovery_value() | {'account_id': 'different'})
        self.assertEqual(self.store.events(), before)
        self.assertEqual(self.store.latest_action_record(TARGET)['status'], 'pending')

    def test_export_failure_keeps_committed_evidence_and_recovery_without_regrant(self):
        self.record('pending')
        with patch.object(self.store, 'export_logs', side_effect=GuardError('synthetic export failure')):
            with self.assertRaises(GuardError):
                self.record('requested', recovery_evidence=recovery_value())
        reopened = DataStore(self.folder)
        self.assertEqual(reopened.recovery_tracking_ids(TARGET, self.operation), {TARGET, CONTROL})
        self.assertEqual(reopened.latest_action_record(TARGET)['status'], 'requested')
        with self.assertRaises(GuardError):
            self.record('requested', recovery_evidence=recovery_value())
        self.store.export_logs()

    def test_corrupted_evidence_never_silently_falls_back_or_becomes_foreign_account(self):
        self.begin()
        with self.store._connect() as db:
            number, payload = db.execute("SELECT id,payload FROM events WHERE kind='action' ORDER BY id DESC LIMIT 1").fetchone()
            action = json.loads(payload)
            action['recovery_evidence']['account_id'] = 'different-account'
            db.execute('UPDATE events SET payload=? WHERE id=?', (json.dumps(action), number))
        with self.assertRaises(GuardError):
            self.store.recovery_tracking_ids(TARGET, self.operation)
        with self.assertRaises(GuardError):
            self.record('unknown_result', continuation=True)
        self.assertEqual(self.store.latest_action_record(TARGET)['status'], 'requested')

    def test_conflicting_historical_context_is_rejected(self):
        self.begin()
        self.record('unknown_result', continuation=True)
        with self.store._connect() as db:
            number, payload = db.execute("SELECT id,payload FROM events WHERE kind='action' ORDER BY id DESC LIMIT 1").fetchone()
            action = json.loads(payload)
            action['recovery_evidence']['known_tracking_ids'] = [TARGET, OTHER]
            db.execute('UPDATE events SET payload=? WHERE id=?', (json.dumps(action), number))
        with self.assertRaises(GuardError):
            self.store.recovery_tracking_ids(TARGET, self.operation)

    def test_missing_workbook_and_same_account_successor_preserve_context(self):
        self.begin()
        self.record('unknown_result', continuation=True)
        successor = self.store.create_successor(self.folder.parent / 'successor', keep_account=True)
        self.assertEqual(successor.recovery_tracking_ids(TARGET, self.operation), {TARGET, CONTROL})
        successor.workbook_path.unlink()
        self.assertEqual(successor.recovery_tracking_ids(TARGET, self.operation), {TARGET, CONTROL})
        successor.record_action(NAME, TARGET, 'absent_verified', 'synthetic resolved',
                                operation_id=self.operation, continuation=True)
        self.assertEqual(successor.unresolved_actions(), [])
        self.assertEqual(self.store.unresolved_actions()[0]['operation_id'], self.operation)

    def test_context_is_recovered_from_original_event_not_only_latest_payload(self):
        self.begin()
        # Compatibility with a historical writer that retained the action but
        # omitted this new field on a later result. Original evidence is intact.
        with self.store._connect() as db:
            self.store._event(db, 'action', {'name': NAME, 'tracking_id': TARGET,
                'operation_id': self.operation, 'status': 'unknown_result', 'detail': 'legacy continuation'})
        self.assertNotIn('recovery_evidence', self.store.latest_action_record(TARGET))
        self.assertEqual(self.store.recovery_tracking_ids(TARGET, self.operation), {TARGET, CONTROL})
        successor = self.store.create_successor(self.folder.parent / 'successor', keep_account=True)
        self.assertEqual(successor.recovery_tracking_ids(TARGET, self.operation), {TARGET, CONTROL})

    def test_binding_scopes_include_retired_and_active_without_workbook(self):
        self.begin()
        self.record('absent_verified', continuation=True)
        replacement = 'synthetic-replacement-004'
        # Legacy/reconciled multi-binding history is a database fixture. The
        # ordinary binding API deliberately refuses automatic same-name rebinds.
        with self.store._connect() as db:
            db.execute('INSERT INTO bindings VALUES (?,?,1)', (replacement, NAME))
        before = self.store.events()
        self.store.workbook_path.unlink()
        expected = {NAME: tuple(sorted(((TARGET, False), (replacement, True))))}
        self.assertEqual(self.store.binding_scopes(), expected)
        self.assertEqual(self.store.events(), before)


if __name__ == '__main__':
    unittest.main()
