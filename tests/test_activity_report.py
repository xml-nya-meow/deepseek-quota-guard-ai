"""Timeline facts, raw evidence and recovery navigation use the existing ledger."""
import json
from decimal import Decimal
import tkinter as tk
import unittest
from unittest.mock import Mock

from quota_guard.activity_report import ActivityReportFrame, event_title
from quota_guard.core import GuardError
import test_workflow as fixtures


class ActivityReportTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.addCleanup(self.root.update_idletasks)

    def test_transitions_keep_request_presence_and_absence_distinct(self):
        locate = Mock()
        frame = ActivityReportFrame(self.root, on_locate=locate)
        pending = dict(id=5, kind="action", name="雪松", tracking_id="complete-id", status="requested",
                       operation_id="original-op", created="2026-11-24T12:01:00+08:00")
        old = {**pending, "id": 4, "status": "pending"}
        ended = {**pending, "id": 6, "status": "absent_verified"}
        frame.show([pending, old], [pending], ledger="period-A")
        self.assertIn("许可可能已发出", frame.tree.item("5", "values")[-1])
        self.assertIn("original-op", frame.reference.get())
        self.assertFalse(frame.expanded.get())
        self.assertEqual(frame.raw_text.cget("state"), "disabled")
        self.assertEqual(json.loads(frame.raw_text.get("1.0", "end")), pending)
        frame.locate.invoke()
        locate.assert_called_once_with("original-op", "complete-id")
        frame.show([ended, pending], [], ledger="period-A")
        self.assertTrue(frame.locate.instate(["disabled"]))
        self.assertIn("列表中无此 Key", frame.tree.item("6", "values")[-1])
        self.assertIn("独立核实时，平台列表中无此 Key。移除原因未知", frame.timeline_text.get("1.0", "end"))
        self.assertIn("列表仍有此 Key", event_title({**pending, "status": "present_verified"}))
        frame.clear()
        self.assertFalse(frame.tree.get_children())
        self.assertEqual(frame.raw_text.get("1.0", "end").strip(), "")
        self.assertEqual(frame.timeline_text.get("1.0", "end").strip(), "")

    def test_current_unresolved_record_remains_reachable_outside_recent_event_window(self):
        locate = Mock()
        frame = ActivityReportFrame(self.root, on_locate=locate)
        old = dict(id=1, kind="action", name="甲", tracking_id="legacy-target",
                   status="unknown_result", created="2026-01-01")
        recent = [dict(id=n, kind="snapshot", tracked_key_count=1) for n in range(300, 100, -1)]
        frame.show(recent, [old], ledger="original")
        self.assertEqual(frame.tree.selection(), ("1",))
        frame.locate.invoke()
        locate.assert_called_once_with(None, "legacy-target")
        self.assertNotIn("原操作", frame.reference.get())
        self.assertIn("旧记录缺少操作编号", frame.summary.get())
        self.assertFalse(frame.timeline.winfo_manager())
        frame.show(recent, [], ledger="another")
        self.assertNotIn("1", frame.tree.get_children())

    def test_partial_and_legacy_read_only_timelines_do_not_infer_a_submission(self):
        frame = ActivityReportFrame(self.root, on_locate=Mock())
        origin = dict(id=2, kind="action", operation_id="old-observation", tracking_id="fixture",
                      name="同名", status="pending", detail="用户请求只读核实平台状态；未授权或提交撤销")
        result = {**origin, "id": 5, "status": "unknown_result", "detail": "旧记录"}
        other = {**origin, "id": 3, "operation_id": "old-observation-2", "status": "requested"}
        no_id = {**origin, "id": 4, "operation_id": None, "status": "requested"}
        load = Mock(side_effect=GuardError("暂时无法读取原记录"))
        frame.show([result, no_id, other, origin], [], ledger="fixture", read_operation=load)
        history = frame.timeline_text.get("1.0", "end")
        self.assertIn("仅显示当前列表", frame.timeline.cget("text"))
        self.assertIn("已开始只读核实", history)
        self.assertIn("只读核实未取得明确结果", history)
        self.assertIn("只读核实未取得明确结果", frame.summary.get())
        self.assertIn("本次只读核实未获明确结果", frame.tree.item("5", "values")[-1])
        self.assertNotIn("实际提交次数", history)
        self.assertNotIn("#3 ", history)
        self.assertNotIn("#4 ", history)
        self.assertEqual(frame.timeline_text.cget("state"), "disabled")
        frame.tree.selection_set("4")
        self.root.update()
        self.assertIn("仅显示本条", frame.summary.get())
        self.assertEqual(frame.timeline_text.get("1.0", "end").strip(), "")

    def test_forced_intent_and_definite_non_submission_keep_their_own_evidence(self):
        frame = ActivityReportFrame(self.root, on_locate=Mock())
        action = dict(id=1, kind="action", name="合成甲", tracking_id="exact", operation_id="force-1",
                      status="pending", intent="manual_force",
                      evidence={"spent": None, "amount": "0", "share": "75", "status": "triggered",
                                "reasons": ["个人额度为零"]})
        stopped = {**action, "id": 2, "status": "not_submitted"}
        frame.show([stopped, action], [], ledger="fixture")
        history = frame.timeline_text.get("1.0", "end")
        self.assertIn("手动强制删除意图", history)
        self.assertIn("费用尚未知，个人额度 0 元，公共人均额度 75 元，个人额度为零", history)
        self.assertIn("已核实本次未提交", history)
        self.assertNotIn("一次性提交许可可能已发出", history)


