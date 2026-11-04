from datetime import datetime
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from quota_guard.core import AccountMismatch, DataStore, GuardError, Person, TZ


class _ForcedBindingFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.store = DataStore(self.home / 'ledger')
        self.today = datetime.now(TZ).date().isoformat()
        self.store.initialize(3, '300', self.today)
        self.store.save_people([Person('选中目标', Decimal('-1')), Person('其他人员', Decimal('50'))])
        self.keys = [{'name': '选中目标', 'tracking_id': 'fixture-target'},
                     {'name': '对照目标', 'tracking_id': 'fixture-control'}]

    def tearDown(self):
        self.temp.cleanup()

    def bind(self, keys=None, account='fixture-account', name='选中目标', tracking_id='fixture-target'):
        self.store.bind_forced_target(self.keys if keys is None else keys, account, name, tracking_id)


class ForcedBindingTests(_ForcedBindingFixture, unittest.TestCase):
    def test_skipped_target_can_bind_without_other_roster_name_on_platform(self):
        self.bind()
        self.assertEqual(self.store.load().account_id, 'fixture-account')
        self.assertEqual(self.store.active_bindings(), [{'name': '选中目标', 'tracking_id': 'fixture-target'}])
        evidence = self.store.action_evidence('选中目标', 'fixture-target')
        self.assertEqual(evidence['amount'], '-1')
        self.assertEqual(evidence['status'], 'skipped')
        self.assertIsNone(evidence['spent'])
        self.assertFalse(evidence['complete'])
        self.assertEqual(evidence['reasons'], [])

    def test_unknown_and_below_quota_evidence_remains_truthful(self):
        self.store.save_people([Person('选中目标', Decimal('10'))])
        self.bind()
        self.assertEqual(self.store.action_evidence('选中目标', 'fixture-target')['status'], 'unknown')
        self.store.apply_snapshot({'fixture-target': Decimal('0.1')}, 'fixture-snapshot', self.today, self.today)
        self.bind()
        evidence = self.store.action_evidence('选中目标', 'fixture-target')
        self.assertEqual((evidence['status'], evidence['spent'], evidence['reasons']), ('ok', '0.1', []))

    def test_normal_binding_still_skips_minus_one(self):
        self.store.save_people([Person('选中目标', Decimal('-1'))])
        self.store.bind_keys(self.keys, 'fixture-account')
        self.assertEqual(self.store.active_bindings(), [])
        self.bind()
        self.assertEqual(self.store.active_tracking_ids(), {'fixture-target'})

    def test_repeated_binding_is_idempotent(self):
        self.bind()
        before = self.store.events()
        self.bind()
        self.assertEqual(self.store.events(), before)

    def test_ambiguous_full_platform_list_is_rejected_without_writes(self):
        malformed = [[], {}, [None], [{'name': '选中目标'}],
                     self.keys + [{'name': '重名以外', 'tracking_id': 'fixture-control'}],
                     self.keys + [{'name': '选中目标', 'tracking_id': 'fixture-other'}],
                     self.keys + [{'name': '', 'tracking_id': 'fixture-other'}]]
        before = self.store.events()
        for keys in malformed:
            with self.subTest(keys=keys), self.assertRaises(GuardError):
                self.bind(keys=keys)
            self.assertEqual(self.store.active_bindings(), [])
            self.assertEqual(self.store.load().account_id, '')
            self.assertEqual(self.store.events(), before)

    def test_unrelated_duplicate_names_do_not_block_unique_selected_target(self):
        keys = self.keys + [{'name': '对照目标', 'tracking_id': 'fixture-other'}]
        self.bind(keys=keys)
        self.assertEqual(self.store.active_bindings(), [{'name': '选中目标', 'tracking_id': 'fixture-target'}])
        self.assertEqual(self.store.action_evidence('选中目标', 'fixture-target')['status'], 'skipped')

    def test_target_must_be_exact_existing_named_roster_row(self):
        cases = [('未在名单', 'fixture-control'), ('', 'fixture-target'),
                 ('选中目标', 'fixture-control'), ('选中目标', 'missing-target')]
        for name, tracking_id in cases:
            with self.subTest(name=name, tracking_id=tracking_id), self.assertRaises(GuardError):
                self.bind(name=name, tracking_id=tracking_id)
        self.assertEqual(self.store.active_bindings(), [])

    def test_different_account_and_replacement_id_do_not_change_existing_binding(self):
        self.bind()
        before = self.store.events()
        with self.assertRaises(AccountMismatch):
            self.bind(account='other-account')
        with self.assertRaises(GuardError):
            self.bind(keys=[{'name': '选中目标', 'tracking_id': 'new-target'}], tracking_id='new-target')
        self.assertEqual(self.store.active_tracking_ids(), {'fixture-target'})
        self.assertEqual(self.store.events(), before)

    def test_binding_cannot_move_to_a_different_person(self):
        self.bind()
        self.store.save_people([Person('改名目标', Decimal('-1'))])
        with self.assertRaises(GuardError):
            self.bind(keys=[{'name': '改名目标', 'tracking_id': 'fixture-target'}], name='改名目标')
        self.assertEqual(self.store.active_bindings(), [{'name': '选中目标', 'tracking_id': 'fixture-target'}])

    def test_retirement_cannot_be_undone_by_forced_binding(self):
        self.bind()
        self.store.record_action('选中目标', 'fixture-target', 'absent_verified', 'fixture')
        for key_id in ('fixture-target', 'new-target'):
            with self.subTest(key_id=key_id), self.assertRaises(GuardError):
                self.bind(keys=[{'name': '选中目标', 'tracking_id': key_id}], tracking_id=key_id)
        self.assertEqual(self.store.active_bindings(), [])

    def test_workbook_change_during_target_validation_prevents_binding(self):
        fingerprint = self.store.workbook_hash()
        with patch.object(self.store, 'workbook_hash', side_effect=[fingerprint, 'changed']):
            with self.assertRaises(GuardError):
                self.bind()
        self.assertEqual(self.store.active_bindings(), [])


