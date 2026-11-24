"""Desktop workspace layout and presentation of the next available user action."""
import tkinter as tk
from tkinter import ttk

from .fee_review import FeeReviewFrame, amount_text
from .window_layout import ScrollableContent


def next_step(app):
    """Use existing application facts for guidance. Buttons still invoke the guarded commands."""
    if app.cleanup_failed or app.interrupting:
        return "会话清理", "等待独立浏览器和本地模型完成清理。", "interrupt", "重试清理"
    if app.audit_failed:
        return "保存上次处理记录", "账本里已有原操作。恢复本地保存后可继续核对。", "save_audit", "恢复保存"
    if app.busy or app.pending_start is not None or app.pending_login:
        return "正在处理", "进度与用时显示在窗口底部，处理完成后可继续。", None, ""
    if app.observing:
        return "全自动管理已开启", app.queue_summary_var.get(), "report_view", "查看处理记录"
    if app.account_mismatch:
        return "当前登录账户与账本不同", "重新登录原账户。需要管理另一个账户时，可在工具中为它建立独立账本。", "login", "重新登录"
    if not app.config:
        if app.open_ledger_failed:
            return "打开已有账本", "选择正确的数据文件夹以恢复原有费用和处理记录。", "folder", "选择账本"
        return "建立第一份额度名单", "设置总预算和分配人数，填入要管理的 Key 名称及个人额度。", "setup", "新建列表"
    if app.recovery_mode:
        return "恢复当前人员表", "导入原人员表。已有费用、绑定和待核实操作仍在这个账本中。", "import_people", "恢复人员 Excel"
    if any(row.get("status") in {"recovery_pending", "present_unresolved"} for row in app.rows):
        return "核实上次操作", "从处理记录查看原操作及平台最后一次核实结果。", "report_view", "查看待核实记录"
    if not any(row.get("name") and row.get("amount") is not None and row["amount"] >= 0 for row in app.rows):
        return "填写要管理的 Key", "填平台上的 Key 名称和个人额度。空名称与额度 −1 会跳过；额度 0 可立即触发。", "edit", "填写人员表"
    if (app.browser is None or getattr(app.browser, "closed", False)
            or not app.session_ready and app.action_states.get("login") in {"待核验", "待重新登录"}):
        if app.is_demo:
            return "连接本机模拟控制台", "连接后可读取示例费用，查看各人距离额度还有多少。", "login", "连接示例控制台"
        return "登录平台账户", "在独立浏览器中完成官方登录，再回到这里读取费用。", "login", "打开登录窗口"
    review = app.fee_review.data
    if not review or not review.identity_current:
        return "读取并核对费用", "本次读取会核对账户、Key 绑定和累计费用。完成后自动打开费用核对页。", "collect", "读取示例费用" if app.is_demo else "立即读取费用"
    if not review.fees_current:
        return "本轮费用待补全", "可重新读取费用；已核验绑定的零额度人员仍可在核对页查看。", "collect", "重新读取费用"
    if app.needs_scope_recovery:
        return "准备或恢复管理记录", "费用已经核对。已有记录沿原账本恢复；首次使用可在向导中建立本机管理范围。", "responsibility", "管理准备 / 恢复"
    return "核对后开启管理", "查看已达线和待补全的人员；确认开启后，程序按设置间隔持续读取并处理达线 Key。", "review_view", "查看费用核对"


