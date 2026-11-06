"""Forecast shadow is local evidence only, never a second quota engine."""
from contextlib import closing
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from quota_guard import forecast_shadow as shadow


def points(amounts, *, interval=3600, context="scope", marker="unchanged-history"):
    return [shadow.Observation(context, f"evidence-{index}", value, True,
                               index * interval + 2, index * interval, interval, marker)
            for index, value in enumerate(amounts)]


class ForecastTests(unittest.TestCase):
    def test_known_comparable_times_produce_data_anchored_eta(self):
        history = points(["0.25", "0.35", "0.50", "0.70"])
        result = shadow.forecast_observations("scope", "1", history, 10802)
        self.assertEqual(result["status"], "estimated")
        self.assertLess(abs(Decimal(result["eta_at"]) - 18000), Decimal("1e-20"))
        self.assertEqual(result["data_as_of"], 10800)
        self.assertTrue(result["shadow_only"])
        self.assertFalse(result["low_resolution"])
        reread = shadow.forecast_observations("scope", "1", history, 11000)
        self.assertEqual(reread["eta_at"], result["eta_at"])

    def test_daily_evidence_keeps_daily_resolution(self):
        result = shadow.forecast_observations(
            "scope", "1", points(["0.25", "0.35", "0.50", "0.70"], interval=86400), 259202)
        self.assertEqual(result["status"], "estimated")
        self.assertTrue(result["low_resolution"])
        self.assertEqual(result["interval_seconds"], 86400)

    def test_default_console_totals_cannot_manufacture_time(self):
        history = [shadow.Observation("scope", f"s-{n}", str(n / 10), True, 100+n*10)
                   for n in range(4)]
        result = shadow.forecast_observations("scope", "1", history, 130)
        self.assertEqual(result["status"], "data_time_unknown")
        self.assertIsNone(result["eta_at"])

    def test_same_target_coverage_from_other_payloads_is_not_new_interval(self):
        first = points(["0.40"])[0]
        history = [replace(first, evidence=f"other-key-changed-{n}", first_observed_at=2+n)
                   for n in range(4)]
        result = shadow.forecast_observations("scope", "1", history, 5)
        self.assertEqual(result["status"], "insufficient_history")
        self.assertEqual(result["usable_endpoints"], 1)

    def test_flat_new_coverage_and_intermittent_are_explicit(self):
        flat = shadow.forecast_observations("scope", "1", points(["0.4"] * 4), 10802)
        burst = shadow.forecast_observations("scope", "1", points(["0.1", "0.1", "0.1", "0.4"]), 10802)
        self.assertEqual(flat["status"], "no_observed_growth")
        self.assertEqual(burst["status"], "intermittent_growth")
        self.assertIsNone(flat["eta_at"])
        self.assertIsNone(burst["eta_at"])

    def test_positive_revision_and_offsetting_old_bucket_revision_reset_window(self):
        history = points(["0.25", "0.35", "0.50", "0.70"])
        correction = replace(history[-1], evidence="correction", cumulative="0.80",
                             first_observed_at=10803, revision_marker="old-bucket-fixed")
        result = shadow.forecast_observations("scope", "1", history + [correction], 10803)
        self.assertEqual(result["status"], "revision_detected")
        # A marker change with a larger total also catches the case where an
        # old bucket decreased but another bucket increased by a larger amount.
        correction = replace(correction, data_as_of=14400, first_observed_at=14402)
        result = shadow.forecast_observations("scope", "1", history + [correction], 14402)
        self.assertEqual(result["status"], "revision_detected")

    def test_new_comparable_window_can_resume_after_revision(self):
        old = points(["0.1", "0.2"])
        new = [replace(p, evidence="new-"+p.evidence, data_as_of=p.data_as_of+7200,
                       first_observed_at=p.first_observed_at+7200)
               for p in points(["0.3", "0.4", "0.5", "0.6"], marker="revised-history")]
        result = shadow.forecast_observations("scope", "1", old + new, 18002)
        self.assertEqual(result["status"], "estimated")

    def test_missing_or_unknown_revision_does_not_produce_eta(self):
        history = points(["0.1", "0.2", "0.3", "0.4"])
        cases = [(replace(history[-1], complete=False), "coverage_incomplete"),
                 (replace(history[-1], cumulative=None), "coverage_incomplete"),
                 (replace(history[-1], revision_marker=None), "revision_unknown"),
                 (replace(history[-1], interval_seconds=None), "granularity_unknown"),
                 (replace(history[-1], context="different-bindings"), "scope_changed")]
        for last, status in cases:
            with self.subTest(status=status):
                result = shadow.forecast_observations("scope", "1", history[:-1]+[last], 10802)
                self.assertEqual(result["status"], status)
                self.assertIsNone(result["eta_at"])

    def test_stale_repeat_does_not_refresh_data_age(self):
        history = points(["0.1", "0.2", "0.3", "0.4"])
        history.append(replace(history[-1], first_observed_at=16000))
        result = shadow.forecast_observations("scope", "1", history, 16000)
        self.assertEqual(result["status"], "stale_data")
        self.assertIsNone(result["eta_at"])

    def test_invalid_conflicting_and_future_input_is_contained(self):
        history = points(["0.1", "0.2", "0.3", "0.4"])
        conflict = replace(history[-1], cumulative="0.9", first_observed_at=10803)
        self.assertEqual(shadow.forecast_observations("scope", "1", history+[conflict], 10803)["status"],
                         "evidence_conflict")
        future = replace(history[-1], data_as_of=11000)
        self.assertEqual(shadow.forecast_observations("scope", "1", history[:-1]+[future], 10802)["status"],
                         "invalid_time")
        self.assertEqual(shadow.forecast_observations("scope", "NaN", history, 10802)["status"], "invalid_input")
        self.assertEqual(shadow.forecast_observations("scope", "0", history, 10802)["status"], "zero_limit")


class ShadowPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="quota-shadow-test-")
        self.folder = Path(self.temp.name)
        self.row = dict(name="private-person-name", tracking_id="synthetic-tracking-id-001",
                        amount=Decimal("1"), share=Decimal("100"), spent=Decimal("0.4"),
                        complete=True, status="ok", binding_scope="complete-person-binding-scope")

    def tearDown(self):
        self.temp.cleanup()

    def record(self, **changes):
        args = dict(scope="synthetic-account-period-cny", rows=[self.row],
                    source_id="whole-payload-source", collected_at=10802)
        args.update(changes)
        return shadow.record_collection(self.folder, **args)

    def rows(self):
        with closing(sqlite3.connect(self.folder / shadow.DATABASE)) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM observations ORDER BY id")]

    def test_default_production_records_unknown_without_touching_rows_or_business_file(self):
        marker = self.folder / "状态.db"
        marker.write_bytes(b"not-opened-by-shadow")
        before = dict(self.row)
        result = self.record()
        self.assertEqual(result["statuses"], {"data_time_unknown": 1})
        self.assertEqual(result["estimated"], 0)
        self.assertEqual(before, self.row)
        self.assertEqual(marker.read_bytes(), b"not-opened-by-shadow")
        saved = self.rows()[0]
        self.assertIsNone(saved["coverage_end"])
        self.assertIsNone(json.loads(saved["forecast"])["eta_at"])

    def test_repeat_and_other_key_source_change_preserve_first_observed(self):
        self.record()
        self.record(source_id="another-key-changed-the-payload", collected_at=10812)
        saved = self.rows()
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["first_observed"], 10802)
        self.assertEqual(saved[0]["last_read"], 10812)
        self.assertIsNone(saved[0]["coverage_end"])

    def test_old_target_observation_replay_is_not_a_current_forecast(self):
        self.record()
        self.record(rows=[self.row | dict(spent=Decimal("0.5"))], collected_at=10812)
        result = self.record(collected_at=10822)
        self.assertEqual(result["statuses"], {"stale_replay": 1})
        saved = self.rows()
        self.assertEqual(len(saved), 2)
        self.assertEqual(saved[0]["first_observed"], 10802)
        self.assertEqual(saved[1]["cumulative"], "0.5")
        self.assertIsNone(json.loads(saved[0]["forecast"])["eta_at"])

    def test_disk_contains_only_hashed_references(self):
        self.record(source_id="arbitrary-source-must-not-be-copied")
        payload = (self.folder / shadow.DATABASE).read_bytes()
        for raw in (self.row["name"], self.row["tracking_id"], self.row["binding_scope"],
                    "synthetic-account-period-cny", "arbitrary-source-must-not-be-copied"):
            self.assertNotIn(raw.encode(), payload)

    def test_actual_sqlite_reopen_retains_timestamps_and_explicit_forecast(self):
        for index, amount in enumerate(("0.25", "0.35", "0.50", "0.70")):
            result = self.record(rows=[self.row | dict(spent=Decimal(amount))],
                                 source_id=f"response-{index}", collected_at=index*3600+2,
                                 data_as_of=index*3600, interval_seconds=3600,
                                 revision_marker="known-unchanged-covered-history")
        self.assertEqual(result["statuses"], {"estimated": 1})
        saved = self.rows()
        self.assertEqual(len(saved), 4)
        self.assertEqual([r["first_observed"] for r in saved], [2, 3602, 7202, 10802])
        self.assertLess(abs(Decimal(json.loads(saved[-1]["forecast"])["eta_at"])-18000), Decimal("1e-20"))

    def test_scope_and_binding_change_do_not_reuse_old_window(self):
        for index, amount in enumerate(("0.25", "0.35", "0.50", "0.70")):
            self.record(rows=[self.row | dict(spent=Decimal(amount))],
                        source_id=f"response-{index}", collected_at=index*3600+2,
                        data_as_of=index*3600, interval_seconds=3600, revision_marker="history")
        for changes in (dict(scope="next-period"), dict(rows=[self.row | dict(binding_scope="changed-binding")])):
            result = self.record(**changes, data_as_of=14400, collected_at=14402,
                                 interval_seconds=3600, revision_marker="history")
            self.assertEqual(result["statuses"], {"insufficient_history": 1})

    def test_retention_bounds_are_applied_to_real_database(self):
        with patch.object(shadow, "MAX_TARGET_HISTORY", 4), patch.object(shadow, "MAX_ROWS", 8):
            for index in range(12):
                self.record(rows=[self.row | dict(spent=Decimal(index)/100)], collected_at=10802+index)
            self.assertEqual(len(self.rows()), 4)
            for index in range(8):
                self.record(scope=f"scope-{index}", collected_at=10900+index)
            self.assertEqual(len(self.rows()), 8)

    def test_skip_blank_retired_and_unbound_rows_create_no_file(self):
        rows = [self.row | dict(amount=Decimal("-1")), self.row | dict(name=""),
                self.row | dict(status="retired"), self.row | dict(tracking_id=None)]
        result = self.record(rows=rows)
        self.assertEqual(result["count"], 0)
        self.assertFalse((self.folder / shadow.DATABASE).exists())

    def test_unknown_fee_and_zero_limit_remain_different(self):
        result = self.record(rows=[self.row | dict(spent=None, complete=False)])
        self.assertEqual(result["statuses"], {"coverage_incomplete": 1})
        result = self.record(rows=[self.row | dict(amount=Decimal("0"), spent=None, complete=False)])
        self.assertEqual(result["statuses"], {"zero_limit": 1})

    def test_duplicate_identity_rejects_whole_batch(self):
        with self.assertRaises(shadow.ShadowRecordError):
            self.record(rows=[self.row, self.row | dict(name="different-person")])
        self.assertFalse((self.folder / shadow.DATABASE).exists())

    def test_foreign_file_and_corrupt_database_are_not_replaced(self):
        path = self.folder / shadow.DATABASE
        path.write_bytes(b"foreign-existing-file")
        before = sha256(path.read_bytes()).hexdigest()
        with self.assertRaises(shadow.ShadowRecordError):
            self.record()
        self.assertEqual(sha256(path.read_bytes()).hexdigest(), before)

    def test_sqlite_contention_is_a_controlled_optional_failure(self):
        self.record()
        with closing(sqlite3.connect(self.folder / shadow.DATABASE)) as db, db:
            db.execute("BEGIN EXCLUSIVE")
            with self.assertRaises(shadow.ShadowRecordError) as caught:
                self.record(collected_at=10812)
            self.assertNotIn("locked", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
