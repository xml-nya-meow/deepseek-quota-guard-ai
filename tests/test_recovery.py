"""Offline recovery contracts: damaged tables must not erase audit duties.

The GUI uses synthetic responses only. No platform browser or network request is
created by these tests; all ledgers and backup files live in temporary folders.
"""
from concurrent.futures import Future
from datetime import datetime
from decimal import Decimal
import json
import os
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from quota_guard.browser_service import key_fingerprint
from quota_guard.core import DataStore, GuardError, Person, TZ
from quota_guard.ui import App
from quota_guard.workspace import POINTER, current_ledger, save_current_ledger


class _LedgerFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name).resolve()
        self.home = self.base / "home"
        self.home.mkdir()
        self.folder = self.home / "账本" / "recovery-period"
        self.store = DataStore(self.folder)
        self.start = datetime.now(TZ).date().isoformat()
        self.config = self.store.initialize(14, "1500", self.start)
        self.people = [Person("恢复甲", Decimal(0)), Person("对照乙", Decimal(80))]
        self.keys = [{"name": "恢复甲", "tracking_id": "recovery-target-a"},
                     {"name": "对照乙", "tracking_id": "recovery-anchor-b"}]
        self.target = self.keys[0]["tracking_id"]
        self.anchor = self.keys[1]["tracking_id"]
        self.store.save_people(self.people)
        self.store.bind_keys(self.keys, key_fingerprint(self.keys))
        self.store.apply_snapshot({self.target: Decimal("0.0096"),
                                   self.anchor: Decimal("5.123456")},
                                  "original-snapshot", self.start, self.start)
        self.config = self.store.load()
        self.workbook_bytes = self.store.workbook_path.read_bytes()
        save_current_ledger(self.home, self.folder)

    def tearDown(self):
        self.temp.cleanup()

    def pending(self, operation_id="recovery-operation"):
        evidence = self.store.action_evidence("恢复甲", self.target)
        self.store.record_action("恢复甲", self.target, "pending", "合成待处理动作",
                                 operation_id=operation_id, evidence=evidence)
        self.store.record_action("恢复甲", self.target, "requested", "合成已保存许可意图",
                                 operation_id=operation_id, evidence=evidence)
        return self.store.latest_action_record(self.target)

    def damage(self, kind):
        if kind == "missing":
            self.store.workbook_path.unlink()
        elif kind == "corrupt":
            self.store.workbook_path.write_bytes(b"synthetic damaged workbook; preserve these bytes")
        else:
            raise AssertionError(kind)

    def snapshot_events(self):
        return [event for event in self.store.events() if event["kind"] == "snapshot"]


