from decimal import Decimal
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile
from xml.etree import ElementTree as ET

from openpyxl import Workbook
from openpyxl.styles import PatternFill

from quota_guard.core import GuardError, HEADERS, Person
from quota_guard.excel_io import read_people_workbook


class ExcelImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "import.xlsx"

    def workbook(self, rows=(), header=HEADERS):
        book = Workbook()
        book.active.title = "任意名字"
        book.active.append(header)
        for row in rows:
            book.active.append(row)
        return book

    def read(self, book):
        book.save(self.path)
        book.close()
        return read_people_workbook(self.path)

    def test_renamed_sheet_numeric_text_styles_and_blank_rows(self):
        book = self.workbook([("  示例甲  ", " 12.50 "), (None, None),
                              ("示例乙", 0), (None, -1), ("示例丙", 2.75)])
        book.active["B6"].number_format = '"¥"#,##0.00'
        book.active["XFD3"].fill = PatternFill("solid", fgColor="FFFF00")
        book.create_sheet("说明").append(["可修改颜色、字体与工作表名称"])
        self.assertEqual(self.read(book), [Person("示例甲", Decimal("12.50")),
            Person("示例乙", Decimal(0)), Person("", Decimal(-1)), Person("示例丙", Decimal("2.75"))])

    def test_column_order_and_whitespace_headers(self):
        self.assertEqual(self.read(self.workbook([(3, "示例")],
            (" " + HEADERS[1], HEADERS[0] + " "))), [Person("示例", Decimal(3))])

    def test_source_is_not_modified(self):
        book = self.workbook([("示例", 5)])
        book.save(self.path)
        before = self.path.read_bytes()
        read_people_workbook(self.path)
        self.assertEqual(before, self.path.read_bytes())

    def test_extra_populated_column_reports_row(self):
        with self.assertRaisesRegex(GuardError, "第 2 行.*两列之外"):
            self.read(self.workbook([("示例", 1, "unexpected")]))

    def test_missing_and_duplicate_headers(self):
        for header in [(HEADERS[0], "wrong"), (HEADERS[0], HEADERS[0], HEADERS[1])]:
            with self.subTest(header=header), self.assertRaisesRegex(GuardError, "第 1 行.*重复或缺列"):
                self.read(self.workbook(header=header))

    def test_two_matching_sheets_are_ambiguous(self):
        book = self.workbook([("示例", 1)])
        book.create_sheet("另一份").append(HEADERS)
        with self.assertRaisesRegex(GuardError, "多个工作表"):
            self.read(book)

    def test_invalid_amounts_are_not_silently_zero(self):
        for amount in [None, " ", "not numeric", True, -2, "NaN", "Infinity", "1e13", "0.0000000000000000001"]:
            with self.subTest(amount=amount), self.assertRaisesRegex(GuardError, "第 2 行"):
                self.read(self.workbook([("示例", amount)]))

    def test_blank_name_requires_skipped_amount(self):
        for amount in [0, 2]:
            with self.subTest(amount=amount), self.assertRaisesRegex(GuardError, "第 2 行.*必须填写"):
                self.read(self.workbook([(None, amount)]))
        self.assertEqual(self.read(self.workbook([(None, -1)])), [Person("", Decimal(-1))])

    def test_duplicate_trimmed_names_report_both_rows(self):
        with self.assertRaisesRegex(GuardError, "第 3 行.*第 2 行重复"):
            self.read(self.workbook([("示例", 1), (" 示例 ", 2)]))

    def test_formula_without_cache_requires_recalculation(self):
        with self.assertRaisesRegex(GuardError, "第 2 行.*重新计算"):
            self.read(self.workbook([("示例", "=1+2")]))

    def test_formula_with_numeric_cache_is_read_without_execution(self):
        book = self.workbook([("示例", "=1+2")])
        book.save(self.path)
        result = BytesIO()
        with ZipFile(self.path) as original, ZipFile(result, "w", ZIP_DEFLATED) as modified:
            for entry in original.infolist():
                data = original.read(entry.filename)
                if entry.filename == "xl/worksheets/sheet1.xml":
                    root = ET.fromstring(data)
                    ns = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
                    cached = root.find(f'.//{ns}c[@r="B2"]/{ns}v')
                    self.assertIsNotNone(cached)
                    cached.text = '3'
                    data = ET.tostring(root)
                modified.writestr(entry, data)
        self.path.write_bytes(result.getvalue())
        self.assertEqual(read_people_workbook(self.path), [Person("示例", Decimal(3))])

    def test_formula_names_and_excel_errors_are_rejected(self):
        for row in [("=A3", 1), ("示例", "#VALUE!")]:
            with self.subTest(row=row), self.assertRaisesRegex(GuardError, "第 2 行"):
                self.read(self.workbook([row]))

    def test_file_and_expanded_size_limits(self):
        book = self.workbook([("示例", 1)])
        book.save(self.path)
        with patch("quota_guard.excel_io.MAX_FILE_BYTES", 10), self.assertRaisesRegex(GuardError, "10 MB"):
            read_people_workbook(self.path)
        with patch("quota_guard.excel_io.MAX_EXPANDED_BYTES", 10), self.assertRaisesRegex(GuardError, "解压内容"):
            read_people_workbook(self.path)

    def test_rows_are_bounded(self):
        with patch("quota_guard.excel_io.MAX_ROWS", 2), self.assertRaisesRegex(GuardError, "范围过大"):
            self.read(self.workbook([("示例甲", 1), ("示例乙", 2)]))

    def test_corrupt_or_wrong_format_workbook(self):
        self.path.write_text("not a workbook", encoding="utf-8")
        with self.assertRaisesRegex(GuardError, "完整保存"):
            read_people_workbook(self.path)
        with self.assertRaisesRegex(GuardError, ".xlsx"):
            read_people_workbook(self.path.with_suffix(".xls"))


if __name__ == "__main__":
    unittest.main()
