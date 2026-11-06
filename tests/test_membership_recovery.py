"""Exact local membership witnesses, using synthetic workspace files only."""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from quota_guard.core import DataStore, GuardError, Person, TZ
from quota_guard import membership_recovery as membership


TEMP_ROOT = Path(__file__).resolve().parents[2] / 'test-temp'
NAME, OTHER_NAME = 'Synthetic member', 'Synthetic second'
TARGET, OTHER = 'synthetic-membership-target', 'synthetic-membership-other'
ACCOUNT = 'synthetic-membership-account'


class MembershipRecoveryTests(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='membership-', dir=TEMP_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.keys = [{'name': NAME, 'tracking_id': TARGET},
                     {'name': OTHER_NAME, 'tracking_id': OTHER}]
        self.first = self.store('X')
        self.second = self.store('Y')

    def store(self, name):
        store = DataStore(self.folder / name)
        store.initialize(2, '100', datetime.now(TZ).date().isoformat())
        store.save_people([Person(NAME, Decimal(1)), Person(OTHER_NAME, Decimal(1))])
        store.bind_keys(self.keys, ACCOUNT)
        return store

    @staticmethod
    def snapshot(store):
        with store._connect() as db:
            return {'meta': list(db.execute('SELECT key,value FROM meta ORDER BY key')),
                    'events': list(db.execute('SELECT * FROM events ORDER BY id')),
                    'bindings': list(db.execute('SELECT * FROM bindings ORDER BY tracking_id'))}

    @staticmethod
    def audits(store):
        with store._connect() as db:
            return [json.loads(row[0]) for row in db.execute(
                'SELECT payload FROM events WHERE kind=? ORDER BY id', (membership.AUDIT_KIND,))]

    @staticmethod
    def anchor(store):
        with store._connect() as db:
            return membership._anchor(db)

    def plan(self):
        return membership.prepare_initial_plan([self.first, self.second])

    def legacy(self, store, *, operation=None, name=NAME, target=TARGET, status='unknown_result'):
        operation = operation or uuid4().hex
        # Legacy fixture insertion intentionally represents histories predating
        # the current single-operation guard; no current send API is bypassed.
        with store._connect() as db:
            for phase in ('pending', status):
                store._event(db, 'action', {'operation_id': operation, 'name': name,
                    'tracking_id': target, 'status': phase, 'detail': 'synthetic legacy history'})
        return operation

    def ready_snapshot(self, plan):
        return {'phase': 'ready', 'epoch': plan['epoch'],
                'generation': len(plan['operations']), 'revision': 7, 'open_obligations': [],
                'ledgers': [{'ledger_id': item['ledger_id'], 'path': item['path'],
                             'checkpoint': item['target_anchor']['checkpoint'], 'epoch': plan['epoch']}
                            for item in plan['members']]}

    def test_prepare_is_read_only_and_exact_target_is_audited_once(self):
        before = [self.snapshot(s) for s in (self.first, self.second)]
        plan = self.plan()
        original_plan = deepcopy(plan)
        self.assertEqual([self.snapshot(s) for s in (self.first, self.second)], before)
        self.assertEqual(membership.preflight(plan), {'before': 2, 'target': 0})
        with self.assertRaises(GuardError):
            membership.verify(plan)
        self.assertEqual(membership.apply(plan), {'applied': 2, 'already_target': 0})
        self.assertEqual(membership.verify(plan), {'verified': 2})
        after = [self.snapshot(s) for s in (self.first, self.second)]
        self.assertEqual(membership.apply(plan), {'applied': 0, 'already_target': 2})
        self.assertEqual([self.snapshot(s) for s in (self.first, self.second)], after)
        self.assertEqual(plan, original_plan)
        for member in plan['members']:
            store = DataStore(member['path'])
            self.assertEqual(self.anchor(store), member['target_anchor'])
            self.assertEqual(self.audits(store), [membership._audit(plan, member)])

    def test_different_targets_all_import_with_deterministic_checkpoints(self):
        first_op = self.legacy(self.first)
        second_op = self.legacy(self.first, name=OTHER_NAME, target=OTHER)
        plan = self.plan()
        self.assertEqual({o['operation_id'] for o in plan['operations']}, {first_op, second_op})
        self.assertEqual([o['generation'] for o in plan['operations']], [1, 2])
        self.assertEqual([o['prior_checkpoint'] for o in plan['operations']], [0, 1])
        owner = next(m for m in plan['members'] if m['path'] == str(self.first.folder))
        self.assertEqual(owner['target_anchor']['checkpoint'], 2)
        history = self.snapshot(self.first)['events']
        membership.apply(plan)
        self.assertEqual(self.snapshot(self.first)['events'][:-1], history)
        self.assertEqual(len(self.first.unresolved_actions()), 2)

    def test_same_target_in_different_ledgers_preserves_both_original_operations(self):
        first_op = self.legacy(self.first)
        second_op = self.legacy(self.second)
        plan = self.plan()
        self.assertEqual({o['operation_id'] for o in plan['operations']}, {first_op, second_op})
        self.assertEqual(len({o['target_hash'] for o in plan['operations']}), 1)
        self.assertEqual(len({o['ledger_id'] for o in plan['operations']}), 2)
        membership.apply(plan)
        self.assertEqual(self.first.unresolved_actions()[0]['operation_id'], first_op)
        self.assertEqual(self.second.unresolved_actions()[0]['operation_id'], second_op)

    def test_gui_one_row_projection_cannot_hide_two_old_operations_for_same_target(self):
        self.legacy(self.first)
        self.legacy(self.first)
        self.assertEqual(len(self.first.unresolved_actions()), 1, 'Existing GUI projection returns just one row.')
        before = self.snapshot(self.first)
        with self.assertRaisesRegex(GuardError, '多笔未决'):
            self.plan()
        self.assertEqual(self.snapshot(self.first), before)

    def test_duplicate_operation_across_ledgers_is_not_silently_merged(self):
        operation = uuid4().hex
        self.legacy(self.first, operation=operation)
        self.legacy(self.second, operation=operation)
        with self.assertRaisesRegex(GuardError, '同一旧操作编号'):
            self.plan()

    def test_missing_old_operation_identity_is_not_invented(self):
        with self.first._connect() as db:
            self.first._event(db, 'action', {'name': NAME, 'tracking_id': TARGET,
                'status': 'unknown_result', 'detail': 'synthetic legacy no operation'})
        with self.assertRaisesRegex(GuardError, '缺少原操作编号'):
            self.plan()

    def test_read_only_observation_and_absence_cutoff_keep_existing_business_semantics(self):
        self.legacy(self.first)
        # A verified absence settles preceding uncertainty for that exact target.
        with self.first._connect() as db:
            self.first._event(db, 'action', {'operation_id': uuid4().hex, 'name': NAME,
                'tracking_id': TARGET, 'status': 'absent_verified', 'detail': 'synthetic observed absence'})
        self.second.record_action(NAME, TARGET, 'pending', 'synthetic read-only observation',
                                  operation_id=uuid4().hex, read_only=True)
        plan = self.plan()
        self.assertEqual(plan['operations'], [])
        membership.apply(plan)
        self.assertEqual(membership.verify(plan), {'verified': 2})

    def test_partial_apply_resumes_original_plan_without_duplicate_first_audit(self):
        plan = self.plan()
        real_apply = membership._apply_member
        visited = []
        def fail_second(current, member):
            visited.append(member['ledger_id'])
            if len(visited) == 2:
                raise OSError('synthetic second member failure')
            return real_apply(current, member)
        with patch.object(membership, '_apply_member', side_effect=fail_second):
            with self.assertRaises(OSError):
                membership.apply(plan)
        self.assertEqual(membership.preflight(plan), {'before': 1, 'target': 1})
        with self.assertRaises(GuardError):
            membership.verify(plan)
        first_member = DataStore(plan['members'][0]['path'])
        first_audit = self.audits(first_member)
        self.assertEqual(membership.apply(plan), {'applied': 1, 'already_target': 1})
        self.assertEqual(self.audits(first_member), first_audit)
        self.assertEqual(membership.verify(plan), {'verified': 2})

    def test_anchor_and_audit_are_one_local_transaction(self):
        plan = self.plan()
        before = [self.snapshot(s) for s in (self.first, self.second)]
        with patch.object(DataStore, '_event', side_effect=OSError('synthetic audit insertion failure')):
            with self.assertRaises(OSError):
                membership.apply(plan)
        self.assertEqual([self.snapshot(s) for s in (self.first, self.second)], before)
        self.assertEqual(membership.preflight(plan), {'before': 2, 'target': 0})

    def test_export_failure_keeps_target_and_exact_audit_for_bounded_resume(self):
        plan = self.plan()
        with patch.object(DataStore, 'export_logs', side_effect=OSError('synthetic export failure')):
            with self.assertRaises(OSError):
                membership.apply(plan)
        first = DataStore(plan['members'][0]['path'])
        audit = self.audits(first)
        self.assertEqual(len(audit), 1)
        self.assertEqual(membership.preflight(plan), {'before': 1, 'target': 1})
        membership.apply(plan)
        self.assertEqual(self.audits(first), audit)

    def test_target_anchor_without_original_plan_audit_is_rejected(self):
        plan = self.plan()
        member = plan['members'][0]
        store = DataStore(member['path'])
        with store._connect() as db:
            db.execute('INSERT INTO meta VALUES (?,?)',
                       (membership.ANCHOR_KEY, json.dumps(member['target_anchor'])))
        before = [self.snapshot(s) for s in (self.first, self.second)]
        with self.assertRaises(GuardError):
            membership.apply(plan)
        self.assertEqual([self.snapshot(s) for s in (self.first, self.second)], before)

    def test_wrong_plan_audit_cannot_validate_an_identical_target(self):
        plan = self.plan()
        membership.apply(plan)
        store = DataStore(plan['members'][0]['path'])
        with store._connect() as db:
            payload = self.audits(store)[0]
            payload['plan_hash'] = '0' * 64
            db.execute('UPDATE events SET payload=? WHERE kind=?',
                       (json.dumps(payload), membership.AUDIT_KIND))
        with self.assertRaises(GuardError):
            membership.verify(plan)

    def test_any_member_history_change_stops_all_plan_writes_at_preflight(self):
        plan = self.plan()
        self.legacy(self.second)
        before = [self.snapshot(s) for s in (self.first, self.second)]
        with self.assertRaises(GuardError):
            membership.apply(plan)
        self.assertEqual([self.snapshot(s) for s in (self.first, self.second)], before)

    def test_structurally_valid_plan_cannot_omit_one_original_unresolved_operation(self):
        self.legacy(self.first)
        self.legacy(self.first, name=OTHER_NAME, target=OTHER)
        plan = self.plan()
        plan['operations'] = plan['operations'][:1]
        owner = plan['operations'][0]['ledger_id']
        for member in plan['members']:
            member['target_anchor']['checkpoint'] = 1 if member['ledger_id'] == owner else 0
        # Registry structure alone cannot establish that the old history is
        # complete. The local manifest must reject this otherwise valid plan.
        membership.validate_membership_plan(plan)
        before = [self.snapshot(s) for s in (self.first, self.second)]
        with self.assertRaises(GuardError):
            membership.apply(plan)
        self.assertEqual([self.snapshot(s) for s in (self.first, self.second)], before)

    def test_each_member_rechecks_history_inside_its_own_write_transaction(self):
        plan = self.plan()
        real_apply = membership._apply_member
        count = 0
        def change_second_after_global_preflight(current, member):
            nonlocal count
            count += 1
            if count == 2:
                self.legacy(DataStore(member['path']))
            return real_apply(current, member)
        with patch.object(membership, '_apply_member', side_effect=change_second_after_global_preflight):
            with self.assertRaises(GuardError):
                membership.apply(plan)
        first = DataStore(plan['members'][0]['path'])
        second = DataStore(plan['members'][1]['path'])
        self.assertEqual(self.anchor(first), plan['members'][0]['target_anchor'])
        self.assertEqual(len(self.audits(first)), 1)
        self.assertIsNone(self.anchor(second))
        self.assertEqual(self.audits(second), [])
        with self.assertRaises(GuardError):
            membership.verify(plan)

    def test_account_change_is_detected_even_when_period_and_database_remain_valid(self):
        plan = self.plan()
        with self.second._connect() as db:
            db.execute("UPDATE meta SET value='synthetic-different-account' WHERE key='account_id'")
        self.assertEqual(self.second.load(require_workbook=False).account_id, 'synthetic-different-account')
        before = [self.snapshot(s) for s in (self.first, self.second)]
        with self.assertRaises(GuardError):
            membership.apply(plan)
        self.assertEqual([self.snapshot(s) for s in (self.first, self.second)], before)

    def test_moved_original_is_not_found_or_replaced_by_scanning(self):
        plan = self.plan()
        original = self.first.folder.resolve()
        renamed = (self.folder / 'renamed-original').resolve()
        self.assertTrue(original.is_relative_to(self.folder) and renamed.is_relative_to(self.folder))
        original.rename(renamed)
        with self.assertRaises(GuardError):
            membership.apply(plan)
        self.assertFalse(original.exists())
        self.assertIsNone(self.anchor(DataStore(renamed)))
        self.assertIsNone(self.anchor(self.second))

    def test_workbook_is_not_required_for_membership_evidence_recovery(self):
        plan = self.plan()
        source = self.first.workbook_path.resolve()
        saved = source.with_name('synthetic-workbook-unavailable.xlsx')
        self.assertTrue(source.is_relative_to(self.folder) and saved.is_relative_to(self.folder))
        source.rename(saved)
        membership.apply(plan)
        self.assertEqual(membership.verify(plan), {'verified': 2})

    def test_enrollment_only_moves_exact_old_roster_to_fixed_target(self):
        initial = self.plan()
        membership.apply(initial)
        third = self.store('Z')
        plan = membership.prepare_enrollment_plan(self.ready_snapshot(initial), third)
        self.assertEqual(plan['operations'], [])
        self.assertEqual(len(plan['before_members']), 2)
        self.assertEqual(plan['base_revision'], 7)
        old_anchors = [self.anchor(s) for s in (self.first, self.second)]
        self.assertEqual(membership.apply(plan), {'applied': 3, 'already_target': 0})
        for old, store in zip(old_anchors, (self.first, self.second)):
            self.assertEqual(self.anchor(store)['checkpoint'], old['checkpoint'])
            self.assertNotEqual(self.anchor(store)['roster_hash'], old['roster_hash'])
        with self.assertRaises(GuardError):
            membership.apply(initial)

    def test_unknown_roster_is_not_overwritten_during_enrollment(self):
        initial = self.plan()
        membership.apply(initial)
        third = self.store('Z')
        plan = membership.prepare_enrollment_plan(self.ready_snapshot(initial), third)
        corrupt = self.anchor(self.second)
        corrupt['roster_hash'] = 'f' * 64
        with self.second._connect() as db:
            db.execute('UPDATE meta SET value=? WHERE key=?', (json.dumps(corrupt), membership.ANCHOR_KEY))
        before = [self.snapshot(s) for s in (self.first, self.second, third)]
        with self.assertRaises(GuardError):
            membership.apply(plan)
        self.assertEqual([self.snapshot(s) for s in (self.first, self.second, third)], before)

    def test_settled_successor_history_is_preserved_without_new_legacy_obligation(self):
        self.legacy(self.first, status='absent_verified')
        initial = self.plan()
        membership.apply(initial)
        successor = self.first.create_successor(self.folder / 'successor', keep_account=True)
        before = self.snapshot(successor)['events']
        plan = membership.prepare_enrollment_plan(self.ready_snapshot(initial), successor)
        self.assertEqual(plan['operations'], [])
        membership.apply(plan)
        self.assertEqual(self.snapshot(successor)['events'][:-1], before)
        self.assertEqual(successor.unresolved_actions(), [])


if __name__ == '__main__':
    unittest.main()
