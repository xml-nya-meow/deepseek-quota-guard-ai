import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path

from quota_guard.ui import (amount_text, has_saved_data, parse_editor_person, safe_error,
                            collection_is_fresh, eligible_for_automatic_action)


class HelperTests(unittest.TestCase):
    def test_special_values(self):
        self.assertEqual(parse_editor_person("", "-1").amount, Decimal(-1))
        self.assertEqual(parse_editor_person("某人", "0").amount, Decimal(0))
        for value in ("-2", "NaN", "Infinity", "", "=1+1"):
            with self.assertRaises(ValueError):
                parse_editor_person("某人", value)
        with self.assertRaises(ValueError):
            parse_editor_person("", "0")

    def test_precise_display(self):
        self.assertEqual(amount_text(Decimal("107.0001")), "107.0001")
        self.assertEqual(amount_text(None), "未知")

    def test_history_is_not_new(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)
            self.assertFalse(has_saved_data(path))
            (path / "运行日志-2026-10-29.jsonl").write_text("", encoding="utf8")
            self.assertTrue(has_saved_data(path))

    def test_error_redaction(self):
        self.assertNotIn("sk-secret123", safe_error("failed sk-secret123"))

    def test_old_or_cross_day_collection_cannot_authorize(self):
        tz = timezone(timedelta(hours=8))
        start = datetime(2026, 10, 30, 23, 59, 50, tzinfo=tz).timestamp()
        self.assertTrue(collection_is_fresh(start, 100, '2026-10-30',
                                           wall_now=start + 2, monotonic_now=102))
        self.assertFalse(collection_is_fresh(start, 100, '2026-10-30',
                                            wall_now=start + 11, monotonic_now=111))
        self.assertFalse(collection_is_fresh(start, 100, '2026-10-30',
                                            wall_now=start - 1, monotonic_now=101))
        self.assertFalse(collection_is_fresh(start, 100, '2026-10-30',
                                            wall_now=start + 301, monotonic_now=401))

    def test_sleep_or_clock_discontinuity_invalidates_collection(self):
        start = datetime(2026, 10, 30, 12, tzinfo=timezone(timedelta(hours=8))).timestamp()
        self.assertFalse(collection_is_fresh(start, 100, '2026-10-30',
                                            wall_now=start + 100, monotonic_now=102))
        self.assertFalse(collection_is_fresh(None, None, '2026-10-30',
                                            wall_now=start, monotonic_now=100))

    def test_missing_target_fees_cannot_reuse_a_historical_trigger(self):
        row = {'status': 'triggered', 'tracking_id': 'example-id', 'amount': Decimal(1),
               'share': Decimal(2), 'spent': Decimal(3), 'complete': False}
        self.assertFalse(eligible_for_automatic_action(row))
        row['complete'] = True
        self.assertTrue(eligible_for_automatic_action(row))
        row.update(amount=Decimal(0), complete=False, spent=None)
        self.assertTrue(eligible_for_automatic_action(row))
        row['tracking_id'] = ''
        self.assertFalse(eligible_for_automatic_action(row))


if __name__ == "__main__":
    unittest.main()
