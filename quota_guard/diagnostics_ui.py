"""A bounded, on-demand diagnostic viewer with no continuous memory buffer."""
import os
import queue
import threading
import tkinter as tk
from tkinter import ttk

from .diagnostics import read_recent_diagnostics
from .appearance import apply_theme
from .local_ai_assistant import EXAMPLES, STAGES, explain, facts


def _assistant_tab(notebook, app):
    tab = ttk.Frame(notebook, padding=8)
    notebook.add(tab, text='本地 AI 助手')
    tab.columnconfigure(0, weight=1)
    tab.rowconfigure(2, weight=1)
    ttk.Label(tab, text='选择一条历史事件或教学示例。程序先列出该条记录的事实；按“本地 AI 解释”后，'
              '离线模型只接收分类并排列阅读建议。记录的时间与当前账本可能不同。',
              wraplength=490).grid(row=0, column=0, sticky='ew', pady=(0, 6))
    selector = ttk.Combobox(tab, state='readonly')
    selector.grid(row=1, column=0, sticky='ew', pady=(0, 6))
    panel = ttk.Frame(tab)
    panel.grid(row=2, column=0, sticky='nsew')
    panel.columnconfigure(0, weight=1)
    panel.rowconfigure(0, weight=1)
    out = tk.Text(panel, state='disabled', wrap='word', width=30, height=4)
    out.grid(row=0, column=0, sticky='nsew')
    scroller = ttk.Scrollbar(panel, command=out.yview)
    scroller.grid(row=0, column=1, sticky='ns')
    out.configure(yscrollcommand=scroller.set)
    options = []
    revision = 0
    mine = None
    timer = None

    def show(text):
        out.configure(state='normal')
        out.delete('1.0', 'end')
        out.insert('end', text)
        out.configure(state='disabled')

    def describe():
        pos = selector.current()
        item = options[pos]
        return item, f'{selector.get()}\n\n程序记录的事实\n{facts(item)}'

    def select(event=None):
        nonlocal revision
        revision += 1
        if options and selector.current() >= 0:
            show(describe()[1])

    def load(cases):
        entries = [(f'历史 #{index} {stamp} · {STAGES[item.stage]} · {item.reason}', item)
                   for index, (stamp, item) in enumerate(cases, start=1)]
        entries.extend((f'教学示例 · {STAGES[item.stage]} · {item.reason}', item) for item in EXAMPLES)
        options[:] = [item for _, item in entries]
        selector.configure(values=[title for title, _ in entries])
        selector.current(0)
        select()

    def run():
        nonlocal mine, timer
        if mine is not None:
            return
        item, description = describe()
        previous = getattr(app, '_local_ai_thread', None)
        if previous is not None and previous.is_alive():
            show(description + '\n\n上一次本地推理仍在退出，请稍后再解释。')
            return
        source_revision = revision
        replies = queue.SimpleQueue()

        def work():
            replies.put(explain(item))

        mine = threading.Thread(target=work, name='local-ai-explanation', daemon=True)
        app._local_ai_thread = mine
        button.configure(state='disabled')
        show(description + '\n\n正在本机加载模型并排列建议，可继续使用主窗口。')
        mine.start()

        def receive():
            nonlocal mine, timer
            timer = None
            try:
                source, details = replies.get_nowait()
            except queue.Empty:
                timer = tab.after(100, receive)
                return
            mine = None
            button.configure(state='normal')
            if source_revision == revision:
                show(f'{description}\n\n{source}\n{details}')

        timer = tab.after(100, receive)

    def closing(event):
        if event.widget == tab and timer is not None:
            tab.after_cancel(timer)

    button = ttk.Button(tab, text='本地 AI 解释', command=run)
    button.grid(row=3, column=0, sticky='w', pady=(5, 0))
    selector.bind('<<ComboboxSelected>>', select)
    tab.bind('<Destroy>', closing)
    return load


def open_diagnostics_viewer(app):
    existing = getattr(app, '_diagnostics_window', None)
    if existing is not None and existing.winfo_exists():
        existing.lift()
        return
    dialog = tk.Toplevel(app.root)
    app._diagnostics_window = dialog
    dialog.title('诊断日志与本地 AI 助手')
    dialog.resizable(True, True)
    dialog.geometry('900x580')
    dialog.minsize(600, 360)
    dialog.columnconfigure(0, weight=1)
    dialog.rowconfigure(1, weight=1)
    heading = ttk.Label(dialog, text='诊断日志保留 14 天，单文件约 2 MB，通常最多 12 份；活跃日志暂不清理。\n只显示最近 300 条（最多 256 KiB），点击刷新读取。账本与删除处理记录不会自动清理。',
                        wraplength=820, padding=12)
    heading.grid(row=0, column=0, sticky='ew')
    pages = ttk.Notebook(dialog)
    pages.grid(row=1, column=0, sticky='nsew', padx=12)
    frame = ttk.Frame(pages)
    pages.add(frame, text='诊断日志')
    load_cases = _assistant_tab(pages, app)
    frame.columnconfigure(0, weight=1)
    frame.rowconfigure(0, weight=1)
    content = tk.Text(frame, wrap='word', state='disabled', width=60, height=10)
    content.grid(row=0, column=0, sticky='nsew')
    scrollbar = ttk.Scrollbar(frame, command=content.yview)
    scrollbar.grid(row=0, column=1, sticky='ns')
    content.configure(yscrollcommand=scrollbar.set)
    status = tk.StringVar()
    status_label = ttk.Label(dialog, textvariable=status, padding=(12, 6), wraplength=820)
    status_label.grid(row=2, column=0, sticky='ew')
    footer = ttk.Frame(dialog, padding=(12, 0, 12, 12))
    footer.grid(row=3, column=0, sticky='ew')

    def refresh():
        preview = read_recent_diagnostics(app.diagnostics_dir)
        load_cases(preview.cases)
        content.configure(state='normal')
        content.delete('1.0', 'end')
        content.insert('end', preview.text or '暂无诊断记录。')
        content.configure(state='disabled')
        content.see('end')
        notice = f'显示 {preview.record_count} 条；跳过 {preview.skipped_lines} 条不完整记录。'
        if preview.truncated:
            notice += ' 较早内容已省略。'
        if preview.unavailable:
            notice += ' 部分日志暂时无法读取，可稍后刷新。'
        if app.diagnostics.disabled:
            notice += ' 当前诊断写入失败；业务处理记录单独保存。'
        status.set(notice)

    def folder():
        try:
            app.diagnostics_dir.mkdir(parents=True, exist_ok=True)
            os.startfile(str(app.diagnostics_dir))
        except OSError as exc:
            app.error('无法打开诊断日志文件夹', exc)

    ttk.Button(footer, text='刷新', command=refresh).pack(side='left')
    ttk.Button(footer, text='打开日志文件夹', command=folder).pack(side='left', padx=8)
    ttk.Button(footer, text='关闭', command=dialog.destroy).pack(side='right')
    def rewrap(event):
        if event.widget is dialog:
            heading.configure(wraplength=max(250, event.width - 48))
            status_label.configure(wraplength=max(250, event.width - 48))
    dialog.bind('<Configure>', rewrap)
    apply_theme(app.root, app.current_theme)
    refresh()
    app._action_state('diagnostics', '已打开')