class ActivityNavigationTests(unittest.TestCase):
    setUp = fixtures.WorkflowTests.setUp
    tearDown = fixtures.WorkflowTests.tearDown
    pump = fixtures.WorkflowTests.pump

    def test_full_operation_across_recent_window_reads_only_its_ledger_events(self):
        self.store.bind_keys(self.keys, "fixture-account")
        name, target = "测试乙", "test-key-b"
        other, original = "a" * 31 + "1", "a" * 31 + "2"
        self.store.record_action(name, target, "pending", "只读观察", operation_id=other, read_only=True)
        self.store.record_action(name, target, "present_verified", "观察完成", operation_id=other, continuation=True)
        self.store.apply_snapshot({"test-key-a": Decimal(0), target: Decimal("95.25")},
                                  "historical-cost", self.start, self.start)
        evidence = self.store.action_evidence(name, target)
        gate = self.app.responsibility_gate
        receipt = gate.claim(self.store, original, target)
        self.store.record_action(name, target, "pending", "达线", operation_id=original,
                                 evidence=evidence, responsibility=receipt.to_dict())
        self.assertTrue(gate.possible_send(self.store, receipt, write_requested=lambda:
            self.store.record_action(name, target, "requested", "保存许可记录", operation_id=original,
                                     evidence=evidence)))
        self.store.record_action(name, target, "unknown_result", "尚待观察",
                                 operation_id=original, continuation=True)
        with self.store._connect() as db:
            for _ in range(205):
                self.store._event(db, "snapshot", {"tracked_key_count": 2})
        self.store.record_action(name, target, "present_verified", "仍在列表",
                                 operation_id=original, continuation=True)
        self.app.browser = fixtures.ControlledBrowser()
        events = self.store.events(1000)
        older_ids = {e["id"] for e in events if e.get("operation_id") == other}
        expected = [e for e in reversed(events) if e.get("operation_id") == original or
                    e.get("receipt", {}).get("operation_id") == original]
        self.assertEqual(len(expected), 5)
        self.app.refresh_events()
        report = self.app.activity_report
        self.assertNotIn(str(expected[0]["id"]), report.tree.get_children())
        self.assertEqual(report.tree.selection(), (str(expected[-1]["id"]),))
        history = report.timeline_text.get("1.0", "end")
        self.assertEqual([int(line.split()[0][1:]) for line in history.splitlines() if line],
                         [event["id"] for event in expected])
        self.assertFalse(older_ids.intersection(e["id"] for e in expected))
        self.assertIn("5 条", report.timeline.cget("text"))
        self.assertIn("本账本", report.timeline.cget("text"))
        self.assertIn("已知费用 95.25 元", history)
        self.assertIn("个人额度 80 元", history)
        self.assertIn("公共人均额度 107 元", history)
        self.assertIn("达到个人额度", history)
        self.assertIn("一次性提交许可可能已发出，实际提交次数未知", history)
        self.assertIn("已登记本次可能提交的责任", history)
        self.assertIn("独立核实时，平台列表仍有此 Key", history)
        self.assertIn("原操作仍待核实", history)
        self.assertEqual(self.app.browser.calls, [])
        self.assertEqual(self.store.events(1000), events)
        with self.assertRaises(GuardError):
            self.store.operation_events(original, period_id="another-ledger")
        self.assertEqual(self.store.operation_events(original[:-1], period_id=self.app.config.period_id), [])

    def test_locate_selects_only_the_original_and_never_sends_or_appends_an_action(self):
        self.store.bind_keys(self.keys, "fixture-account")
        self.store.record_action("测试乙", "test-key-b", "pending", "已建立合成操作", operation_id="selected-op")
        self.app.refresh_rows()
        self.app.refresh_events()
        self.app.browser = fixtures.ControlledBrowser()
        before = self.store.events()
        latest = self.store.latest_action_record("test-key-b")
        self.app.activity_report.tree.selection_set(str(latest["id"]))
        self.app.activity_report.locate.invoke()
        self.assertEqual(self.app.selected()["tracking_id"], "test-key-b")
        self.assertEqual(self.app.selected()["operation_id"], "selected-op")
        self.assertEqual(self.app.review_tabs.select(), str(self.app.overview_tab))
        self.assertIn("selected-op", self.app.detail_var.get())
        self.assertEqual(self.app.browser.calls, [])
        self.assertEqual(self.store.events(), before)
