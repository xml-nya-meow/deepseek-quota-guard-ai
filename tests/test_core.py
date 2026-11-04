from contextlib import closing
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import load_workbook
from quota_guard.core import AccountMismatch, Config, DataStore, GuardError, Person, SnapshotApplyError, TZ, money


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.store = DataStore(self.folder)
        self.start = datetime.now(TZ).date().isoformat()
        self.store.initialize(14, "1500", self.start)

    def tearDown(self):
        self.temp.cleanup()

    def people(self, *pairs):
        self.store.save_people([Person(name, Decimal(str(amount))) for name, amount in pairs])

    def bind(self, *names):
        self.store.bind_keys([{"name": n, "tracking_id": f"id-{n}"} for n in names], "account-test")

    def snapshot(self, values, sid="sample"):
        return self.store.apply_snapshot({f"id-{name}": Decimal(str(amount)) for name, amount in values.items()},
                                         sid, self.start, self.start)

    def test_init_template_and_fixed_share(self):
        self.assertEqual(self.store.load().share, Decimal(107))
        self.assertEqual(len(self.store.read_people()), 14)
        self.assertTrue(all(p == Person("", Decimal(-1)) for p in self.store.read_people()))
        self.people(("甲", 80), ("乙", 200))
        self.assertEqual(self.store.load().share, Decimal(107))

    def test_boundaries_and_skipping(self):
        self.people(("甲", 80), ("乙", 200), ("丙", -1), ("丁", 0))
        self.bind("甲", "乙", "丁")
        before = {r["name"]: r for r in self.store.evaluate()}
        self.assertEqual(before["甲"]["status"], "unknown")
        self.assertEqual(before["丙"]["status"], "skipped")
        self.assertEqual(before["丁"]["status"], "triggered")
        rows = {r["name"]: r for r in self.snapshot({"甲":80, "乙":107, "丁":0})}
        self.assertEqual(rows["甲"]["reasons"], ["达到个人额度"])
        self.assertEqual(rows["乙"]["reasons"], ["达到公共人均额度"])
        self.assertEqual(rows["丁"]["status"], "triggered")

    def test_jump_over_and_precision(self):
        self.people(("甲", 80))
        self.bind("甲")
        self.assertEqual(self.snapshot({"甲":"79.999999"})[0]["status"], "ok")
        self.assertEqual(self.snapshot({"甲":"80.000001"}, "new")[0]["status"], "triggered")

    def test_snapshot_is_replacement_and_idempotent(self):
        self.people(("甲", 80))
        self.bind("甲")
        self.snapshot({"甲":90})
        events_before = len(self.store.events())
        self.snapshot({"甲":90})
        self.assertEqual(len(self.store.events()), events_before)
        self.snapshot({"甲":95}, "sample-new")
        self.assertEqual(self.store.evaluate()[0]["spent"], Decimal(95))
        triggers = [e for e in self.store.events() if e["kind"] == "quota_triggered"]
        self.assertEqual(len(triggers), 1)
        self.assertEqual(DataStore(self.folder).evaluate()[0]["spent"], Decimal(95))

    def test_missing_decreasing_and_conflicting_data_rejected(self):
        self.people(("甲", 80))
        self.bind("甲")
        self.snapshot({"甲":50})
        for totals, source in [({}, "missing"), ({"甲":49}, "lower"), ({"甲":51}, "sample")]:
            with self.assertRaises(GuardError):
                self.snapshot(totals, source)
        self.assertEqual(self.store.evaluate()[0]["spent"], Decimal(50))

    def test_snapshot_audit_keeps_exact_received_amounts_and_missing_keys(self):
        self.people(("甲", 80), ("乙", 80))
        self.bind("甲", "乙")
        self.store.apply_snapshot({"id-甲": Decimal("0.009600000000000001"),
                                   "unbound-key": Decimal("999")}, "first", self.start, self.start)
        self.snapshot({"甲": "0.009700000000000001", "乙": 0}, "second")
        events = {event["source_id"]: event for event in self.store.events() if event["kind"] == "snapshot"}
        first, second = events["first"], events["second"]
        self.assertEqual(first["totals"], {"id-甲": "0.009600000000000001"})
        self.assertEqual(second["totals"], {"id-甲": "0.009700000000000001", "id-乙": "0"})
        self.assertEqual(first["timezone"], "UTC+08:00")
        self.assertEqual(first["currency"], "CNY")
        self.assertEqual(first["schema_version"], 1)
        self.assertEqual(first["coverage"], {"scope": "bound_keys", "complete": False,
            "missing_tracking_ids": ["id-乙"], "missing_cost": "unknown"})
        self.assertTrue(second["coverage"]["complete"])
        restored = DataStore(self.folder)
        self.assertEqual(next(e for e in restored.events() if e.get("source_id") == "first")["totals"],
                         first["totals"])
        before = [e for e in restored.events() if e["kind"] == "snapshot"]
        with self.assertRaises(GuardError):
            self.snapshot({"甲": "0.0096"}, "decreased")
        self.assertEqual([e for e in restored.events() if e["kind"] == "snapshot"], before)

    def test_legacy_snapshot_replay_does_not_invent_historical_evidence(self):
        self.people(("甲", 80))
        self.bind("甲")
        self.snapshot({"甲": 50})
        with self.store._connect() as db:
            number, payload = db.execute("SELECT id,payload FROM events WHERE kind='snapshot'").fetchone()
            legacy = json.loads(payload)
            for field in ("totals", "timezone", "currency", "schema_version", "coverage"):
                legacy.pop(field)
            db.execute("UPDATE events SET payload=? WHERE id=?", (json.dumps(legacy), number))
        self.snapshot({"甲": 50})
        events = [event for event in self.store.events() if event["kind"] == "snapshot"]
        self.assertEqual(len(events), 1)
        self.assertNotIn("totals", events[0])

    def test_wrong_period_rejected(self):
        self.people(("甲", 80))
        self.bind("甲")
        with self.assertRaises(GuardError):
            self.store.apply_snapshot({"id-甲":Decimal(20)}, "bad", "2000-01-01", self.start)

    def test_key_rename_or_replacement_cannot_reset_history(self):
        self.people(("甲", 80))
        self.bind("甲")
        self.snapshot({"甲":70})
        with self.assertRaises(GuardError):
            self.store.bind_keys([{"name":"甲", "tracking_id":"new-id"}], "account-test")
        self.people(("乙", 80))
        with self.assertRaises(GuardError):
            self.store.bind_keys([{"name":"乙", "tracking_id":"id-甲"}], "account-test")
        self.people(("甲", 80))
        self.assertEqual(self.store.evaluate()[0]["spent"], Decimal(70))

    def test_duplicate_platform_names_and_different_account_rejected(self):
        self.people(("甲", 80))
        with self.assertRaises(GuardError):
            self.store.bind_keys([{"name":"甲", "tracking_id":"one"}, {"name":"甲", "tracking_id":"two"}], "account-test")
        self.bind("甲")
        with self.assertRaises(GuardError):
            self.store.bind_keys([{"name":"甲", "tracking_id":"id-甲"}], "wrong-account")

    def test_missing_file_does_not_reinitialize(self):
        self.store.workbook_path.unlink()
        with self.assertRaises(GuardError): self.store.load()
        with self.assertRaises(GuardError): self.store.initialize(14, "1500", self.start)
        self.assertTrue(self.store.db_path.exists())

    def test_settings_tampering_is_rejected(self):
        content = self.store.config_path.read_text(encoding="utf-8")
        self.store.config_path.write_text(content.replace('"1500"', '"2000"'), encoding="utf-8")
        with self.assertRaises(GuardError): self.store.load()

    def test_partial_initialization_is_not_fresh(self):
        (self.folder / self.store.MARKER).write_text("interrupted", encoding="utf-8")
        with self.assertRaises(GuardError): self.store.load()
        with self.assertRaises(GuardError): self.store.initialize(14, "1500", self.start)

    def test_invalid_values_and_duplicates(self):
        for value in ("NaN", "Infinity", "-2", "-0.5", "=1+1", True, None):
            with self.subTest(value=value), self.assertRaises(GuardError):
                money(value, allow_skip=True)
        with self.assertRaises(GuardError): self.people(("甲", 1), ("甲", 2))
        with self.assertRaises(GuardError): self.people(("", 0))

    def test_excel_formulas_rejected(self):
        wb = load_workbook(self.store.workbook_path)
        wb.active["A2"] = "甲"
        wb.active["B2"] = "=80"
        wb.save(self.store.workbook_path)
        wb.close()
        with self.assertRaises(GuardError): self.store.read_people()

    def test_concurrent_edit_protection(self):
        fingerprint = self.store.workbook_hash()
        self.people(("甲", 80))
        with self.assertRaises(GuardError):
            self.store.save_people([Person("乙", Decimal(10))], expected_hash=fingerprint)
        self.assertEqual(self.store.read_people()[0].name, "甲")

    def test_action_results_logged_and_secrets_redacted(self):
        self.people(("甲", 0))
        self.bind("甲")
        self.store.record_action("甲", "id-甲", "pending", "准备执行")
        self.store.record_action("甲", "id-甲", "unknown", "Bearer sensitive123 sk-testlongsecret cookie=sessionsecret")
        logs = "\n".join(p.read_text(encoding="utf-8") for p in self.folder.glob("运行*.*"))
        self.assertIn("unknown", logs)
        for secret in ("sensitive123", "sk-testlongsecret", "sessionsecret"):
            self.assertNotIn(secret, logs)
        with self.assertRaises(GuardError):
            self.store.record_action("乙", "id-甲", "confirmed", "wrong target")

    def test_zero_shared_budget_triggers_active_people(self):
        other = DataStore(self.folder / "other")
        other.initialize(14, 0, self.start)
        other.save_people([Person("甲", Decimal(50)), Person("乙", Decimal(-1))])
        self.assertEqual([r["status"] for r in other.evaluate()], ["triggered", "skipped"])

    def test_partial_snapshot_updates_present_keys_without_inventing_zero(self):
        self.people(("甲", 80), ("乙", 80))
        self.bind("甲", "乙")
        rows = {r["name"]: r for r in self.snapshot({"甲": 81})}
        self.assertEqual(rows["甲"]["status"], "triggered")
        self.assertTrue(rows["甲"]["complete"])
        self.assertIsNone(rows["乙"]["spent"])
        self.assertEqual(rows["乙"]["status"], "unknown")
        self.assertFalse(rows["乙"]["complete"])

    def test_missing_latest_cost_keeps_history_as_lower_bound(self):
        self.people(("甲", 80), ("乙", 80))
        self.bind("甲", "乙")
        self.snapshot({"甲": 20, "乙": 30})
        rows = {r["name"]: r for r in self.snapshot({"甲": 25}, "partial")}
        self.assertEqual(rows["乙"]["spent"], Decimal(30))
        self.assertEqual(rows["乙"]["status"], "unknown")
        self.assertTrue(rows["乙"]["spent_is_lower_bound"])
        self.assertEqual(rows["甲"]["status"], "ok")
        self.assertFalse(rows["甲"]["spent_is_lower_bound"])
        # Replaying older data must not make an omitted key look current.
        self.snapshot({"甲": 20, "乙": 30})
        replayed = {r["name"]: r for r in self.store.evaluate()}
        self.assertEqual(replayed["乙"]["status"], "unknown")
        rows = {r["name"]: r for r in self.snapshot({"甲": 25, "乙": 35}, "complete")}
        self.assertEqual(rows["乙"]["status"], "ok")
        self.assertTrue(rows["乙"]["complete"])

    def test_known_lower_bound_still_triggers(self):
        self.people(("甲", 80), ("乙", 80))
        self.bind("甲", "乙")
        self.snapshot({"甲": 20, "乙": 80})
        rows = {r["name"]: r for r in self.snapshot({"甲": 25}, "partial")}
        self.assertEqual(rows["乙"]["status"], "triggered")
        self.assertEqual(rows["乙"]["spent"], Decimal(80))
        self.assertTrue(rows["乙"]["spent_is_lower_bound"])

    def test_verified_absence_retires_without_losing_costs(self):
        self.people(("甲", 80), ("乙", 80))
        self.bind("甲", "乙")
        self.snapshot({"甲": 80, "乙": 20})
        self.store.record_action("甲", "id-甲", "absent_verified", "平台列表已不存在")
        self.assertEqual(self.store.active_tracking_ids(), {"id-乙"})
        rows = {r["name"]: r for r in self.store.evaluate()}
        self.assertEqual(rows["甲"]["status"], "retired")
        self.assertEqual(rows["甲"]["tracking_id"], "")
        self.assertEqual(rows["甲"]["spent"], Decimal(80))
        self.bind("乙")
        rows = {r["name"]: r for r in self.snapshot({"乙": 30}, "after-retirement")}
        self.assertEqual(rows["甲"]["status"], "retired")
        self.assertEqual(rows["甲"]["spent"], Decimal(80))
        self.assertEqual(rows["乙"]["status"], "ok")
        with self.assertRaises(GuardError):
            self.store.bind_keys([{"name": "甲", "tracking_id": "replacement"},
                                  {"name": "乙", "tracking_id": "id-乙"}], "account-test")
        with self.assertRaises(GuardError):
            self.bind("甲", "乙")

    def test_action_contract_and_uncertain_results_do_not_retire(self):
        self.people(("甲", 0))
        self.bind("甲")
        for state in ("pending", "prepared", "unknown_result", "requested", "confirmed", "failed", "unknown", "cancelled"):
            self.store.record_action("甲", "id-甲", state, state)
            self.assertEqual(self.store.active_tracking_ids(), {"id-甲"})
        with self.assertRaises(GuardError):
            self.store.record_action("乙", "id-甲", "absent_verified", "wrong target")
        self.assertEqual(self.store.active_tracking_ids(), {"id-甲"})
        self.store.record_action("甲", "id-甲", "absent_verified", "平台列表已不存在")
        self.assertEqual(self.store.evaluate()[0]["status"], "retired")
        self.assertEqual(self.store.active_tracking_ids(), set())
        self.people(("甲", -1))
        self.assertEqual(self.store.evaluate()[0]["status"], "skipped")

    def test_first_binding_uses_one_persistent_account_source(self):
        self.people(("甲", 80))
        original_settings = self.store.config_path.read_bytes()
        # Binding must not depend on a second filesystem commit after SQLite.
        with patch.object(self.store, "_write_json", side_effect=OSError("simulated write failure")) as writer:
            self.bind("甲")
        writer.assert_not_called()
        self.assertEqual(self.store.config_path.read_bytes(), original_settings)
        restored = DataStore(self.folder)
        self.assertEqual(restored.load().account_id, "account-test")
        self.assertEqual(restored.active_tracking_ids(), {"id-甲"})
        with self.assertRaises(GuardError):
            restored.bind_keys([{"name": "甲", "tracking_id": "id-甲"}], "other-account")

    def test_legacy_account_copy_must_match_authoritative_database(self):
        self.people(("甲", 80))
        self.bind("甲")
        config = json.loads(self.store.config_path.read_text(encoding="utf-8"))
        config["account_id"] = "account-test"
        self.store.config_path.write_text(json.dumps(config), encoding="utf-8")
        self.assertEqual(self.store.load().account_id, "account-test")
        config["account_id"] = "different-account"
        self.store.config_path.write_text(json.dumps(config), encoding="utf-8")
        with self.assertRaises(GuardError):
            self.store.load()
        config["account_id"] = "account-test"
        self.store.config_path.write_text(json.dumps(config), encoding="utf-8")
        with closing(sqlite3.connect(self.store.db_path)) as db, db:
            db.execute("UPDATE meta SET value='' WHERE key='account_id'")
        with self.assertRaises(GuardError):
            self.store.load()

    def test_zero_budget_records_trigger_without_any_snapshot(self):
        self.people(("甲", 0), ("乙", -1))
        self.bind("甲")
        events = [e for e in self.store.events() if e["kind"] == "quota_triggered"]
        bound_events = [e for e in events if e["tracking_id"] == "id-甲"]
        self.assertEqual(len(bound_events), 1)
        self.assertEqual(bound_events[0]["reasons"], ["个人额度为零"])
        self.assertFalse(any(e["kind"] == "snapshot" for e in self.store.events()))
        self.assertFalse(any(e["name"] == "乙" for e in events))
        self.store.evaluate()
        DataStore(self.folder).evaluate()
        self.assertEqual(len([e for e in self.store.events() if e["kind"] == "quota_triggered"]), len(events))

    def test_budget_change_logs_trigger_with_unchanged_cost_report(self):
        self.people(("甲", 100))
        self.bind("甲")
        self.snapshot({"甲": 60})
        self.people(("甲", 50))
        events = [e for e in self.store.events() if e["kind"] == "quota_triggered"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["amount"], "50")
        self.assertEqual(events[0]["spent"], "60")
        self.snapshot({"甲": 60})
        self.assertEqual(len([e for e in self.store.events() if e["kind"] == "quota_triggered"]), 1)
        # An external Excel edit does not call save_people; a replay must still
        # evaluate and log the new rule without rewriting the existing costs.
        wb = load_workbook(self.store.workbook_path)
        wb.active["B2"] = "40"
        wb.save(self.store.workbook_path)
        wb.close()
        self.snapshot({"甲": 60})
        events = [e for e in self.store.events() if e["kind"] == "quota_triggered"]
        self.assertEqual({e["amount"] for e in events}, {"40", "50"})

    def test_snapshot_and_trigger_commit_or_rollback_together(self):
        self.people(("甲", 80))
        self.bind("甲")
        with patch.object(self.store, "_record_triggers", side_effect=RuntimeError("simulated interruption")):
            with self.assertRaises(SnapshotApplyError) as caught:
                self.snapshot({"甲": 90})
        self.assertEqual(caught.exception.reason_code, "SNAPSHOT_PERSISTENCE_UNKNOWN")
        restored = DataStore(self.folder)
        self.assertIsNone(restored.evaluate()[0]["spent"])
        self.assertFalse(any(e["kind"] == "snapshot" for e in restored.events()))
        self.snapshot({"甲": 90})
        self.assertEqual(len([e for e in self.store.events() if e["kind"] == "quota_triggered"]), 1)

    def test_replay_repairs_legacy_missing_trigger_and_export_failure(self):
        self.people(("甲", 80))
        self.bind("甲")
        self.snapshot({"甲": 90})
        # Model an old-version interruption after the snapshot commit but before
        # its separate trigger commit. The unchanged source must repair it.
        with closing(sqlite3.connect(self.store.db_path)) as db, db:
            db.execute("DELETE FROM events WHERE kind='quota_triggered'")
        with patch.object(self.store, "export_logs", side_effect=GuardError("simulated export failure")):
            with self.assertRaises(GuardError):
                self.snapshot({"甲": 90})
        self.snapshot({"甲": 90})
        self.assertEqual(len([e for e in self.store.events() if e["kind"] == "quota_triggered"]), 1)
        exported = [json.loads(line) for path in self.folder.glob("运行日志-*.jsonl")
                    for line in path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len([e for e in exported if e["kind"] == "quota_triggered"]), 1)

    def test_pending_bindings_and_latest_action_survive_restart(self):
        self.people(("甲", 80), ("乙", 80))
        self.bind("甲", "乙")
        self.assertIsNone(self.store.latest_action("id-甲"))
        self.store.record_action("甲", "id-甲", "requested", "等待结果")
        self.store.record_action("乙", "id-乙", "pending", "另一个目标")
        self.people(("甲", -1), ("乙", 80))
        restored = DataStore(self.folder)
        self.assertEqual(restored.latest_action("id-甲"), "requested")
        self.assertEqual(restored.latest_action("id-乙"), "pending")
        self.assertEqual({r["tracking_id"] for r in restored.active_bindings()}, {"id-甲", "id-乙"})
        restored.record_action("甲", "id-甲", "unknown_result", "尚未核实")
        self.assertEqual(restored.latest_action("id-甲"), "unknown_result")
        restored.record_action("甲", "id-甲", "absent_verified", "有存活身份核实")
        self.assertEqual(restored.latest_action("id-甲"), "absent_verified")
        self.assertEqual(restored.active_bindings(), [{"name": "乙", "tracking_id": "id-乙"}])
        self.assertIsNone(restored.latest_action("never-bound"))

    def test_action_evidence_preserves_current_rule_and_exact_snapshot_basis(self):
        self.people(("甲", "0.01"), ("乙", 80))
        self.bind("甲", "乙")
        self.snapshot({"甲": "0.010000000000000001", "乙": 20})
        config = self.store.load()
        evidence = self.store.action_evidence("甲", "id-甲")
        self.assertEqual(evidence, {"period_id": config.period_id, "account_id": "account-test",
            "workbook_hash": self.store.workbook_hash(), "amount": "0.01", "share": "107",
            "spent": "0.010000000000000001", "reasons": ["达到个人额度"],
            "snapshot_id": "sample", "complete": True, "status": "triggered"})
        self.snapshot({"乙": 21}, "partial")
        partial = self.store.action_evidence("甲", "id-甲")
        self.assertFalse(partial["complete"])
        self.assertEqual(partial["snapshot_id"], "partial")
        self.assertEqual(partial["spent"], evidence["spent"])
        self.assertEqual(partial["status"], "triggered")

    def test_zero_action_evidence_needs_no_cost_snapshot_and_detects_changed_workbook(self):
        self.people(("甲", 0))
        self.bind("甲")
        evidence = self.store.action_evidence("甲", "id-甲")
        self.assertIsNone(evidence["snapshot_id"])
        self.assertIsNone(evidence["spent"])
        self.assertFalse(evidence["complete"])
        self.assertEqual(evidence["reasons"], ["个人额度为零"])
        with self.assertRaises(GuardError):
            self.store.action_evidence("乙", "id-甲")
        original_hash = self.store.workbook_hash()
        with patch.object(self.store, "workbook_hash", side_effect=[original_hash, "0" * 64]):
            with self.assertRaises(GuardError):
                self.store.action_evidence("甲", "id-甲")

    def test_operation_id_is_bound_to_one_target_and_full_record_survives_restart(self):
        self.people(("甲", 0), ("乙", 0))
        self.bind("甲", "乙")
        evidence = self.store.action_evidence("甲", "id-甲")
        self.store.record_action("甲", "id-甲", "pending", "待处理", operation_id="operation-a", evidence=evidence)
        self.store.record_action("甲", "id-甲", "requested", "已批准", operation_id="operation-a", evidence=evidence)
        with self.assertRaises(GuardError):
            self.store.record_action("乙", "id-乙", "absent_verified", "错误复用", operation_id="operation-a")
        self.assertEqual(self.store.active_tracking_ids(), {"id-甲", "id-乙"})
        restored = DataStore(self.folder)
        record = restored.latest_action_record("id-甲")
        self.assertEqual(record["operation_id"], "operation-a")
        self.assertEqual(record["evidence"], evidence)
        self.assertEqual(record["name"], "甲")
        self.assertEqual(record["tracking_id"], "id-甲")
        self.assertEqual(record["status"], "requested")
        self.assertEqual(record["kind"], "action")
        self.assertIsInstance(record["id"], int)
        self.assertTrue(record["created"])
        self.assertEqual(restored.latest_action("id-甲"), record["status"])
        self.assertIsNone(restored.latest_action_record("never-bound"))
        restored.record_action("甲", "id-甲", "unknown_result", "结果待核实", operation_id="operation-a")
        self.assertEqual(restored.latest_action_record("id-甲")["evidence"], evidence)
        with self.assertRaises(GuardError):
            restored.record_action("甲", "id-甲", "pending", "不得越过旧操作", operation_id="operation-new")
        self.assertEqual(restored.latest_action_record("id-甲")["operation_id"], "operation-a")

    def test_legacy_action_record_has_no_invented_operation_or_evidence(self):
        self.people(("甲", 0))
        self.bind("甲")
        self.store.record_action("甲", "id-甲", "requested", "旧记录")
        record = self.store.latest_action_record("id-甲")
        self.assertIsNone(record["operation_id"])
        self.assertNotIn("evidence", record)
        self.assertEqual(self.store.unresolved_actions(), [record])

    def test_unresolved_actions_uses_all_history_and_ignores_current_workbook_rows(self):
        states = ("pending", "requested", "unknown_result", "unknown", "failed", "prepared", "confirmed")
        names = [f"人员{i}" for i in range(len(states))]
        self.people(*[(name, 0) for name in names])
        self.bind(*names)
        for name, state in zip(names, states):
            self.store.record_action(name, f"id-{name}", state, "等待核验", operation_id=f"operation-{state}")
        self.people((names[0], -1))
        with self.store._connect() as db:
            for index in range(210):
                self.store._event(db, "synthetic_later_event", {"number": index})
        self.store.workbook_path.write_bytes(b"unreadable workbook must not hide recovery records")
        restored = DataStore(self.folder)
        records = restored.unresolved_actions()
        self.assertEqual([record["status"] for record in records], list(states))
        self.assertEqual({record["name"] for record in records}, set(names))
        self.assertTrue(all(record["operation_id"] for record in records))
        self.store.workbook_path.unlink()
        self.assertEqual(restored.unresolved_actions(), records)
        self.assertEqual(restored.latest_action_record(f"id-{names[0]}"), records[0])
        with self.assertRaises(GuardError):
            restored.load()

    def test_present_verified_keeps_uncertainty_without_retiring_or_calling_it_cancelled(self):
        self.people(("甲", 0), ("乙", 0))
        self.bind("甲", "乙")
        self.store.record_action("甲", "id-甲", "unknown_result", "结果待核验", operation_id="a")
        self.store.record_action("乙", "id-乙", "pending", "尚未发许可", operation_id="b")
        self.store.record_action("甲", "id-甲", "present_verified", "仍在平台列表", operation_id="a")
        self.store.record_action("乙", "id-乙", "cancelled", "许可前取消", operation_id="b")
        self.assertEqual(self.store.unresolved_actions(), [self.store.latest_action_record("id-甲")])
        self.assertEqual(self.store.latest_action("id-甲"), "present_verified")
        self.assertEqual(self.store.latest_action("id-乙"), "cancelled")
        self.assertEqual(self.store.active_tracking_ids(), {"id-甲", "id-乙"})

    def test_action_evidence_rejects_unknown_or_secret_fields_before_writing(self):
        self.people(("甲", 0))
        self.bind("甲")
        evidence = self.store.action_evidence("甲", "id-甲")
        invalid = [dict(evidence, cookie="not-allowed"), dict(evidence, spent="NaN"),
                   dict(evidence, complete=1), dict(evidence, reasons=["Bearer credential"]),
                   dict(evidence, status={"unexpected": True})]
        for value in invalid:
            with self.subTest(fields=sorted(value)):
                with self.assertRaises(GuardError):
                    self.store.record_action("甲", "id-甲", "pending", "invalid", operation_id="a", evidence=value)
        self.assertIsNone(self.store.latest_action("id-甲"))

    def test_orphan_sqlite_sidecars_prevent_reinitialization(self):
        for suffix in ("-wal", "-shm"):
            with self.subTest(suffix=suffix):
                folder = self.folder / suffix
                folder.mkdir()
                artifact = folder / (DataStore.DATABASE + suffix)
                artifact.write_bytes(b"preserved history")
                other = DataStore(folder)
                self.assertTrue(other.has_history())
                with self.assertRaises(GuardError):
                    other.initialize(14, "1500", self.start)
                self.assertEqual(artifact.read_bytes(), b"preserved history")

    def test_initial_wrong_binding_can_be_reset_without_losing_audit(self):
        self.people(("甲", 0))
        self.bind("甲")
        original = self.store.load()
        workbook = self.store.workbook_path.read_bytes()
        # Older installations also asserted the account in their JSON file.
        raw = json.loads(self.store.config_path.read_text(encoding="utf-8"))
        raw["account_id"] = original.account_id
        self.store.config_path.write_text(json.dumps(raw), encoding="utf-8")
        self.assertTrue(self.store.can_correct_setup())
        reset = self.store.reset_initial_binding()
        self.assertEqual(reset.period_id, original.period_id)
        self.assertEqual(reset.account_id, "")
        self.assertEqual(self.store.active_bindings(), [])
        self.assertEqual(self.store.workbook_path.read_bytes(), workbook)
        events = self.store.events()
        self.assertTrue(any(e["kind"] == "keys_bound" for e in events))
        reset_event = next(e for e in events if e["kind"] == "initial_binding_reset")
        self.assertEqual(reset_event["previous_account_id"], "account-test")
        self.assertEqual(reset_event["previous_bindings"], [{"name": "甲", "tracking_id": "id-甲"}])
        self.store.bind_keys([{"name": "甲", "tracking_id": "correct-key"}], "correct-account")
        self.assertEqual(self.store.load().account_id, "correct-account")

    def test_reset_interruption_retains_old_authoritative_account(self):
        self.people(("甲", 80))
        self.bind("甲")
        raw = json.loads(self.store.config_path.read_text(encoding="utf-8"))
        raw["account_id"] = "account-test"
        self.store.config_path.write_text(json.dumps(raw), encoding="utf-8")
        with patch.object(self.store, "_event", side_effect=RuntimeError("simulated interruption")):
            with self.assertRaises(RuntimeError):
                self.store.reset_initial_binding()
        restored = DataStore(self.folder)
        self.assertEqual(restored.load().account_id, "account-test")
        self.assertEqual(restored.active_tracking_ids(), {"id-甲"})
        self.assertFalse(any(e["kind"] == "initial_binding_reset" for e in restored.events()))
        self.assertEqual(restored.reset_initial_binding().account_id, "")

    def test_any_snapshot_even_zero_cost_prevents_account_reset(self):
        self.people(("甲", 80))
        self.bind("甲")
        self.snapshot({"甲": 0})
        self.assertFalse(self.store.can_correct_setup())
        with self.assertRaises(GuardError):
            self.store.reset_initial_binding()
        self.assertEqual(self.store.load().account_id, "account-test")
        with self.assertRaises(AccountMismatch):
            self.store.bind_keys([{"name": "甲", "tracking_id": "foreign-key"}], "foreign-account")

    def test_action_without_any_snapshot_prevents_account_reset(self):
        self.people(("甲", 0))
        self.bind("甲")
        for state in ("pending", "unknown_result", "cancelled", "absent_verified"):
            with self.subTest(state=state):
                self.store.record_action("甲", "id-甲", state, "fixture")
                self.assertFalse(self.store.can_correct_setup())
                with self.assertRaises(GuardError):
                    self.store.reset_initial_binding()

    def test_changed_start_creates_separate_period_without_carrying_costs(self):
        self.people(("甲", 80))
        self.bind("甲")
        self.snapshot({"甲": 90})
        old_config = self.store.load()
        old_files = {p.name: p.read_bytes() for p in self.folder.iterdir() if p.is_file()}
        earlier = (datetime.now(TZ).date() - timedelta(days=1)).isoformat()
        successor = self.store.update_start_date(earlier, self.folder / "ledgers" / "earlier")
        new_config = successor.load()
        self.assertNotEqual(new_config.period_id, old_config.period_id)
        self.assertEqual(new_config.start_date, earlier)
        self.assertEqual(new_config.account_id, old_config.account_id)
        self.assertEqual(successor.read_people(), self.store.read_people())
        self.assertIsNone(successor.evaluate()[0]["spent"])
        self.assertFalse(any(e["kind"] == "snapshot" for e in successor.events()))
        self.assertEqual({p.name: p.read_bytes() for p in self.folder.iterdir() if p.is_file()}, old_files)
        # Same source identifier in a different range belongs to the new DB;
        # its lower total cannot overwrite or add to the preserved old period.
        successor.apply_snapshot({"id-甲": Decimal(20)}, "sample", earlier, self.start)
        self.assertEqual(successor.evaluate()[0]["spent"], Decimal(20))
        self.assertEqual(self.store.evaluate()[0]["spent"], Decimal(90))
        self.assertEqual(self.store.load(), old_config)

    def test_same_account_successor_keeps_retirement_and_latest_uncertain_action(self):
        self.people(("甲", 80), ("乙", 0))
        self.bind("甲", "乙")
        self.snapshot({"甲": 90, "乙": 0})
        self.store.record_action("甲", "id-甲", "absent_verified", "fixture retired")
        self.store.record_action("乙", "id-乙", "requested", "fixture approved")
        self.store.record_action("乙", "id-乙", "unknown_result", "fixture uncertain")
        successor = self.store.create_successor(self.folder / "same-account", keep_account=True)
        self.assertEqual(successor.active_tracking_ids(), {"id-乙"})
        self.assertEqual(successor.latest_action("id-乙"), "unknown_result")
        self.assertEqual(successor.latest_action("id-甲"), "absent_verified")
        actions = [e for e in successor.events() if e["kind"] == "action"]
        self.assertEqual(len(actions), 2)
        self.assertTrue(all(e["inherited_from_period"] == self.store.load().period_id for e in actions))
        self.assertEqual(successor.evaluate()[0]["status"], "retired")
        self.assertIsNone(successor.evaluate()[0]["spent"])
        self.assertFalse(successor.can_correct_setup())

    def test_same_account_successor_preserves_operation_id_and_original_evidence(self):
        self.people(("甲", 0), ("乙", 0))
        self.bind("甲", "乙")
        evidence = self.store.action_evidence("甲", "id-甲")
        self.store.record_action("甲", "id-甲", "requested", "已发许可", operation_id="original-operation", evidence=evidence)
        self.store.record_action("甲", "id-甲", "unknown_result", "待核验", operation_id="original-operation")
        successor = self.store.create_successor(self.folder / "operation-successor", keep_account=True)
        record = successor.latest_action_record("id-甲")
        self.assertEqual(record["operation_id"], "original-operation")
        self.assertEqual(record["evidence"], evidence)
        self.assertNotEqual(record["evidence"]["period_id"], successor.load().period_id)
        self.assertEqual(successor.unresolved_actions(), [record])
        with self.assertRaises(GuardError):
            successor.record_action("乙", "id-乙", "requested", "不能跨目标复用", operation_id="original-operation")
        successor.record_action("甲", "id-甲", "present_verified", "仍存在", operation_id="original-operation")
        self.assertEqual(successor.unresolved_actions(), [successor.latest_action_record("id-甲")])

    def test_other_account_successor_starts_unbound_and_preserves_original(self):
        self.people(("甲", 80))
        self.bind("甲")
        self.snapshot({"甲": 90})
        self.store.record_action("甲", "id-甲", "unknown_result", "fixture uncertain")
        successor = self.store.create_successor(self.folder / "other-account", keep_account=False)
        self.assertEqual(successor.load().account_id, "")
        self.assertEqual(successor.active_bindings(), [])
        self.assertIsNone(successor.latest_action("id-甲"))
        self.assertIsNone(successor.evaluate()[0]["spent"])
        successor.bind_keys([{"name": "甲", "tracking_id": "foreign-key"}], "foreign-account")
        self.assertEqual(successor.load().account_id, "foreign-account")
        self.assertEqual(self.store.load().account_id, "account-test")
        self.assertEqual(self.store.evaluate()[0]["spent"], Decimal(90))
        self.assertEqual(self.store.latest_action("id-甲"), "unknown_result")

    def test_failed_successor_is_not_published_and_old_ledger_is_unchanged(self):
        self.people(("甲", 80))
        self.bind("甲")
        self.snapshot({"甲": 60})
        old_files = {p.name: p.read_bytes() for p in self.folder.iterdir() if p.is_file()}
        destination = self.folder / "new-ledger"
        with patch.object(DataStore, "save_people", side_effect=GuardError("fixture write failure")):
            with self.assertRaises(GuardError):
                self.store.create_successor(destination, keep_account=True)
        self.assertFalse(destination.exists())
        self.assertEqual({p.name: p.read_bytes() for p in self.folder.iterdir() if p.is_file()}, old_files)
        staging = list(self.folder.glob(".new-ledger-*"))
        self.assertEqual(len(staging), 1)
        with self.assertRaises(GuardError):
            DataStore(staging[0]).load()

    def test_date_tampering_and_existing_destination_remain_rejected(self):
        destination = self.folder / "existing"
        destination.mkdir()
        sentinel = destination / "preserve.txt"
        sentinel.write_text("keep", encoding="utf-8")
        with self.assertRaises(GuardError):
            self.store.create_successor(destination)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
        unused = self.folder / "unchanged"
        self.assertIs(self.store.update_start_date(self.start, unused), self.store)
        self.assertFalse(unused.exists())
        future = (datetime.now(TZ).date() + timedelta(days=1)).isoformat()
        with self.assertRaises(GuardError):
            self.store.update_start_date(future, unused)
        raw = json.loads(self.store.config_path.read_text(encoding="utf-8"))
        raw["start_date"] = "2000-01-01"
        self.store.config_path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(GuardError):
            self.store.load()


if __name__ == "__main__":
    unittest.main()