class ForcedActionIntentTests(_ForcedBindingFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.bind()
        self.evidence = self.store.action_evidence('选中目标', 'fixture-target')

    def record(self, status, **kwargs):
        return self.store.record_action('选中目标', 'fixture-target', status, 'fixture',
                                        operation_id=kwargs.pop('operation_id', 'force-operation'), **kwargs)

    def test_force_intent_propagates_and_keeps_actual_rule_evidence(self):
        self.record('pending', intent='manual_force', evidence=self.evidence)
        for status in ('prepared', 'requested', 'unknown_result'):
            self.record(status, continuation=status == 'unknown_result')
            record = self.store.latest_action_record('fixture-target')
            self.assertEqual(record['intent'], 'manual_force')
            self.assertEqual(record['evidence'], self.evidence)
        self.assertEqual(record['evidence']['status'], 'skipped')
        self.assertEqual(record['evidence']['amount'], '-1')

    def test_new_requested_can_record_explicit_force_intent(self):
        self.record('requested', intent='manual_force', evidence=self.evidence)
        self.assertEqual(self.store.latest_action_record('fixture-target')['intent'], 'manual_force')

    def test_force_intent_survives_blank_successor_and_result_recovery(self):
        self.record('pending', intent='manual_force', evidence=self.evidence)
        self.record('requested')
        self.record('unknown_result', continuation=True)
        new = self.store.create_list(self.home / 'successor', 1, '10', self.today, inherit_people=False)
        new.record_action('选中目标', 'fixture-target', 'absent_verified', 'fixture',
                          operation_id='force-operation', continuation=True)
        result = new.latest_action_record('fixture-target')
        self.assertEqual(result['intent'], 'manual_force')
        self.assertEqual(result['evidence'], self.evidence)

    def test_existing_ordinary_operation_cannot_be_reclassified(self):
        self.record('pending', evidence=self.evidence)
        before = self.store.latest_action_record('fixture-target')
        for status in ('pending', 'requested'):
            with self.subTest(status=status), self.assertRaises(GuardError):
                self.record(status, intent='manual_force')
        self.assertEqual(self.store.latest_action_record('fixture-target'), before)
        self.record('requested')
        self.assertNotIn('intent', self.store.latest_action_record('fixture-target'))

    def test_force_marker_only_belongs_to_new_numbered_non_readonly_intent(self):
        cases = [('pending', {'operation_id': None}), ('pending', {'read_only': True}),
                 ('pending', {'continuation': True}), ('prepared', {}),
                 ('unknown_result', {}), ('pending', {'intent': 'automatic'})]
        for status, kwargs in cases:
            kwargs = {'intent': 'manual_force', **kwargs}
            with self.subTest(status=status, kwargs=kwargs), self.assertRaises(GuardError):
                self.record(status, **kwargs)
        self.assertIsNone(self.store.latest_action_record('fixture-target'))

    def test_repeat_explicit_force_marker_cannot_rewrite_operation(self):
        self.record('pending', intent='manual_force')
        with self.assertRaises(GuardError):
            self.record('pending', intent='manual_force')
        with self.assertRaises(GuardError):
            self.record('requested', intent='manual_force')
        self.record('requested')
        self.assertEqual(self.store.latest_action_record('fixture-target')['intent'], 'manual_force')


if __name__ == '__main__':
    unittest.main()
