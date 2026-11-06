"""Windows desktop workflow. All Tk and ledger access stays on the UI thread.

The browser adapter owns its own worker thread. The UI persists a deletion
intent before granting a single-use permit; unknown outcomes are never retried.
"""
from __future__ import annotations

from concurrent.futures import Future
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
import json
import os
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from typing import Any, Callable
from uuid import uuid4

from . import __version__
from .core import AccountMismatch, DataStore, GuardError, Person, SnapshotApplyError, clean_name, money
from .build_identity import diagnostic_build_identity
from .revocation_permit import KeyInspectionError, RevocationNotSubmitted
from .target_lock import TargetLockError
from .workspace import new_ledger_path, save_current_ledger
from .preferences import (DEFAULT_POLL_INTERVAL_SECONDS, load_poll_interval,
                          save_poll_interval, validate_poll_interval, TableDefaults,
                          load_table_defaults, save_table_defaults)
from .workbook_template import write_personnel_template
from .text_prompt import ask_text
from .appearance import (THEME_MODES, load_theme_mode, save_theme_mode,
                         resolve_theme, apply_theme)
from .window_layout import ScrollableContent, fit_window
from .scheduler import CandidateQueue

TZ = timezone(timedelta(hours=8))
POLL_MS = DEFAULT_POLL_INTERVAL_SECONDS * 1000
COLLECTION_MAX_AGE_SECONDS = 300
MANAGEMENT_CONTRACT_REASONS = frozenset({
    'KEY_KIND_INVALID', 'KEY_TARGET_KIND_UNSUPPORTED', 'KEY_TARGET_NOT_DELETABLE',
    'KEY_REDACTED_ID_INVALID', 'KEY_CREATED_AT_INVALID', 'KEY_NAME_INVALID',
    'KEY_TRACKING_ID_INVALID', 'KEY_LIST_IDENTITY_MISMATCH',
    'KEY_LIST_DUPLICATE_IDENTITY', 'KEY_LIST_SCHEMA_INVALID',
})
FEE_STATE_LABELS = {
    "NO_USAGE_DATA": "暂无明细，费用未知", "AUTH_REQUIRED": "需重新登录",
    "NO_SELECTED_USAGE_DATA": "受管 Key 暂无明细",
    "NO_MANAGED_KEYS": "无受管 Key，已跳过",
    "COST_SCOPE_MISMATCH": "平台返回范围不符",
    "SESSION_UNAVAILABLE": "会话已失效", "NETWORK_ERROR": "网络读取失败",
    "HTTP_ERROR": "平台读取失败", "PLATFORM_REJECTED": "平台拒绝读取",
    "PERIOD_INCOMPLETE": "账单范围不足", "PERIOD_MISMATCH": "账单范围不符",
    "CHARGE_TYPE_UNSUPPORTED": "费用类别待适配",
}
COLLECTION_RESULT_CODES = frozenset(FEE_STATE_LABELS) | {
    'OK', 'NOT_COLLECTED', 'SCHEMA_UNSUPPORTED', 'SOURCE_MISMATCH',
    'TIMEZONE_MISMATCH', 'KEY_ID_INVALID', 'KEY_ID_UNBOUND', 'INVALID_KEY_SCOPE',
    'BUCKET_MISALIGNED', 'BUCKET_UNSUPPORTED', 'DUPLICATE_COST_BUCKET',
    'AMOUNT_INVALID', 'CURRENCY_UNSUPPORTED', 'REDIRECT_REJECTED',
    'BODY_TOO_LARGE', 'CONTENT_TYPE_UNSUPPORTED',
}
COLLECTION_COVERAGE_FIELDS = (
    'bound_total_count', 'active_managed_count', 'fee_tracking_count',
    'matched_total_count', 'matched_active_count', 'matched_historical_count',
    'active_missing_count',
)
COLLECTION_REJECTION_LABELS = {
    'TARGET_IN_USE': ('其他实例占用', '平台读取已返回，但本机旧版本、其他管理器或退出清理进程仍占用目标；本次未绑定、未写入费用。请正常关闭旧管理器并等待清理完成，再在本窗口重新读取；无需改日期、清空账本或重新登录。'),
    'TARGET_LOCK_FAILED': ('目标保护不可用', '平台读取已返回，但本机目标保护未能建立；本次未绑定、未写入费用。请查看诊断并正常退出本程序后重开；未认定为其他实例占用。'),
    'COLLECTION_CONTEXT_CHANGED': ('读取期间设置已变化', '本次结果不属于当前账本或统计设置，未应用费用或绑定；请按当前设置重新读取。'),
    'NO_MANAGED_BINDINGS': ('尚无受管绑定', '费用已读取，但本账本没有可接收费用的绑定；请检查人员与 Key 绑定。'),
    'NO_BOUND_COST_MATCH': ('费用与绑定不匹配', '费用已读取，但没有匹配本账本的 Key 绑定；本次费用快照未更新，请核对绑定。'),
    'CUMULATIVE_DECREASE': ('累计费用下降', '本次累计小于已有记录，已保留原金额并暂停管理；请核对期间或账单修订。'),
    'SNAPSHOT_CONFLICT': ('费用来源冲突', '相同费用来源与既有内容不一致；已保留原记录，请核对来源。'),
    'PERIOD_CONFIG_MISMATCH': ('账本期间不符', '费用期间与当前账本不一致；本次费用快照未更新，请核对统计起点。'),
    'END_DATE_REGRESSION': ('账单截止日回退', '本次账单截止日早于已有统计；已保留原记录，请核对期间。'),
    'STALE_SNAPSHOT_REPLAY': ('返回旧费用来源', '平台返回的是历史费用来源，账本仍采用更新的快照；本次不能刷新自动管理资格，请重新读取。'),
    'LOCAL_CONTEXT_UNAVAILABLE': ('本地文件待恢复', '本地账本或人员表无法完成核验；已暂停管理，请恢复文件后重新读取。'),
    'SNAPSHOT_DATA_INVALID': ('费用数据待核查', '费用标识或金额未通过账本校验；本次费用快照未更新，请核对来源。'),
}
STATUS_LABELS = {
    "unassigned": "待填写姓名 · 不参与管理",
    "skipped": "跳过管理", "unknown": "等待有效费用", "ok": "尚未达到额度",
    "triggered": "达到额度 · 待处理", "prepared": "待你在平台确认",
    "pending": "待处理", "absent_verified": "平台列表已不存在 · 原因需核实",
    "failed": "处理失败", "unknown_result": "处理结果未知 · 待核实",
    "retired": "已从平台列表移除 · 保留账目",
    "recovery_pending": "上次操作待核实",
    "present_unresolved": "观察时仍存在 · 旧操作待核实",
    "not_submitted": "达到额度 · 准备失败，未提交",
}


def collection_is_fresh(observed_wall, observed_monotonic, end_date,
                        *, wall_now=None, monotonic_now=None) -> bool:
    """Reject old, cross-day or clock-discontinuous evidence before approval."""
    if observed_wall is None or observed_monotonic is None:
        return False
    wall_now = time.time() if wall_now is None else wall_now
    monotonic_now = time.monotonic() if monotonic_now is None else monotonic_now
    wall_age, monotonic_age = wall_now - observed_wall, monotonic_now - observed_monotonic
    return (0 <= wall_age < COLLECTION_MAX_AGE_SECONDS
            and 0 <= monotonic_age < COLLECTION_MAX_AGE_SECONDS
            and abs(wall_age - monotonic_age) <= 5
            and end_date == datetime.fromtimestamp(wall_now, TZ).date().isoformat())


def eligible_for_automatic_action(row: dict) -> bool:
    """A historical lower bound may be displayed, but cannot authorize deletion."""
    return (row.get("status") == "triggered" and bool(row.get("tracking_id"))
            and (row.get("amount") == Decimal(0) or row.get("share") == Decimal(0)
                 or row.get("complete") is True))


def amount_text(value: Any) -> str:
    """Display full supplied precision, rather than hide a threshold crossing."""
    if value is None:
        return "未知"
    try:
        number = Decimal(str(value))
        if not number.is_finite():
            return "未知"
        return format(number, "f")
    except (ValueError, InvalidOperation):
        return "未知"


def safe_error(exc: Exception | str) -> str:
    # External errors can contain credentials. Never put raw error URLs in logs.
    text = str(exc)
    text = re.sub(r"sk-[A-Za-z0-9_*.-]+", "[密钥已隐藏]", text)
    text = re.sub(r"(?i)(authorization|cookie|access_token|refresh_token)\s*[:=]\s*[^\s,;]+", r"\1=[已隐藏]", text)
    return text[:1200]


def has_saved_data(folder: Path) -> bool:
    """A leftover ledger/log is history, not permission to initialize again."""
    if not folder.exists():
        return False
    names = {"管理设置.json", "人员额度.xlsx", "状态.db", "状态.db-wal", "状态.db-shm"}
    for item in folder.iterdir():
        if item.name in names or item.name.startswith(("运行日志-", "运行摘要-", ".initial", ".初始化", "初始化")):
            return True
    return False


def parse_editor_person(name: str, amount: str) -> Person:
    name, amount = name.strip(), amount.strip()
    if not amount or amount.startswith("="):
        raise ValueError("个人金额必须填写数字；暂不管理填 -1。")
    try:
        number = Decimal(amount)
    except InvalidOperation as exc:
        raise ValueError("个人金额不是有效数字。") from exc
    if not number.is_finite() or (number < 0 and number != -1):
        raise ValueError("金额只允许 -1、0 或正数。")
    if number != -1 and not name:
        raise ValueError("金额为 0 或正数时，必须填写 API 名/人员名。")
    return Person(name, number)


def prepare_initial_draft(rows) -> tuple[list[Person], int]:
    """Only a new-table draft may leave unnamed rows unassigned at save time."""
    people, unassigned = [], 0
    for name, amount in rows:
        name = clean_name(name)
        number = money(amount, allow_skip=True)
        if not name and number != -1:
            number = Decimal(-1)
            unassigned += 1
        people.append(Person(name, number))
    return people, unassigned


