"""Offline business regressions for a positive threshold of 0.0000000001 CNY."""

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest

from openpyxl import Workbook

from quota_guard.browser_service import parse_cost_response
from quota_guard.core import DataStore, HEADERS, Person, TZ
from quota_guard.excel_io import read_people_workbook
from quota_guard.ui import amount_text, eligible_for_automatic_action, parse_editor_person


THRESHOLD_TEXT = '0.0000000001'
THRESHOLD = Decimal(THRESHOLD_TEXT)
TARGET_NAME = '微额测试人员'
TARGET_ID = '10000000-0000-4000-8000-000000000001'
CONTROL_NAME = '保留对照人员'
CONTROL_ID = '10000000-0000-4000-8000-000000000002'


class TinyThresholdTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)
        self.start = datetime.now(TZ).date().isoformat()
        self.store = DataStore(self.folder / 'ledger')
        self.store.initialize(2, '2', self.start)
        self.store.save_people([
            parse_editor_person(TARGET_NAME, THRESHOLD_TEXT),
            Person(CONTROL_NAME, Decimal(1)),
        ])
        self.store.bind_keys([
            {'name': TARGET_NAME, 'tracking_id': TARGET_ID},
            {'name': CONTROL_NAME, 'tracking_id': CONTROL_ID},
        ], 'synthetic-tiny-threshold-account')

    def snapshot(self, amount, source):
        rows = self.store.apply_snapshot(
            {TARGET_ID: Decimal(amount), CONTROL_ID: Decimal(0)},
            source, self.start, self.start)
        return next(row for row in rows if row['tracking_id'] == TARGET_ID)

    def test_editor_workbook_and_reopen_keep_positive_threshold_not_zero(self):
        reopened = DataStore(self.store.folder)
        person = reopened.read_people()[0]
        self.assertEqual(person.amount, THRESHOLD)
        self.assertEqual(amount_text(person.amount), THRESHOLD_TEXT)
        # Reopening the editor and saving again must preserve the same rule.
        edited = parse_editor_person(person.name, amount_text(person.amount))
        reopened.save_people([edited, Person(CONTROL_NAME, Decimal(1))])
        before = next(row for row in reopened.evaluate() if row['name'] == TARGET_NAME)
        self.assertEqual(before['status'], 'unknown')
        self.assertFalse(eligible_for_automatic_action(before))
        zero = self.snapshot('0', 'explicit-zero')
        self.assertEqual(zero['status'], 'ok')
        self.assertEqual(zero['amount'], THRESHOLD)
        self.assertEqual(zero['reasons'], [])
        self.assertFalse(eligible_for_automatic_action(zero))

    def test_exact_boundary_and_audit_survive_excel_and_database_round_trip(self):
        below = self.snapshot('0.000000000099999999', 'below-threshold')
        self.assertEqual(below['status'], 'ok')
        self.assertFalse(eligible_for_automatic_action(below))
        at = self.snapshot(THRESHOLD_TEXT, 'at-threshold')
        self.assertEqual(at['reasons'], ['达到个人额度'])
        self.assertTrue(eligible_for_automatic_action(at))
        above = self.snapshot('0.000000000100000001', 'above-threshold')
        self.assertEqual(above['reasons'], ['达到个人额度'])
        restored = DataStore(self.store.folder)
        evidence = restored.action_evidence(TARGET_NAME, TARGET_ID)
        self.assertEqual(evidence['amount'], THRESHOLD_TEXT)
        self.assertEqual(evidence['spent'], '0.000000000100000001')
        self.assertEqual(evidence['status'], 'triggered')
        self.assertIs(evidence['complete'], True)
        triggers = [event for event in restored.events()
                    if event['kind'] == 'quota_triggered' and event['name'] == TARGET_NAME]
        self.assertEqual(len(triggers), 1)
        self.assertEqual(Decimal(triggers[0]['amount']), THRESHOLD)
        self.assertEqual(Decimal(triggers[0]['spent']), THRESHOLD)

    def test_imported_numeric_or_text_excel_threshold_remains_positive(self):
        for value in (1e-10, THRESHOLD_TEXT, '1E-10'):
            with self.subTest(value=value):
                external = self.folder / 'synthetic-import.xlsx'
                workbook = Workbook()
                try:
                    sheet = workbook.active
                    sheet.append(HEADERS)
                    sheet.append([TARGET_NAME, value])
                    sheet.append([CONTROL_NAME, 1])
                    # Display formatting must not change the underlying amount.
                    sheet['B2'].number_format = '0.00'
                    workbook.save(external)
                finally:
                    workbook.close()
                self.store.save_people(read_people_workbook(external))
                restored = DataStore(self.store.folder)
                person = restored.read_people()[0]
                self.assertEqual(person.amount, THRESHOLD)
                self.assertEqual(amount_text(person.amount), THRESHOLD_TEXT)
                self.assertEqual(restored.evaluate()[0]['status'], 'unknown')

    def test_two_tiny_model_charges_trigger_after_real_parser_and_persistence(self):
        begin = int(datetime.fromisoformat(self.start).replace(tzinfo=TZ).timestamp())
        end = int((datetime.fromisoformat(self.start).replace(tzinfo=TZ)
                   + timedelta(days=1)).timestamp())
        series = [
            {'api_key': {'tracking_id': TARGET_ID}, 'model': model,
             'buckets': [{'time': begin, 'cost': cost}]}
            for model, cost in [('synthetic-a', '0.00000000004'),
                                ('synthetic-b', '0.00000000006')]
        ]
        payload = {'code': 0, 'data': {'biz_code': 0, 'biz_data': {
            'start': begin, 'end': end, 'bucket': 86400,
            'models': ['synthetic-a', 'synthetic-b'], 'functions': [],
            'data': [{'currency': 'CNY', 'function_series': [], 'series': series}],
        }}}
        url = ('https://platform.deepseek.com/api/v0/usage/by_api_key/cost'
               f'?start={begin}&end={end}&tz=28800')
        totals = parse_cost_response(payload, url, self.start, self.start)
        rows = self.store.apply_snapshot(totals, 'two-tiny-charges', self.start, self.start)
        target = next(row for row in rows if row['tracking_id'] == TARGET_ID)
        self.assertEqual(target['spent'], THRESHOLD)
        self.assertEqual(target['reasons'], ['达到个人额度'])
        self.assertTrue(eligible_for_automatic_action(target))
        persisted = DataStore(self.store.folder).action_evidence(TARGET_NAME, TARGET_ID)
        self.assertEqual(Decimal(persisted['spent']), THRESHOLD)

    def test_missing_latest_cost_does_not_authorize_from_old_tiny_trigger(self):
        self.snapshot(THRESHOLD_TEXT, 'complete-trigger')
        rows = self.store.apply_snapshot({CONTROL_ID: Decimal(0)},
                                         'target-missing', self.start, self.start)
        target = next(row for row in rows if row['tracking_id'] == TARGET_ID)
        self.assertEqual(target['spent'], THRESHOLD)
        self.assertEqual(target['status'], 'triggered')
        self.assertIs(target['complete'], False)
        self.assertIs(target['spent_is_lower_bound'], True)
        self.assertFalse(eligible_for_automatic_action(target))
        self.assertIs(self.store.action_evidence(TARGET_NAME, TARGET_ID)['complete'], False)


if __name__ == '__main__':
    unittest.main()