class DesktopHome:
    def __init__(self, app):
        self.app = app
        app.buttons = {}
        app.path_var = tk.StringVar(value=f"数据文件夹：{app.folder}")
        app.summary_var = tk.StringVar(value="正在查找已有账本")
        app.collection_summary_var = tk.StringVar(value="费用核验：尚未读取。")
        app.queue_summary_var = tk.StringVar(value="候选队列尚未运行。")
        app.forecast_summary_var = tk.StringVar(value="预测记录尚未读取费用。")
        app.status_var = tk.StringVar(value="正在打开工作台")
        app.progress_var = tk.StringVar(value="当前操作：空闲")
        app.audit_var = tk.StringVar()
        app.detail_var = tk.StringVar(value="选中一行可查看完整 Tracking ID 和处理依据。")
        self.status = tk.StringVar(value="管理未开启")
        self.title = tk.StringVar()
        self.hint = tk.StringVar()
        self.roster_summary = tk.StringVar()
        self.primary_key = None

        footer = ttk.Frame(app.root, padding=(18, 5))
        footer.pack(side="bottom", fill="x")
        note = ttk.Label(footer, textvariable=app.status_var, justify="left", wraplength=950)
        note.pack(anchor="w")
        self.progress = ttk.Label(footer, textvariable=app.progress_var, style="Muted.TLabel", justify="left", wraplength=950)
        self.audit = ttk.Label(footer, textvariable=app.audit_var, style="Warn.TLabel", justify="left", wraplength=950)
        footer.bind("<Configure>", lambda e: self._wrap((note, self.progress, self.audit), e.width - 40))
        app.main_content = ScrollableContent(app.root, padding=(16, 10), fit_width=True)
        app.main_content.pack(fill="both", expand=True)
        outer = app.main_content.content
        header = ttk.Frame(outer)
        header.pack(fill="x", pady=(0, 10))
        title = "DeepSeek 额度管理"
        if app.is_demo:
            title += "  ·  示例账本"
        ttk.Label(header, text=title, style="Title.TLabel", font=("Microsoft YaHei UI", 17, "bold")).pack(side="left")
        self.state_badge = ttk.Label(header, textvariable=self.status, style="Workspace.State.TLabel", padding=(10, 4))
        self.state_badge.pack(side="right")
        self.stop = self.button(header, "stop", "停止管理", app.stop)
        self.interrupt = self.button(header, "interrupt", "中断当前操作", app.interrupt_operation)

        tabs = app.review_tabs = ttk.Notebook(outer)
        tabs.pack(fill="both", expand=True)
        app.overview_tab = ttk.Frame(tabs, padding=(14, 12))
        app.fee_review = FeeReviewFrame(tabs, on_start=app._button_command("start", app.start))
        app.buttons["start"] = app.fee_review.start_button
        app.reports_tab = ttk.Frame(tabs, padding=(12, 10))
        self.tools_tab = ttk.Frame(tabs, padding=(14, 10))
        for panel, label in ((app.overview_tab, "管理工作台"), (app.fee_review, "费用核对"),
                             (app.reports_tab, "处理记录"), (self.tools_tab, "工具与设置")):
            tabs.add(panel, text=label)
        tabs.bind("<<NotebookTabChanged>>", app._review_tab_changed)
        self._build_overview(app.overview_tab)
        self._build_reports(app.reports_tab)
        self._build_tools(self.tools_tab)
        app.audit_var.trace_add("write", self._audit_visibility)
        for page in (app.overview_tab, app.fee_review, app.reports_tab, self.tools_tab):
            page.bind("<Configure>", self._fit_page, add=True)
        tabs.bind("<<NotebookTabChanged>>", self._fit_page, add=True)

    def _fit_page(self, _event=None):
        app = self.app
        tabs = app.review_tabs
        selected = tabs.select()
        if not selected or not tabs.winfo_ismapped():
            return
        page = tabs.nametowidget(selected)
        natural = page.winfo_reqheight()
        # The rosters and timeline can shrink their tables and scroll them.
        # Settings forms keep their full height in the outer scroll container.
        height = natural if page is self.tools_tab else min(natural, max(440, app.main_content.canvas.winfo_height() - tabs.winfo_y() - 65))
        if int(tabs.cget("height") or 0) != height:
            tabs.configure(height=height)

    @staticmethod
    def _wrap(labels, width):
        width = max(180, width)
        for label in labels:
            if int(label.cget("wraplength")) != width:
                label.configure(wraplength=width)

    def button(self, parent, key, label, command):
        app = self.app
        button = ttk.Button(parent, text=label, command=app._button_command(key, command))
        app.buttons[key] = button
        return button

    def _build_overview(self, pane):
        app = self.app
        hero = ttk.Frame(pane, style="Workspace.Hero.TFrame", padding=(18, 12))
        hero.pack(fill="x", pady=(0, 12))
        self.stage = tk.StringVar(value="首次使用")
        ttk.Label(hero, textvariable=self.stage, style="Workspace.Stage.TLabel", font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w")
        heading = ttk.Label(hero, textvariable=self.title, style="Workspace.Heading.TLabel", font=("Microsoft YaHei UI", 16, "bold"), wraplength=780)
        heading.pack(anchor="w", pady=(3, 2))
        hint = ttk.Label(hero, textvariable=self.hint, style="Workspace.Hint.TLabel", wraplength=780, justify="left")
        hint.pack(anchor="w")
        hero.bind("<Configure>", lambda e: self._wrap((heading, hint), e.width - 40))
        self.action_bar = ttk.Frame(hero, style="Workspace.Hero.TFrame")
        self.action_bar.pack(fill="x", pady=(10, 0))
        self.primary = ttk.Button(self.action_bar, style="Workspace.Primary.TButton", command=self._primary)
        self.primary.pack(side="left")
        if app.on_demo is not None:
            self.demo = ttk.Button(self.action_bar, text="体验示例账本", command=app.on_demo)
            self.demo.pack(side="left", padx=(8, 0))

        roster = ttk.Frame(pane)
        roster.pack(fill="x", pady=(0, 4))
        ttk.Label(roster, text="人员与额度", font=("Microsoft YaHei UI", 13, "bold")).pack(side="left")
        self.edit = self.button(roster, "edit", "填写两列表格", app.edit_people)
        self.edit.pack(side="right")
        summary = ttk.Label(pane, textvariable=self.roster_summary, style="Muted.TLabel", wraplength=820)
        summary.pack(anchor="w", pady=(0, 7))
        pane.bind("<Configure>", lambda e: self._wrap((summary,), e.width - 30), add=True)
        grid = ttk.Frame(pane)
        grid.pack(fill="both", expand=True)
        grid.columnconfigure(0, weight=1)
        grid.rowconfigure(0, weight=1)
        cols = ("name", "amount", "share", "spent", "status")
        app.tree = ttk.Treeview(grid, columns=cols, show="headings", selectmode="browse", height=6)
        for col, column_label, width in zip(cols, ("Key 名称 / 人员", "个人额度（元）", "公共额度（元）",
                                              "累计费用（元）", "当前状态"), (174, 115, 115, 125, 225)):
            app.tree.heading(col, text=column_label)
            app.tree.column(col, width=width, minwidth=78, anchor="w" if col in {"name", "status"} else "e")
        app.tree.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(grid, orient="vertical", command=app.tree.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(grid, orient="horizontal", command=app.tree.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        app.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        app.tree.bind("<<TreeviewSelect>>", app._selection)
        self.empty = ttk.Label(grid, text="尚未添加人员", style="Workspace.Empty.TLabel")
        self.row_bar = ttk.Frame(pane)
        self.row_bar.pack(fill="x", pady=(7, 0))
        self.row_menu = ttk.Menubutton(self.row_bar, text="选中 Key 的操作 ▾")
        self.row_menu.pack(side="right")
        menu = tk.Menu(self.row_menu, tearoff=False)
        self.row_menu.configure(menu=menu)
        for key, text, cmd in (("inspect", "检查删除准备（只读）", app.inspect_revoke),
                               ("verify", "核实平台状态", app.verify_revoke),
                               ("revoke", "强制删除选中 Key…", app.revoke_selected_once)):
            button = self.button(self.row_bar, key, text.rstrip("…"), cmd)
            if key == "revoke":
                menu.add_separator()
            menu.add_command(label=text, command=button.invoke)
        def sync_menu():
            for index, key in ((0, "inspect"), (1, "verify"), (3, "revoke")):
                menu.entryconfigure(index, state=app.buttons[key].cget("state"))
        menu.configure(postcommand=sync_menu)
        detail = ttk.Label(self.row_bar, textvariable=app.detail_var, wraplength=640, justify="left")
        detail.pack(side="left", fill="x", expand=True)
        self.row_bar.bind("<Configure>", lambda e: self._wrap((detail,), e.width - self.row_menu.winfo_reqwidth() - 20))

    def _build_reports(self, pane):
        from .activity_report import ActivityReportFrame
        app = self.app
        bar = ttk.Frame(pane)
        bar.pack(fill="x", pady=(0, 6))
        self.button(bar, "reports", "刷新汇报", lambda: app.refresh_events(show_status=True)).pack(side="left")
        self.button(bar, "diagnostics", "诊断日志 / 本地 AI 建议", app.open_diagnostics).pack(side="left", padx=6)
        self.button(bar, "logs", "打开日志文件夹", app.open_folder).pack(side="left")
        app.activity_report = ActivityReportFrame(pane, on_locate=app._locate_report_operation)
        app.activity_report.pack(fill="both", expand=True)
        app.report_text = app.activity_report.raw_text

    def _build_tools(self, pane):
        app = self.app
        tools = ttk.Notebook(pane)
        tools.pack(fill="both", expand=True)
        files = ttk.Frame(tools, padding=14)
        platform = ttk.Frame(tools, padding=14)
        settings = ttk.Frame(tools, padding=14)
        for page, title in ((files, "账本与表格"), (platform, "平台与恢复"), (settings, "管理偏好")):
            tools.add(page, text=title)

        def group(root, title, commands):
            ttk.Label(root, text=title, font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", pady=(6, 5))
            row = ttk.Frame(root)
            row.pack(fill="x", pady=(0, 6))
            for index, (key, text, cmd) in enumerate(commands):
                self.button(row, key, text, cmd).grid(row=index // 3, column=index % 3, sticky="ew", padx=(0, 6), pady=3)
        group(files, "账本", (("folder", "选择数据文件夹", app.choose_folder), ("setup", "新建列表", app.setup),
                             ("date", "统计方式", app.change_statistics_mode)))
        path = ttk.Label(files, textvariable=app.path_var, wraplength=760)
        path.pack(anchor="w")
        summary = ttk.Label(files, textvariable=app.summary_var, wraplength=760)
        summary.pack(anchor="w", pady=(4, 12))
        files.bind("<Configure>", lambda e: self._wrap((path, summary), e.width - 30))
        group(files, "人员表与账单", (("excel", "用 Excel/WPS 打开", app.open_excel),
              ("import_people", "导入人员 Excel", app.import_people), ("template", "保存 Excel 模板", app.save_template),
              ("check", "检查表格", app.check_people), ("import", "检查官方导出", app.import_billing)))
        group(platform, "账户与费用", (("login", "连接示例控制台" if app.is_demo else "登录 / 重新登录", app.login),
                                      ("collect", "立即读取费用", app.collect)))
        group(platform, "账本恢复", (("responsibility", "管理准备 / 恢复", app.initialize_responsibility),
              ("save_audit", "保存处理记录", app.retry_audit), ("account_ledger", "为当前账户建账本", app.resolve_account_mismatch)))
        queue = ttk.Label(platform, textvariable=app.queue_summary_var, wraplength=760)
        queue.pack(anchor="w", pady=(12, 3))
        forecast = ttk.Label(platform, textvariable=app.forecast_summary_var, wraplength=760, style="Muted.TLabel")
        forecast.pack(anchor="w")
        platform.bind("<Configure>", lambda e: self._wrap((queue, forecast), e.width - 30))

        ttk.Label(settings, text="自动管理间隔", font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        row = ttk.Frame(settings)
        row.pack(fill="x", pady=(5, 3))
        app.poll_interval_var = tk.StringVar(value=str(app.poll_interval_seconds))
        app.poll_interval_entry = ttk.Entry(row, textvariable=app.poll_interval_var, width=8)
        app.poll_interval_entry.pack(side="left")
        app.poll_interval_entry.bind("<Return>", lambda _event: app.apply_poll_interval())
        ttk.Label(row, text="秒").pack(side="left", padx=5)
        self.button(row, "poll_interval", "保存间隔", app.apply_poll_interval).pack(side="left")
        app.poll_feedback_var = tk.StringVar(value=app.poll_preference_notice or "每轮处理完成后等待，可设置 1～3600 秒。")
        feedback = ttk.Label(settings, textvariable=app.poll_feedback_var, style="Muted.TLabel", wraplength=760)
        feedback.pack(anchor="w")
        ttk.Label(settings, text="新表默认个人额度", font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w", pady=(14, 2))
        row = ttk.Frame(settings)
        row.pack(fill="x", pady=(3, 3))
        ttk.Label(row, text="斩杀线（元）").pack(side="left")
        app.kill_line_var = tk.StringVar(value=amount_text(app.table_defaults.kill_line))
        ttk.Entry(row, textvariable=app.kill_line_var, width=12).pack(side="left", padx=5)
        app.table_fill_var = tk.StringVar(value="-1（跳过管理）" if app.table_defaults.fill_mode == "skip" else "斩杀线")
        ttk.Combobox(row, textvariable=app.table_fill_var, values=("-1（跳过管理）", "斩杀线"),
                     width=14, state="readonly").pack(side="left", padx=5)
        self.button(row, "table_defaults", "保存新表默认", app.apply_table_defaults).pack(side="left")
        app.defaults_feedback_var = tk.StringVar(value=app.table_defaults_notice or "仅预填新表；已有个人额度按原值使用。")
        defaults = ttk.Label(settings, textvariable=app.defaults_feedback_var, style="Muted.TLabel", wraplength=760)
        defaults.pack(anchor="w")
        settings.bind("<Configure>", lambda e: self._wrap((feedback, defaults), e.width - 30))
        app.theme_button = ttk.Button(settings, command=app.cycle_theme)
        app.theme_button.pack(anchor="w", pady=(18, 0))

    def _primary(self):
        app = self.app
        if self.primary_key == "review_view":
            app._refresh_fee_review(select=True)
        elif self.primary_key == "report_view":
            app.review_tabs.select(app.reports_tab)
        elif self.primary_key in app.buttons:
            app.buttons[self.primary_key].invoke()

    def _audit_visibility(self, *_args):
        if self.app.audit_var.get():
            self.audit.pack(anchor="w")
        else:
            self.audit.pack_forget()

    def update(self):
        app = self.app
        title, hint, action, label = next_step(app)
        self.title.set(title)
        self.hint.set(hint)
        self.primary_key = action
        mode = "已开启" if app.observing else "未开启"
        if app.is_demo:
            self.status.set(f"本机模拟控制台   ·   管理{mode}")
        else:
            self.status.set(f"全自动管理{mode}")
        self.stage.set("运行状态" if app.busy or app.observing or app.interrupting else "接下来")
        if action:
            if not self.primary.winfo_manager():
                options = {"before": self.demo} if self.app.on_demo is not None else {}
                self.primary.pack(side="left", **options)
            state = app.buttons[action].cget("state") if action in app.buttons else "normal"
            self.primary.configure(text=label, state=state)
        else:
            self.primary.pack_forget()
        for button, show in ((self.stop, app.observing or app.pending_start is not None or app.single_revoke_id is not None),
                             (self.interrupt, app.busy or app.interrupting or app.cleanup_failed)):
            if show and not button.winfo_manager():
                button.pack(side="right", padx=6)
            elif not show:
                button.pack_forget()
        if app.busy or app.interrupting:
            self.progress.pack(anchor="w")
        else:
            self.progress.pack_forget()
        if app.config and action != "edit":
            self.edit.pack(side="right")
        else:
            self.edit.pack_forget()
        if app.config:
            count = sum(bool(r.get("name")) and r.get("amount") is not None and r["amount"] >= 0 for r in app.rows)
            period = "各 Key 创建以来累计" if app.config.fee_basis == "key_lifetime" else f"自 {app.config.start_date} 起累计"
            self.roster_summary.set(f"纳入管理 {count} 人    ·    公共人均额度 ¥{amount_text(app.config.share)}    ·    {period}")
        else:
            self.roster_summary.set("新建名单后，这里会列出个人额度、公共额度、累计费用和当前状态。")
        if app.tree.get_children():
            self.empty.place_forget()
        else:
            self.empty.place(in_=app.tree, relx=.5, rely=.56, anchor="center")
        self.row_menu.configure(state="normal" if app.tree.selection() else "disabled")
