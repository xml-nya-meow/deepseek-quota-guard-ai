"""A read-only ledger event timeline. All status facts come from recorded events."""
import json
import tkinter as tk
from tkinter import ttk

from .core import DataStore, GuardError


ACTION_LABELS = {
    "pending": "已记录操作意图",
    "prepared": "已完成准备，结果待核实",
    "requested": "提交许可可能已发出",
    "confirmed": "已记录确认，结果待核实",
    "unknown_result": "处理结果待核实",
    "unknown": "结果待核实",
    "failed": "处理遇到错误，结果待核实",
    "not_submitted": "准备已停止，未提交",
    "absent_verified": "已核实平台列表中无此 Key",
    "present_verified": "核实时平台列表仍有此 Key",
    "observation_failed": "本次只读核实未完成",
    "cancelled": "操作已取消",
}

EVENT_LABELS = {
    "initialized": "已建立额度账本",
    "people_saved": "人员表已保存",
    "workbook_restored": "人员表已恢复",
    "keys_bound": "平台身份已绑定",
    "initial_binding_reset": "已重置初始账户绑定",
    "successor_created": "已建立后续账本",
    "snapshot": "费用已入账",
    "quota_triggered": "已记录额度达线",
    "responsibility_send_boundary": "已登记可能提交的责任",
    "responsibility_recovery": "已恢复提交前的原操作记录",
    "membership_recovery": "已记录本机账本范围",
}


def event_operation(event):
    receipt = event.get("receipt")
    operation = event.get("operation_id") or (receipt.get("operation_id") if isinstance(receipt, dict) else None)
    return operation if isinstance(operation, str) and operation else None


def fee_facts(evidence):
    spent = evidence.get("spent")
    facts = ["费用尚未知"] if spent is None else [
        f"{'已知费用下界' if evidence.get('complete') is False else '已知费用'} {spent} 元"]
    for field, label in (("amount", "个人额度"), ("share", "公共人均额度")):
        if evidence.get(field) is not None:
            facts.append(f"{label} {evidence[field]} 元")
    if evidence.get("status") == "triggered" or evidence.get("kind") == "quota_triggered":
        facts.extend(evidence.get("reasons") or ["已达额度"])
    return "，".join(facts)


def event_title(event):
    kind = event.get("kind")
    if kind == "action":
        status = event.get("status")
        if status == "pending" and DataStore._read_only_operation([event]):
            return "已开始只读核实"
        if event.get("read_only") is True and status in {"unknown_result", "unknown", "failed"}:
            return "本次只读核实未获明确结果"
        return ACTION_LABELS.get(status, "操作记录（" + str(status or "状态未知") + "）")
    return EVENT_LABELS.get(kind, "账本记录（" + str(kind or "类型未知") + "）")


def event_detail(event):
    kind = event.get("kind")
    if kind == "action":
        status = event.get("status")
        if status == "absent_verified":
            return "独立核实时，平台列表中无此 Key。移除原因未知。"
        if status == "requested":
            return "一次性提交许可可能已发出，实际提交次数未知。"
        if status == "present_verified":
            detail = "独立核实时，平台列表仍有此 Key。"
            if event.get("requires_reconciliation"):
                detail += "原操作仍待核实。"
            return detail
        if status == "pending" and event.get("intent") == "manual_force":
            return "已记录手动强制删除意图，本次操作可跳过额度条件。"
        if status == "pending" and DataStore._read_only_operation([event]):
            return "已开始只读核实，本次操作未授权或提交撤销。"
        if status == "not_submitted":
            return "准备已停止，已核实本次未提交。"
        if status == "observation_failed" or (event.get("read_only") is True
                                               and status in {"unknown_result", "unknown", "failed"}):
            return "只读核实未取得明确结果。"
        if status in {"unknown_result", "unknown", "failed"}:
            return "本次处理结果未知，实际提交次数未确认。"
        return event_title(event)
    if kind == "snapshot":
        matched = event.get("tracked_key_count")
        missing = event.get("missing_key_count")
        facts = []
        if matched is not None:
            facts.append(f"本次入账 {matched} 枚")
        if missing is not None:
            facts.append(f"费用待补全 {missing} 枚")
        if event.get("end_date"):
            facts.append(f"查询至 {event['end_date']}")
        return "，".join(facts) or event_title(event)
    if kind == "quota_triggered":
        return fee_facts(event)
    if kind == "responsibility_send_boundary":
        return "已登记本次可能提交的责任，实际提交次数未知。"
    if kind == "responsibility_recovery":
        return "已核实原责任停留在提交前，未签发删除许可，原操作已结清。"
    if kind == "people_saved" and isinstance(event.get("people"), list):
        return f"已记录 {len(event['people'])} 个已填写的名称。"
    if kind == "keys_bound" and isinstance(event.get("bindings"), list):
        return f"本次绑定 {len(event['bindings'])} 枚 Key。"
    return event_title(event)


