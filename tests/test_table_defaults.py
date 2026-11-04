"""Offline defaults migration and exact, isolated personnel-template generation."""
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import load_workbook

from quota_guard.core import DataStore, GuardError, HEADERS, Person
from quota_guard.excel_io import read_people_workbook
from quota_guard.preferences import (PREFERENCES_FILENAME, TableDefaults,
    load_poll_interval, load_table_defaults, save_poll_interval,
    save_table_defaults, validate_kill_line)
from quota_guard.workbook_template import write_personnel_template


class TablePreferenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.path = self.home / PREFERENCES_FILENAME

    def test_defaults_do_not_create_files_or_enable_zero_prefill(self):
        self.assertEqual(load_table_defaults(self.home), (TableDefaults(), None))
        self.assertEqual(TableDefaults().kill_line, Decimal(0))
        self.assertEqual(TableDefaults().amount, Decimal(-1))
        self.assertEqual(list(self.home.iterdir()), [])

    def test_kill_line_validation_preserves_precision_and_normalizes_zero(self):
        for source, expected in [
            (0, Decimal(0)), ('-0.000', Decimal(0)),
            ('0E+999999999', Decimal(0)), (' 0.100 ', Decimal('0.100')),
            ('0.000000000000000001', Decimal('1e-18')),
            ('1000000000000', Decimal('1000000000000')),
        ]:
            with self.subTest(source=source):
                self.assertEqual(validate_kill_line(source), expected)
                self.assertEqual(TableDefaults(source, 'kill_line').amount, expected)
        for source in (-1, '-0.01', True, False, None, '', '=1', 'NaN',
                       'Infinity', '1e13', '1e-19'):
            with self.subTest(source=source), self.assertRaisesRegex(ValueError, '斩杀线'):
                validate_kill_line(source)
        for mode in ('', '-1', 'zero', True, None, ['skip']):
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, '请选择'):
                TableDefaults('0.1', mode)

    def test_legacy_interval_load_is_read_only_and_migrates_on_save(self):
        original = '{"schema":1,"poll_interval_seconds":37}'
        self.path.write_text(original, encoding='utf-8')
        self.assertEqual(load_poll_interval(self.home), (37, None))
        self.assertEqual(load_table_defaults(self.home), (TableDefaults(), None))
        self.assertEqual(self.path.read_text(encoding='utf-8'), original)
        saved = save_table_defaults(self.home, '0.0000000001', 'kill_line')
        self.assertEqual(saved.amount, Decimal('0.0000000001'))
        self.assertEqual(load_poll_interval(self.home), (37, None))
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8')), {
            'schema': 2, 'poll_interval_seconds': 37,
            'kill_line': '0.0000000001', 'fill_mode': 'kill_line',
        })

    def test_independent_saves_preserve_other_preferences_and_ledger_bytes(self):
        ledger = self.home / '账本'
        ledger.mkdir()
        settings = ledger / DataStore.CONFIG
        workbook = ledger / DataStore.WORKBOOK
        settings.write_bytes(b'{"total_budget":"1500"}')
        workbook.write_bytes(b'fixture personnel ledger')
        before = {p: p.read_bytes() for p in (settings, workbook)}
        save_table_defaults(self.home, '0.100000000000000001', 'kill_line')
        save_poll_interval(self.home, 45)
        expected = TableDefaults('0.100000000000000001', 'kill_line')
        self.assertEqual(load_table_defaults(self.home), (expected, None))
        save_table_defaults(self.home, '0.100000000000000001', 'skip')
        self.assertEqual(load_poll_interval(self.home), (45, None))
        self.assertEqual(load_table_defaults(self.home)[0].amount, Decimal(-1))
        self.assertEqual(load_table_defaults(self.home)[0].kill_line, expected.kill_line)
        self.assertEqual(before, {p: p.read_bytes() for p in before})
        self.assertFalse((ledger / PREFERENCES_FILENAME).exists())

    def test_bad_or_unknown_preferences_fail_closed_and_are_not_overwritten(self):
        valid = {'schema': 2, 'poll_interval_seconds': 25, 'kill_line': '0.1', 'fill_mode': 'kill_line'}
        variants = [
            'broken', '[]', json.dumps({'schema': 2, 'poll_interval_seconds': 10}),
            json.dumps(dict(valid, schema=3)), json.dumps(dict(valid, schema=True)),
            json.dumps(dict(valid, poll_interval_seconds=True)),
            json.dumps(dict(valid, kill_line=-1)), json.dumps(dict(valid, kill_line='-1')),
            json.dumps(dict(valid, kill_line='NaN')), json.dumps(dict(valid, fill_mode='zero')),
            json.dumps(dict(valid, unexpected='field')),
        ]
        for raw in variants:
            with self.subTest(raw=raw):
                self.path.write_text(raw, encoding='utf-8')
                defaults, notice = load_table_defaults(self.home)
                self.assertEqual(defaults, TableDefaults())
                self.assertIn('原文件保留', notice)
                self.assertEqual(load_poll_interval(self.home)[0], 10)
                for save in (lambda: save_poll_interval(self.home, 30),
                             lambda: save_table_defaults(self.home, '0.2', 'kill_line')):
                    with self.assertRaisesRegex(ValueError, '未覆盖原文件'):
                        save()
                    self.assertEqual(self.path.read_text(encoding='utf-8'), raw)

    def test_invalid_new_defaults_do_not_change_old_file(self):
        save_table_defaults(self.home, '0.5', 'kill_line')
        before = self.path.read_bytes()
        for amount, mode in [('-1', 'skip'), ('1e-19', 'skip'), ('0', 'invalid')]:
            with self.subTest(amount=amount, mode=mode), self.assertRaises(ValueError):
                save_table_defaults(self.home, amount, mode)
            self.assertEqual(self.path.read_bytes(), before)

    def test_failed_default_save_keeps_previous_interval_and_amount(self):
        save_poll_interval(self.home, 27)
        save_table_defaults(self.home, '0.2', 'kill_line')
        before = self.path.read_bytes()
        with patch('quota_guard.core.os.replace', side_effect=OSError('fixture replace failure')):
            with self.assertRaises(OSError):
                save_table_defaults(self.home, '0', 'skip')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(load_poll_interval(self.home), (27, None))
        self.assertEqual(load_table_defaults(self.home), (TableDefaults('0.2', 'kill_line'), None))
        self.assertEqual([p.name for p in self.home.iterdir()], [PREFERENCES_FILENAME])


class PersonnelTemplateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.target = self.folder / 'generated.xlsx'
        self.source = Path(__file__).resolve().parents[1] / 'templates' / '人员额度模板.xlsx'

    def test_skip_mode_generates_requested_rows_and_preserves_source(self):
        before = self.source.read_bytes()
        for count in (2, 14, 25):
            with self.subTest(count=count):
                write_personnel_template(self.target, self.source, Decimal(-1), count)
                self.assertEqual(read_people_workbook(self.target), [Person('', Decimal(-1))] * count)
                book = load_workbook(self.target)
                try:
                    sheet = book['人员额度']
                    self.assertEqual(sheet.max_row, count + 1)
                    self.assertEqual(tuple(c.value for c in sheet[1]), HEADERS)
                    self.assertEqual(sheet['B2'].value, '-1')
                    self.assertEqual(sheet['B2'].data_type, 's')
                    self.assertEqual(sheet.freeze_panes, 'A2')
                    self.assertGreater(sheet.column_dimensions['A'].width, 20)
                    notes = '\n'.join(str(row[0].value) for row in book['填写说明'])
                    self.assertIn('不要填写密钥', notes)
                    self.assertIn('必须填写名称', notes)
                finally:
                    book.close()
                self.assertEqual(self.source.read_bytes(), before)

    def test_kill_line_templates_require_names_and_keep_exact_imported_amount(self):
        for amount in (Decimal(0), Decimal('0.000000000000000001'),
                       Decimal('123456789.123456789')):
            with self.subTest(amount=amount):
                write_personnel_template(self.target, self.source, amount, 2)
                with self.assertRaisesRegex(GuardError, '必须填写'):
                    read_people_workbook(self.target)
                book = load_workbook(self.target)
                try:
                    sheet = book['人员额度']
                    self.assertEqual(sheet['B2'].value, format(amount, 'f'))
                    self.assertEqual(sheet['B2'].data_type, 's')
                    sheet['A2'], sheet['A3'] = '合成甲', '合成乙'
                    book.save(self.target)
                finally:
                    book.close()
                self.assertEqual(read_people_workbook(self.target),
                                 [Person('合成甲', amount), Person('合成乙', amount)])

    def test_zero_with_large_exponent_is_written_as_zero(self):
        write_personnel_template(self.target, self.source, '0E+999999999', 1)
        book = load_workbook(self.target)
        try:
            self.assertEqual(book['人员额度']['B2'].value, '0')
        finally:
            book.close()

    def test_invalid_amount_or_count_preserves_existing_target(self):
        self.target.write_bytes(b'original target')
        for amount, count in [(-2, 14), (True, 14), ('NaN', 14), ('1e-19', 14),
                              ('0.1', 0), ('0.1', 10001), ('0.1', True), ('0.1', 1.5)]:
            with self.subTest(amount=amount, count=count), self.assertRaises(GuardError):
                write_personnel_template(self.target, self.source, amount, count)
            self.assertEqual(self.target.read_bytes(), b'original target')
        self.assertEqual([p.name for p in self.folder.iterdir()], [self.target.name])

    def test_source_and_live_workbook_cannot_be_overwritten(self):
        original_source = self.source.read_bytes()
        with self.assertRaisesRegex(GuardError, '自带'):
            write_personnel_template(self.source, self.source, '0.1')
        self.assertEqual(self.source.read_bytes(), original_source)
        live = self.folder / DataStore.WORKBOOK
        live.write_bytes(b'fixture ledger workbook')
        with self.assertRaisesRegex(GuardError, '不能覆盖'):
            write_personnel_template(live, self.source, '0.1')
        self.assertEqual(live.read_bytes(), b'fixture ledger workbook')

    def test_atomic_failure_keeps_existing_target_and_removes_temp(self):
        self.target.write_bytes(b'previous complete file')
        with patch('quota_guard.workbook_template.os.replace', side_effect=OSError('fixture replace failure')):
            with self.assertRaises(OSError):
                write_personnel_template(self.target, self.source, '0.1', 2)
        self.assertEqual(self.target.read_bytes(), b'previous complete file')
        self.assertEqual([p.name for p in self.folder.iterdir()], [self.target.name])


if __name__ == '__main__':
    unittest.main()