class App:
    def __init__(self, root: tk.Tk, folder: Path, home_folder: Path | None = None, *, responsibility_directory=None):
        self.root = root
        self.folder = Path(folder).resolve()
        self.home_folder = Path(home_folder or folder).resolve()
        from .responsibility_gate import ResponsibilityGate
        self.responsibility_gate = ResponsibilityGate(responsibility_directory)
        from .instance import FolderLock
        self.folder_lock = FolderLock(self.folder)
        self.target_lock = None
        self.recovery_mode = False
        self.store: DataStore | None = None
        self.config = None
        self.browser = None
        self.busy = False
        self.interrupting = False
        self.cleanup_failed = False
        self.audit_failed = False
        self.audit_pending = []
        self.candidate_queue = CandidateQueue()
        self.account_mismatch = None
        self.current_action = None
        self.interrupt_future = None
        self.collection_has_fees = False
        self.collection_source_id = ''
        self.collection_reason_code = ''
        self.session_generation = 0
        self.session_ready = False
        self.collection_context = None
        self.collection_observed_wall = None
        self.collection_observed_monotonic = None
        self.collection_end_date = None
        self.action_states = {}
        self.button_labels = {}
        self.pending_login = False
        self.pending_start = None
        self.start_refresh_required = False
        self.reset_on_login = False
        self.pending_change = None
        self.observing = False
        self.automatic = False
        self.single_revoke_id = None
        self.management_generation = 0
        self.delete_permit = None
        self.retry_collection = True
        self.closing = False
        self.timer = None
        self.poll_interval_seconds, self.poll_preference_notice = load_poll_interval(self.home_folder)
        self.table_defaults, self.table_defaults_notice = load_table_defaults(self.home_folder)
        self.theme_mode, self.theme_notice = load_theme_mode(self.home_folder)
        self.current_theme = None
        self.theme_timer = None
        self.poll_timer_generation = 0
        self.poll_wait_started = None
        self.rows: list[dict] = []
        self.last_keys: list[dict] = []
        self.validated_hash: str | None = None
        self.checked_workbook_signature = None
        from .diagnostics import DiagnosticLog
        self.diagnostics_dir = self.home_folder / "临时诊断日志"
        self.diagnostics = DiagnosticLog(self.diagnostics_dir)
        self._diagnostic("ui_opened", **diagnostic_build_identity())
        self.root.title(f"DeepSeek 额度管理 · 本地 AI {__version__} · 预览版")
        fit_window(self.root)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        self._refresh_theme()
        self.theme_timer = self.root.after(5000, self._watch_theme)
        self.root.after(60, self.load_folder)
        self.root.after(1000, self._watch_workbook)

    def _build(self):
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Treeview", rowheight=31, font=("Microsoft YaHei UI", 10))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 19, "bold"))
        style.configure("Muted.TLabel", foreground="#526174")
        style.configure("Warn.TLabel", foreground="#915212")
        self.root.option_add("*Font", ("Microsoft YaHei UI", 10))
        self.main_content = ScrollableContent(self.root, padding=18)
        self.main_content.pack(fill="both", expand=True)
        outer = self.main_content.content
        ttk.Label(outer, text="DeepSeek 额度管理 · 本地 AI", style="Title.TLabel").pack(anchor="w")
        ttk.Label(outer, text="核对平台费用 · 按人员核算 · 保留处理记录", style="Muted.TLabel").pack(anchor="w", pady=(3, 12))
        self.path_var = tk.StringVar(value=f"数据文件夹：{self.folder}")
        self.main_content.wrap(ttk.Label(outer, textvariable=self.path_var, wraplength=1040)).pack(anchor="w")
        self.summary_var = tk.StringVar(value="正在检查已有数据……")
        self.main_content.wrap(ttk.Label(outer, textvariable=self.summary_var, wraplength=1040)).pack(anchor="w", pady=(8, 8))
        bar = ttk.Frame(outer)
        bar.pack(fill="x", pady=(0, 7))
        self.buttons = {}
        specs = [
            ("folder", "数据文件夹", self.choose_folder),
            ("setup", "新建列表", self.setup),
            ("date", "修改统计起点", self.change_start_date),
            ("template", "保存 Excel 模板", self.save_template),
            ("import_people", "导入人员 Excel", self.import_people),
            ("import", "检查官方导出", self.import_billing),
        ]
        for key, text, command in specs:
            b = ttk.Button(bar, text=text, command=self._button_command(key, command))
            b.pack(side="left", padx=(0, 7))
            self.buttons[key] = b
        sheet_bar = ttk.Frame(outer)
        sheet_bar.pack(fill="x", pady=(0, 7))
        for key, text, command in [("edit", "填写两列表格", self.edit_people),
                                   ("excel", "用 Excel/WPS 打开", self.open_excel),
                                   ("check", "检查表格", self.check_people),
                                   ("inspect", "检查删除准备（只读）", self.inspect_revoke),
                                   ("responsibility", "建立本机责任范围", self.initialize_responsibility)]:
            button = ttk.Button(sheet_bar, text=text, command=self._button_command(key, command))
            button.pack(side="left", padx=(0, 7))
            self.buttons[key] = button
        bar2 = ttk.Frame(outer)
        bar2.pack(fill="x", pady=(0, 10))
        for key, text, command in [
            ("login", "登录 / 重新登录", self.login),
            ("collect", "立即读取费用", self.collect),
            ("start", "启动全自动管理", self.start),
            ("stop", "停止管理", self.stop),
            ("revoke", "强制删除选中 Key", self.revoke_selected_once),
            ("verify", "核实平台状态", self.verify_revoke),
        ]:
            b = ttk.Button(bar2, text=text, command=self._button_command(key, command))
            b.pack(side="left", padx=(0, 7))
            self.buttons[key] = b
        operation_bar = ttk.Frame(outer)
        operation_bar.pack(fill="x", pady=(0, 8))
        self.buttons["interrupt"] = ttk.Button(operation_bar, text="中断当前操作",
            command=self._button_command("interrupt", self.interrupt_operation))
        self.buttons["interrupt"].pack(side="left", padx=(0, 8))
        self.buttons["diagnostics"] = ttk.Button(operation_bar, text="诊断日志 / 本地 AI",
            command=self._button_command("diagnostics", self.open_diagnostics))
        self.buttons["diagnostics"].pack(side="left", padx=(0, 12))
        self.buttons["save_audit"] = ttk.Button(operation_bar, text="保存处理记录",
            command=self._button_command("save_audit", self.retry_audit))
        self.buttons["save_audit"].pack(side="left", padx=(0, 8))
        self.buttons["account_ledger"] = ttk.Button(operation_bar, text="为当前账户建账本",
            command=self._button_command("account_ledger", self.resolve_account_mismatch))
        self.buttons["account_ledger"].pack(side="left")
        poll_bar = ttk.Frame(outer)
        poll_bar.pack(fill="x", pady=(0, 8))
        ttk.Label(poll_bar, text="自动管理间隔").pack(side="left")
        self.poll_interval_var = tk.StringVar(value=str(self.poll_interval_seconds))
        self.poll_interval_entry = ttk.Entry(poll_bar, textvariable=self.poll_interval_var, width=7)
        self.poll_interval_entry.pack(side="left", padx=(7, 4))
        self.poll_interval_entry.bind('<Return>', lambda _event: self.apply_poll_interval())
        ttk.Label(poll_bar, text="秒").pack(side="left", padx=(0, 7))
        self.buttons['poll_interval'] = ttk.Button(poll_bar, text="保存间隔", command=self.apply_poll_interval)
        self.buttons['poll_interval'].pack(side="left", padx=(0, 10))
        self.poll_feedback_var = tk.StringVar(value=self.poll_preference_notice or '每轮处理完成后等待；可设置 1～3600 秒。')
        ttk.Label(poll_bar, textvariable=self.poll_feedback_var, style='Muted.TLabel').pack(side="left")
        defaults_bar = ttk.Frame(outer)
        defaults_bar.pack(fill="x", pady=(0, 5))
        ttk.Label(defaults_bar, text="斩杀线").pack(side="left")
        self.kill_line_var = tk.StringVar(value=amount_text(self.table_defaults.kill_line))
        ttk.Entry(defaults_bar, textvariable=self.kill_line_var, width=22).pack(side="left", padx=(7, 4))
        ttk.Label(defaults_bar, text="元　新表默认填").pack(side="left")
        self.table_fill_var = tk.StringVar(value='-1（跳过管理）' if self.table_defaults.fill_mode == 'skip' else '斩杀线')
        ttk.Combobox(defaults_bar, textvariable=self.table_fill_var, state='readonly', width=17,
                     values=('-1（跳过管理）', '斩杀线')).pack(side="left", padx=7)
        self.buttons['table_defaults'] = ttk.Button(defaults_bar, text="保存新表默认", command=self.apply_table_defaults)
        self.buttons['table_defaults'].pack(side="left")
        self.theme_button = ttk.Button(defaults_bar, command=self.cycle_theme)
        self.theme_button.pack(side="right")
        self.defaults_feedback_var = tk.StringVar(value=self.table_defaults_notice or '斩杀线最小为 0，仅预填新表个人额度；已有人员额度不变。')
        self.main_content.wrap(ttk.Label(outer, textvariable=self.defaults_feedback_var, style='Muted.TLabel', wraplength=1100)).pack(anchor='w', pady=(0, 8))
        self.main_content.wrap(ttk.Label(outer, text="强制删除只处理选中的 Key，不要求达到额度；全自动管理会周期读取并自动永久撤销达额 Key，无需逐枚人工审批。未填写姓名的行不参与管理。“立即读取”只核对数据。账单延迟与在途请求仍可能超支；真实自动撤销尚待验证。", style="Warn.TLabel", wraplength=1040)).pack(anchor="w", pady=(0, 10))
        self.collection_summary_var = tk.StringVar(value="费用核验：尚未读取。")
        self.main_content.wrap(ttk.Label(outer, textvariable=self.collection_summary_var,
                                        wraplength=1040)).pack(anchor="w", pady=(0, 8))
        self.queue_summary_var = tk.StringVar(value='候选队列尚未运行。')
        self.main_content.wrap(ttk.Label(outer, textvariable=self.queue_summary_var,
                                        wraplength=1040)).pack(anchor='w', pady=(0, 4))
        self.forecast_summary_var = tk.StringVar(value='预测记录尚未读取费用。')
        self.main_content.wrap(ttk.Label(outer, textvariable=self.forecast_summary_var,
                                        style='Muted.TLabel', wraplength=1040)).pack(anchor='w', pady=(0, 8))
        tabs = ttk.Notebook(outer)
        tabs.pack(fill="both", expand=True)
        overview = ttk.Frame(tabs, padding=8)
        reports = ttk.Frame(tabs, padding=8)
        tabs.add(overview, text="人员与额度")
        tabs.add(reports, text="处理汇报")
        columns = ("name", "amount", "share", "spent", "status")
        self.tree = ttk.Treeview(overview, columns=columns, show="headings", selectmode="browse", height=7)
        for col, title, width in zip(columns, ["API名 / 人员名", "个人额度（元）", "公共额度（元）", "累计费用（元）", "当前状态"], [190, 150, 150, 150, 270]):
            self.tree.heading(col, text=title)
            self.tree.column(col, width=width, minwidth=90, anchor="w")
        scroll = ttk.Scrollbar(overview, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.tag_configure("triggered", foreground="#a52323")
        self.tree.tag_configure("unknown", foreground="#94600a")
        self.tree.tag_configure("skipped", foreground="#758192")
        self.tree.bind("<<TreeviewSelect>>", self._selection)
        self.detail_var = tk.StringVar(value="-1：跳过管理　　0：零额度，进入停用流程　　正数：达到个人或公共额度即触发")
        ttk.Label(overview, textvariable=self.detail_var, wraplength=1000).pack(anchor="w", pady=(8, 0))
        report_bar = ttk.Frame(reports)
        report_bar.pack(fill="x", pady=(0, 6))
        self.buttons["reports"] = ttk.Button(report_bar, text="刷新汇报", command=self._button_command("reports", lambda: self.refresh_events(show_status=True)))
        self.buttons["reports"].pack(side="left")
        self.buttons["logs"] = ttk.Button(report_bar, text="打开日志文件夹", command=self._button_command("logs", self.open_folder))
        self.buttons["logs"].pack(side="left", padx=8)
        self.report_text = tk.Text(reports, wrap="word", state="disabled", relief="flat", bg="#f6f8fa", padx=12, pady=10, height=8)
        report_scroll = ttk.Scrollbar(reports, command=self.report_text.yview)
        self.report_text.configure(yscrollcommand=report_scroll.set)
        report_scroll.pack(side="right", fill="y")
        self.report_text.pack(fill="both", expand=True)
        self.status_var = tk.StringVar(value="尚未启动观察")
        self.main_content.wrap(ttk.Label(outer, textvariable=self.status_var, wraplength=1040)).pack(anchor="w", pady=(10, 0))
        self.progress_var = tk.StringVar(value="当前操作：空闲")
        self.main_content.wrap(ttk.Label(outer, textvariable=self.progress_var, style="Muted.TLabel", wraplength=1040)).pack(anchor="w", pady=(4, 0))
        self.audit_var = tk.StringVar(value="")
        self.main_content.wrap(ttk.Label(outer, textvariable=self.audit_var, style="Warn.TLabel", wraplength=1040)).pack(anchor="w")
        self._update_buttons()

    def _update_buttons(self):
        if self.browser is not None and getattr(self.browser, 'cleanup_failed', False):
            self.cleanup_failed = True
        changing = self.pending_login or self.pending_change is not None or self.pending_start is not None or self.interrupting or self.cleanup_failed or self.audit_failed
        ready = self.config is not None and not self.busy and not self.closing and not changing
        for key, button in self.buttons.items():
            self.button_labels.setdefault(key, str(button.cget("text")))
            label = self.button_labels[key]
            state = self.action_states.get(key)
            if key == "start":
                label = ("启动前：正在重新读取" if self.pending_start is not None else
                         "全自动管理：已开启" if self.observing else ("全自动管理：已停止" if state == "已停止" else "启动全自动管理"))
            elif state:
                label = {"login": "登录", "collect": "读取费用"}.get(key, label) + "：" + state
            enabled = ready
            if key in {"folder", "setup"}:
                enabled = not self.busy and not self.closing and not changing
            if key == "setup":
                enabled = enabled and not self.recovery_mode and self.single_revoke_id is None
            if key == "stop":
                enabled = (self.observing or self.single_revoke_id is not None or self.pending_start is not None) and not self.closing
            if key == "start":
                enabled = ready and not self.observing
            if key in {"edit", "excel", "check", "import_people"}:
                enabled = ready and not self.observing
            if key == "login":
                enabled = self.config is not None and not self.closing and self.pending_change is None and not self.interrupting and not self.cleanup_failed and not self.audit_failed
            if key == "date":
                enabled = self.config is not None and not self.closing and not changing
            if key == "template":
                enabled = not self.closing
            if key == "diagnostics":
                enabled = not self.closing
            if key in {'poll_interval', 'table_defaults'}:
                enabled = not self.closing
            if key in {"revoke", "verify", "inspect"}:
                enabled = ready and not self.observing
            if key == "interrupt":
                enabled = not self.closing and not self.interrupting and (self.busy or self.observing or self.browser is not None or self.cleanup_failed)
            if key == "save_audit":
                enabled = self.audit_failed and not self.busy and not self.interrupting and not self.closing
            if key == "account_ledger":
                enabled = ready and self.account_mismatch is not None
            if self.recovery_mode and key in {"collect", "start", "revoke", "inspect", "date", "edit", "import", "account_ledger"}:
                enabled = False
            if self.recovery_mode and key == "import_people":
                label = "恢复人员 Excel"
            button.configure(text=label, state="normal" if enabled else "disabled")

    def _refresh_theme(self):
        theme = resolve_theme(self.theme_mode)
        if theme != self.current_theme:
            apply_theme(self.root, theme)
            self.current_theme = theme
        label = {'system': '跟随系统', 'light': '浅色', 'dark': '深色'}[self.theme_mode]
        if self.theme_mode == 'system':
            label += '（深色）' if theme == 'dark' else '（浅色）'
        self.theme_button.configure(text='外观：' + label)

    def _watch_theme(self):
        self.theme_timer = None
        if self.closing:
            return
        if self.theme_mode == 'system':
            self._refresh_theme()
        self.theme_timer = self.root.after(5000, self._watch_theme)

    def cycle_theme(self):
        if self.closing:
            return
        mode = THEME_MODES[(THEME_MODES.index(self.theme_mode) + 1) % len(THEME_MODES)]
        try:
            save_theme_mode(self.home_folder, mode)
        except (ValueError, OSError) as exc:
            self.status_var.set('外观未保存，继续使用原模式：' + safe_error(exc))
            return
        self.theme_mode = mode
        self._refresh_theme()
        self.status_var.set('外观已保存。点击外观按钮可在跟随系统、浅色、深色之间切换。')

    def _set_table_defaults(self, line, mode):
        defaults = save_table_defaults(self.home_folder, line, mode)
        self.table_defaults = defaults
        self.kill_line_var.set(amount_text(defaults.kill_line))
        self.table_fill_var.set('-1（跳过管理）' if defaults.fill_mode == 'skip' else '斩杀线')
        self.defaults_feedback_var.set(f'已保存：斩杀线 {amount_text(defaults.kill_line)} 元，新表预填 {amount_text(defaults.amount)}；已有人员额度不变。')
        return defaults

    def apply_table_defaults(self):
        if self.closing:
            return
        try:
            mode = {'-1（跳过管理）': 'skip', '斩杀线': 'kill_line'}[self.table_fill_var.get()]
            self._set_table_defaults(self.kill_line_var.get(), mode)
        except (ValueError, OSError, KeyError) as exc:
            self.defaults_feedback_var.set('新表默认未保存：' + safe_error(exc))

    def apply_poll_interval(self):
        """Save local timing preferences without changing any ledger evidence."""
        if self.closing:
            return
        try:
            interval = validate_poll_interval(self.poll_interval_var.get())
            save_poll_interval(self.home_folder, interval)
        except (ValueError, OSError) as exc:
            self.poll_feedback_var.set(str(exc) if isinstance(exc, ValueError) else '保存失败，仍沿用原间隔；请检查数据目录是否可写。')
            return
        self.poll_interval_seconds = interval
        self.poll_interval_var.set(str(interval))
        # A running request finishes normally. Only an existing wait is moved;
        # completion of the current cycle will use the latest saved interval.
        waiting = self.timer is not None
        if waiting:
            self._schedule_collection_poll(new_cycle=False)
        if self.observing and self.busy:
            detail = '本轮完成后按新间隔等待。'
        elif self.observing and waiting:
            detail = '已按上轮完成时间重新安排下一轮。'
        else:
            detail = '每轮处理完成后等待；保存不会启动管理。'
        self.poll_feedback_var.set(f'已保存 {interval} 秒。' + detail)

    def _cancel_collection_poll(self, *, reset_wait=True):
        self.poll_timer_generation += 1
        if self.timer is not None:
            self.root.after_cancel(self.timer)
            self.timer = None
        if reset_wait:
            self.poll_wait_started = None

    def _schedule_collection_poll(self, *, new_cycle=True):
        """One non-overlapping wait, measured from completion of the last cycle."""
        self._cancel_collection_poll(reset_wait=False)
        if (not self.automatic or not self.observing or self.closing or self.recovery_mode
                or self.interrupting or self.cleanup_failed or self.audit_failed
                or self.pending_login or self.pending_change is not None):
            self.poll_wait_started = None
            return
        if self.busy:
            self.poll_wait_started = None
            return
        if new_cycle or self.poll_wait_started is None:
            self.poll_wait_started = time.monotonic()
        delay_ms = max(0, int((self.poll_wait_started + self.poll_interval_seconds - time.monotonic()) * 1000 + .999))
        timer_generation = self.poll_timer_generation
        management_generation = self.management_generation
        def read_next():
            if (timer_generation != self.poll_timer_generation
                    or management_generation != self.management_generation):
                return
            self.timer = None
            self.poll_wait_started = None
            if (self.busy or not self.automatic or not self.observing or self.closing
                    or self.recovery_mode or self.pending_login or self.pending_change is not None
                    or self.interrupting or self.cleanup_failed or self.audit_failed):
                return
            self.collect()
        self.timer = self.root.after(delay_ms, read_next)

    def _claim_targets(self, keys=(), *, recovery_ids=()):
        """Keep verified identities reserved until this window safely closes.

        Reserve the whole observed list, not merely the chosen workbook subset.
        A stop/relogin cannot release a manually opened platform operation.
        """
        from .target_lock import TargetLock
        targets = {row['tracking_id'] for row in keys}
        targets.update(recovery_ids)
        targets.update(self.store.active_tracking_ids(require_workbook=False))
        for action in self.store.unresolved_actions():
            targets.add(action['tracking_id'])
            # Restarted windows must also reserve the original independent
            # controls, including ones never bound to a workbook person.
            targets.update(self.store.recovery_tracking_ids(
                action['tracking_id'], action.get('operation_id')))
        if self.target_lock is None:
            # Retain the cleanup owner before the first resource acquisition:
            # even a failed initial batch plus failed rollback can be closed.
            self.target_lock = TargetLock()
        self.target_lock.update(self.target_lock.tracking_ids | targets)

    def _target_is_claimed(self, tracking_id):
        return self.target_lock is not None and self.target_lock.covers({tracking_id})

    def _enter_recovery(self):
        self.recovery_mode = True
        self.stop(quiet=True)
        self.validated_hash = None
        self._invalidate_collection(session=True)
        self.action_states['check'] = '待恢复'
        self.summary_var.set('人员表不可用 · 受限恢复模式。账本与设置已核验；可登录核实上次操作、查看记录或恢复人员表。')
        self.status_var.set('请选中待核实记录后点击“核实平台状态”，或点击“恢复人员 Excel”。恢复期间不读取费用、不建立新撤销。')
        self._update_buttons()

    def _diagnostic(self, event: str, **fields):
        try:
            self.diagnostics.emit(event, **fields)
        except Exception:
            # Diagnostics never decide or block a ledger operation.
            pass

    def _button_command(self, key: str, command: Callable):
        def invoke():
            self._diagnostic("button_started", action=key, busy=self.busy, automatic=self.observing)
            try:
                result = command()
            except Exception as exc:
                self._diagnostic("button_failed", action=key, error_type=type(exc).__name__)
                raise
            self._diagnostic("button_returned", action=key, busy=self.busy, automatic=self.observing)
            return result
        return invoke

    def _action_state(self, key: str, state: str):
        self.action_states[key] = state
        self._diagnostic("action_state", action=key, status=state)
        self._update_buttons()

    def _audit_notice(self):
        if any(item['action'] == 'collect' for item in self.audit_pending):
            message = ("费用已保存在统计库，日志导出或本地刷新尚未完成。恢复文件访问后点击“保存处理记录”完成本地恢复；"
                       "不会再次应用费用或操作平台。自动管理保持停止，恢复后仍须重新读取费用。")
        else:
            message = f"有 {len(self.audit_pending)} 项处理结果尚未完整保存。请恢复数据目录写入后点击“保存处理记录”；此操作仅保存账本，不会再次操作平台。"
        self.audit_var.set(message)
        self.status_var.set(message)
        self._action_state("save_audit", "待保存")

    def _queue_audit(self, callback: Callable, *, action: str, result=None, preserve_session=False):
        self.audit_pending.append({"callback": callback, "action": action, "result": result})
        self.audit_failed = True
        self.pending_login = self.reset_on_login = False
        self.pending_change = None
        self.stop(quiet=True)
        self._invalidate_collection(session=not preserve_session)
        self._diagnostic("audit_save_pending", action=action, count=len(self.audit_pending))
        self._audit_notice()

    def retry_audit(self):
        if not self.audit_pending or self.busy or self.interrupting or self.closing:
            return
        self.stop(quiet=True)
        collection_recovery = any(item['action'] == 'collect' for item in self.audit_pending)
        self._action_state("save_audit", "保存中")
        while self.audit_pending:
            item = self.audit_pending[0]
            try:
                item["callback"]()
            except Exception as exc:
                self._diagnostic("audit_save_failed", action=item["action"], error_type=type(exc).__name__)
                self._audit_notice()
                return
            self.audit_pending.pop(0)
        self.audit_failed = False
        self.audit_var.set("")
        self._action_state("save_audit", "已完成")
        self.status_var.set("费用日志与本地界面已恢复。自动管理保持停止；请重新读取费用后再决定是否启动。"
                            if collection_recovery else
                            "处理记录已保存。自动管理保持停止；请先核实平台状态，再决定是否重新登录或关闭。")
        self._diagnostic("audit_save_completed")

    def resolve_account_mismatch(self):
        if self.recovery_mode or self.account_mismatch is None or self.busy or self.audit_failed or self.interrupting or self.closing:
            return
        self._offer_account_ledger(self.account_mismatch)

    def _invalidate_collection(self, *, session=False):
        self.action_states.pop('inspect', None)
        self.collection_context = None
        self.collection_observed_wall = None
        self.collection_observed_monotonic = None
        self.collection_end_date = None
        self.collection_has_fees = False
        self.collection_source_id = ''
        self.collection_reason_code = ''
        self.action_states["collect"] = "待重新读取"
        self._show_collection_summary('费用核验：待重新读取；已有金额可能来自旧快照，不代表本次已核验。')
        if session:
            self.pending_start = None
            self.start_refresh_required = False
            self.session_ready = False
            self.action_states["login"] = "待核验"
        self._update_buttons()

    def _show_collection_summary(self, text):
        variable = getattr(self, 'collection_summary_var', None)
        if variable is not None:
            variable.set(text)

    def _saved_fee_fact(self):
        """A historical UI fact only; never grants or refreshes permission."""
        fact = getattr(self, 'last_saved_fee_fact', None)
        if not fact or self.store is None:
            return ''
        try:
            config = self.store.load()
            if (fact['folder'] != self.folder or fact['period_id'] != config.period_id
                    or fact['source_id'] != self.store.current_snapshot_id()):
                return ''
        except Exception:
            return ''
        when = datetime.fromtimestamp(fact['observed_wall'], TZ).strftime('%m-%d %H:%M:%S')
        return (f"最近费用已保存：{fact['start_date']} 至 {fact['end_date']}，"
                f"读取于 {when}（UTC+8）。")

    @staticmethod
    def _coverage_text(summary):
        """Display only anonymous counts; this text never grants authority."""
        values = {}
        for field in ('platform_key_count', 'fee_tracking_count', 'matched_platform_count',
                      'unmatched_fee_count', 'case_only_match_count'):
            value = (summary or {}).get(field) if isinstance(summary, dict) else None
            if type(value) is int and 0 <= value <= 1_000_000:
                values[field] = value
        required = {'platform_key_count', 'fee_tracking_count', 'matched_platform_count'}
        if not required.issubset(values):
            return ''
        text = (f"平台列表 {values['platform_key_count']} 枚，费用记录涉及 {values['fee_tracking_count']} 枚，"
                f"两者精确匹配 {values['matched_platform_count']} 枚。")
        if values.get('case_only_match_count', 0):
            text += ' 检测到 UUID 大小写表示差异，未自动合并，请核对诊断。'
        return text

    def _collection_context(self):
        config = self.store.load()
        return (self.session_generation, self.folder, config.period_id, config.account_id,
                config.start_date, config.people_count, config.total_budget, self.store.workbook_hash())

    def _remember_collection(self, result, *, application=None):
        # Publish only after every fallible read succeeds. An observation of S1
        # must never make a later ledger snapshot S2 eligible for deletion.
        context = self._collection_context()
        source = ''
        if application is not None:
            source = application.observed_source_id
            if (not application.fees_current or application.export_state != 'complete'
                    or self.store.current_snapshot_id() != source):
                raise GuardError('本次观察的费用不是当前已完成保存的快照，请重新读取。')
        end_date = result['end_date']
        observed_wall = result['collection_started_wall']
        observed_monotonic = result['collection_started_monotonic']
        if not collection_is_fresh(observed_wall, observed_monotonic, end_date):
            raise GuardError('本地处理期间读取已过期或跨日，请重新读取。')
        self.collection_context = context
        self.start_refresh_required = False
        self.collection_end_date = end_date
        # The worker's timestamps prevent a queued result becoming "fresh"
        # merely because the UI resumed after sleep or a blocked event loop.
        self.collection_observed_wall = observed_wall
        self.collection_observed_monotonic = observed_monotonic
        self.collection_source_id = source
        self.collection_has_fees = application is not None
        if application is not None:
            self.last_saved_fee_fact = dict(folder=self.folder, period_id=context[2],
                source_id=source, start_date=context[4], end_date=end_date,
                observed_wall=observed_wall)

    def _collection_is_current(self):
        return (self.collection_context is not None
                and self.collection_context == self._collection_context()
                and collection_is_fresh(getattr(self, 'collection_observed_wall', None),
                                        getattr(self, 'collection_observed_monotonic', None),
                                        getattr(self, 'collection_end_date', None))
                and (not self.collection_has_fees
                     or bool(self.collection_source_id)
                     and self.collection_source_id == self.store.current_snapshot_id()))

    def _watch_workbook(self):
        if self.closing:
            # A failed cleanup may keep this same window open for recovery.
            self.root.after(1000, self._watch_workbook)
            return
        try:
            if self.store is not None and self.validated_hash is not None:
                stat = self.store.workbook_path.stat()
                signature = (stat.st_mtime_ns, stat.st_size)
                if self.checked_workbook_signature is not None and signature != self.checked_workbook_signature:
                    if self.store.workbook_hash() != self.validated_hash:
                        self.stop(quiet=True)
                        self.validated_hash = None
                        self._invalidate_collection()
                        self._action_state("check", "待重新检查")
                        self.status_var.set("人员表已变化，自动管理已停止。请重新检查表格并读取费用。")
                        self._diagnostic("workbook_validation_invalidated")
                        self.refresh_rows()
                self.checked_workbook_signature = signature
        except OSError:
            self.stop(quiet=True)
            self.validated_hash = None
            self._invalidate_collection()
            self._action_state("check", "待重新检查")
            self.status_var.set("当前人员表不可读取，自动管理已停止。请检查文件是否保存并重新检查表格。")
            try:
                self.refresh_rows()
            except GuardError as exc:
                self.config = None
                self.error('账本需要恢复', exc, dialog=False)
                self._update_buttons()
        except GuardError as exc:
            self.stop(quiet=True)
            self.validated_hash = None
            self._invalidate_collection()
            self.error('数据需要核查', exc, dialog=False)
        finally:
            self.root.after(1000, self._watch_workbook)

    def error(self, title: str, exc: Exception | str, *, dialog=True, next_step=""):
        self.status_var.set(f"{title}：{safe_error(exc)}" + (" " + next_step if next_step else ""))
        if dialog and not self.closing:
            messagebox.showerror(title, safe_error(exc), parent=self.root)

    def load_folder(self):
        self.config = None
        self.recovery_mode = False
        self.validated_hash = None
        self.candidate_queue.reset()
        self.queue_summary_var.set('候选队列尚未运行。')
        self.forecast_summary_var.set('当前账本的预测记录尚未读取费用。')
        self.action_states.clear()
        self._invalidate_collection(session=True)
        self.path_var.set(f"数据文件夹：{self.folder}")
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            existing = has_saved_data(self.folder)
            self.store = DataStore(self.folder)
            if not existing:
                self.summary_var.set("首次运行：先填写人数和总预算，然后生成两列表格。")
                self.status_var.set("没有既有记录。请选择“新建列表”，或打开已有数据文件夹。")
                self._update_buttons()
                return
            self.config = self.store.load(require_workbook=False)
            self._show_config()
            self.refresh_rows()
            self.refresh_events()
            self.action_states["folder"] = "已完成"
            self.action_states["setup"] = "已完成"
            if not self.recovery_mode:
                self.status_var.set("已恢复原有统计。请检查表格并读取平台费用；不会自动执行任何删除。")
            try:
                # Complete only an already durable local terminal; no browser
                # request or new permission is produced by reopening a ledger.
                self.responsibility_gate.reconcile(self.store)
            except GuardError as exc:
                self.status_var.set('账本已载入；共享责任需核查，读取与核实仍可用。' + safe_error(exc))
        except Exception as exc:
            self.config = None
            self.summary_var.set("已有数据需要核查；未覆盖文件，未清零统计。")
            self.error("无法恢复数据", exc)
        self._update_buttons()

    def _show_config(self):
        c = self.config
        self.summary_var.set(f"分配人数 {c.people_count}　｜　本期总预算 ¥{amount_text(c.total_budget)}　｜　公共人均额度 ¥{amount_text(c.share)}　｜　起点 {c.start_date}（UTC+8）")

    def initialize_responsibility(self):
        """One explicit migration declaration, never per-key authorisation."""
        if not self.config or self.busy or self.observing or self.closing or self.audit_failed:
            return
        stores = [self.store]
        while messagebox.askyesno('纳入已有账本',
                '当前账本已列入范围。是否还有需要纳入的旧账本？\n'
                '有：选择其数据目录，可重复添加。无：结束选择。\n'
                '只盘点所选目录，不会扫描其他文件。', parent=self.root):
            path = filedialog.askdirectory(title='选择另一个已有账本的数据目录', parent=self.root)
            if not path:
                return
            candidate = DataStore(Path(path))
            candidate.load(require_workbook=False)
            if all(s.folder != candidate.folder for s in stores):
                stores.append(candidate)
        if not messagebox.askyesno('一次性建立本机责任范围',
                '已声明账本数：' + str(len(stores)) + '\n'
                '请先关闭旧版管理程序，确认需要保护的旧账本都已纳入。\n'
                '本程序不能发现未选入的旧历史，也不能约束旧客户端、其他 Windows 用户或其他设备。\n'
                '登记缺失或来源不明时请恢复原登记，不能以此重置责任。\n'
                '创建后，原未决会阻止整个范围的新删除；证据充分时自动结清，不增加逐枚审批。\n\n'
                '确认这是明确建立的新范围，且声明完整？', parent=self.root):
            return
        self.responsibility_gate.initialize(stores)
        self.status_var.set('本机责任范围已建立。原未决须先核实；全自动管理仍需正常启动。')

    def choose_folder(self):
        if self.busy or self.closing or self.audit_failed:
            return
        self.stop(quiet=True)
        path = filedialog.askdirectory(title="选择已有数据文件夹，或一个新的空文件夹", initialdir=str(self.folder), parent=self.root)
        if not path or Path(path).resolve() == self.folder:
            return
        self._queue_change(lambda: self._switch_folder(Path(path)), "正在切换账本……")

    def _switch_folder(self, folder: Path, *, reset_session=True):
        """Select and persist only after validation and acquisition of the new lock."""
        if self.audit_failed:
            self._audit_notice()
            return
        folder = Path(folder).resolve()
        if folder == self.folder:
            return
        from .instance import FolderLock
        new_lock = FolderLock(folder)
        try:
            candidate = DataStore(folder)
            if candidate.has_history():
                candidate.load(require_workbook=False)
            save_current_ledger(self.home_folder, folder)
        except Exception:
            new_lock.close()
            raise
        previous_lock = self.folder_lock
        self.folder_lock = new_lock
        self.folder = folder
        previous_lock.close()
        self.tree.delete(*self.tree.get_children())
        self.rows = []
        self.last_keys = []
        self.load_folder()
        if reset_session and (self.config or self.browser is not None):
            self._queue_login(correct_initial=False)
        elif self.config:
            self._action_state("date", "已完成")
            self.status_var.set("统计起点已修改，旧账本已保留。请重新读取费用；自动管理保持停止。")

    def _queue_change(self, callback: Callable, status: str):
        if self.audit_failed:
            self._audit_notice()
            return
        self.stop(quiet=True)
        self.session_generation += 1
        self._invalidate_collection(session=True)
        self.pending_login = False
        self.pending_change = callback
        self.status_var.set(status + (" 当前操作完成并保存结果后继续。" if self.busy else ""))
        self._update_buttons()
        self.root.after(0, self._drain_pending)

    def _drain_pending(self):
        if self.busy or self.closing or self.interrupting or self.cleanup_failed or self.audit_failed:
            return
        try:
            if self.pending_change is not None:
                callback, self.pending_change = self.pending_change, None
                callback()
            elif self.pending_login:
                correct = self.reset_on_login
                self.pending_login = self.reset_on_login = False
                if correct and not self.recovery_mode and self.store.can_correct_setup():
                    self.config = self.store.reset_initial_binding()
                    self.last_keys = []
                    self.refresh_rows()
                    self.refresh_events()
                def done(_result):
                    self._action_state("login", "待登录")
                    self.status_var.set("浏览器已打开。请登录原账户，再选中待核实记录并点击“核实平台状态”。恢复期间不能启动新撤销。" if self.recovery_mode else
                                        "浏览器已打开，待登录。请在新页面完成登录，再点击“立即读取费用”核验账户和费用；自动管理已停止。")
                self._browser_action("login", done)
        except Exception as exc:
            self.stop(quiet=True)
            self.error("未完成切换", exc)
        finally:
            self._update_buttons()

    def change_start_date(self):
        if self.recovery_mode or not self.config or self.closing or self.audit_failed:
            return
        self.stop(quiet=True)
        start = ask_text("修改统计起点", "新的统计起点（YYYY-MM-DD）：\n将保留原账本，并从完整新周期重新读取费用。",
                                       initialvalue=self.config.start_date, parent=self.root)
        if not start or start.strip() == self.config.start_date:
            return
        start = start.strip()
        try:
            if datetime.strptime(start, "%Y-%m-%d").date() > datetime.now(TZ).date():
                raise ValueError("统计起点不能晚于今天。")
        except ValueError as exc:
            self.error("日期无效", exc)
            return
        def change():
            successor = self.store.update_start_date(start, new_ledger_path(self.home_folder))
            self._switch_folder(successor.folder, reset_session=False)
        self._queue_change(change, "将保留旧账本并修改统计起点。")

    def _offer_account_ledger(self, exc):
        if self.recovery_mode or self.closing or self.pending_login or self.pending_change is not None:
            return
        if messagebox.askyesno("登录账户与账本不一致", safe_error(exc) +
                "\n\n为其他账户建立独立新账本？原账本和费用记录将完整保留。新账本沿用人数、预算和人员表，并重新登录。", parent=self.root):
            def change():
                successor = self.store.create_successor(new_ledger_path(self.home_folder), keep_account=False)
                self._switch_folder(successor.folder)
            self._queue_change(change, "正在为其他账户建立独立账本。")
        else:
            self.status_var.set("原账本已保留。请点击“登录 / 重新登录”登录原账户。")

    def setup(self):
        if (self.busy or self.closing or self.recovery_mode or self.pending_login
                or self.pending_change is not None or self.interrupting or self.cleanup_failed
                or self.audit_failed or self.single_revoke_id is not None):
            return
        if self.config is None and has_saved_data(self.folder):
            self.error("请先恢复原列表", "当前文件夹已有记录但未正常加载。请先恢复记录，不能覆盖初始化。")
            return
        self.stop(quiet=True)
        original_store, original_config = self.store, self.config
        original_hash = self.store.workbook_hash() if self.config else None
        dialog = tk.Toplevel(self.root)
        dialog.title("新建列表")
        fit_window(dialog, preferred=(680, 680), minimum=(440, 360))
        dialog.transient(self.root)
        dialog.grab_set()
        footer = ttk.Frame(dialog, padding=(22, 10))
        footer.pack(side='bottom', fill='x')
        scroller = ScrollableContent(dialog, padding=22)
        scroller.pack(fill="both", expand=True)
        body = scroller.content
        body.columnconfigure(1, weight=1)
        ttk.Label(body, text="新建列表并设置预算", style="Title.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 15))
        values = [tk.StringVar(value=str(self.config.people_count) if self.config else "14"),
                  tk.StringVar(value=amount_text(self.config.total_budget) if self.config else "1500"),
                  tk.StringVar(value=self.config.start_date if self.config else datetime.now(TZ).strftime("%Y-%m-%d"))]
        for row, (label, value) in enumerate(zip(["分配人数", "本期总预算（元）", "统计起点（YYYY-MM-DD）"], values), 1):
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=7, padx=(0, 18))
            ttk.Entry(body, textvariable=value, width=24).grid(row=row, column=1, sticky='ew')
        line_var = tk.StringVar(value=amount_text(self.table_defaults.kill_line))
        mode_var = tk.StringVar(value=self.table_defaults.fill_mode)
        ttk.Label(body, text="斩杀线（元，最小 0）").grid(row=4, column=0, sticky="w", pady=7)
        ttk.Entry(body, textvariable=line_var, width=24).grid(row=4, column=1, sticky='ew')
        mode_frame = ttk.Frame(body)
        mode_frame.grid(row=5, column=0, columnspan=2, sticky='w', pady=7)
        ttk.Label(mode_frame, text='新表默认填：').pack(side='left')
        ttk.Radiobutton(mode_frame, text='-1（跳过管理）', variable=mode_var, value='skip').pack(side='left')
        ttk.Radiobutton(mode_frame, text='斩杀线', variable=mode_var, value='kill_line').pack(side='left', padx=8)
        inherit_var = tk.BooleanVar(value=self.config is not None)
        choices = ttk.Frame(body)
        choices.grid(row=6, column=0, columnspan=2, sticky='w', pady=7)
        ttk.Radiobutton(choices, text='延续现有人员与个人额度', variable=inherit_var,
                        value=True, state='normal' if self.config else 'disabled').pack(anchor='w')
        ttk.Radiobutton(choices, text='建立空白列表（使用上方新表默认值）', variable=inherit_var,
                        value=False).pack(anchor='w')
        scroller.wrap(ttk.Label(body, text="点击创建后进入独立新列表，旧列表和记录完整保留；取消则不新建。\n延续只保留人员与个人额度，费用按所选日期重新读取。\n同账户的 Key 绑定和未结清处理记录始终保留。\n公共额度按总预算 ÷ 人数向下取整到元；空白姓名不纳入管理。", wraplength=500), padding=48).grid(row=7, column=0, columnspan=2, pady=15, sticky="w")

        def validate_source():
            if (self.store is not original_store or self.config != original_config
                        or (original_config and self.store.workbook_hash() != original_hash)
                        or self.busy or self.closing or self.interrupting or self.cleanup_failed
                        or self.audit_failed or self.pending_login or self.pending_change is not None
                        or self.single_revoke_id is not None or self.observing or self.automatic):
                raise GuardError('原列表或会话状态已变化，请关闭此窗口后重新新建。')

        def create():
            try:
                validate_source()
                n = int(values[0].get())
                budget = Decimal(values[1].get().strip())
                start = values[2].get().strip()
                if n <= 0 or not budget.is_finite() or budget < 0:
                    raise ValueError("人数必须为正整数，总预算必须为非负金额。")
                if datetime.strptime(start, "%Y-%m-%d").date() > datetime.now(TZ).date():
                    raise ValueError("统计起点不能在未来。")
                if budget == 0 and not messagebox.askyesno("零总预算", "公共额度将为 0。所有纳入管理的人员都会进入停用流程。仍按此预算生成表格？", parent=dialog):
                    return
                validate_source()
                defaults = self._set_table_defaults(line_var.get(), mode_var.get())
                self.stop(quiet=True)
                self.session_generation += 1
                self._invalidate_collection(session=True)
                if original_config:
                    successor = original_store.create_list(new_ledger_path(self.home_folder), n, budget, start,
                                                           inherit_people=inherit_var.get(), initial_amount=defaults.amount)
                    self._switch_folder(successor.folder, reset_session=False)
                else:
                    self.config = self.store.initialize(n, budget, start, initial_amount=defaults.amount)
                    save_current_ledger(self.home_folder, self.folder)
            except Exception as exc:
                messagebox.showerror("创建失败", safe_error(exc), parent=dialog)
                return
            dialog.destroy()
            self._show_config()
            self.refresh_rows()
            self._action_state("setup", "已完成")
            self._update_buttons()
            self.status_var.set("新列表已创建，旧列表和记录已保留。请保存人员表并重新读取费用；全自动管理保持停止。")
            self.edit_people()

        ttk.Button(footer, text="创建新列表", command=self._button_command("setup_save", create)).pack(side='left')
        ttk.Button(footer, text="取消", command=self._button_command("setup_cancel", dialog.destroy)).pack(side='right')
        apply_theme(self.root, self.current_theme)

    def open_excel(self):
        try:
            os.startfile(str(self.folder / "人员额度.xlsx"))
            self._invalidate_collection()
            self.action_states.pop("check", None)
            self._action_state("excel", "已完成")
            self.status_var.set("已请求 Excel/WPS 打开人员表。修改并保存后，请重新检查表格和读取费用。")
        except OSError:
            self._action_state("excel", "未完成")
            self.status_var.set("未能打开 Excel/WPS。请使用“填写两列表格”编辑并保存。")
            messagebox.showinfo("可使用内置表格", "没有可用的 Excel/WPS 打开方式也没关系，请点击“填写两列表格”。", parent=self.root)

    def save_template(self):
        path = filedialog.asksaveasfilename(title="保存人员额度模板", initialfile="人员额度模板.xlsx",
            defaultextension=".xlsx", filetypes=[("Excel 表格", "*.xlsx")], parent=self.root)
        if not path:
            return
        try:
            target = Path(path).resolve()
            if target == self.folder / DataStore.WORKBOOK or (target.name.casefold() == DataStore.WORKBOOK.casefold() and DataStore(target.parent).has_history()):
                raise ValueError("模板不能覆盖当前使用的人员额度表，请另选文件名。")
            runtime = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
            write_personnel_template(target, runtime / "templates" / "人员额度模板.xlsx",
                                     self.table_defaults.amount, self.config.people_count if self.config else 14)
            self._action_state("template", "已完成")
            self.status_var.set(f"模板已保存：{target}，金额预填 {amount_text(self.table_defaults.amount)}。填写姓名后导入；不用的行填 -1 或删除。")
        except Exception as exc:
            self._action_state("template", "失败")
            self.error("模板未保存", exc)

    def import_people(self):
        if not self.config or self.busy or self.observing or self.closing:
            return
        path = filedialog.askopenfilename(title="导入两列人员额度表", filetypes=[("Excel 表格", "*.xlsx")], parent=self.root)
        if not path:
            return
        try:
            from .excel_io import read_people_workbook
            recovering = self.recovery_mode
            expected = self.store.recovery_workbook_hash() if recovering else self.store.workbook_hash()
            people = read_people_workbook(path)
            # Validate names/count and amounts before presenting a save approval.
            people = self.store._validate_people(people, self.config.people_count)
            managed = sum(p.amount >= 0 for p in people)
            zeros = sum(p.amount == 0 for p in people)
            skipped = sum(p.amount == -1 for p in people)
            note = ("将恢复当前账本的人员表；存在的损坏表格会先另存备份。账本、费用和原操作记录保留，自动管理不会开启。"
                    if recovering else "导入后替换当前账本的人员表；原始导入文件保留。零额度人员将在启动管理后进入停用流程。")
            if not messagebox.askyesno("确认恢复人员表" if recovering else "确认导入人员额度",
                    f"共 {len(people)} 行：管理 {managed} 人，零额度 {zeros} 人，跳过 {skipped} 行。\n\n" + note + "\n确认导入？", parent=self.root):
                return
            if Path(path).resolve() == self.store.workbook_path:
                # Importing the canonical file itself requires no rewrite.
                if self.store.workbook_hash() != expected:
                    raise ValueError("表格已改变，请重新导入。")
            elif recovering:
                self.store.restore_people(people, expected_hash=expected)
            else:
                self.store.save_people(people, expected_hash=expected)
            self.validated_hash = None
            self._invalidate_collection()
            self.check_people(show_dialog=False)
            self.refresh_events()
            self._action_state("import_people", "已完成")
            self.status_var.set(f"已导入：管理 {managed} 人，零额度 {zeros} 人，跳过 {skipped} 行。自动管理未启动。")
        except Exception as exc:
            self.validated_hash = None
            self._invalidate_collection()
            self._action_state("import_people", "失败")
            self.error("导入未完整完成，请检查当前表格", exc)

    def import_billing(self):
        if self.recovery_mode or not self.config:
            return
        path = filedialog.askopenfilename(title='选择官方 ZIP 或其中一个 CSV', filetypes=[('官方导出', '*.zip *.csv')], parent=self.root)
        if path:
            try:
                from .billing import inspect_export
                result = inspect_export(path, self.config.start_date)
                detail = result['message'] + f" 用量记录 {result['amount_rows']} 行，费用记录 {result['cost_rows']} 行。"
                self._action_state("import", "已完成")
                self.status_var.set("官方导出检查已完成：" + detail)
                messagebox.showinfo('导出检查', detail, parent=self.root)
            except Exception as exc:
                self._action_state("import", "失败")
                self.error('导出检查未通过', exc)

    def open_folder(self):
        try:
            os.startfile(str(self.folder))
            self._action_state("logs", "已完成")
            self.status_var.set(f"已打开日志文件夹：{self.folder}")
        except OSError as exc:
            self.error("无法打开文件夹", exc)

    def open_diagnostics(self):
        from .diagnostics_ui import open_diagnostics_viewer
        open_diagnostics_viewer(self)

    def edit_people(self, initial_amount=None):
        if self.recovery_mode:
            self.status_var.set('请先恢复原人员表或导入有效 Excel；不会从空白表覆盖原账本。')
            return
        from .people_editor_ui import open_people_editor
        open_people_editor(self, initial_amount)

    def check_people(self, show_dialog=True, *, allow_empty=False) -> bool:
        try:
            people = self.store.read_people()
            was_recovering = self.recovery_mode
            self.recovery_mode = False
            self.config = self.store.load()
            self._show_config()
            if was_recovering:
                self.stop(quiet=True)
                self._invalidate_collection(session=True)
            self.refresh_rows()
            if self.recovery_mode:
                raise GuardError('检查期间人员表再次不可用，请恢复完整表格后重新检查。')
            count = sum(bool(p.name) and p.amount != -1 for p in people)
            zeros = sum(bool(p.name) and p.amount == 0 for p in people)
            checked_hash = self.store.workbook_hash()
            if self.validated_hash != checked_hash:
                self._invalidate_collection()
            self.validated_hash = checked_hash
            stat = self.store.workbook_path.stat()
            self.checked_workbook_signature = (stat.st_mtime_ns, stat.st_size)
            self._action_state("check", "已完成")
            message = f"表格可读：待管理 {count} 人，其中零额度 {zeros} 人。\n文件检查通过不等于平台费用已核实，请继续读取平台。"
            if count == 0:
                message = "当前没有待管理 API。填写姓名和金额后再启动观察。"
            self.status_var.set(message.replace("\n", " "))
            if show_dialog:
                messagebox.showinfo("表格检查", message, parent=self.root)
            return count > 0 or allow_empty
        except Exception as exc:
            self.validated_hash = None
            self._invalidate_collection()
            self._action_state("check", "失败")
            self.stop(quiet=True)
            try:
                self.refresh_rows()
            except GuardError:
                self.config = None
                self._update_buttons()
            self.error("表格检查未通过", exc, dialog=show_dialog, next_step="请修正并保存人员表，再点击“检查表格”。")
            return False

    def refresh_rows(self, *, readonly=False):
        if not self.store or not self.config:
            return
        # Validate the ledger separately: database/settings damage is not a
        # workbook recovery mode and must never be hidden by an empty table.
        self.config = self.store.load(require_workbook=False)
        try:
            self.store.read_people()
        except GuardError:
            if readonly:
                raise
            self._enter_recovery()
        self.rows = ([] if self.recovery_mode else
                     self.store.read_evaluation() if readonly else self.store.evaluate())
        # Recovery must not depend on a person still appearing in the workbook.
        for action in self.store.unresolved_actions():
            existing = next((r for r in self.rows if r.get('tracking_id') == action['tracking_id']), None)
            if existing is None:
                existing = {'name': action['name'], 'tracking_id': action['tracking_id'],
                            'amount': None, 'spent': None, 'share': self.config.share}
                self.rows.append(existing)
            present = action.get('status') == 'present_verified'
            existing['status'] = 'present_unresolved' if present else 'recovery_pending'
            existing['reasons'] = (['最近观察时原 Key 仍存在；旧提交责任未解除，不会自动重试'] if present else
                                   ['上次操作结果尚未核实；请选中此行，点击“核实平台状态”'])
            existing['operation_id'] = action.get('operation_id')
            existing['original_evidence'] = action.get('evidence')
        self.tree.delete(*self.tree.get_children())
        for i, row in enumerate(self.rows):
            status = row.get("status", "unknown")
            if (status == 'triggered' and not self.automatic and row.get('tracking_id')
                    and self.store.latest_action(row['tracking_id']) == 'not_submitted'):
                status = 'not_submitted'
            amount = "-1（跳过）" if str(row.get("amount")) == "-1" else amount_text(row.get("amount"))
            spent = amount_text(row.get("spent"))
            if row.get('spent_is_lower_bound') and row.get('spent') is not None:
                spent = '≥' + spent + '（待补全）'
            self.tree.insert("", "end", iid=str(i), values=(row.get("name") or "（待填写）", amount, amount_text(row.get("share", self.config.share)), spent, STATUS_LABELS.get(status, status)), tags=(status,))

    def refresh_events(self, *, show_status=False, strict=False):
        if not self.store or not self.config:
            return
        try:
            events = self.store.events(limit=200)
            lines = []
            for event in events:
                # Field names remain visible to avoid suppressing new audit fields.
                lines.append(json.dumps(event, ensure_ascii=False, default=str, indent=2))
            self.report_text.configure(state="normal")
            self.report_text.delete("1.0", "end")
            self.report_text.insert("end", "\n\n".join(lines) if lines else "暂时没有处理记录。日志保存在当前数据文件夹。")
            self.report_text.configure(state="disabled")
            if show_status:
                self._action_state("reports", "已完成")
                self.status_var.set(f"处理汇报刷新已完成，显示最近 {len(events)} 条记录。")
        except Exception as exc:
            if strict:
                raise
            self.status_var.set(f"汇报读取失败：{safe_error(exc)}")

    def _selection(self, _event=None):
        row = self.selected()
        if not row:
            return
        reason = row.get("reasons") or []
        if isinstance(reason, list):
            reason = "；".join(map(str, reason))
        detail = f"{row.get('name', '')}：{reason or STATUS_LABELS.get(row.get('status'), '等待检查')}。平台标识：{row.get('tracking_id') or '尚未绑定'}"
        if row.get('status') in {'recovery_pending', 'present_unresolved'}:
            detail += '。原操作编号：' + (row.get('operation_id') or '旧记录未保存')
            evidence = row.get('original_evidence')
            if isinstance(evidence, dict):
                detail += (f"；原个人额度 ¥{amount_text(evidence.get('amount'))}，"
                           f"原公共额度 ¥{amount_text(evidence.get('share'))}，"
                           f"原已知费用 ¥{amount_text(evidence.get('spent'))}。这些是原操作依据，不是新的撤销授权。")
            else:
                detail += '；旧记录没有保存完整规则依据，不补造。'
        self.detail_var.set(detail)

    def selected(self) -> dict | None:
        chosen = self.tree.selection()
        return self.rows[int(chosen[0])] if chosen else None

    def _browser_action(self, action: str, on_success: Callable, on_error: Callable | None = None,
                        on_progress: Callable | None = None, on_result_error: Callable | None = None,
                        diagnostic_operation_id='', **kwargs):
        if self.recovery_mode and action not in {'login', 'verify_revoke'}:
            self.status_var.set('恢复期间仅可登录原账户并核实已有操作；请先恢复人员表。')
            return
        if self.browser is not None and getattr(self.browser, 'cleanup_failed', False):
            self.cleanup_failed = True
            self.error("浏览器清理尚未完成", "请先点击“中断当前操作”完成清理，再重新登录。", dialog=False)
            self._update_buttons()
        if self.busy or self.closing or self.interrupting or self.cleanup_failed or self.audit_failed:
            return
        button_key = {"prepare_revoke": "revoke", "read_keys": "revoke", "verify_revoke": "verify", "inspect_revoke": "inspect"}.get(action, action)
        action_title = {"login": "打开登录浏览器", "collect": "读取平台费用", "prepare_revoke": "定位处理页面",
                        "read_keys": "核验所选 Key 身份", "inspect_revoke": "只读检查删除准备", "verify_revoke": "核实平台状态", "revoke": "撤销 Key"}.get(action, "操作平台")

        def failed(exc):
            self.stop(quiet=True)
            self._invalidate_collection(session=True)
            self.last_keys = []
            if (action in {'inspect_revoke', 'revoke'}
                    and isinstance(exc, (KeyInspectionError, RevocationNotSubmitted))
                    and exc.reason_code in MANAGEMENT_CONTRACT_REASONS):
                self._show_collection_summary(
                    self._saved_fee_fact() + '管理准备核验未通过，自动管理已停止；'
                    '已保存记录保留，当前删除资格已失效。此错误不等于平台已退出登录；'
                    '修正原因后可直接重新读取费用进行核验。')
            self._diagnostic("browser_action_failed", action=action, error_type=type(exc).__name__,
                             operation_id=diagnostic_operation_id)
            self._action_state(button_key, "失败")
            log_failure = None
            if on_error:
                try:
                    on_error(exc)
                except Exception as log_exc:
                    log_failure = log_exc
                    self._queue_audit(lambda error=exc: on_error(error), action=action, result={"error_type": type(exc).__name__})
            return log_failure

        def report_failure(exc, log_failure=None):
            next_step = {"collect": "请检查平台登录状态和统计日期；会话已失效时先重新登录，再点击“立即读取费用”重试。",
                         "login": "请检查网络后点击“登录 / 重新登录”重试。"}.get(action,
                         "请查看处理汇报并核实平台状态；结果不明时不会自动重试。")
            if log_failure:
                self._audit_notice()
            elif isinstance(exc, AccountMismatch):
                self.account_mismatch = exc
                self._action_state("account_ledger", "待选择")
                self.error("账户与当前账本不一致", exc, dialog=False,
                           next_step="请重新登录原账户。" if self.recovery_mode else "请重新登录原账户，或点击“为当前账户建账本”确认建立独立账本。")
            else:
                self.error(action_title + "失败", exc, dialog=action != "collect", next_step=next_step)

        try:
            if self.browser is None or getattr(self.browser, 'closed', False):
                from .browser_process import IsolatedBrowserService
                self.browser = IsolatedBrowserService(diagnostics_dir=self.diagnostics_dir)
            if action in {'revoke', 'prepare_revoke', 'verify_revoke'}:
                if self.target_lock is None:
                    raise GuardError('未取得本机目标互斥，未派发平台处理。')
                kwargs['target_lock_names'] = self.target_lock.names
            future = self.browser.submit(action, **kwargs)
        except Exception as exc:
            log_failure = failed(exc)
            self._update_buttons()
            report_failure(exc, log_failure)
            return
        self.busy = True
        self.current_action = action
        self._diagnostic("browser_action_started", action=action, generation=self.session_generation,
                         operation_id=diagnostic_operation_id)
        generation = self.session_generation
        preserve_result = action in {"revoke", "verify_revoke"}
        started_at = time.monotonic()
        last_stage = None
        self._action_state(button_key, "读取中" if action == "collect" else "处理中")
        self.status_var.set(action_title + "中。请勿在同一浏览器窗口切换账户或修改筛选。")
        self.progress_var.set(f"当前操作：{action_title}｜已用时 0 秒")
        self._update_buttons()

        def poll():
            nonlocal last_stage
            if not future.done():
                elapsed = max(0, int(time.monotonic() - started_at))
                progress = getattr(self.browser, "progress", None)
                stage = progress.get("stage") if isinstance(progress, dict) else None
                if stage != last_stage:
                    last_stage = stage
                    self._diagnostic("browser_progress", action=action, stage=stage or action_title, elapsed=elapsed)
                detail = f"当前操作：{safe_error(stage or action_title)}｜已用时 {elapsed} 秒"
                if elapsed >= 30:
                    detail += "｜平台响应较慢，任务仍在处理，请勿重复提交。"
                self.progress_var.set(detail)
                if on_progress:
                    try:
                        on_progress()
                    except Exception as exc:
                        self.stop(quiet=True)
                        self.status_var.set('撤销授权未完成：' + safe_error(exc))
                self.root.after(100, poll)
                return
            failure = log_failure = None
            result_received = False
            result_failure_handled = False
            result = None
            try:
                if generation != self.session_generation and not preserve_result:
                    self._diagnostic("browser_result_discarded", action=action, generation=generation)
                    return
                result = future.result()
                result_received = True
                on_success(result)
                fields = {"action": action, "elapsed": round(time.monotonic() - started_at, 2),
                          "operation_id": diagnostic_operation_id}
                if isinstance(result, dict):
                    if isinstance(result.get("keys"), list):
                        fields["key_count"] = len(result["keys"])
                    fields["totals_available"] = result.get("totals") is not None
                    fields["outcome"] = "absent_verified" if result.get("absent") is True else "returned"
                    if action == "verify_revoke" and result.get("absent") is False and result.get("present") is True:
                        fields["outcome"] = "present_verified"
                self._diagnostic("browser_action_completed", **fields)
            except Exception as exc:
                failure = exc
                if result_received and on_result_error is not None:
                    # Transport success is distinct from a failed local apply.
                    # Do not replay the callback: it may already have committed.
                    try:
                        on_result_error(exc)
                        result_failure_handled = True
                    except Exception as handling_exc:
                        failure = handling_exc
                        log_failure = failed(handling_exc)
                elif preserve_result and result_received:
                    self.delete_permit = None
                    self._queue_audit(lambda value=result: on_success(value), action=action, result=result)
                    log_failure = exc
                else:
                    log_failure = failed(exc)
            finally:
                # Persist the operation's outcome first; release UI state before
                # any optional modal notification can enter a nested event loop.
                self.busy = False
                self.current_action = None
                if not self.interrupting:
                    self.progress_var.set("当前操作：空闲")
                self._update_buttons()
                self.root.after(0, self._drain_pending)
            if self.audit_failed:
                self._audit_notice()
            elif failure is not None and not result_failure_handled and not self.closing and not self.interrupting and not self.pending_login and self.pending_change is None:
                report_failure(failure, log_failure)
        self.root.after(100, poll)

    def login(self):
        if not self.config or self.closing or self.interrupting or self.cleanup_failed or self.audit_failed or self.pending_change is not None:
            return
        self._queue_login(correct_initial=True)

    def _queue_login(self, *, correct_initial: bool):
        if self.interrupting or self.cleanup_failed or self.audit_failed:
            return
        self.stop(quiet=True)
        self.session_generation += 1
        self.account_mismatch = None
        self._invalidate_collection(session=True)
        self.action_states["login"] = "等待重开" if self.busy else "打开中"
        self.pending_login = True
        self.reset_on_login = correct_initial and not self.recovery_mode
        self.status_var.set("已停止自动管理，等待当前操作保存结果后重新登录。" if self.busy else "正在重建登录会话……")
        self._update_buttons()
        self.root.after(0, self._drain_pending)

    def _collection_trace(self, event, operation_id, stage, *, reason_code='OK',
                          persistence_state='not_written', application=None,
                          coverage_summary=None, error_type=None):
        """Only controlled result fields and aggregate counts enter diagnostics."""
        fields = {'operation_id': operation_id, 'stage': stage, 'reason_code': reason_code,
                  'persistence_state': persistence_state}
        if application is not None:
            fields.update(apply_outcome=application.apply_outcome,
                          export_state=application.export_state,
                          observed_source_is_effective=(application.observed_source_id
                                                        == application.effective_snapshot_id))
            for field in ('observed_source_id', 'effective_snapshot_id'):
                value = getattr(application, field)
                if isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value):
                    fields[field] = value
            coverage_summary = application.coverage_summary if coverage_summary is None else coverage_summary
        for key in COLLECTION_COVERAGE_FIELDS:
            value = (coverage_summary or {}).get(key)
            if type(value) is int and value >= 0:
                fields[key] = value
        if error_type:
            fields['error_type'] = error_type
        self._diagnostic(event, **fields)

    def _recover_collection_application(self, store, operation_id):
        """Finish local work only. Never reapply or publish a new observation."""
        if self.store is not store:
            raise GuardError('待恢复的费用属于另一个账本，请返回原账本。')
        store.export_logs()
        self._collection_context()
        self.refresh_rows(readonly=True)
        self.refresh_events(strict=True)
        self._invalidate_collection()
        self._action_state('collect', '本地已恢复，待重新读取')
        self._collection_trace('collection_recovery_completed', operation_id, 'local_recovery',
                               persistence_state='persisted')

    def _collection_application_failed(self, exc, *, operation_id, state):
        """A returned platform result can fail locally without losing login."""
        application = state.get('application')
        stage = state['stage']
        persistence = 'persisted' if application is not None else 'not_written'
        reason = 'LOCAL_APPLICATION_FAILED'
        coverage = None
        if isinstance(exc, SnapshotApplyError):
            reason = exc.reason_code
            stage = exc.stage
            persistence = exc.persistence_state
            application = exc.application or application
            coverage = exc.coverage_summary
        elif stage == 'target_lock_acquisition':
            reason = 'TARGET_IN_USE' if isinstance(exc, TargetLockError) else 'TARGET_LOCK_FAILED'
        elif application is not None:
            reason = 'POST_COMMIT_REFRESH_FAILED'
        elif stage in {'local_context', 'identity_refresh'} and isinstance(exc, GuardError):
            reason = 'LOCAL_CONTEXT_UNAVAILABLE'
        elif stage == 'snapshot_validation':
            # An unexpected exception may have escaped after a commit. Do not
            # infer rollback from the absence of a returned application object.
            persistence = 'unknown'
            reason = 'SNAPSHOT_PERSISTENCE_UNKNOWN'
        allowed_reasons = set(COLLECTION_REJECTION_LABELS) | {
            'ACCOUNT_NOT_BOUND', 'AUDIT_EXPORT_FAILED', 'POST_COMMIT_REFRESH_FAILED',
            'SNAPSHOT_PERSISTENCE_UNKNOWN', 'LOCAL_APPLICATION_FAILED',
        }
        if reason not in allowed_reasons:
            reason, persistence = 'LOCAL_APPLICATION_FAILED', 'unknown'
        if persistence not in {'not_written', 'persisted', 'unknown'}:
            persistence = 'unknown'
        if stage not in {'collection_received', 'local_context', 'target_lock_acquisition', 'binding_verification',
                         'identity_refresh', 'snapshot_validation', 'snapshot_transaction',
                         'audit_export', 'post_commit_refresh', 'post_commit_context',
                         'collection_publication'}:
            stage = 'local_context'
        recoverable = (persistence == 'persisted' and application is not None
                       and reason in {'AUDIT_EXPORT_FAILED', 'POST_COMMIT_REFRESH_FAILED'})
        preserve_session = (state.get('identity_verified', False)
                            and persistence != 'unknown'
                            and (reason in COLLECTION_REJECTION_LABELS or recoverable))
        self.stop(quiet=True)
        self.delete_permit = None
        self._invalidate_collection(session=not preserve_session)
        self.collection_reason_code = reason
        self.last_keys = []
        self._collection_trace('collection_application_failed', operation_id, stage,
                               reason_code=reason, persistence_state=persistence,
                               application=application, coverage_summary=coverage,
                               error_type=type(exc).__name__)
        if recoverable:
            store = self.store
            self._queue_audit(lambda: self._recover_collection_application(store, operation_id),
                              action='collect', result={'operation_id': operation_id,
                                                        'persistence_state': 'persisted'},
                              preserve_session=preserve_session)
            self._action_state('collect', '已入账，本地恢复待完成')
        elif isinstance(exc, AccountMismatch):
            self.account_mismatch = exc
            self._action_state('account_ledger', '待选择')
            self._action_state('collect', '账户与账本不一致')
            self.error('账户与当前账本不一致', exc, dialog=False,
                       next_step='请重新登录原账户，或为当前账户建立独立账本。')
        elif reason in COLLECTION_REJECTION_LABELS:
            label, message = COLLECTION_REJECTION_LABELS[reason]
            self._action_state('collect', label)
            self.status_var.set(message + ' 自动管理已停止，旧删除许可已清除。')
        elif persistence == 'unknown':
            self._action_state('collect', '保存结果待核实')
            self.status_var.set('费用已经取回，但本次保存结果待核实；不能认定未写入。请先核对本地账本，自动管理保持停止。')
        else:
            self._action_state('collect', '本地处理失败')
            self.error('费用已取回，本地处理失败', exc, dialog=False,
                       next_step='自动管理已停止；请核对本地文件与绑定后重新读取。')
        period = state.get('period_label', '')
        if reason == 'NO_BOUND_COST_MATCH':
            text = (f'费用核验：{period}平台已返回费用，但未匹配本账本绑定。'
                    '原累计保留，本次未入账，未匹配费用仍未知；不代表账户已更换。')
            text += ' ' + self._coverage_text(state.get('platform_coverage'))
            self._show_collection_summary(text.strip())
        else:
            self._show_collection_summary(f'费用核验：{period}{self.action_states.get("collect", "未完成")}。'
                                          '本次未取得可用于自动处理的新费用依据。')

    def _record_forecast_shadow(self, result):
        """Optional, bounded observations; never a quota or scheduling input."""
        try:
            from .forecast_shadow import record_collection
            context = self._collection_context()
            bindings = self.store.binding_scopes()
            rows = [{**row, 'binding_scope': json.dumps(bindings.get(row.get('name'), ()),
                                                       ensure_ascii=True)} for row in self.rows]
            # Session generations and reread clocks are not evidence versions.
            scope = json.dumps(context[1:], default=str, ensure_ascii=True)
            summary = record_collection(self.folder, scope=scope, rows=rows,
                                        source_id=result['source_id'], collected_at=time.time())
            # The console contract currently supplies a requested date range,
            # not a certified data-as-of time. Do not invent an ETA from it.
            self.forecast_summary_var.set(
                f"预测影子记录已更新 {summary['count']} 项。当前来源尚无可信数据截止时间，"
                '不提供到线时间估计；删除资格与读取周期沿用原规则。')
            self._diagnostic('forecast_shadow_recorded', count=summary['count'])
        except Exception:
            self.forecast_summary_var.set('预测影子记录暂不可用；费用与自动管理继续按原规则处理。')
            self._diagnostic('forecast_shadow_unavailable')

    def collect(self, *, after_read=None):
        if self.recovery_mode or self.busy or not self.config or self.pending_login or self.pending_change is not None or self.closing or self.interrupting or self.cleanup_failed or self.audit_failed:
            return
        self._cancel_collection_poll()
        if not self.check_people(show_dialog=False, allow_empty=True):
            self.stop(quiet=True)
            return
        checked_hash = self.validated_hash
        self._invalidate_collection()
        operation_id = uuid4().hex
        state = {'stage': 'collection_received', 'application': None, 'identity_verified': False}
        requested_store = self.store
        try:
            requested_context = self._collection_context()
            managed_names = [person.name for person in self.store.read_people()
                             if person.name and person.amount >= 0]
        except Exception as exc:
            state['stage'] = 'local_context'
            self._collection_application_failed(exc, operation_id=operation_id, state=state)
            return
        if not managed_names:
            self.stop(quiet=True)
            self.collection_reason_code = 'NO_MANAGED_KEYS'
            self._action_state('collect', FEE_STATE_LABELS['NO_MANAGED_KEYS'])
            self._show_collection_summary('费用核验：没有已填写且额度不为 -1 的 Key；未发送费用请求。')
            self.status_var.set('未填写或额度为 -1 的行不查询费用、不参与自动管理。')
            self._diagnostic('cost_collection_skipped', reason_code='NO_MANAGED_KEYS', count=0)
            return
        requested_start = requested_context[4]
        self._show_collection_summary(f'费用核验：正在查询 {len(managed_names)} 个受管名称、'
                                      f'{requested_start} 起的费用；未填写和 -1 的行不查询。')

        def done(result):
            incoming_reason = result.get('reason_code', 'OK')
            if not isinstance(incoming_reason, str) or incoming_reason not in COLLECTION_RESULT_CODES:
                incoming_reason = 'UNKNOWN_COLLECTION_RESULT'
            self._collection_trace('collection_result_received', operation_id, 'collection_received',
                                   reason_code=incoming_reason)
            state['stage'] = 'local_context'
            if not collection_is_fresh(result.get('collection_started_wall'),
                                       result.get('collection_started_monotonic'), result['end_date']):
                raise ValueError('本次读取已过期或跨日，未应用费用；请重新读取。')
            if self.store.workbook_hash() != checked_hash:
                raise ValueError("读取平台期间表格发生变化。本次结果未应用，请重新检查。")
            if (self.store is not requested_store or self._collection_context() != requested_context
                    or result.get('start_date') != requested_start):
                raise SnapshotApplyError('采集期间账本、身份或统计设置已变化，未应用本次绑定或费用。',
                                         reason_code='COLLECTION_CONTEXT_CHANGED', stage='local_context',
                                         persistence_state='not_written', operation_id=operation_id)
            state['period_label'] = f"{requested_start} 至 {result['end_date']}（UTC+8），"
            state['platform_coverage'] = result.get('coverage_summary')
            state['stage'] = 'target_lock_acquisition'
            self._claim_targets(result['keys'])
            self._collection_trace('collection_targets_claimed', operation_id, 'target_lock_acquisition')
            state['stage'] = 'binding_verification'
            self.store.bind_keys(result["keys"], account_id=result["account_id"])
            if 'requested_tracking_ids' in result:
                expected = {item['tracking_id'] for item in self.store.active_bindings()
                            if item['name'] in managed_names}
                actual = result['requested_tracking_ids']
                if (not isinstance(actual, list) or len(actual) != len(expected)
                        or any(not isinstance(key, str) for key in actual) or set(actual) != expected
                        or result.get('totals') is not None
                        and not set(result['totals']).issubset(expected)):
                    raise SnapshotApplyError('返回的费用查询范围不属于当前受管绑定，未应用费用。',
                                             reason_code='COLLECTION_CONTEXT_CHANGED',
                                             stage='binding_verification', persistence_state='not_written')
            self.session_ready = True
            state['identity_verified'] = True
            self._action_state("login", "已核验")
            self._collection_trace('collection_identity_verified', operation_id, 'binding_verification')
            if result.get('totals') is None:
                state['stage'] = 'identity_refresh'
                self.retry_collection = bool(result.get('retryable'))
                self.last_keys = result['keys']
                self.refresh_rows(readonly=True)
                self.refresh_events(strict=True)
                self._remember_collection(result)
                reason = result.get('reason_code', 'SCHEMA_UNSUPPORTED')
                self.collection_reason_code = reason
                self._action_state("collect", FEE_STATE_LABELS.get(reason, "费用结构待核验"))
                if reason == 'NO_SELECTED_USAGE_DATA':
                    self._show_collection_summary(f"费用核验：{state['period_label']}指定受管 Key 暂无费用明细；"
                                                  '实际费用仍未知，没有按零元入账，也不认定绑定错误。')
                elif reason == 'NO_MANAGED_KEYS':
                    self._show_collection_summary('费用核验：受管目标当前均不在平台列表中，未发送费用请求。')
                elif reason == 'NO_USAGE_DATA':
                    self._show_collection_summary(f"费用核验：{state['period_label']}平台成功返回空明细；"
                                                  '实际费用仍未知，没有按零元入账。')
                elif reason == 'PLATFORM_REJECTED':
                    self._show_collection_summary(f"费用核验：{state['period_label']}平台业务层拒绝本次查询，"
                                                  '具体原因未确认；不是已确认的账户不匹配，请查看诊断。')
                else:
                    self._show_collection_summary(f"费用核验：{state['period_label']}"
                                                  f"{FEE_STATE_LABELS.get(reason, '费用未核验')}；原累计保留。")
                self.status_var.set(result.get('message', '未取得可核验费用。') +
                                    ' 费用未核验，仅身份核验有效时的零额度可处理；未知费用不按零元计算。')
                self._diagnostic('collection_unavailable', reason_code=reason,
                                 retryable=self.retry_collection,
                                 start_date=result['start_date'], end_date=result['end_date'],
                                 operation_id=operation_id)
                if reason in {'AUTH_REQUIRED', 'SESSION_UNAVAILABLE', 'REDIRECT_REJECTED'}:
                    self.stop(quiet=True)
                    self._invalidate_collection(session=True)
                if self.observing:
                    self._schedule_processing()
                if after_read is not None:
                    # The continuation clears its token before rechecking fresh
                    # evidence, including a clock/day change after this read.
                    self.root.after(0, after_read)
                return
            self.retry_collection = True
            state['stage'] = 'snapshot_validation'
            application = self.store.apply_snapshot_result(
                result["totals"], result["source_id"], result["start_date"], result["end_date"],
                operation_id=operation_id)
            state['application'] = application
            self._collection_trace('collection_snapshot_result', operation_id, 'audit_export',
                                   reason_code=application.reason_code,
                                   persistence_state=application.persistence_state, application=application)
            if not application.fees_current:
                raise SnapshotApplyError('历史费用来源不能刷新当前账本的管理资格。',
                                         reason_code='STALE_SNAPSHOT_REPLAY', stage='snapshot_validation',
                                         persistence_state=application.persistence_state,
                                         application=application, operation_id=operation_id)
            state['stage'] = 'post_commit_refresh'
            self.last_keys = result["keys"]
            self.refresh_rows(readonly=True)
            self.refresh_events(strict=True)
            state['stage'] = 'post_commit_context'
            self._remember_collection(result, application=application)
            state['stage'] = 'collection_publication'
            self._action_state("collect", "成功")
            self.collection_reason_code = 'OK'
            coverage = application.coverage_summary
            self._show_collection_summary(
                f"费用核验：{state['period_label']}费用已应用；"
                f"当前受管 {coverage.get('active_managed_count', 0)} 枚，"
                f"本次匹配 {coverage.get('matched_active_count', 0)} 枚，"
                f"未覆盖 {coverage.get('active_missing_count', 0)} 枚。未覆盖费用保留未知或历史下界。")
            self._collection_trace('collection_application_completed', operation_id, 'collection_publication',
                                   persistence_state='persisted', application=application)
            self._record_forecast_shadow(result)
            now = datetime.now(TZ).strftime("%H:%M:%S")
            reached = sum(row.get("status") == "triggered" for row in self.rows)
            unknown = sum(row.get('status') == 'unknown' for row in self.rows)
            self.status_var.set(f"费用读取成功 {now}（UTC+8）｜{reached} 人达到额度，{unknown} 人费用未完整。" + (f"全自动管理已开启，本轮处理完成后等待 {self.poll_interval_seconds} 秒。" if self.observing else "全自动管理未开启，可点击“启动全自动管理”。"))
            if reached:
                self.root.bell()
            if self.observing:
                self._schedule_processing()
            if after_read is not None:
                self.root.after(0, after_read)
        self._browser_action("collect", done,
                             on_result_error=lambda exc: self._collection_application_failed(
                                 exc, operation_id=operation_id, state=state),
                             diagnostic_operation_id=operation_id, operation_id=operation_id,
                             start_date=requested_start,
                             managed_names=managed_names,
                             account_id=requested_context[3],
                             known_keys=self.store.active_tracking_ids())

    def start(self):
        if self.recovery_mode or self.busy or self.observing or self.pending_start is not None or self.pending_login or self.pending_change is not None or self.closing or self.interrupting or self.cleanup_failed or self.audit_failed or not self.check_people(show_dialog=False):
            return
        if not self.session_ready:
            self.status_var.set("自动管理未开启。请先点击“登录 / 重新登录”，在浏览器完成登录，再点击“立即读取费用”核验账户与费用。")
            return
        if not self._collection_is_current():
            if self.start_refresh_required:
                self._refresh_before_start()
                return
            self._invalidate_collection()
            self.status_var.set("自动管理未开启。请先点击“立即读取费用”；读取已过期、跨日或配置已变化时需要重新核验。")
            return
        if self.store.unresolved_actions():
            self.status_var.set('自动管理未开启。请先选中“上次操作待核实”的人员，点击“核实平台状态”。')
            return
        try:
            self._claim_targets(self.last_keys)
        except GuardError as exc:
            self.error('自动管理未开启', exc, dialog=False)
            return
        try:
            self.responsibility_gate.reconcile(self.store)
            self.responsibility_gate.check(self.store)
        except GuardError as exc:
            self.error('自动管理未开启', exc, dialog=False)
            return
        managed = [p.name for p in self.store.read_people() if p.name and p.amount >= 0]
        fees_note = ("\n当前费用尚未就绪：费用未核验，仅零额度可处理；正额度人员的未知费用不会按零元计算。\n" if not self.collection_has_fees else "")
        confirmation_generation = self.management_generation
        confirmation_store = self.store
        confirmation_hash = self.validated_hash
        confirmation_context = self.collection_context
        if not messagebox.askyesno('启动全自动管理（含自动撤销）',
            '管理人员：' + '、'.join(managed) + '\n\n本次开启后，达到个人额度或公共额度（含 0 元）时，将自动永久撤销对应 Key，无需逐枚人工审批。-1 和表格外的 Key 不参与。\n'
            '撤销不可恢复；账单延迟与在途请求可能导致超支。身份、表格、页面结构或结果无法核实时会停止；结果不明不会自动重试。' + fees_note + '\n\n启动本次全自动管理？', parent=self.root):
            return
        # A modal dialog runs a nested Tk loop. A stop, login, ledger switch or
        # changed workbook during confirmation invalidates this start request.
        if (self.management_generation != confirmation_generation or self.store is not confirmation_store
                or self.recovery_mode or self.busy or self.observing or self.automatic
                or self.single_revoke_id is not None or self.pending_login or self.pending_change is not None
                or self.closing or self.interrupting or self.cleanup_failed or self.audit_failed):
            return
        try:
            if (not self.session_ready or self.collection_context != confirmation_context
                    or not self._collection_is_current() or self.validated_hash != confirmation_hash
                    or self.store.workbook_hash() != confirmation_hash):
                self._invalidate_collection()
                self.status_var.set('自动管理未开启：确认期间身份、人员表或费用核验发生变化，请重新读取后启动。')
                return
            if self.store.unresolved_actions():
                self.status_var.set('自动管理未开启：确认期间出现待核实操作，请先核实平台状态。')
                return
            self._claim_targets(self.last_keys)
        except Exception as exc:
            self.error('自动管理未开启', exc, dialog=False)
            return
        self.observing = True
        self.automatic = True
        self.management_generation += 1
        self.action_states.pop("stop", None)
        self.status_var.set("全自动管理已开启（含自动撤销，无需逐枚人工审批），正在检查当前额度。" + f"每轮处理完成后等待 {self.poll_interval_seconds} 秒再读取。")
        self._update_buttons()
        self._schedule_processing()

    def _refresh_before_start(self):
        """An explicit start after force-identity work gets a new read first.

        Do not reuse old fees or re-login. This read never starts management by
        itself: its continuation must still reach the normal start confirmation.
        """
        if self.pending_start is not None:
            return
        if self.store.unresolved_actions():
            self.status_var.set('自动管理未开启。请先核实上次操作的结果，再启动。')
            self._diagnostic('start_refresh_blocked', reason_code='UNRESOLVED_ACTION')
            return
        if self.browser is None or getattr(self.browser, 'closed', False):
            self.status_var.set('自动管理未开启。浏览器会话已关闭，请先登录并读取费用。')
            return
        token = object()
        store = self.store
        session, management = self.session_generation, self.management_generation
        context = self._collection_context()
        self.pending_start = token
        self._diagnostic('start_refresh_started', reason_code='AFTER_FORCED_IDENTITY')
        self._update_buttons()

        def resume():
            if self.pending_start is not token:
                return
            self.pending_start = None
            self._update_buttons()
            try:
                valid = (self.store is store and self.session_generation == session
                         and self.management_generation == management
                         and not self.busy and not self.observing and not self.automatic
                         and not self.closing and not self.interrupting and not self.cleanup_failed
                         and not self.audit_failed and not self.recovery_mode
                         and not self.pending_login and self.pending_change is None
                         and self.single_revoke_id is None and self.session_ready
                         and self._collection_context() == context and self._collection_is_current())
                if not valid:
                    self._diagnostic('start_refresh_discarded', reason_code='CONTEXT_CHANGED')
                    return
                self._diagnostic('start_refresh_ready')
                self.start()
            except Exception as exc:
                self.stop(quiet=True)
                self.error('自动管理未开启', exc, dialog=False)

        try:
            self.collect(after_read=resume)
        except Exception as exc:
            # Local file/argument reads can fail before a browser task exists.
            # Do not leave a pending start blocking the other controls.
            if self.pending_start is token:
                self.pending_start = None
                self._update_buttons()
            self.error('自动管理未开启', exc, dialog=False)
            return
        if self.busy and self.pending_start is token:
            self.status_var.set('强制删除后需要更新 Key 列表与费用，正在使用当前登录会话重新读取；完成后再确认启动。可点击“停止管理”取消后续启动。')
        elif self.pending_start is token:
            self.pending_start = None
            self._update_buttons()

    def stop(self, quiet=False):
        was_running = self.observing or self.automatic or self.single_revoke_id is not None or self.pending_start is not None
        if self.pending_start is not None:
            self._diagnostic('start_refresh_cancelled')
        self.pending_start = None
        self.observing = False
        self.automatic = False
        self.single_revoke_id = None
        self.management_generation += 1
        self.candidate_queue.reset()
        self.queue_summary_var.set('候选队列已停止。已有操作仍按原记录核实。')
        if was_running or not quiet:
            self.action_states["start"] = "已停止"
        if self.delete_permit is not None:
            self.delete_permit.decide(False)
        self._cancel_collection_poll()
        if not quiet:
            self.action_states["stop"] = "已停止"
            self.status_var.set("管理已停止；已提交的撤销不能取消，结果仍会核实并记入日志。其余 Key 保持原状态。")
        self._update_buttons()

    def interrupt_operation(self):
        """Stop scheduling now; only release the session after process cleanup."""
        if self.interrupting:
            return
        self._diagnostic("interrupt_started", action=self.current_action or "idle", busy=self.busy)
        self.stop(quiet=True)
        self.pending_login = self.reset_on_login = False
        self.pending_change = None
        self.session_generation += 1
        self.last_keys = []
        self.validated_hash = None
        self._invalidate_collection(session=True)
        self.action_states.pop("check", None)
        if self.current_action:
            key = {"prepare_revoke": "revoke", "verify_revoke": "verify"}.get(self.current_action, self.current_action)
            self.action_states[key] = "正在中断"
        self.interrupting = True
        self.cleanup_failed = False
        self._action_state("interrupt", "清理中")
        self.status_var.set("管理已停止，正在中断当前操作并清理独立浏览器。撤销结果不明时会记入汇报，禁止自动重试。")
        self.progress_var.set("当前操作：清理独立浏览器与本次会话")
        started_at = time.monotonic()
        try:
            if self.browser is None:
                self.interrupt_future = Future()
                self.interrupt_future.set_result(None)
            else:
                self.interrupt_future = self.browser.interrupt()
        except Exception as exc:
            self.interrupting = False
            self.cleanup_failed = True
            self.closing = False
            self._action_state("interrupt", "清理失败")
            self._diagnostic("interrupt_failed", error_type=type(exc).__name__)
            self.error("中断清理未完成", exc, dialog=False, next_step="请再次点击“中断当前操作”重试；清理完成前不能重新登录。")
            return

        def wait_interrupt():
            elapsed = max(0, int(time.monotonic() - started_at))
            if not self.interrupt_future.done():
                self.progress_var.set(f"当前操作：清理独立浏览器并保存处理结果｜已用时 {elapsed} 秒")
                self.root.after(100, wait_interrupt)
                return
            try:
                self.interrupt_future.result()
            except Exception as exc:
                self.interrupting = False
                self.cleanup_failed = True
                self._action_state("interrupt", "清理失败")
                self._diagnostic("interrupt_failed", error_type=type(exc).__name__)
                self.error("中断清理未完成", exc, dialog=False, next_step="请再次点击“中断当前操作”重试；清理完成前不能重新登录。")
                if self.closing:
                    self.closing = False
                    self._update_buttons()
                return
            if self.busy:
                self.progress_var.set(f"当前操作：浏览器已清理，正在保存处理结果｜已用时 {elapsed} 秒")
                self.root.after(100, wait_interrupt)
                return
            self.interrupting = False
            self.cleanup_failed = False
            self.action_states["login"] = "待重新登录"
            self.action_states["collect"] = "待重新读取"
            for key, state in tuple(self.action_states.items()):
                if state == "正在中断":
                    self.action_states[key] = "已中断"
            self._action_state("interrupt", "已完成")
            self.progress_var.set("当前操作：空闲")
            self._diagnostic("interrupt_completed", elapsed=elapsed)
            if self.audit_failed:
                self.closing = False
                self._audit_notice()
            elif self.closing:
                self._finish_close()
            else:
                self.status_var.set("中断与会话清理已完成。账本和处理记录已保留；请重新登录，再读取费用。若有撤销结果不明，请先核实平台状态。")
        self.root.after(0, wait_interrupt)

    def _process_next(self):
        if self.recovery_mode or self.busy or not self.automatic or not self.observing or self.closing or self.audit_failed:
            return
        if self.store.workbook_hash() != self.validated_hash:
            self._invalidate_collection()
            self.action_states.pop("check", None)
            self.stop(quiet=True)
            raise ValueError('表格已变化，自动管理暂停，请检查后重新启动。')
        if not self._collection_is_current():
            self._invalidate_collection()
            self.queue_summary_var.set('候选队列等待新鲜证据，已记录的本次等待时间保留。')
            self.status_var.set('读取已过期或跨日，正在重新核验身份与费用；不会沿用旧许可。')
            self.collect()
            return
        self.refresh_rows()
        if self.store.unresolved_actions():
            self.stop(quiet=True)
            self.queue_summary_var.set('候选队列已阻塞，须先核实原操作结果。')
            self.status_var.set('有上次操作待核实，请先选中对应人员并点击“核实平台状态”。')
            return
        targets = [row for row in self.rows if eligible_for_automatic_action(row)]
        if not self.collection_has_fees:
            targets = [row for row in targets if Decimal(str(row['amount'])) == 0 or Decimal(str(row['share'])) == 0]
        stamp = time.monotonic()
        scope = (self.management_generation, *self._collection_context())
        self.candidate_queue.sync(targets, scope=scope, now=stamp)
        queue_state = self.candidate_queue.summary(now=stamp)
        self.queue_summary_var.set(
            f"待处理 {queue_state['count']} 枚，最长等待 {queue_state['oldest_wait_seconds']:.1f} 秒。"
            '当前按确认批次与稳定身份处理，预测记录不改变删除资格。')
        if not targets:
            if self.retry_collection:
                self._schedule_collection_poll()
            else:
                self._diagnostic('automatic_management_stopped',
                                 reason_code=getattr(self, 'collection_reason_code', '') or 'FEES_UNAVAILABLE',
                                 stage='automatic_quota_check', count=0,
                                 complete=self.collection_has_fees, retryable=False)
                self.stop(quiet=True)
                self.status_var.set('费用未通过核验，且当前没有可自动处理的目标；全自动管理已停止。请查看读取状态与诊断记录。')
            return
        selected = self.candidate_queue.select(now=stamp)
        if selected is not None:
            self._diagnostic('candidate_selected', count=queue_state['count'])
            self._execute_revoke(selected)

    def _current_revoke_target(self, row, *, forced=False):
        """Force uses verified identity; automatic management also needs quota evidence."""
        if (self.recovery_mode or not self.session_ready or (not forced and not self._collection_is_current())
                or self.store.workbook_hash() != self.validated_hash):
            raise GuardError('身份或表格核验已失效；自动管理还需要有效费用核验。未执行删除。')
        if self.store.unresolved_actions():
            raise GuardError('有上次操作待核实，请先核实平台状态；未执行新的删除。')
        current = next((item for item in self.store.evaluate()
                        if item['name'] == row.get('name')
                        and item.get('tracking_id') == row.get('tracking_id')), None)
        if forced:
            if not current or not current.get('tracking_id') or current['status'] == 'retired':
                raise GuardError('所选人员没有有效的当前 Key 绑定，未执行强制删除。')
            return current
        if (not current or not eligible_for_automatic_action(current)
                or (not self.collection_has_fees and current['amount'] != Decimal(0)
                    and current['share'] != Decimal(0))):
            raise GuardError('只能删除已绑定且根据当前有效费用达到额度的 Key；费用未知不按零元处理。')
        return current

    def inspect_revoke(self):
        """Exercise actual management preparation, without issuing any intent."""
        if (not self.config or self.recovery_mode or self.busy or self.observing or self.automatic
                or self.single_revoke_id is not None or self.pending_login or self.pending_change is not None
                or self.closing or self.interrupting or self.cleanup_failed or self.audit_failed):
            return
        row = self.selected()
        if not row or not row.get('tracking_id') or not self.session_ready:
            self.status_var.set('请先登录并读取费用以核对绑定，再选中目标检查删除准备；本检查不会删除。')
            return
        if self.store.workbook_hash() != self.validated_hash:
            self.status_var.set('人员表已变化，请重新检查表格和绑定后再进行只读预检。')
            return
        name, tracking_id = row['name'], row['tracking_id']
        store, fingerprint = self.store, self.validated_hash
        def done(result):
            if (self.store is not store or store.workbook_hash() != fingerprint
                    or self.validated_hash != fingerprint):
                raise GuardError('预检期间人员表或账本变化，结果未采用；未提交删除。')
            if (not isinstance(result, dict) or result.get('preflight_passed') is not True
                    or result.get('name') != name or result.get('tracking_id') != tracking_id
                    or type(result.get('key_count')) is not int or result['key_count'] < 2):
                raise GuardError('只读预检结果与所选目标不一致，未提交删除。')
            self._action_state('inspect', '准备通过')
            self.status_var.set(f'{name}：管理只读预检通过。尚未提交删除，删除权限和撤销结果仍待验证；费用核验时间未延长。')
        self._browser_action('inspect_revoke', done, name=name, tracking_id=tracking_id)

    def revoke_selected_once(self):
        """Read fresh identity, then force only the selected Key without a quota gate."""
        if (not self.config or self.recovery_mode or self.busy or self.observing or self.automatic
                or self.single_revoke_id is not None or self.pending_login or self.pending_change is not None
                or self.closing or self.interrupting or self.cleanup_failed or self.audit_failed):
            return
        row = self.selected()
        if not row or not row.get('name'):
            self.error('尚未开始强制删除', '请先填写平台 Key 名称并选中对应人员，再执行强制删除。当前没有发出删除请求。')
            return
        if self.browser is None or getattr(self.browser, 'closed', False):
            self.error('尚未开始强制删除', '请先点击登录，再强制删除所选 Key；不要求读取费用或达到额度。当前没有发出删除请求。')
            return
        try:
            if self.store.unresolved_actions():
                raise GuardError('有上次操作待核实，请先核实平台状态；未执行新的删除。')
            self.store.read_people()
            fingerprint = self.store.workbook_hash()
            config = self.store.load()
        except GuardError as exc:
            self.error('未开始强制删除', exc, dialog=False)
            return
        generation, session_generation = self.management_generation, self.session_generation
        store = self.store
        name, old_id = row['name'], row.get('tracking_id')

        def unchanged():
            return (not self.closing and not self.interrupting and not self.cleanup_failed
                    and not self.audit_failed and not self.recovery_mode and not self.observing
                    and not self.automatic and not self.pending_login and self.pending_change is None
                    and self.single_revoke_id is None and self.store is store
                    and self.management_generation == generation and self.session_generation == session_generation
                    and store.workbook_hash() == fingerprint)

        def confirm(target):
            try:
                if self.busy or not unchanged():
                    return
                if not messagebox.askyesno('强制删除选中 Key',
                    f"人员：{name}\n平台 Tracking ID：{target['tracking_id']}\n\n"
                    '将永久删除这一枚 Key，忽略个人额度、公共额度和累计费用；-1 也可强制删除。\n'
                    '本次只处理所选 Key，不启动周期管理。删除不可恢复。\n\n确认强制删除？', parent=self.root):
                    self._action_state('revoke', '已取消')
                    return
                if self.busy or not unchanged():
                    return
                self._execute_revoke(target, single=True, forced=True)
            except Exception as exc:
                self.stop(quiet=True)
                self.error('未开始强制删除', exc, dialog=False)

        def identity_ready(result):
            if not unchanged():
                raise GuardError('核验期间列表或会话变化，未执行强制删除。')
            matches = [key for key in result['keys'] if key['name'] == name]
            if len(matches) != 1 or (old_id and matches[0]['tracking_id'] != old_id):
                raise GuardError('平台所选名称缺失、重名或 Tracking ID 已变化，未执行删除。')
            target = matches[0]
            self._claim_targets(result['keys'])
            store.bind_forced_target(result['keys'], result['account_id'], name, target['tracking_id'])
            self.config = store.load()
            self._invalidate_collection()
            self.start_refresh_required = True
            self.session_ready = True
            self.validated_hash = fingerprint
            self.last_keys = result['keys']
            self._action_state('login', '已核验')
            self.refresh_rows()
            # _browser_action releases busy in its finally block. Confirm only
            # after that block, and recheck the captured target after the modal.
            self.root.after(0, lambda: confirm(target))

        self._browser_action('read_keys', identity_ready, account_id=config.account_id,
                             known_keys=store.active_tracking_ids())

    def _execute_revoke(self, row, *, single=False, forced=False):
        """Shared single-target executor; only the caller chooses polling or one-shot."""
        if forced and not single:
            raise GuardError('强制删除只允许用于用户选中的单枚 Key。')
        if (self.busy or self.closing or self.interrupting or self.cleanup_failed or self.audit_failed
                or self.pending_login or self.pending_change is not None):
            return
        if single:
            if self.observing or self.automatic or self.single_revoke_id is not None:
                return
        elif not self.automatic or not self.observing:
            return
        row = self._current_revoke_target(row, forced=forced)
        self._claim_targets(self.last_keys)
        if not self._target_is_claimed(row['tracking_id']):
            raise GuardError('当前目标未取得本机独占管理权，未建立撤销意图。')
        previous = self.store.latest_action(row['tracking_id'])
        if previous in {'pending', 'requested', 'unknown_result', 'unknown', 'failed', 'prepared', 'confirmed'}:
            self.stop(quiet=True)
            self.status_var.set(f"{row['name']} 上次操作尚未核实。请选中该人员，点击“核实平台状态”，再决定是否重新启动。")
            return
        from .browser_service import DeletePermit
        permit = DeletePermit(target_tracking_id=row['tracking_id'])
        self.delete_permit = permit
        checked_hash = self.validated_hash
        checked_session = self.session_generation
        operation_store = self.store
        operation_id = uuid4().hex
        # Claim is shared by automatic and force; force only waives fees.
        responsibility = self.responsibility_gate.claim(operation_store, operation_id, row['tracking_id'])
        if single:
            self.single_revoke_id = operation_id
            self.action_states.pop('stop', None)

        def active():
            return (self.single_revoke_id == operation_id if single
                    else self.automatic and self.observing)

        def finish_single():
            if single and self.single_revoke_id == operation_id:
                self.single_revoke_id = None

        def record(status, detail, *, evidence=None, recovery_evidence=None):
            operation_store.record_action(row['name'], row['tracking_id'], status, detail,
                                          operation_id=operation_id, evidence=evidence,
                                          recovery_evidence=recovery_evidence,
                                          responsibility=responsibility.to_dict() if status == 'pending' else None,
                                          intent='manual_force' if forced and status == 'pending' else None,
                                          continuation=status not in {'pending', 'requested'})
            if status == 'unknown_result':
                self._diagnostic('revocation_outcome', status=status)
        record('pending', ('用户选择强制删除，忽略额度与费用条件；' if forced else '达到额度，') +
               '准备通过控制台 HTTP 单次撤销；尚未进入提交步骤',
               evidence=operation_store.action_evidence(row['name'], row['tracking_id']))
        verified_absence = False
        audit_recorded = False
        authorization_marked = False

        def context_valid():
            return (not self.closing and active() and operation_store is self.store
                    and not self.recovery_mode and self._target_is_claimed(row['tracking_id'])
                    and self.session_ready and self.session_generation == checked_session
                    and (forced or self._collection_is_current())
                    and operation_store.workbook_hash() == checked_hash)

        def authorize():
            nonlocal authorization_marked
            if not permit.ready.is_set() or permit.decided.is_set():
                return
            if not context_valid():
                permit.decide(False)
                return
            current = next((r for r in operation_store.evaluate() if r['name'] == row['name']), None)
            if (not current or current['tracking_id'] != row['tracking_id'] or current['status'] == 'retired'
                    or (not forced and not eligible_for_automatic_action(current))):
                permit.decide(False)
                return
            # Commit before the worker is allowed to click, including after a crash.
            evidence = operation_store.action_evidence(row['name'], row['tracking_id'])
            if (evidence['workbook_hash'] != checked_hash or (not forced and
                    (evidence['status'] != 'triggered' or not (evidence['complete']
                        or Decimal(evidence['amount']) == 0 or Decimal(evidence['share']) == 0)))):
                permit.decide(False)
                return
            inventory = permit.prepared_inventory(row['tracking_id'])
            recovery_evidence = {'schema': 1, 'account_id': evidence['account_id'],
                                 **inventory, 'source': 'prepared_http_inventory_v1'}
            if not self.responsibility_gate.possible_send(operation_store, responsibility,
                    write_requested=lambda: record('requested',
                        ('所选目标与强制删除意图已复核，未要求达到额度；' if forced else '目标与额度已复核，') +
                        '控制台 HTTP 提交许可可能已发出；最终结果仍待平台核实',
                        evidence=evidence, recovery_evidence=recovery_evidence),
                    context_valid=context_valid):
                permit.decide(False)
                return
            authorization_marked = True
            if not context_valid():
                permit.decide(False)
                return
            permit.decide(True)

        def done(result):
            nonlocal verified_absence, audit_recorded
            self.delete_permit = None
            finish_single()
            if result.get('cancelled') and not permit.allowed and not authorization_marked:
                if not audit_recorded:
                    record('cancelled', '最终确认前已取消，未执行删除')
                    self.responsibility_gate.settle(operation_store, responsibility)
                    audit_recorded = True
                self.stop(quiet=True)
                self._action_state("revoke", "已取消")
                self.status_var.set(f"{row['name']}：提交许可发出前已取消，未执行删除。" +
                                    ("单次删除已结束。" if single else "自动管理已停止。"))
            elif result.get('absent') is True:
                verified_absence = True
                if not audit_recorded:
                    record('absent_verified', '撤销尝试后已核实平台列表中不再存在此 Tracking ID；不据此确定删除原因，在途费用仍可能入账')
                    self.responsibility_gate.settle(operation_store, responsibility)
                    audit_recorded = True
                self._action_state("revoke", "已确认不存在")
                self.status_var.set(f"{row['name']}：平台列表中已不存在该 Key，处理记录已保存。" +
                                    ("本次仅处理所选 Key，未启动周期读取。点击启动全自动管理时，会在当前会话重新读取并核验，无需中断或重登录。" if single and forced else
                                     "本次仅处理所选 Key，未启动周期读取。" if single else
                                     (("自动管理继续。" if self.collection_has_fees else "自动管理继续；费用未核验，仅零额度可处理。") if self.observing else "自动管理已停止。")))
            else:
                if not audit_recorded:
                    record('unknown_result', '未取得明确撤销结果，停止且不自动重试。')
                    audit_recorded = True
                self.stop(quiet=True)
                self._action_state("revoke", "结果未知，待核实")
                self.status_var.set('未取得明确撤销结果，已保存待核实记录；请核实平台状态，禁止自动重试。')
            self.refresh_rows()
            self.refresh_events()
            if not single:
                self._schedule_processing()

        def failed(exc):
            permit.decide(False)
            self.delete_permit = None
            finish_single()
            if verified_absence:
                # A local write/display error cannot change an observed outcome.
                self.status_var.set('平台已核实 Key 不存在，但记录保存或显示失败，请保留数据并重新核实。')
                return
            if isinstance(exc, RevocationNotSubmitted) and not permit.ready.is_set() and not permit.allowed:
                record('not_submitted', safe_error(exc))
                self.responsibility_gate.settle(operation_store, responsibility)
                self._action_state('revoke', '准备失败，未提交')
                self.refresh_rows()
            else:
                record('unknown_result', '自动处理未完成或结果不明，禁止自动重试：' + safe_error(exc))
                self._action_state('revoke', '结果未知，待核实')
            self.refresh_events()

        self._browser_action('revoke', done, on_error=failed, on_progress=authorize,
                             name=row['name'], tracking_id=row['tracking_id'], permit=permit)

    def _schedule_processing(self):
        generation = self.management_generation
        def advance():
            if (generation != self.management_generation or self.closing or self.audit_failed
                    or not self.automatic or not self.observing):
                return
            if self.busy:
                self.root.after(100, advance)
                return
            try:
                self._process_next()
            except Exception as exc:
                self.stop(quiet=True)
                if not self.closing:
                    self.error('自动管理已暂停', exc, dialog=False, next_step="请检查处理汇报，修正后重新读取费用并启动。")
        self.root.after(0, advance)

    def prepare_revoke(self):
        if self.recovery_mode or self.busy or self.closing or self.interrupting or self.cleanup_failed:
            return
        if self.audit_failed:
            self._audit_notice()
            return
        self.stop(quiet=True)
        if not self.config or self.store.workbook_hash() != self.validated_hash:
            messagebox.showinfo('请重新检查', '表格已变更或尚未校验，请检查并重新读取平台。', parent=self.root)
            return
        row = self.selected()
        if not row or row.get("status") != "triggered" or not row.get("tracking_id"):
            messagebox.showinfo("先选择待处理人员", "请选择已绑定平台 Key 且达到额度的人员。若状态未知，请先读取费用。", parent=self.root)
            return
        if (not self._collection_is_current() or not eligible_for_automatic_action(row)
                or (not self.collection_has_fees and row.get('amount') != Decimal(0)
                    and row.get('share') != Decimal(0))):
            self.status_var.set('请先重新读取当前人员的完整费用。历史费用可查看，但不会据此打开撤销流程。')
            return
        if not messagebox.askyesno("打开平台处理页面", f"准备处理 {row['name']} 的 API Key。\n删除 Key 后不能恢复原 Key，后续需要发新 Key。\n\n本程序定位对应列表行，由你核对后自行点击删除及确认。继续？", parent=self.root):
            return
        operation_store = self.store
        operation_id = uuid4().hex
        def record(state, detail):
            operation_store.record_action(row['name'], row['tracking_id'], state, detail,
                                          operation_id=operation_id, continuation=True)
        try:
            self._claim_targets(self.last_keys)
            responsibility = self.responsibility_gate.claim(operation_store, operation_id, row['tracking_id'])
            operation_store.record_action(row["name"], row["tracking_id"], "pending", "用户请求定位平台 Key 列表行；未执行删除",
                                          operation_id=operation_id,
                                          responsibility=responsibility.to_dict(),
                                          evidence=operation_store.action_evidence(row['name'], row['tracking_id']))
        except Exception as exc:
            self.error("未执行操作：日志无法保存", exc)
            return
        self.refresh_events()

        def done(_result):
            record("prepared", "已定位平台 Key 列表，等待用户自行点击删除及确认；未确认删除结果")
            self.refresh_events()
            self._action_state("revoke", "待平台确认")
            self.status_var.set("请在 DeepSeek 页面核对并自行确认或取消。随后点击“核实平台状态”。")
        def failed(exc):
            record("unknown_result", "定位列表行未完成，未执行删除：" + safe_error(exc))
            self.refresh_events()
        self._browser_action("prepare_revoke", done, on_error=failed, name=row["name"], tracking_id=row["tracking_id"])

    def verify_revoke(self):
        if not self.config or self.busy or self.observing or self.closing or self.interrupting or self.cleanup_failed:
            return
        if self.audit_failed:
            self._audit_notice()
            return
        row = self.selected()
        if not row or not row.get("tracking_id"):
            messagebox.showinfo("先选择人员", "请选择已有平台绑定的人员。", parent=self.root)
            return

        operation_store = self.store
        try:
            unresolved = operation_store.unresolved_actions()
            previous = next((action for action in unresolved if action['tracking_id'] == row['tracking_id']),
                            operation_store.latest_action_record(row['tracking_id']))
            if self.recovery_mode and not any(action['tracking_id'] == row['tracking_id']
                                             for action in operation_store.unresolved_actions()):
                raise GuardError('恢复模式只核实账本中已有的待核实操作。')
            known_keys = operation_store.recovery_tracking_ids(
                row['tracking_id'], previous.get('operation_id') if previous else None)
            self._claim_targets(self.last_keys, recovery_ids=known_keys)
        except GuardError as exc:
            self.error('未开始核实', exc, dialog=False)
            return
        # Preserve a legacy operation's lack of an ID; do not invent old evidence.
        continuing = previous is not None and (previous.get('status') in {
            'pending', 'prepared', 'requested', 'unknown_result', 'unknown', 'failed', 'confirmed', 'observation_failed'}
            or any(action['tracking_id'] == row['tracking_id'] for action in unresolved))
        operation_id = previous.get('operation_id') if continuing else uuid4().hex
        if not continuing:
            try:
                # Persist the read-only attempt too: the table may disappear
                # while its result is in flight. This authorizes no deletion.
                operation_store.record_action(row['name'], row['tracking_id'], 'pending',
                    '用户请求只读核实平台状态；未授权或提交撤销', operation_id=operation_id, read_only=True)
            except Exception as exc:
                self.error('未开始核实：记录无法保存', exc, dialog=False)
                return
        def record(state, detail):
            operation_store.record_action(row['name'], row['tracking_id'], state, detail,
                                          operation_id=operation_id, continuation=True)
        verified_absence = False
        audit_recorded = False
        def done(result):
            nonlocal verified_absence, audit_recorded
            absent = isinstance(result, dict) and result.get("absent") is True
            verified_absence = absent
            present = result.get('present') is True
            state = "absent_verified" if absent else ("present_verified" if present else "observation_failed")
            detail = "平台列表中已不存在该 Tracking ID；不据此认定由本程序删除，也不代表在途请求费用取消" if absent else ("本次核实时原 Key 仍存在；列表存在不代表 Key 可调用，也不能证明旧撤销请求以后不会生效。旧操作若有未知责任仍保留，自动管理不会因此恢复。" if present else "未确认 Key 已从平台消失，请检查平台状态；不能认为已经停用")
            if not audit_recorded:
                record(state, detail)
                audit_recorded = True
                if absent:
                    try:
                        self.responsibility_gate.settle_operation(operation_store, operation_id)
                    except GuardError:
                        self.status_var.set('平台缺席已记录，但共享责任尚未结清；请保留账本和登记核查。')
                        self.refresh_rows()
                        self.refresh_events()
                        return
            self.refresh_rows()
            self.refresh_events()
            self._action_state("verify", "已完成" if absent or present else "待核实")
            self.status_var.set(detail)
        def failed(exc):
            if verified_absence:
                return
            record("observation_failed", "本次只读核实失败；原操作责任与历史观察保留：" + safe_error(exc))
            self.refresh_events()
        self._browser_action("verify_revoke", done, on_error=failed, name=row["name"], tracking_id=row["tracking_id"], known_keys=known_keys)

    def _close_local_locks(self):
        try:
            if self.target_lock is not None:
                self.target_lock.close()
            self.folder_lock.close()
            return True
        except Exception as exc:
            self.closing = False
            self.cleanup_failed = True
            self.error('管理锁尚未释放', exc, dialog=False, next_step='请再次关闭窗口重试；当前不会继续管理。')
            self._update_buttons()
            return False

    def close(self):
        if self.closing:
            return
        self.closing = True
        self._diagnostic("close_requested", action=self.current_action or "idle", busy=self.busy)
        self._update_buttons()
        self.interrupt_operation()

    def _finish_close(self):
        """Cleanup has settled all browser futures before releasing the ledger."""
        if self.audit_failed:
            self.closing = False
            self._audit_notice()
            return
        if self.browser is None:
            if not self._close_local_locks():
                return
            self._diagnostic("ui_closed")
            self.root.destroy()
            return
        finished = threading.Event()
        errors = []

        def shutdown():
            try:
                self.browser.close()
            except Exception as exc:
                errors.append(exc)
            finally:
                finished.set()
        threading.Thread(target=shutdown, name="browser-close", daemon=True).start()

        def wait_close():
            if finished.is_set():
                if errors:
                    self.closing = False
                    self.cleanup_failed = True
                    self._action_state("interrupt", "清理失败")
                    self.error("浏览器关闭未完成", errors[0], dialog=False,
                               next_step="窗口与账本锁已保留。请点击“中断当前操作”重试清理，再关闭。")
                    self._diagnostic("close_failed", error_type=type(errors[0]).__name__)
                    return
                if self.audit_failed:
                    self.closing = False
                    self._audit_notice()
                    return
                if not self._close_local_locks():
                    return
                self._diagnostic("ui_closed")
                self.root.destroy()
            else:
                self.root.after(150, wait_close)
        self.root.after(150, wait_close)


def run(folder: Path):
    root = tk.Tk()
    App(root, folder)
    root.mainloop()
