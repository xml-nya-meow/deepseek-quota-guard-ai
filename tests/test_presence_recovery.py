"""Offline presence observations never erase an earlier revocation duty."""
from datetime import datetime
from decimal import Decimal
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from quota_guard.core import DataStore, GuardError, Person, TZ


class PresenceRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.folder = Path(self.temporary.name) / 'ledger'
        self.store = DataStore(self.folder)
        self.store.initialize(2, '20', datetime.now(TZ).date().isoformat())
        self.store.save_people([Person('合成目标', Decimal('0'))])
        self.target = 'synthetic-presence-target'
        self.store.bind_keys([{'name': '合成目标', 'tracking_id': self.target}], 'synthetic-account')

    def record(self, status, operation='original', **kwargs):
        self.store.record_action('合成目标', self.target, status, 'synthetic ' + status,
                                 operation_id=operation, **kwargs)

    def begin_revocation(self):
        evidence = self.store.action_evidence('合成目标', self.target)
        self.record('pending', evidence=evidence)
        self.record('requested', evidence=evidence)
        self.record('unknown_result', continuation=True)
        return evidence

    def test_presence_preserves_original_duty_evidence_and_restart_block(self):
        evidence = self.begin_revocation()
        before = self.store.events()
        self.record('present_verified', continuation=True)
        record = self.store.latest_action_record(self.target)
        self.assertEqual(record['operation_id'], 'original')
        self.assertEqual(record['evidence'], evidence)
        self.assertTrue(record['requires_reconciliation'])
        self.assertEqual(self.store.events()[1:], before)
        restored = DataStore(self.folder)
        self.assertEqual(restored.unresolved_actions(), [record])
        self.assertIn(self.target, restored.active_tracking_ids())
        with self.assertRaises(GuardError):
            self.record('pending', operation='would-retry')
        self.assertEqual(restored.latest_action_record(self.target), record)

    def test_later_absence_can_settle_original_presence_without_workbook(self):
        evidence = self.begin_revocation()
        self.record('present_verified', continuation=True)
        self.store.workbook_path.unlink()
        self.record('present_verified', continuation=True)
        self.record('absent_verified', continuation=True)
        record = self.store.latest_action_record(self.target)
        self.assertEqual(record['operation_id'], 'original')
        self.assertEqual(record['evidence'], evidence)
        self.assertEqual(self.store.unresolved_actions(), [])
        self.assertEqual(self.store.active_tracking_ids(require_workbook=False), set())

    def test_plain_read_only_presence_does_not_permanently_block_new_management(self):
        self.record('pending', operation='read', read_only=True)
        self.record('present_verified', operation='read', continuation=True)
        observation = self.store.latest_action_record(self.target)
        self.assertTrue(observation['read_only'])
        self.assertNotIn('requires_reconciliation', observation)
        self.assertEqual(DataStore(self.folder).unresolved_actions(), [])
        self.record('pending', operation='new-management')
        self.assertEqual(self.store.latest_action_record(self.target)['operation_id'], 'new-management')

    def test_failed_read_only_check_can_observe_presence_without_becoming_revocation(self):
        self.record('pending', operation='read', read_only=True)
        self.record('unknown_result', operation='read', continuation=True)
        self.assertEqual(len(self.store.unresolved_actions()), 1)
        self.record('present_verified', operation='read', continuation=True)
        self.assertEqual(self.store.unresolved_actions(), [])
        self.assertNotIn('evidence', self.store.latest_action_record(self.target))

    def test_failed_observation_after_presence_keeps_original_duty_until_absence(self):
        evidence = self.begin_revocation()
        self.record('present_verified', continuation=True)
        presence = self.store.latest_action_record(self.target)
        with self.assertRaises(GuardError):
            self.record('unknown_result', continuation=True)
        self.record('observation_failed', continuation=True)
        failed = self.store.latest_action_record(self.target)
        self.assertEqual(failed['status'], 'observation_failed')
        self.assertEqual(failed['operation_id'], presence['operation_id'])
        self.assertEqual(failed['evidence'], evidence)
        self.assertIn(presence, self.store.events())
        self.assertEqual(DataStore(self.folder).unresolved_actions(), [failed])
        self.record('absent_verified', continuation=True)
        self.assertEqual(self.store.latest_action_record(self.target)['evidence'], evidence)
        self.assertEqual(self.store.unresolved_actions(), [])

    def test_plain_read_only_observation_failure_can_finish_with_presence(self):
        self.record('pending', operation='read', read_only=True)
        self.record('observation_failed', operation='read', continuation=True)
        failed = self.store.latest_action_record(self.target)
        self.assertTrue(failed['read_only'])
        self.assertEqual(self.store.unresolved_actions(), [failed])
        self.record('present_verified', operation='read', continuation=True)
        self.assertEqual(self.store.unresolved_actions(), [])
        self.assertTrue(self.store.latest_action_record(self.target)['read_only'])
        self.record('pending', operation='later-management')

    def test_observation_failure_cannot_create_intent_or_reopen_settled_operation(self):
        with self.assertRaises(GuardError):
            self.record('observation_failed')
        with self.assertRaises(GuardError):
            self.record('observation_failed', continuation=True)
        self.record('pending', read_only=True)
        self.record('present_verified', continuation=True)
        before = self.store.events()
        with self.assertRaises(GuardError):
            self.record('observation_failed', continuation=True)
        self.assertEqual(self.store.events(), before)

    def test_read_only_origin_cannot_be_added_to_existing_operation_or_grant_permission(self):
        self.record('pending', read_only=True)
        before = self.store.events()
        for status in ('prepared', 'requested', 'confirmed'):
            with self.subTest(status=status), self.assertRaises(GuardError):
                self.record(status)
        with self.assertRaises(GuardError):
            self.record('pending', read_only=True)
        with self.assertRaises(GuardError):
            self.record('pending', operation='other', read_only=True,
                        evidence=self.store.action_evidence('合成目标', self.target))
        self.assertEqual(self.store.events(), before)

    def test_legacy_presence_keeps_unknown_responsibility_without_rewriting_history(self):
        # Simulate an old ledger before presence carried structured provenance.
        with self.store._connect() as db:
            for status in ('requested', 'unknown_result', 'present_verified'):
                self.store._event(db, 'action', {'name': '合成目标', 'tracking_id': self.target,
                                               'status': status, 'detail': 'legacy', 'operation_id': 'legacy'})
        before = self.store.events()
        blocked = self.store.unresolved_actions()
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]['status'], 'present_verified')
        self.assertNotIn('requires_reconciliation', blocked[0])
        self.assertEqual(self.store.events(), before)
        successor = self.store.create_successor(self.folder.parent / 'successor', keep_account=True)
        inherited = successor.unresolved_actions()[0]
        self.assertEqual(inherited['operation_id'], 'legacy')
        self.assertTrue(inherited['requires_reconciliation'])
        self.assertEqual(self.store.events(), before)

    def test_legacy_explicit_read_only_presence_remains_nonblocking(self):
        self.store.record_action('合成目标', self.target, 'pending',
                                 '用户请求只读核实平台状态；未授权或提交撤销', operation_id='legacy-read')
        with self.store._connect() as db:
            self.store._event(db, 'action', {'name': '合成目标', 'tracking_id': self.target,
                                           'status': 'present_verified', 'detail': 'legacy',
                                           'operation_id': 'legacy-read'})
        self.assertEqual(self.store.unresolved_actions(), [])
        successor = self.store.create_successor(self.folder.parent / 'read-successor', keep_account=True)
        self.assertEqual(successor.unresolved_actions(), [])
        self.assertTrue(successor.latest_action_record(self.target)['read_only'])

    def test_later_legacy_read_only_operation_does_not_hide_older_unknown(self):
        self.begin_revocation()
        with self.store._connect() as db:
            self.store._event(db, 'action', {'name': '合成目标', 'tracking_id': self.target,
                                           'status': 'present_verified', 'detail': 'legacy observation',
                                           'operation_id': 'original'})
            self.store._event(db, 'action', {'name': '合成目标', 'tracking_id': self.target,
                                           'status': 'pending', 'detail': 'read-only check',
                                           'operation_id': 'later-read', 'read_only': True})
            self.store._event(db, 'action', {'name': '合成目标', 'tracking_id': self.target,
                                           'status': 'present_verified', 'detail': 'still here',
                                           'operation_id': 'later-read', 'read_only': True})
        self.assertEqual(self.store.latest_action_record(self.target)['operation_id'], 'later-read')
        self.assertEqual(self.store.unresolved_actions()[0]['operation_id'], 'original')
        self.record('absent_verified', continuation=True)
        self.assertEqual(self.store.unresolved_actions(), [])

    def test_legacy_presence_without_origin_remains_uncertain(self):
        with self.store._connect() as db:
            self.store._event(db, 'action', {'name': '合成目标', 'tracking_id': self.target,
                                           'status': 'present_verified', 'detail': 'no origin retained'})
        record = self.store.unresolved_actions()[0]
        self.assertIsNone(record['operation_id'])
        self.assertNotIn('evidence', record)

    def test_not_submitted_only_closes_a_new_preparation_and_preserves_evidence(self):
        evidence = self.store.action_evidence('合成目标', self.target)
        self.record('pending', evidence=evidence)
        self.record('not_submitted', continuation=True)
        record = self.store.latest_action_record(self.target)
        self.assertEqual(record['evidence'], evidence)
        self.assertEqual(self.store.unresolved_actions(), [])
        self.record('pending', operation='explicit-later-attempt')
        self.assertEqual(self.store.latest_action_record(self.target)['operation_id'], 'explicit-later-attempt')

    def test_not_submitted_cannot_reclassify_unknown_or_presence(self):
        self.begin_revocation()
        for observed in (False, True):
            if observed:
                self.record('present_verified', continuation=True)
            before = self.store.events()
            with self.assertRaises(GuardError):
                self.record('not_submitted', continuation=True)
            self.assertEqual(self.store.events(), before)
        with self.assertRaises(GuardError):
            self.record('not_submitted', operation='invented', continuation=False)

    def test_not_submitted_export_retry_is_idempotent_after_sqlite_commit(self):
        evidence = self.store.action_evidence('合成目标', self.target)
        self.record('pending', evidence=evidence)
        export = self.store.export_logs
        export_attempts = []

        def fail_first_export():
            export_attempts.append(True)
            if len(export_attempts) == 1:
                raise GuardError('synthetic log export failure after commit')
            return export()

        with patch.object(self.store, 'export_logs', side_effect=fail_first_export):
            with self.assertRaisesRegex(GuardError, 'synthetic log export failure'):
                self.record('not_submitted', continuation=True)
            committed = self.store.events()
            result = self.store.latest_action_record(self.target)
            self.assertEqual(result['status'], 'not_submitted')
            self.assertEqual(result['evidence'], evidence)
            self.record('not_submitted', continuation=True)
        self.assertEqual(len(export_attempts), 2)
        self.assertEqual(self.store.events(), committed)
        self.assertEqual(self.store.unresolved_actions(), [])
        exported = ''.join(path.read_text(encoding='utf-8') for path in self.folder.glob('运行日志-*.jsonl'))
        self.assertEqual(exported.count('"status": "not_submitted"'), 1)

    def test_not_submitted_replay_rejects_changed_detail_or_evidence(self):
        evidence = self.store.action_evidence('合成目标', self.target)
        self.record('pending', evidence=evidence)
        self.record('not_submitted', continuation=True)
        committed = self.store.events()
        with patch.object(self.store, 'export_logs') as export:
            with self.assertRaises(GuardError):
                self.store.record_action('合成目标', self.target, 'not_submitted', 'different result',
                                         operation_id='original', continuation=True)
            with self.assertRaises(GuardError):
                self.record('not_submitted', continuation=True, evidence={**evidence, 'spent': '99'})
            export.assert_not_called()
        self.assertEqual(self.store.events(), committed)

    def test_not_submitted_rejects_a_repeated_pending_after_possible_submission(self):
        self.record('pending')
        self.record('requested')
        with self.store._connect() as db:
            self.store._event(db, 'action', {'name': '合成目标', 'tracking_id': self.target,
                                           'status': 'pending', 'detail': 'legacy repeated preparation',
                                           'operation_id': 'original'})
        with self.assertRaises(GuardError):
            self.record('not_submitted', continuation=True)


if __name__ == '__main__':
    unittest.main()
