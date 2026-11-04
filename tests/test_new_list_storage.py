from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from quota_guard.core import DataStore, GuardError, Person, TZ


class NewListStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.store = DataStore(self.home / 'original')
        self.today = datetime.now(TZ).date().isoformat()
        self.earlier = (datetime.now(TZ).date() - timedelta(days=1)).isoformat()
        self.store.initialize(3, '90', self.today)
        self.store.save_people([Person('目标甲', Decimal('12.000000000000000001')),
                                Person('目标乙', Decimal('-1')), Person('', Decimal('-1'))])
        self.keys = [{'name': '目标甲', 'tracking_id': 'fixture-key-a'},
                     {'name': '目标乙', 'tracking_id': 'fixture-key-b'}]
        self.store.bind_keys(self.keys, 'fixture-account')

    def tearDown(self):
        self.temp.cleanup()

    def source_files(self):
        return {path.name: path.read_bytes() for path in self.store.folder.iterdir() if path.is_file()}

    def test_inherit_names_exact_amounts_and_independent_new_settings(self):
        self.store.apply_snapshot({'fixture-key-a': Decimal('0.2')}, 'snapshot-a', self.today, self.today)
        before = self.source_files()
        old_config = self.store.load()
        new = self.store.create_list(self.home / 'new', 4, '25', self.earlier, inherit_people=True)
        config = new.load()
        self.assertNotEqual(config.period_id, old_config.period_id)
        self.assertEqual((config.people_count, config.total_budget, config.start_date, config.share),
                         (4, Decimal('25'), self.earlier, Decimal('6')))
        self.assertEqual(config.account_id, old_config.account_id)
        self.assertEqual(new.read_people(), [Person('目标甲', Decimal('12.000000000000000001')),
                                            Person('目标乙', Decimal('-1')),
                                            Person('', Decimal('-1')), Person('', Decimal('-1'))])
        self.assertTrue(all(row['spent'] is None for row in new.evaluate()))
        with new._connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM costs').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM snapshots').fetchone()[0], 0)
        self.assertEqual(self.source_files(), before)

    def test_blank_roster_preserves_account_binding_and_unresolved_operation(self):
        evidence = self.store.action_evidence('目标甲', 'fixture-key-a')
        self.store.record_action('目标甲', 'fixture-key-a', 'requested', 'fixture',
                                operation_id='original-operation', evidence=evidence)
        self.store.record_action('目标甲', 'fixture-key-a', 'unknown_result', 'fixture',
                                operation_id='original-operation', continuation=True)
        before = self.source_files()
        new = self.store.create_list(self.home / 'blank', 2, '18', self.earlier, inherit_people=False)
        self.assertEqual(new.read_people(), [Person('', Decimal('-1'))] * 2)
        self.assertEqual(new.load().account_id, 'fixture-account')
        self.assertEqual(new.active_bindings(), self.store.active_bindings())
        pending = new.unresolved_actions()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]['operation_id'], 'original-operation')
        self.assertEqual(pending[0]['evidence'], evidence)
        self.assertEqual(pending[0]['status'], 'unknown_result')
        self.assertEqual(self.source_files(), before)

    def test_blank_roster_keeps_retired_identity(self):
        self.store.record_action('目标甲', 'fixture-key-a', 'absent_verified', 'fixture retired')
        new = self.store.create_list(self.home / 'retired', 1, '50', self.today, inherit_people=False)
        new.save_people([Person('目标甲', Decimal('-1'))])
        self.assertEqual(new.latest_action('fixture-key-a'), 'absent_verified')
        with self.assertRaises(GuardError):
            new.bind_forced_target(self.keys, 'fixture-account', '目标甲', 'fixture-key-a')

    def test_blank_roster_can_be_smaller_than_previous_filled_roster(self):
        new = self.store.create_list(self.home / 'small-blank', 1, '0', self.today, inherit_people=False)
        self.assertEqual(new.read_people(), [Person('', Decimal('-1'))])
        self.assertEqual(new.load().share, Decimal('0'))

    def test_inherited_names_must_fit_new_count(self):
        before = self.source_files()
        target = self.home / 'too-small'
        with self.assertRaises(GuardError):
            self.store.create_list(target, 1, '20', self.today, inherit_people=True)
        self.assertFalse(target.exists())
        self.assertEqual(self.source_files(), before)

    def test_legacy_successor_defaults_remain_unbound_and_keep_roster(self):
        new = self.store.create_successor(self.home / 'legacy')
        self.assertEqual(new.load().people_count, 3)
        self.assertEqual(new.load().total_budget, Decimal('90'))
        self.assertEqual(new.load().account_id, '')
        self.assertEqual(new.active_bindings(), [])
        self.assertEqual(new.read_people(), self.store.read_people())

    def test_selected_default_fills_only_new_list_unassigned_rows(self):
        before = self.source_files()
        amount = Decimal('0.000000000000000001')
        new = self.store.create_list(self.home / 'prefilled-inherited', 4, '25', self.today,
                                     inherit_people=True, initial_amount=amount)
        self.assertEqual(new.read_people(), [Person('目标甲', Decimal('12.000000000000000001')),
                                            Person('目标乙', Decimal('-1')),
                                            Person('', amount), Person('', amount)])
        self.assertEqual(DataStore(new.folder).read_people(), new.read_people())
        self.assertEqual(self.source_files(), before)

    def test_legacy_successor_preserves_existing_unassigned_amounts(self):
        people = [Person('目标甲', Decimal('12.000000000000000001')),
                  Person('', Decimal(0)), Person('', Decimal('0.0000000001'))]
        self.store.save_people(people, allow_unassigned=True)
        before = self.source_files()
        new = self.store.create_successor(self.home / 'legacy-unassigned')
        self.assertEqual(new.read_people(), people)
        self.assertEqual(self.source_files(), before)

    def test_invalid_inputs_cannot_publish_or_mutate_original(self):
        before = self.source_files()
        future = (datetime.now(TZ).date() + timedelta(days=1)).isoformat()
        cases = [(None, '20', self.today, True), (True, '20', self.today, True),
                 (0, '20', self.today, True), (10001, '20', self.today, True),
                 (3, '-1', self.today, True), (3, None, self.today, True),
                 (3, '20', future, True), (3, '20', self.today, 'yes')]
        for number, args in enumerate(cases):
            with self.subTest(args=args):
                target = self.home / f'invalid-{number}'
                with self.assertRaises(GuardError):
                    self.store.create_list(target, *args[:3], inherit_people=args[3])
                self.assertFalse(target.exists())
                self.assertEqual(self.source_files(), before)

    def test_existing_destination_is_never_replaced(self):
        target = self.home / 'existing'
        target.mkdir()
        sentinel = target / 'preserved.txt'
        sentinel.write_text('fixture', encoding='utf-8')
        with self.assertRaises(GuardError):
            self.store.create_list(target, 3, '25', self.today)
        self.assertEqual(sentinel.read_text(encoding='utf-8'), 'fixture')

    def test_interrupted_write_is_not_published_and_original_files_remain_exact(self):
        before = self.source_files()
        target = self.home / 'failed'
        with patch.object(DataStore, 'save_people', side_effect=GuardError('fixture write failure')):
            with self.assertRaises(GuardError):
                self.store.create_list(target, 3, '25', self.today, inherit_people=False)
        self.assertFalse(target.exists())
        self.assertEqual(self.source_files(), before)
        staging = list(self.home.glob('.new-ledger-*'))
        self.assertEqual(len(staging), 1)
        with self.assertRaises(GuardError):
            DataStore(staging[0]).load()

    def test_original_workbook_change_before_publish_is_detected(self):
        target = self.home / 'changed'
        fingerprint = self.store.workbook_hash()
        with patch.object(self.store, 'workbook_hash', side_effect=[fingerprint, 'changed']):
            with self.assertRaisesRegex(GuardError, '发生变化'):
                self.store.create_list(target, 3, '25', self.today)
        self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