class ActivityReportFrame(ttk.Frame):
    """Shows recent events and every currently unresolved original operation."""

    def __init__(self, parent, *, on_locate):
        super().__init__(parent)
        self.on_locate = on_locate
        self._events = {}
        self._unresolved = set()
        self._ledger = None
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.tree = ttk.Treeview(self, columns=("when", "target", "event"),
                                 show="headings", selectmode="browse", height=6)
        for name, title, width in (("when", "记录时间", 195), ("target", "对象", 130), ("event", "处理进展", 285)):
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, minwidth=78, stretch=name != "when", anchor="w")
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(self, orient="vertical", command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.summary = tk.StringVar(self)
        ttk.Label(self, textvariable=self.summary, justify="left", wraplength=760).grid(
            row=1, column=0, sticky="ew", pady=(5, 0))
        self.reference = tk.StringVar(self)
        self.reference_field = ttk.Entry(self, textvariable=self.reference, state="readonly")
        self.reference_field.grid(row=2, column=0, sticky="ew", pady=(2, 3))
        self.actions = ttk.Frame(self)
        self.actions.grid(row=3, column=0, sticky="ew")
        self.locate = ttk.Button(self.actions, text="定位待核实人员", command=self._locate)
        self.locate.pack(side="left")
        self.expanded = tk.BooleanVar(self, False)
        ttk.Checkbutton(self.actions, text="展开原始记录", variable=self.expanded,
                        command=self._toggle_raw).pack(side="left", padx=(8, 0))
        self.timeline = ttk.LabelFrame(self, text="同一操作的时间线", padding=(4, 2))
        self.timeline.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self.timeline_text = tk.Text(self.timeline, width=1, height=5, wrap="word",
                                     state="disabled", font="TkDefaultFont")
        timeline_scroll = ttk.Scrollbar(self.timeline, command=self.timeline_text.yview)
        self.timeline_text.configure(yscrollcommand=timeline_scroll.set)
        timeline_scroll.pack(side="right", fill="y")
        self.timeline_text.pack(fill="both", expand=True)
        self.raw_frame = ttk.Frame(self)
        self.raw_text = tk.Text(self.raw_frame, height=5, wrap="word", state="disabled")
        raw_scroll = ttk.Scrollbar(self.raw_frame, orient="vertical", command=self.raw_text.yview)
        self.raw_text.configure(yscrollcommand=raw_scroll.set)
        raw_scroll.pack(side="right", fill="y")
        self.raw_text.pack(fill="both", expand=True)
        self.raw_frame.grid(row=5, column=0, sticky="ew", pady=(3, 0))
        self.raw_frame.grid_remove()
        self.tree.bind("<<TreeviewSelect>>", self._selected)
        self.clear()

    def _toggle_raw(self):
        if self.expanded.get():
            self.raw_frame.grid()
        else:
            self.raw_frame.grid_remove()

    def clear(self):
        self.expanded.set(False)
        self._toggle_raw()
        self.show([], [], ledger=None)

    def show(self, events, unresolved, *, ledger, read_operation=None):
        selection = self.tree.selection() if self._ledger == ledger else ()
        self._ledger = ledger
        self._read_operation = read_operation
        self._operation_cache = {}
        # Unresolved may predate the recent-event limit. Include the exact
        # original record provided by the ledger, preserving its actual ID.
        records = {str(e["id"]): e for e in unresolved}
        records.update({str(e["id"]): e for e in events})
        self._unresolved = {str(e["id"]) for e in unresolved}
        self._events = records
        self.tree.delete(*self.tree.get_children())
        order = sorted(records, key=int, reverse=True)
        for iid in order:
            event = records[iid]
            stamp = str(event.get("created") or "").replace("T", " ", 1)
            self.tree.insert("", "end", iid=iid, values=(
                stamp, event.get("name") or "账本", event_title(event)))
        previous = next((iid for iid in selection if iid in records), None)
        focused = previous or next((iid for iid in order if iid in self._unresolved), None)
        focused = focused or next(iter(order), None)
        if focused:
            self.tree.selection_set(focused)
            self.tree.see(focused)
        self._selected()

    def _selected(self, _event=None):
        selection = self.tree.selection()
        iid = selection[0] if selection else None
        record = self._events.get(iid)
        pending_here = iid in self._unresolved
        label = "定位待核实人员" if pending_here or not self._unresolved else f"查看待核实记录（{len(self._unresolved)}）"
        self.locate.configure(text=label, state="normal" if self._unresolved else "disabled")
        operation = event_operation(record) if record else None
        rows, caption = self._operation_rows(operation) if operation else ([], "")
        read_only = DataStore._read_only_operation([event for event in rows if event.get("kind") == "action"])
        display = record
        if read_only and record.get("kind") == "action":
            display = {**record, "read_only": True}
            self.tree.set(iid, "event", event_title(display))
        detail = event_detail(display) if display else "暂无处理记录。"
        evidence = record.get("evidence") if record else None
        if isinstance(evidence, dict):
            detail += "　" + fee_facts(evidence) + "。"
        if pending_here:
            detail += "　可定位原人员，在“选中 Key 的操作”内核实平台状态。"
        if record and record.get("kind") == "action" and not operation:
            detail += "　旧记录缺少操作编号，仅显示本条。"
        self.summary.set(detail)
        fields = []
        if record:
            fields.append(f"记录 #{record['id']}")
            if operation:
                fields.append(f"原操作 {operation}")
            if record.get("tracking_id"):
                fields.append(f"Tracking ID {record['tracking_id']}")
            if record.get("source_id"):
                fields.append(f"费用来源 {record['source_id']}")
        self.reference.set("  ·  ".join(fields))
        self.raw_text.configure(state="normal")
        self.raw_text.delete("1.0", "end")
        if record:
            self.raw_text.insert("1.0", json.dumps(record, ensure_ascii=False, indent=2, default=str))
        self.raw_text.configure(state="disabled")
        self._show_timeline(rows, caption, read_only)

    def _operation_rows(self, operation):
        if operation in self._operation_cache:
            return self._operation_cache[operation], "本账本按发生顺序"
        caption = "仅显示当前列表中的事件"
        if self._read_operation:
            try:
                rows = self._read_operation(operation, period_id=self._ledger)
                if rows:
                    self._operation_cache[operation] = rows
                    return rows, "本账本按发生顺序"
            except GuardError:
                caption = "完整记录读取失败，仅显示当前列表中的事件"
        rows = sorted((event for event in self._events.values() if event_operation(event) == operation),
                      key=lambda event: event["id"])
        return rows, caption

    def _show_timeline(self, rows, caption, read_only):
        self.timeline_text.configure(state="normal")
        self.timeline_text.delete("1.0", "end")
        if not rows:
            self.timeline_text.configure(state="disabled")
            self.timeline.grid_remove()
            return
        self.timeline.grid()
        self.timeline.configure(text=f"同一操作的时间线 · {caption} · {len(rows)} 条")
        previous_evidence = None
        lines = []
        for event in rows:
            if read_only and event.get("kind") == "action":
                event = {**event, "read_only": True}
            stamp = str(event.get("created") or "").replace("T", " ", 1)
            line = f"#{event['id']}  {stamp}  {event_detail(event)}"
            evidence = event.get("evidence")
            if isinstance(evidence, dict) and evidence != previous_evidence:
                line += "  " + fee_facts(evidence) + "。"
                previous_evidence = evidence
            if event.get("inherited_from_period"):
                line += f"  继承自账本 {event['inherited_from_period']} 的记录 #{event.get('original_event_id', '未知')}。"
            lines.append(line)
        self.timeline_text.insert("1.0", "\n".join(lines))
        self.timeline_text.configure(state="disabled")

    def _locate(self):
        selection = self.tree.selection()
        if selection and selection[0] in self._unresolved:
            event = self._events[selection[0]]
            self.on_locate(event.get("operation_id"), event["tracking_id"])
        elif self._unresolved:
            iid = max(self._unresolved, key=int)
            self.tree.selection_set(iid)
            self.tree.see(iid)
            self._selected()