class RecoveryCoreTests(_LedgerFixture, unittest.TestCase):
    def test_existing_result_can_finish_without_table_and_keeps_original_evidence(self):
        original = self.pending()
        snapshots = self.snapshot_events()
        self.damage("missing")

        self.store.record_action("恢复甲", self.target, "absent_verified", "合成只读核实结果",
                                 operation_id=original["operation_id"], continuation=True)

        finished = self.store.latest_action_record(self.target)
        self.assertEqual(finished["operation_id"], original["operation_id"])
        self.assertEqual(finished["evidence"], original["evidence"])
        self.assertEqual(finished["status"], "absent_verified")
        self.assertEqual(self.store.active_tracking_ids(require_workbook=False), {self.anchor})
        self.assertEqual(self.store.load(require_workbook=False), self.config)
        self.assertEqual(self.snapshot_events(), snapshots)
        self.assertFalse(self.store.workbook_path.exists())

    def test_damaged_table_cannot_authorize_new_intent_or_replacement_evidence(self):
        original = self.pending()
        self.damage("corrupt")
        cases = [
            {"status": "pending", "operation_id": "new-operation"},
            {"status": "requested", "operation_id": original["operation_id"]},
            {"status": "pending", "operation_id": original["operation_id"], "continuation": True},
            {"status": "requested", "operation_id": original["operation_id"], "continuation": True},
            {"status": "unknown_result", "operation_id": original["operation_id"],
             "continuation": True, "evidence": original["evidence"]},
            {"status": "present_verified", "operation_id": "unrelated-operation", "continuation": True},
        ]
        before = self.store.events()
        for case in cases:
            with self.subTest(case=case):
                arguments = dict(case)
                status = arguments.pop("status")
                with self.assertRaises(GuardError):
                    self.store.record_action("恢复甲", self.target, status, "不得写入", **arguments)
                self.assertEqual(self.store.events(), before)

        with self.assertRaises(GuardError):
            self.store.record_action("对照乙", self.anchor, "absent_verified", "不能借用另一目标动作",
                                     operation_id=original["operation_id"], continuation=True)
        self.assertEqual(self.store.active_tracking_ids(require_workbook=False), {self.target, self.anchor})

    def test_late_unknown_cannot_overwrite_a_verified_conclusion(self):
        original = self.pending()
        self.damage("missing")
        self.store.record_action("恢复甲", self.target, "present_verified", "已核实仍存在",
                                 operation_id=original["operation_id"], continuation=True)
        with self.assertRaises(GuardError):
            self.store.record_action("恢复甲", self.target, "unknown_result", "更早动作迟到",
                                     operation_id=original["operation_id"], continuation=True)
        self.assertEqual(self.store.latest_action(self.target), "present_verified")

    def test_legacy_recovery_does_not_invent_operation_id_or_evidence(self):
        self.store.record_action("恢复甲", self.target, "unknown_result", "旧版未决记录")
        self.damage("corrupt")
        self.store.record_action("恢复甲", self.target, "present_verified", "只读核实旧记录",
                                 operation_id=None, continuation=True)
        record = self.store.latest_action_record(self.target)
        self.assertIsNone(record["operation_id"])
        self.assertNotIn("evidence", record)
        self.assertEqual(self.store.unresolved_actions(), [record])

    def test_restore_corrupt_table_preserves_bytes_history_and_precise_costs(self):
        original = self.pending()
        snapshots = self.snapshot_events()
        self.damage("corrupt")
        damaged = self.store.workbook_path.read_bytes()
        self.store.restore_people(self.people, expected_hash=self.store.recovery_workbook_hash())

        backups = list(self.folder.glob("人员额度-恢复前-*.xlsx.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), damaged)
        self.assertEqual(self.store.read_people(), self.people)
        self.assertEqual(self.store.load(), self.config)
        self.assertEqual(self.store.unresolved_actions(), [original])
        self.assertEqual(self.snapshot_events(), snapshots)
        amounts = {row["tracking_id"]: row["spent"] for row in self.store.evaluate()}
        self.assertEqual(amounts, {self.target: Decimal("0.0096"), self.anchor: Decimal("5.123456")})

    def test_restore_missing_table_never_reinitializes_ledger(self):
        original = self.pending()
        snapshots = self.snapshot_events()
        self.damage("missing")
        self.assertIsNone(self.store.recovery_workbook_hash())
        self.store.restore_people(self.people, expected_hash=None)

        self.assertEqual(self.store.load(), self.config)
        self.assertEqual(self.store.unresolved_actions(), [original])
        self.assertEqual(self.snapshot_events(), snapshots)
        self.assertEqual(list(self.folder.glob("人员额度-恢复前-*.xlsx.bak")), [])
        self.assertEqual(sum(event["kind"] == "initialized" for event in self.store.events()), 1)

    def test_restore_rejects_a_table_changed_during_confirmation(self):
        self.damage("corrupt")
        expected = self.store.recovery_workbook_hash()
        replacement = b"a different synthetic damaged workbook"
        self.store.workbook_path.write_bytes(replacement)
        with self.assertRaises(GuardError):
            self.store.restore_people(self.people, expected_hash=expected)
        self.assertEqual(self.store.workbook_path.read_bytes(), replacement)
        self.assertFalse(any(event["kind"] == "workbook_restored" for event in self.store.events()))

    def test_pointer_still_selects_damaged_table_ledger_but_rejects_bad_database(self):
        self.damage("missing")
        self.assertEqual(current_ledger(self.home), self.folder)
        save_current_ledger(self.home, self.folder)
        self.assertEqual(current_ledger(self.home), self.folder)
        pointer = (self.home / POINTER).read_bytes()
        self.store.db_path.write_bytes(b"not a sqlite database")
        with self.assertRaises(GuardError):
            current_ledger(self.home)
        self.assertEqual((self.home / POINTER).read_bytes(), pointer)
        self.assertFalse(self.store.workbook_path.exists())

    def test_pointer_period_mismatch_is_not_relaxed_for_recovery(self):
        self.damage("corrupt")
        pointer_path = self.home / POINTER
        pointer = json.loads(pointer_path.read_text(encoding="utf-8"))
        pointer["period_id"] = "different-period"
        pointer_path.write_text(json.dumps(pointer), encoding="utf-8")
        with self.assertRaises(GuardError):
            current_ledger(self.home)


class _OfflineBrowser:
    def __init__(self, results=None, *, controlled=False):
        self.results = results or {}
        self.controlled = controlled
        self.calls = []
        self.futures = []

    def submit(self, action, **kwargs):
        self.calls.append((action, kwargs))
        future = Future()
        self.futures.append(future)
        if not self.controlled:
            if action not in self.results:
                raise AssertionError("Unexpected offline browser action: " + action)
            result = self.results[action]
            if isinstance(result, Exception):
                future.set_exception(result)
            else:
                future.set_result(result)
        return future

    def interrupt(self):
        for pending in self.futures:
            if not pending.done():
                pending.set_exception(GuardError("合成中断；平台结果未知"))
        result = Future()
        result.set_result(None)
        return result

    def close(self):
        pass


class _OfflineTargetLock:
    """The kernel exclusion contract has its own tests; record UI coverage here."""
    def __init__(self, targets=()):
        self.tracking_ids = frozenset(targets)
        self.closed = False

    def update(self, targets):
        self.tracking_ids = frozenset(targets)

    def covers(self, targets):
        return not self.closed and set(targets) <= self.tracking_ids

    @property
    def names(self):
        # This double is only sent to _OfflineBrowser, never a kernel witness.
        return tuple('fixture:' + target for target in sorted(self.tracking_ids))

    def close(self):
        self.closed = True


@unittest.skipUnless(os.name == "nt", "The application GUI and folder locks target Windows")
class RecoveryGuiTests(_LedgerFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.root = None
        self.app = None
        self.patchers = [patch("quota_guard.ui.messagebox.showinfo"),
                         patch("quota_guard.ui.messagebox.showerror"),
                         patch("quota_guard.ui.messagebox.askyesno", return_value=True),
                         patch("quota_guard.target_lock.TargetLock", _OfflineTargetLock)]
        self.dialogs = [item.start() for item in self.patchers]

    def tearDown(self):
        if self.app is not None:
            self.app.stop(quiet=True)
            if self.app.target_lock is not None:
                self.app.target_lock.close()
            self.app.folder_lock.close()
        if self.root is not None:
            for callback in self.root.tk.call("after", "info"):
                self.root.after_cancel(callback)
            self.root.destroy()
        for item in reversed(self.patchers):
            item.stop()
        super().tearDown()

    def wait_for(self, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            self.root.update()
            if predicate():
                return
            time.sleep(.01)
        self.fail("Offline GUI callback did not settle within its test deadline")

    def open_app(self, *, damage=None, pending=True):
        if pending:
            self.pending()
        if damage:
            self.damage(damage)
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root, current_ledger(self.home), home_folder=self.home)
        self.wait_for(lambda: self.app.config is not None)
        self.app.browser = _OfflineBrowser()

    def select_target(self):
        index = next(index for index, row in enumerate(self.app.rows)
                     if row.get("tracking_id") == self.target)
        self.app.tree.selection_set(str(index))

    def assert_recovery_startup(self, damage):
        self.open_app(damage=damage)
        self.assertTrue(self.app.recovery_mode)
        self.assertEqual(self.app.config, self.config)
        self.assertEqual(self.app.folder, self.folder)
        self.assertEqual(current_ledger(self.home), self.folder)
        self.assertEqual([(row["tracking_id"], row["status"]) for row in self.app.rows],
                         [(self.target, "recovery_pending")])
        self.assertIn("recovery-operation", self.app.report_text.get("1.0", "end"))
        self.assertFalse(self.app.observing)
        self.assertFalse(self.app.automatic)
        self.assertEqual(self.app.browser.calls, [])

    def test_missing_table_opens_original_ledger_recovery_view(self):
        self.assert_recovery_startup("missing")
        self.assertFalse(self.store.workbook_path.exists())

    def test_corrupt_table_opens_original_ledger_recovery_view(self):
        self.assert_recovery_startup("corrupt")
        self.assertEqual(self.store.workbook_path.read_bytes(),
                         b"synthetic damaged workbook; preserve these bytes")

    def test_recovery_disables_controls_and_direct_new_operation_entry_points(self):
        self.open_app(damage="missing")
        self.select_target()
        self.app._update_buttons()
        for key in ("collect", "start", "revoke", "date", "edit", "import", "account_ledger"):
            with self.subTest(button=key):
                self.assertEqual(str(self.app.buttons[key].cget("state")), "disabled")
        for key in ("login", "verify", "check", "import_people", "reports"):
            with self.subTest(button=key):
                self.assertEqual(str(self.app.buttons[key].cget("state")), "normal")
        before = self.store.events()
        with patch("quota_guard.ui.ask_text") as ask, \
                patch("quota_guard.ui.filedialog.askopenfilename") as choose:
            self.app.collect()
            self.app.start()
            self.app.prepare_revoke()
            self.app.change_start_date()
            self.app.edit_people()
            self.app.import_billing()
            self.app.resolve_account_mismatch()
            self.app._browser_action("revoke", lambda result: self.fail("Unexpected result"))
            ask.assert_not_called()
            choose.assert_not_called()
        self.assertEqual(self.app.browser.calls, [])
        self.assertEqual(self.store.events(), before)
        self.assertFalse(self.app.automatic)

    def test_recovery_login_does_not_reset_original_binding(self):
        self.open_app(damage="corrupt")
        self.app.browser = _OfflineBrowser({"login": {"message": "synthetic login"}})
        before = self.store.active_bindings(require_workbook=False)
        with patch.object(self.app.store, "can_correct_setup") as can_reset, \
                patch.object(self.app.store, "reset_initial_binding") as reset:
            self.app.login()
            self.wait_for(lambda: not self.app.pending_login and not self.app.busy
                          and bool(self.app.browser.calls))
            can_reset.assert_not_called()
            reset.assert_not_called()
        self.assertEqual([action for action, _ in self.app.browser.calls], ["login"])
        self.assertEqual(self.store.active_bindings(require_workbook=False), before)
        self.assertTrue(self.app.recovery_mode)
        self.assertIn("核实平台状态", self.app.status_var.get())

    def verify_scenario(self, result, expected):
        self.open_app(damage="missing")
        original = self.store.latest_action_record(self.target)
        snapshots = self.snapshot_events()
        self.app.browser = _OfflineBrowser({"verify_revoke": result})
        self.select_target()
        self.app.verify_revoke()
        self.wait_for(lambda: not self.app.busy)
        self.assertEqual([action for action, _ in self.app.browser.calls], ["verify_revoke"])
        self.assertEqual(self.app.browser.calls[0][1]["known_keys"], {self.target, self.anchor})
        self.assertTrue(self.app.target_lock.covers({self.target, self.anchor}))
        record = self.store.latest_action_record(self.target)
        self.assertEqual(record["status"], expected)
        self.assertEqual(record["operation_id"], original["operation_id"])
        self.assertEqual(record["evidence"], original["evidence"])
        self.assertEqual(self.snapshot_events(), snapshots)
        self.assertFalse(self.app.audit_failed)
        self.assertFalse(self.app.automatic)
        self.assertTrue(self.app.recovery_mode)
        self.assertFalse(self.store.workbook_path.exists())

    def test_recovery_can_confirm_original_target_absent_without_fees(self):
        self.verify_scenario({"absent": True, "present": False}, "absent_verified")
        self.assertEqual(self.store.active_tracking_ids(require_workbook=False), {self.anchor})
        self.assertEqual(self.store.unresolved_actions(), [])

    def test_recovery_can_confirm_original_target_still_present(self):
        self.verify_scenario({"absent": False, "present": True}, "present_verified")
        self.assertEqual(self.store.active_tracking_ids(require_workbook=False), {self.target, self.anchor})
        self.assertEqual(self.store.unresolved_actions(), [self.store.latest_action_record(self.target)])

    def test_failed_read_only_verification_remains_unknown_without_new_revoke(self):
        self.verify_scenario(GuardError("没有其他存活的已知 Key 可核验账户连续性"), "observation_failed")
        self.assertEqual(self.store.active_tracking_ids(require_workbook=False), {self.target, self.anchor})
        self.assertEqual(len(self.store.unresolved_actions()), 1)

    def test_late_authorized_result_is_saved_after_workbook_disappears(self):
        self.open_app(pending=False)
        self.app.browser = _OfflineBrowser({"collect": {
            "keys": self.keys, "account_id": self.config.account_id,
            "totals": {self.target: Decimal("0.0096"), self.anchor: Decimal("5.123456")},
            "source_id": "current-offline-snapshot", "start_date": self.start, "end_date": self.start,
            "collection_started_wall": time.time(), "collection_started_monotonic": time.monotonic(),
        }})
        self.app.collect()
        self.wait_for(lambda: not self.app.busy)
        self.assertTrue(self.app._collection_is_current())
        browser = self.app.browser = _OfflineBrowser(controlled=True)
        self.app.observing = self.app.automatic = True
        self.app._process_next()
        self.assertEqual(browser.calls[0][0], "revoke")
        permit = browser.calls[0][1]["permit"]
        permit.set_prepared_inventory(self.target, [key['tracking_id'] for key in self.keys])
        permit.ready.set()
        self.wait_for(lambda: self.store.latest_action(self.target) == "requested")
        self.assertTrue(permit.allowed)
        original = self.store.latest_action_record(self.target)

        self.damage("missing")
        self.app.refresh_rows()
        self.assertTrue(self.app.recovery_mode)
        browser.futures[0].set_result({"absent": True})
        self.wait_for(lambda: not self.app.busy)

        record = self.store.latest_action_record(self.target)
        self.assertEqual(record["status"], "absent_verified")
        self.assertEqual(record["operation_id"], original["operation_id"])
        self.assertEqual(record["evidence"], original["evidence"])
        self.assertFalse(self.app.audit_failed)
        self.assertFalse(self.app.automatic)
        self.assertEqual([action for action, _ in browser.calls], ["revoke"])
        self.assertEqual(self.store.active_tracking_ids(require_workbook=False), {self.anchor})

    def test_gui_restore_preserves_history_and_requires_fresh_platform_check(self):
        self.open_app(damage="corrupt")
        original = self.store.latest_action_record(self.target)
        snapshots = self.snapshot_events()
        damaged = self.store.workbook_path.read_bytes()
        candidate = self.base / "valid-restoration.xlsx"
        candidate.write_bytes(self.workbook_bytes)
        with patch("quota_guard.ui.filedialog.askopenfilename", return_value=str(candidate)):
            self.app.import_people()

        self.assertFalse(self.app.recovery_mode)
        self.assertEqual(self.store.load(), self.config)
        self.assertEqual(self.store.unresolved_actions(), [original])
        self.assertEqual(self.snapshot_events(), snapshots)
        self.assertEqual(self.store.read_people(), self.people)
        backups = list(self.folder.glob("人员额度-恢复前-*.xlsx.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), damaged)
        self.assertEqual(candidate.read_bytes(), self.workbook_bytes)
        self.assertFalse(self.app.session_ready)
        self.assertFalse(self.app._collection_is_current())
        self.assertFalse(self.app.observing)
        self.assertFalse(self.app.automatic)
        self.assertIsNone(self.app.timer)
        self.assertEqual(self.app.browser.calls, [])
        self.assertEqual(self.app.rows[0]["status"], "recovery_pending")


if __name__ == "__main__":
    unittest.main()
