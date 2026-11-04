"""Persist new-table defaults without giving unnamed rows any management identity."""
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from openpyxl import load_workbook

from quota_guard.core import DataStore, GuardError, Person, TZ
from quota_guard.excel_io import read_people_workbook


class UnassignedDefaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.today = datetime.now(TZ).date().isoformat()

    def make_store(self, name='ledger', *, amount=-1, count=3, budget='100'):
        store = DataStore(self.home / name)
        store.initialize(count, budget, self.today, initial_amount=amount)
        return store

    def test_initial_default_is_in_workbook_and_survives_a_new_store_instance(self):
        for number, amount in enumerate((Decimal(-1), Decimal(0), Decimal('0.000000000000000001'))):
            with self.subTest(amount=amount):
                store = self.make_store(f'case-{number}', amount=amount)
                book = load_workbook(store.workbook_path, read_only=True, data_only=True)
                try:
                    values = list(book.active.iter_rows(min_row=2, values_only=True))
                    self.assertEqual(values, [(None, str(amount))] * 3)
                finally:
                    book.close()
                reopened = DataStore(store.folder)
                self.assertEqual(reopened.read_people(), [Person('', amount)] * 3)
                self.assertTrue(all(row['status'] == 'unassigned' for row in reopened.evaluate()))

    def test_invalid_default_cannot_initialize_any_files(self):
        for number, amount in enumerate((None, True, '', '-2', 'NaN', 'Infinity', '1e-19', '1e13', '=1+1')):
            with self.subTest(amount=amount):
                store = DataStore(self.home / f'invalid-{number}')
                with self.assertRaises(GuardError):
                    store.initialize(3, '100', self.today, initial_amount=amount)
                self.assertFalse(store.folder.exists())

    def test_unnamed_zero_rows_do_not_trigger_or_bind_even_with_zero_public_quota(self):
        store = self.make_store(amount=0, budget='0')
        store.bind_keys([{'name': '平台目标', 'tracking_id': 'fixture-key'}], 'fixture-account')
        self.assertEqual(store.active_bindings(), [])
        for row in store.evaluate():
            self.assertEqual(row['status'], 'unassigned')
            self.assertEqual(row['tracking_id'], '')
            self.assertIsNone(row['spent'])
            self.assertFalse(row['complete'])
            self.assertFalse(row['spent_is_lower_bound'])
        self.assertFalse(any(event['kind'] == 'quota_triggered' for event in store.events()))

    def test_unnamed_row_never_inherits_an_invalid_empty_name_binding_or_cost(self):
        store = self.make_store(amount=0)
        with store._connect() as db:
            db.execute("INSERT INTO bindings VALUES ('corrupt-empty-binding', '', 1)")
            db.execute("INSERT INTO costs VALUES ('corrupt-empty-binding', '999', 'fixture-source')")
        for row in store.evaluate():
            self.assertEqual(row['status'], 'unassigned')
            self.assertEqual(row['tracking_id'], '')
            self.assertIsNone(row['spent'])
        self.assertFalse(any(event['kind'] == 'quota_triggered' for event in store.events()))

    def test_filling_a_name_activates_only_that_zero_quota_row(self):
        store = self.make_store(amount=0)
        store.save_people([Person('目标甲', Decimal(0)), Person('', Decimal(0)), Person('', Decimal(0))],
                          allow_unassigned=True)
        store.bind_keys([{'name': '目标甲', 'tracking_id': 'fixture-a'},
                         {'name': '不在名单的对照', 'tracking_id': 'fixture-control'}], 'fixture-account')
        rows = store.evaluate()
        self.assertEqual([row['status'] for row in rows], ['triggered', 'unassigned', 'unassigned'])
        self.assertEqual(store.active_bindings(), [{'name': '目标甲', 'tracking_id': 'fixture-a'}])
        triggers = [event for event in store.events() if event['kind'] == 'quota_triggered']
        self.assertTrue(triggers)
        self.assertTrue(all(event['name'] == '目标甲' for event in triggers))

    def test_internal_edit_preserves_unassigned_values_without_silent_conversion(self):
        store = self.make_store(amount=0)
        people = [Person('', Decimal('0.0000000001')), Person('已有人员', Decimal(-1)),
                  Person('', Decimal(0))]
        store.save_people(people, expected_hash=store.workbook_hash(), allow_unassigned=True)
        self.assertEqual(DataStore(store.folder).read_people(), people)
        self.assertEqual([row['status'] for row in store.evaluate()], ['unassigned', 'skipped', 'unassigned'])

    def test_default_save_call_remains_strict_and_does_not_overwrite(self):
        store = self.make_store()
        before = store.workbook_path.read_bytes()
        for amount in (Decimal(0), Decimal('0.1')):
            with self.subTest(amount=amount), self.assertRaises(GuardError):
                store.save_people([Person('', amount)])
            self.assertEqual(store.workbook_path.read_bytes(), before)

    def test_external_import_remains_strict_but_internal_reader_can_reopen_defaults(self):
        store = self.make_store(amount='0.0000000001')
        with self.assertRaisesRegex(GuardError, '必须填写 API 名称'):
            read_people_workbook(store.workbook_path)
        self.assertEqual(read_people_workbook(store.workbook_path, allow_unassigned=True),
                         [Person('', Decimal('0.0000000001'))] * 3)

    def test_unassigned_read_and_save_still_reject_invalid_amounts_and_options(self):
        store = self.make_store(amount=0)
        before = store.workbook_path.read_bytes()
        for value in (None, True, '', '-2', 'NaN', 'Infinity', '1e-19', '1e13', '=1+1'):
            with self.subTest(value=value), self.assertRaises(GuardError):
                store.save_people([Person('', value)], allow_unassigned=True)
            self.assertEqual(store.workbook_path.read_bytes(), before)
        for value in (None, 1, 'yes'):
            with self.subTest(option=value), self.assertRaises(GuardError):
                store.save_people([Person('', Decimal(0))], allow_unassigned=value)
            with self.subTest(reader_option=value), self.assertRaises(GuardError):
                read_people_workbook(store.workbook_path, allow_unassigned=value)

    def test_internal_reader_still_rejects_invalid_unnamed_amount_from_disk(self):
        store = self.make_store(amount=0)
        for value in ('NaN', '-2', '1e-19', '1e13', '=1+1'):
            with self.subTest(value=value):
                book = load_workbook(store.workbook_path)
                book.active['B2'] = value
                book.save(store.workbook_path)
                book.close()
                with self.assertRaises(GuardError):
                    DataStore(store.folder).read_people()

    def test_blank_new_list_persists_selected_default_and_preserves_original(self):
        store = self.make_store()
        store.save_people([Person('已有人员', Decimal(8)), Person('', Decimal(-1))])
        before = store.workbook_path.read_bytes()
        new = store.create_list(self.home / 'blank', 2, '50', self.today,
                                inherit_people=False, initial_amount=Decimal('0.0000000001'))
        self.assertEqual(DataStore(new.folder).read_people(), [Person('', Decimal('0.0000000001'))] * 2)
        self.assertEqual(store.workbook_path.read_bytes(), before)

    def test_inherited_named_amounts_stay_exact_while_empty_positions_use_current_default(self):
        store = self.make_store(amount='0.5', count=4)
        original = [Person('保留金额', Decimal('12.000000000000000001')), Person('保留跳过', Decimal(-1)),
                    Person('', Decimal('0.5')), Person('', Decimal('0.5'))]
        store.save_people(original, allow_unassigned=True)
        before = store.workbook_path.read_bytes()
        new = store.create_list(self.home / 'inherited', 5, '100', self.today,
                                inherit_people=True, initial_amount=0)
        self.assertEqual(new.read_people(), original[:2] + [Person('', Decimal(0))] * 3)
        self.assertEqual(store.read_people(), original)
        self.assertEqual(store.workbook_path.read_bytes(), before)

    def test_legacy_successor_preserves_existing_unassigned_amounts(self):
        store = self.make_store(amount=0)
        original = [Person('已有人员', Decimal(7)), Person('', Decimal('0.1')), Person('', Decimal(0))]
        store.save_people(original, allow_unassigned=True)
        new = store.create_successor(self.home / 'successor')
        self.assertEqual(new.read_people(), original)

    def test_invalid_new_list_default_does_not_publish_or_mutate_original(self):
        store = self.make_store(amount=0)
        before = store.workbook_path.read_bytes()
        for number, amount in enumerate(('-2', 'NaN', '1e-19', True)):
            target = self.home / f'bad-list-{number}'
            with self.subTest(amount=amount), self.assertRaises(GuardError):
                store.create_list(target, 3, '100', self.today, inherit_people=False, initial_amount=amount)
            self.assertFalse(target.exists())
            self.assertEqual(store.workbook_path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
