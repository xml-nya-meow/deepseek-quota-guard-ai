"""Both production Tk tables must describe the evidence for the same exact target."""
from decimal import Decimal
import time
import unittest

import test_workflow as fixtures
from quota_guard.core import Person
from quota_guard.revocation_permit import RevocationNotSubmitted


class RosterFeeEvidenceTests(unittest.TestCase):
    setUp = fixtures.WorkflowTests.setUp
    tearDown = fixtures.WorkflowTests.tearDown
    pump = fixtures.WorkflowTests.pump
    valid_collection = fixtures.WorkflowTests.valid_collection
    identity_result = fixtures.WorkflowTests.identity_result

    def collect(self):
        self.app.browser = fixtures.OfflineBrowser({"collect": self.valid_collection()})
        self.app.collect()
        self.pump()

    def assert_views(self, index, state):
        iid = str(index)
        fee = self.app.fee_review
        self.assertEqual(self.app.tree.set(iid, "status"), state)
        self.assertEqual(fee.tree.set(iid, "state"), state)
        self.assertEqual(self.app.tree.set(iid, "spent"), fee.tree.set(iid, "spent").strip())
        self.assertEqual(self.app.tree.item(iid, "tags"), fee.tree.item(iid, "tags"))
        self.app.tree.selection_set(iid)
        self.app._selection()
        fee.tree.selection_set(iid)
        fee._select()
        self.assertIn(fee.detail.get(), self.app.detail_var.get())

    def test_start_stop_inflight_uncertainty_presence_and_absence(self):
        self.collect()
        self.assert_views(0, "已达线 · 管理未开启")
        self.assert_views(1, "额度内")
        browser = fixtures.ControlledBrowser()
        self.app.browser = browser
        self.app.fee_review.start_button.invoke()
        self.assert_views(0, "已达线 · 管理运行中")
        self.pump()
        self.assert_views(0, "原操作待核实")
        permit = browser.calls[0][1]["permit"]
        self.assertFalse(permit.allowed)
        fixtures.ready_with_inventory(self.app, permit)
        self.pump()
        self.assertTrue(permit.allowed)
        self.assert_views(0, "许可可能已发出 · 原操作待核实")
        self.app.stop()
        browser.futures[0].set_result({})
        self.pump()
        self.assert_views(0, "原操作待核实")
        self.app.fee_review.filter.set("待核实原操作")
        self.assertEqual(self.app.fee_review.tree.get_children(), ("0",))
        self.app.verify_revoke()
        browser.futures[1].set_result({"present": True, "absent": False})
        self.pump()
        self.assert_views(0, "核实时仍在 · 原操作待核实")
        self.app.verify_revoke()
        browser.futures[2].set_result({"absent": True})
        self.pump()
        self.assertEqual(self.app.fee_review.tree.get_children(), ())
        self.app.fee_review.filter.set("全部")
        self.assert_views(0, "已核实平台列表中无此 Key")
        self.assertEqual([action for action, _ in browser.calls], ["revoke", "verify_revoke", "verify_revoke"])

    def test_reordered_tree_and_expired_partial_fees_preserve_target_and_precision(self):
        amount = Decimal("0.000000000123456789")
        self.store.save_people([Person("测试甲", amount), Person("测试乙", Decimal(0))])
        self.app.browser = fixtures.OfflineBrowser({"collect": self.valid_collection(
            totals={"test-key-a": amount, "test-key-b": Decimal(5)})})
        self.app.collect()
        self.pump()
        self.app.tree.move("1", "", 0)
        self.app._refresh_fee_review()
        self.assert_views(0, "已达线 · 管理未开启")
        self.assertEqual(self.app.tree.set("0", "spent"), format(amount, "f"))
        self.app.browser.results["collect"] = self.valid_collection(
            source_id="partial", totals={"test-key-b": Decimal(5)})
        self.app.collect()
        self.pump()
        self.assert_views(0, "费用待补全")
        self.assertEqual(self.app.tree.set("0", "spent"), "≥ " + format(amount, "f") + "（下界）")
        self.assert_views(1, "已达线 · 管理未开启")
        self.app.collection_observed_wall = time.time() - 400
        self.app.collection_observed_monotonic = time.monotonic() - 400
        self.app._watch_workbook()
        self.assert_views(0, "待重新核验")
        self.assertEqual(self.app.tree.set("0", "spent"), "≥ " + format(amount, "f") + "（历史下界）")
        self.assert_views(1, "待重新核验")
        self.assertEqual(self.app.fee_review.data.counts()[-1], 0)
        self.assertFalse(self.store.unresolved_actions())

    def test_force_below_threshold_records_non_submission_without_new_authority(self):
        self.collect()
        self.app.tree.selection_set("1")
        browser = fixtures.ControlledBrowser(identity_result=self.identity_result())
        self.app.browser = browser
        self.app.revoke_selected_once()
        self.pump()
        browser.futures[0].set_exception(RevocationNotSubmitted())
        self.pump()
        self.assert_views(1, "待重新核验 · 上次未提交")
        self.collect()
        self.assert_views(1, "额度内 · 上次未提交")
        self.assertFalse(self.app.fee_review.data.candidate(self.app.fee_review.data.rows[1]))
        self.assertFalse(self.store.unresolved_actions())

    def test_watcher_observes_snapshot_replacement_while_tables_are_open(self):
        self.collect()
        self.store.apply_snapshot({"test-key-a": Decimal(2), "test-key-b": Decimal(10)},
                                  "later-source", self.start, self.start)
        self.app._watch_workbook()
        self.assert_views(1, "待重新核验")
        self.assertEqual(self.app.tree.set("1", "spent"), "5（历史）")

    def test_fee_selection_uses_live_widget_identity_before_tk_delivers_event(self):
        self.collect()
        fee = self.app.fee_review
        fee.tree.selection_set("0")
        fee._select()
        fee.tree.selection_set("1")
        self.store.save_people(list(reversed(self.store.read_people())))
        self.app.refresh_rows(readonly=True)
        self.assertEqual(fee.target_id.get(), "test-key-b")
        self.assertEqual(fee.tree.selection(), ("0",))
        fee.tree.selection_remove("0")
        fee._select()
        self.assertEqual(fee.target_id.get(), "")
        self.assertNotIn("测试乙", fee.detail.get())
