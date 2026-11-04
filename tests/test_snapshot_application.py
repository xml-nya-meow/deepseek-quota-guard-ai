"""Offline contracts for accepted, rejected, and recovered cost snapshots.

Every ledger and workbook belongs to a TemporaryDirectory.  These tests model
platform observations with synthetic IDs; they never open a browser or account.
"""
from contextlib import closing
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from quota_guard.core import DataStore, Person, SnapshotApplyError, SnapshotApplyResult


class SnapshotApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.store = DataStore(self.folder)
        # A complete five-day period crossing a month boundary, with no reliance
        # on live platform dates or changing daily billing data.
        self.start = "2024-01-29"
        self.end = "2024-02-02"
        self.store.initialize(2, "200", self.start)

    def bind_people(self, *pairs):
        pairs = pairs or (("Alpha", "10"), ("Beta", "10"))
        self.store.save_people([Person(name, Decimal(str(amount))) for name, amount in pairs])
        self.store.bind_keys(
            [{"name": name, "tracking_id": "tracking-" + name} for name, _ in pairs],
            "synthetic-account",
        )

    def apply(self, amounts, source="S1", **kwargs):
        return self.store.apply_snapshot_result(
            {"tracking-" + name: Decimal(str(amount)) for name, amount in amounts.items()},
            source,
            kwargs.pop("start", self.start),
            kwargs.pop("end", self.end),
            **kwargs,
        )

    def database_state(self):
        """Inspect persisted state without evaluate(), exports, or repair writes."""
        with closing(sqlite3.connect(self.store.db_path.as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("BEGIN")
            return {
                table: db.execute("SELECT * FROM " + table + " ORDER BY rowid").fetchall()
                for table in ("meta", "bindings", "costs", "snapshots", "events", "sqlite_sequence")
            }

    def events_of_kind(self, kind):
        return [event for event in self.store.events() if event["kind"] == kind]

    def assert_rejected_unchanged(self, reason, amounts, source="rejected", **kwargs):
        before = self.database_state()
        with patch.object(self.store, "export_logs") as exporter:
            with self.assertRaises(SnapshotApplyError) as caught:
                self.apply(amounts, source, operation_id="operation-rejected", **kwargs)
        error = caught.exception
        self.assertEqual(error.reason_code, reason)
        self.assertEqual(error.persistence_state, "not_written")
        self.assertEqual(error.stage, "snapshot_validation")
        self.assertEqual(error.operation_id, "operation-rejected")
        self.assertIsNone(error.application)
        exporter.assert_not_called()
        self.assertEqual(self.database_state(), before)
        return error

    def test_cross_month_snapshot_reports_durable_source_and_exact_coverage(self):
        self.bind_people()
        result = self.apply({"Alpha": "1.234567890123456789", "Beta": "2"}, operation_id="read-one")
        self.assertIsInstance(result, SnapshotApplyResult)
        self.assertEqual(result.operation_id, "read-one")
        self.assertEqual(result.observed_source_id, "S1")
        self.assertEqual(result.effective_snapshot_id, "S1")
        self.assertEqual(result.apply_outcome, "applied")
        self.assertEqual(result.persistence_state, "persisted")
        self.assertEqual(result.reason_code, "OK")
        self.assertEqual(result.export_state, "complete")
        self.assertTrue(result.fees_current)
        self.assertEqual(result.coverage_summary, {
            "bound_total_count": 2, "active_managed_count": 2, "fee_tracking_count": 2,
            "matched_total_count": 2, "matched_active_count": 2,
            "matched_historical_count": 0, "active_missing_count": 0,
        })
        rows = {row["name"]: row for row in result.rows}
        self.assertEqual(rows["Alpha"]["spent"], Decimal("1.234567890123456789"))
        self.assertTrue(all(row["complete"] for row in result.rows))
        self.assertEqual(self.store.current_snapshot_id(), "S1")
        event = self.events_of_kind("snapshot")[0]
        self.assertEqual((event["start_date"], event["end_date"]), (self.start, self.end))
        self.assertEqual(event["totals"]["tracking-Alpha"], "1.234567890123456789")

    def test_unbound_account_cannot_invent_cost_ownership(self):
        self.assert_rejected_unchanged("ACCOUNT_NOT_BOUND", {"Alpha": "1"})
        self.assertEqual(self.store.current_snapshot_id(), "")

    def test_account_without_any_key_binding_rejects_snapshot(self):
        self.store.bind_keys([], "synthetic-account")
        error = self.assert_rejected_unchanged("NO_MANAGED_BINDINGS", {"Alpha": "1"})
        self.assertEqual(error.coverage_summary["bound_total_count"], 0)
        self.assertEqual(error.coverage_summary["matched_total_count"], 0)
        self.assertEqual(self.store.current_snapshot_id(), "")

    def test_disjoint_fees_preserve_previous_amounts_sources_and_bindings(self):
        self.bind_people()
        self.apply({"Alpha": "1", "Beta": "2"})
        error = self.assert_rejected_unchanged("NO_BOUND_COST_MATCH", {"Unrelated": "9"}, "S2")
        self.assertEqual(error.coverage_summary, {
            "bound_total_count": 2, "active_managed_count": 2, "fee_tracking_count": 1,
            "matched_total_count": 0, "matched_active_count": 0,
            "matched_historical_count": 0, "active_missing_count": 2,
        })
        self.assertEqual(self.store.current_snapshot_id(), "S1")
        self.assertEqual([row["spent"] for row in self.store.read_evaluation()], [Decimal(1), Decimal(2)])

    def test_partial_coverage_updates_match_and_preserves_old_amount_as_lower_bound(self):
        self.bind_people()
        self.apply({"Alpha": "1", "Beta": "2"})
        result = self.apply({"Alpha": "3"}, "S2")
        rows = {row["name"]: row for row in result.rows}
        self.assertTrue(result.fees_current)
        self.assertEqual(result.coverage_summary["matched_active_count"], 1)
        self.assertEqual(result.coverage_summary["active_missing_count"], 1)
        self.assertEqual(rows["Alpha"]["spent"], Decimal(3))
        self.assertTrue(rows["Alpha"]["complete"])
        self.assertEqual(rows["Beta"]["spent"], Decimal(2))
        self.assertFalse(rows["Beta"]["complete"])
        self.assertTrue(rows["Beta"]["spent_is_lower_bound"])
        self.assertEqual(rows["Beta"]["status"], "unknown")
        costs = {key: (amount, source) for key, amount, source in self.database_state()["costs"]}
        self.assertEqual(costs["tracking-Beta"], ("2", "S1"))
        event = next(event for event in self.events_of_kind("snapshot") if event["source_id"] == "S2")
        self.assertEqual(event["coverage"]["missing_tracking_ids"], ["tracking-Beta"])

    def test_partial_first_snapshot_leaves_missing_person_unknown_not_zero(self):
        self.bind_people()
        result = self.apply({"Alpha": "1"})
        missing = next(row for row in result.rows if row["name"] == "Beta")
        self.assertIsNone(missing["spent"])
        self.assertFalse(missing["complete"])
        self.assertFalse(missing["spent_is_lower_bound"])
        self.assertEqual(missing["status"], "unknown")
        self.assertEqual(len(self.database_state()["costs"]), 1)

    def test_retired_key_match_does_not_complete_unobserved_current_key(self):
        self.bind_people()
        self.apply({"Alpha": "1", "Beta": "2"})
        self.store.record_action("Alpha", "tracking-Alpha", "absent_verified", "synthetic verified absence")
        result = self.apply({"Alpha": "3"}, "S2")
        self.assertEqual(result.coverage_summary, {
            "bound_total_count": 2, "active_managed_count": 1, "fee_tracking_count": 1,
            "matched_total_count": 1, "matched_active_count": 0,
            "matched_historical_count": 1, "active_missing_count": 1,
        })
        rows = {row["name"]: row for row in result.rows}
        self.assertEqual(rows["Alpha"]["status"], "retired")
        self.assertEqual(rows["Alpha"]["spent"], Decimal(3))
        self.assertTrue(rows["Alpha"]["complete"])
        self.assertEqual(rows["Beta"]["tracking_id"], "tracking-Beta")
        self.assertEqual(rows["Beta"]["spent"], Decimal(2))
        self.assertFalse(rows["Beta"]["complete"])
        self.assertTrue(rows["Beta"]["spent_is_lower_bound"])
        self.assertEqual(rows["Beta"]["status"], "unknown")

    def test_one_increase_and_one_decrease_reject_entire_new_snapshot(self):
        self.bind_people()
        self.apply({"Alpha": "5", "Beta": "5"})
        self.assert_rejected_unchanged("CUMULATIVE_DECREASE", {"Alpha": "12", "Beta": "4"}, "S2")
        self.assertEqual(self.store.current_snapshot_id(), "S1")
        self.assertEqual([row["spent"] for row in self.store.read_evaluation()], [Decimal(5), Decimal(5)])
        self.assertEqual(self.events_of_kind("quota_triggered"), [])

    def test_invalid_periods_are_rejected_without_rewriting_accepted_history(self):
        self.bind_people()
        self.apply({"Alpha": "1", "Beta": "2"})
        for start, end in (("invalid-date", self.end), ("2024-01-30", self.end),
                           (self.start, "2024-01-28"), (self.start, "9999-12-31")):
            with self.subTest(start=start, end=end):
                self.assert_rejected_unchanged("PERIOD_CONFIG_MISMATCH", {"Alpha": "3", "Beta": "4"},
                                               start=start, end=end)

    def test_earlier_end_date_has_distinct_rejection_and_preserves_latest(self):
        self.bind_people()
        self.apply({"Alpha": "1", "Beta": "2"})
        self.assert_rejected_unchanged("END_DATE_REGRESSION", {"Alpha": "3", "Beta": "4"},
                                       end="2024-02-01")
        self.assertEqual(self.store.current_snapshot_id(), "S1")

    def test_same_source_with_different_contents_is_not_accepted_as_duplicate(self):
        self.bind_people()
        self.apply({"Alpha": "1", "Beta": "2"})
        self.assert_rejected_unchanged("SNAPSHOT_CONFLICT", {"Alpha": "3", "Beta": "2"}, "S1")
        self.assertEqual(len(self.events_of_kind("snapshot")), 1)

    def test_old_source_replay_retains_latest_costs_but_cannot_refresh_fee_evidence(self):
        self.bind_people()
        self.apply({"Alpha": "1", "Beta": "2"}, "S1")
        self.apply({"Alpha": "12", "Beta": "3"}, "S2")
        before = self.database_state()
        result = self.apply({"Alpha": "1", "Beta": "2"}, "S1", operation_id="replayed-old")
        self.assertEqual(result.apply_outcome, "stale_replay")
        self.assertEqual(result.reason_code, "STALE_SNAPSHOT_REPLAY")
        self.assertEqual(result.observed_source_id, "S1")
        self.assertEqual(result.effective_snapshot_id, "S2")
        self.assertFalse(result.fees_current)
        self.assertEqual(result.persistence_state, "persisted")
        self.assertEqual(result.export_state, "complete")
        self.assertEqual([row["spent"] for row in result.rows], [Decimal(12), Decimal(3)])
        self.assertEqual(self.store.current_snapshot_id(), "S2")
        self.assertEqual(self.database_state(), before)
        self.assertEqual(len(self.events_of_kind("quota_triggered")), 1)

    def test_old_source_replay_cannot_create_new_trigger_after_external_quota_edit(self):
        self.bind_people()
        self.apply({"Alpha": "1", "Beta": "2"}, "S1")
        self.apply({"Alpha": "5", "Beta": "3"}, "S2")
        # Model an externally edited spreadsheet.  save_people() would itself
        # reconcile triggers and would hide the replay-specific regression.
        self.store._write_workbook([Person("Alpha", Decimal(4)), Person("Beta", Decimal(10))])
        before = self.database_state()
        with patch.object(self.store, "_record_triggers") as reconcile:
            result = self.apply({"Alpha": "1", "Beta": "2"}, "S1")
        reconcile.assert_not_called()
        self.assertEqual(result.rows[0]["status"], "triggered")
        self.assertFalse(result.fees_current)
        self.assertEqual(self.database_state(), before)
        self.assertEqual(self.events_of_kind("quota_triggered"), [])

    def test_latest_source_duplicate_is_current_without_recounting_or_new_snapshot(self):
        self.bind_people()
        self.apply({"Alpha": "12", "Beta": "2"}, "S1")
        before = self.database_state()
        result = self.apply({"Alpha": "12", "Beta": "2"}, "S1", operation_id="new-observation")
        self.assertEqual(result.operation_id, "new-observation")
        self.assertEqual(result.apply_outcome, "current_duplicate")
        self.assertEqual(result.reason_code, "OK")
        self.assertEqual(result.observed_source_id, result.effective_snapshot_id)
        self.assertTrue(result.fees_current)
        self.assertEqual(result.rows[0]["spent"], Decimal(12))
        # INSERT OR IGNORE deduplicates trigger rows but SQLite may advance its
        # AUTOINCREMENT allocator. Compare all business tables, not gapless IDs.
        after = self.database_state()
        self.assertEqual({k: v for k, v in after.items() if k != "sqlite_sequence"},
                         {k: v for k, v in before.items() if k != "sqlite_sequence"})
        self.assertEqual(len(self.events_of_kind("snapshot")), 1)
        self.assertEqual(len(self.events_of_kind("quota_triggered")), 1)

    def test_export_failure_reports_committed_application_and_recovers_without_reapplication(self):
        self.bind_people()
        self.apply({"Alpha": "1", "Beta": "2"}, "S1")
        with patch.object(self.store, "export_logs", side_effect=OSError("synthetic-private-path")):
            with self.assertRaises(SnapshotApplyError) as caught:
                self.apply({"Alpha": "12", "Beta": "3"}, "S2", operation_id="export-failed")
        error = caught.exception
        self.assertEqual(error.reason_code, "AUDIT_EXPORT_FAILED")
        self.assertEqual(error.stage, "audit_export")
        self.assertEqual(error.persistence_state, "persisted")
        self.assertEqual(error.operation_id, "export-failed")
        self.assertNotIn("synthetic-private-path", str(error))
        self.assertIsInstance(error.application, SnapshotApplyResult)
        self.assertEqual(error.application.apply_outcome, "applied")
        self.assertEqual(error.application.effective_snapshot_id, "S2")
        self.assertEqual(error.application.persistence_state, "persisted")
        self.assertEqual(error.application.export_state, "pending")
        self.assertEqual(error.coverage_summary, error.application.coverage_summary)
        self.assertEqual(self.store.current_snapshot_id(), "S2")
        self.assertEqual(len(self.events_of_kind("snapshot")), 2)
        self.assertEqual(len(self.events_of_kind("quota_triggered")), 1)
        committed = self.database_state()
        # Recovery is deliberately export + read, with all paths that could
        # publish a new snapshot or reconcile business events forbidden.
        with patch.object(self.store, "apply_snapshot_result", side_effect=AssertionError("must not reapply")), \
                patch.object(self.store, "_record_triggers", side_effect=AssertionError("must not reconcile")), \
                patch.object(self.store, "_event", side_effect=AssertionError("must not append events")):
            self.store.export_logs()
            self.store.export_logs()
            rows = self.store.read_evaluation()
        self.assertEqual([row["spent"] for row in rows], [Decimal(12), Decimal(3)])
        self.assertEqual(self.database_state(), committed)
        exported = [json.loads(line) for path in self.folder.glob("运行日志-*.jsonl")
                    for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(sorted(exported, key=lambda event: event["id"]),
                         sorted(self.store.events(), key=lambda event: event["id"]))
        self.assertEqual(len([event for event in exported if event["kind"] == "snapshot"]), 2)
        self.assertEqual(len([event for event in exported if event["kind"] == "quota_triggered"]), 1)

    def test_transaction_fault_rolls_back_costs_snapshot_and_written_trigger_atomically(self):
        self.bind_people()
        self.apply({"Alpha": "1", "Beta": "2"}, "S1")
        before = self.database_state()
        original_record_triggers = self.store._record_triggers

        def fail_after_trigger_insert(db, config, results):
            original_record_triggers(db, config, results)
            # Both the snapshot and its quota event have reached SQLite before
            # this injected fault, so rollback must undo more than costs alone.
            self.assertEqual(db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM events WHERE kind='quota_triggered'").fetchone()[0], 1)
            raise RuntimeError("synthetic-sensitive-fault-detail")

        with patch.object(self.store, "_record_triggers", side_effect=fail_after_trigger_insert), \
                patch.object(self.store, "export_logs") as exporter:
            with self.assertRaises(SnapshotApplyError) as caught:
                self.apply({"Alpha": "12", "Beta": "3"}, "S2", operation_id="transaction-failed")
        error = caught.exception
        self.assertEqual(error.reason_code, "SNAPSHOT_PERSISTENCE_UNKNOWN")
        self.assertEqual(error.stage, "snapshot_transaction")
        self.assertEqual(error.persistence_state, "unknown")
        self.assertEqual(error.operation_id, "transaction-failed")
        self.assertIsNone(error.application)
        self.assertNotIn("synthetic-sensitive-fault-detail", str(error))
        exporter.assert_not_called()
        self.assertEqual(self.database_state(), before)
        self.assertEqual(self.store.current_snapshot_id(), "S1")
        self.assertEqual(self.events_of_kind("quota_triggered"), [])
        # The synthetic transaction really rolled back; an explicit later
        # application can persist the source exactly once after reconciliation.
        self.apply({"Alpha": "12", "Beta": "3"}, "S2")
        self.assertEqual(len(self.events_of_kind("snapshot")), 2)
        self.assertEqual(len(self.events_of_kind("quota_triggered")), 1)

    def test_local_context_failure_is_controlled_and_leaves_previous_snapshot_intact(self):
        self.bind_people()
        self.apply({"Alpha": "1", "Beta": "2"})
        before = self.database_state()
        with patch.object(self.store, "read_people", side_effect=OSError("synthetic-private-path")), \
                patch.object(self.store, "export_logs") as exporter:
            with self.assertRaises(SnapshotApplyError) as caught:
                self.apply({"Alpha": "3", "Beta": "4"}, "S2", operation_id="context-failed")
        error = caught.exception
        self.assertEqual(error.reason_code, "LOCAL_CONTEXT_UNAVAILABLE")
        self.assertEqual(error.stage, "local_context")
        self.assertEqual(error.persistence_state, "not_written")
        self.assertEqual(error.operation_id, "context-failed")
        self.assertIsNone(error.application)
        self.assertNotIn("synthetic-private-path", str(error))
        exporter.assert_not_called()
        self.assertEqual(self.database_state(), before)

    def test_read_helpers_do_not_create_triggers_or_exports_after_external_rule_edit(self):
        self.bind_people()
        self.apply({"Alpha": "1", "Beta": "2"})
        self.store._write_workbook([Person("Alpha", Decimal(0)), Person("Beta", Decimal(10))])
        before = self.database_state()
        exported_before = {path.name: path.read_bytes() for path in self.folder.glob("运行*.*")}
        with patch.object(self.store, "_event", side_effect=AssertionError("read must not append")), \
                patch.object(self.store, "_record_triggers", side_effect=AssertionError("read must not reconcile")), \
                patch.object(self.store, "export_logs", side_effect=AssertionError("read must not export")):
            self.assertEqual(self.store.current_snapshot_id(), "S1")
            rows = self.store.read_evaluation()
            self.assertEqual(self.store.current_snapshot_id(), "S1")
        self.assertEqual(rows[0]["status"], "triggered")
        self.assertEqual(rows[0]["amount"], Decimal(0))
        self.assertEqual(self.database_state(), before)
        self.assertEqual({path.name: path.read_bytes() for path in self.folder.glob("运行*.*")}, exported_before)
        self.assertEqual(self.events_of_kind("quota_triggered"), [])

    def test_read_helpers_before_first_snapshot_return_unknown_without_writes(self):
        self.bind_people()
        before = self.database_state()
        with patch.object(self.store, "_event", side_effect=AssertionError("read must not append")), \
                patch.object(self.store, "export_logs", side_effect=AssertionError("read must not export")):
            self.assertEqual(self.store.current_snapshot_id(), "")
            rows = self.store.read_evaluation()
        self.assertTrue(all(row["spent"] is None and not row["complete"] for row in rows))
        self.assertTrue(all(row["status"] == "unknown" for row in rows))
        self.assertEqual(self.database_state(), before)


if __name__ == "__main__":
    unittest.main()
