"""Read-only first review of a ledger's fee and quota evidence."""
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import tkinter as tk
from tkinter import ttk

from .core import eligible_for_automatic_action


def amount_text(value) -> str:
    """Keep the full supplied precision, including tiny quota crossings."""
    try:
        number = Decimal(str(value))
        return format(number, "f") if number.is_finite() else "未知"
    except (ValueError, InvalidOperation):
        return "未知"


@dataclass(frozen=True)
class FeeReviewData:
    rows: tuple
    total_budget: Decimal
    people_count: int
    share: Decimal
    basis_text: str
    account_text: str
    observed_text: str
    identity_current: bool
    fees_current: bool
    coverage_text: str = ""

    def candidate(self, row):
        return self.identity_current and eligible_for_automatic_action(row, fees_current=self.fees_current)

    def state(self, row):
        status = row.get("status")
        if status == "unassigned":
            return "待填写 · 跳过"
        if status == "skipped":
            return "跳过管理"
        if status == "retired":
            return "已移除 · 留存账目"
        if status in {"recovery_pending", "present_unresolved"}:
            return "原操作待核实"
        if self.candidate(row):
            return "已达线 · 待程序复核"
        if not row.get("tracking_id"):
            return "待核对绑定"
        if not self.identity_current:
            return "待重新核验"
        if not self.fees_current or not row.get("complete"):
            return "费用待补全"
        return "额度内" if status == "ok" else "待核实"

    def managed(self, row):
        return bool(row.get("name")) and row.get("amount") is not None and row["amount"] >= 0

    def needs_fees(self, row):
        return (self.managed(row) and row.get("status") != "retired"
                and (not self.fees_current or row.get("complete") is not True))

    def counts(self):
        return (sum(self.managed(row) for row in self.rows),
                sum(row.get("status") in {"skipped", "unassigned"} for row in self.rows),
                sum(self.needs_fees(row) for row in self.rows),
                sum(self.candidate(row) for row in self.rows))

    @property
    def budget_text(self):
        return (f"公共人均额度  ¥{amount_text(self.share)}"
                f"  （总预算 ¥{amount_text(self.total_budget)} ÷ {self.people_count} 人，向下取整到元）")

    def expense(self, row):
        if row.get("spent") is None:
            return "未查询" if row.get("status") in {"skipped", "unassigned"} else "未知"
        amount = amount_text(row["spent"])
        if row.get("spent_is_lower_bound"):
            return f"≥ {amount}（下界）"
        if not self.fees_current or row.get("status") in {"retired", "skipped", "unassigned"}:
            return f"{amount}（历史）"
        return amount


class FeeReviewFrame(ttk.Frame):
    """A passive view. All amounts, IDs and evidence originate in the caller."""

    FILTERS = ("全部", "已达线", "费用待补全", "跳过", "待核实原操作")

    def __init__(self, parent, *, on_start=None):
        super().__init__(parent, padding=(14, 10))
        self.data = None
        self.columnconfigure(0, weight=1)
        self.rowconfigure(5, weight=1)
        self.context = tk.StringVar(self)
        self.budget = tk.StringVar(self)
        self.coverage = tk.StringVar(self)
        self.search = tk.StringVar(self)
        self.filter = tk.StringVar(self, self.FILTERS[0])
        self.detail = tk.StringVar(self)
        self.target_id = tk.StringVar(self)
        self.shown = tk.StringVar(self)
        self.values = [tk.StringVar(self, "—") for _ in range(4)]
        heading = ttk.Frame(self)
        heading.grid(row=0, column=0, sticky="ew")
        ttk.Label(heading, text="费用与额度核对", font=("Microsoft YaHei UI", 15, "bold")).pack(side="left")
        self.start_button = None
        if on_start is not None:
            self.start_button = ttk.Button(heading, text="启动全自动管理", command=on_start)
            self.start_button.pack(side="right")
        context = ttk.Label(self, textvariable=self.context, wraplength=850, justify="left")
        context.grid(row=1, column=0, sticky="w", pady=(3, 4))
        body = ttk.Frame(self)
        body.grid(row=2, column=0, sticky="ew", pady=(2, 6))
        for index, label in enumerate(("纳入管理", "跳过", "费用待补全", "达线待处理")):
            body.columnconfigure(index, weight=1, uniform="metric")
            group = ttk.Frame(body, padding=(6, 1))
            group.grid(row=0, column=index, sticky="ew")
            ttk.Label(group, text=label, style="Muted.TLabel").pack(anchor="w")
            ttk.Label(group, textvariable=self.values[index], font=("Microsoft YaHei UI", 17, "bold")).pack(anchor="w")
        info = ttk.Frame(self)
        info.grid(row=3, column=0, sticky="ew")
        budget = ttk.Label(info, textvariable=self.budget, wraplength=850, justify="left")
        budget.pack(anchor="w")
        coverage = ttk.Label(info, textvariable=self.coverage, style="Muted.TLabel", wraplength=850, justify="left")
        coverage.pack(anchor="w")
        tools = ttk.Frame(self)
        tools.grid(row=4, column=0, sticky="ew", pady=(8, 5))
        ttk.Label(tools, text="查看").pack(side="left")
        ttk.Combobox(tools, textvariable=self.filter, values=self.FILTERS,
                     state="readonly", width=15).pack(side="left", padx=(5, 12))
        ttk.Label(tools, text="名称或 Tracking ID").pack(side="left")
        ttk.Entry(tools, textvariable=self.search, width=25).pack(side="left", padx=(5, 8))
        ttk.Label(tools, textvariable=self.shown, style="Muted.TLabel").pack(side="right")
        table = ttk.Frame(self)
        table.grid(row=5, column=0, sticky="nsew")
        table.columnconfigure(0, weight=1)
        table.rowconfigure(0, weight=1)
        columns = ("name", "spent", "personal", "public", "state")
        self.tree = ttk.Treeview(table, columns=columns, show="headings", selectmode="browse", height=7)
        for col, title, width in zip(columns, ("Key 名称", "累计费用（元）", "个人额度（元）", "公共额度（元）", "本次核对"),
                                     (190, 165, 125, 125, 170)):
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, minwidth=85, anchor="w" if col in {"name", "state"} else "e")
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        horizontal = ttk.Scrollbar(table, orient="horizontal", command=self.tree.xview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.tree.bind("<<TreeviewSelect>>", self._select)
        identity = ttk.Frame(self)
        identity.grid(row=6, column=0, sticky="ew", pady=(6, 2))
        ttk.Label(identity, text="Tracking ID").pack(side="left")
        ttk.Entry(identity, textvariable=self.target_id, state="readonly").pack(side="left", fill="x", expand=True, padx=(8, 0))
        detail = ttk.Label(self, textvariable=self.detail, wraplength=850, justify="left")
        detail.grid(row=7, column=0, sticky="w")
        def wrap(event):
            width = max(180, event.width - 32)
            for label in (context, budget, coverage, detail):
                if int(label.cget("wraplength")) != width:
                    label.configure(wraplength=width)
        self.bind("<Configure>", wrap)
        self.search.trace_add("write", self._render_rows)
        self.filter.trace_add("write", self._render_rows)
        self.clear()

    def clear(self):
        self.data = None
        self.context.set("新建或打开账本，登录后读取费用。")
        self.budget.set("")
        self.coverage.set("")
        for variable in self.values:
            variable.set("—")
        self._render_rows()

    def show(self, data: FeeReviewData):
        if self.data == data:
            return
        previous_id = self.target_id.get()
        self.data = data
        self.context.set(f"{data.account_text}   ·   {data.basis_text}\n{data.observed_text}")
        self.budget.set(data.budget_text + "。个人或公共额度任一达线即进入复核。")
        coverage = data.coverage_text
        if any(data.needs_fees(row) and data.candidate(row) for row in data.rows):
            coverage += " 零额度且身份已核验的行可以同时计入待补全和达线待处理。"
        self.coverage.set(coverage.strip())
        for variable, count in zip(self.values, data.counts()):
            variable.set(str(count))
        self._render_rows(previous_id=previous_id)

    def _render_rows(self, *_event, previous_id=None):
        previous_id = self.target_id.get() if previous_id is None else previous_id
        view = self.tree.yview()
        self.tree.delete(*self.tree.get_children())
        self.target_id.set("")
        self.detail.set("选中一行可核对完整标识、达线依据和费用覆盖。")
        if self.data is None:
            self.shown.set("")
            return
        query = self.search.get().strip().casefold()
        selected_filter = self.filter.get()
        count = 0
        for index, row in enumerate(self.data.rows):
            name, key = row.get("name") or "", row.get("tracking_id") or ""
            if query and query not in name.casefold() and query not in key.casefold():
                continue
            state = self.data.state(row)
            if ((selected_filter == "已达线" and not self.data.candidate(row))
                    or (selected_filter == "费用待补全" and not self.data.needs_fees(row))
                    or (selected_filter == "跳过" and row.get("status") not in {"skipped", "unassigned"})
                    or (selected_filter == "待核实原操作" and row.get("status") not in {"recovery_pending", "present_unresolved"})):
                continue
            tag = ""
            if self.data.candidate(row):
                tag = "triggered"
            elif row.get("status") in {"skipped", "unassigned", "retired"}:
                tag = "skipped"
            elif state != "额度内":
                tag = "unknown"
            self.tree.insert("", "end", iid=str(index), values=(name or "（待填写）", self.data.expense(row) + " ",
                amount_text(row.get("amount")) + " ", amount_text(row.get("share")) + " ", state), tags=(tag,))
            count += 1
            if previous_id and key == previous_id:
                self.tree.selection_set(str(index))
        if view:
            self.tree.yview_moveto(view[0])
        self._select()
        self.shown.set(f"显示 {count} / {len(self.data.rows)} 行")

    def _select(self, _event=None):
        selection = self.tree.selection()
        if self.data is None or not selection:
            return
        row = self.data.rows[int(selection[0])]
        self.target_id.set(row.get("tracking_id") or "")
        reason = "；".join(row.get("reasons") or ())
        if row.get("spent_is_lower_bound"):
            reason += "。部分历史费用待补全，表内金额是已知下界"
        if not row.get("tracking_id"):
            reason += "。当前没有已绑定的活动 Tracking ID"
        if row.get("start_date") and row.get("end_date"):
            reason += f"。此 Key 的查询期间为 {row['start_date']} 至 {row['end_date']}（UTC+8）"
        self.detail.set(f"{row.get('name') or '待填写行'}　{self.data.state(row)}　{reason.strip('。')}".strip())
