"""Autosaving personnel editor; only the table expands with the window."""
import tkinter as tk
from tkinter import ttk, messagebox

from .core import GuardError, Person, clean_name, money
from .people_editor import PeopleEditorSession


def open_people_editor(app, initial_amount=None):
    # Import presentation helpers lazily to avoid an App/editor import cycle.
    from .ui import amount_text, safe_error
    from .appearance import apply_theme

    if (app.busy or app.observing or app.closing or app.pending_change is not None
            or app.pending_login or app.interrupting or app.cleanup_failed or app.audit_failed
            or app.single_revoke_id is not None):
        app.status_var.set('请先停止管理并等待当前操作完成，再编辑人员表。')
        return
    try:
        if initial_amount is not None:
            initial_amount = money(initial_amount, allow_skip=True)
        session = PeopleEditorSession(app.store)
    except Exception as exc:
        app.error('无法读取表格', exc)
        return
    original_store = app.store
    dialog = tk.Toplevel(app.root)
    dialog.title('填写两列表格')
    dialog.resizable(True, True)
    dialog.transient(app.root)
    dialog.grab_set()
    dialog.columnconfigure(0, weight=1)
    dialog.rowconfigure(0, weight=1)
    body = ttk.Frame(dialog, padding=16)
    body.grid(sticky='nsew')
    body.columnconfigure(0, weight=1)
    body.rowconfigure(1, weight=1, minsize=70)
    hint = ttk.Label(body, text='已填写姓名：-1 跳过　｜　0 零额度　｜　正数为个人累计额度\n未填写姓名的行保留金额，不参与管理。有效输入停顿 0.6 秒后自动保存。', wraplength=700)
    hint.grid(row=0, column=0, sticky='ew', pady=(0, 10))
    table_frame = ttk.Frame(body)
    table_frame.grid(row=1, column=0, sticky='nsew')
    table_frame.columnconfigure(0, weight=1)
    table_frame.rowconfigure(0, weight=1)
    table = ttk.Treeview(table_frame, columns=('name', 'amount'), show='headings',
                         selectmode='browse', height=5)
    table.heading('name', text='API名/人员名')
    table.heading('amount', text='个人金额（元）')
    table.column('name', width=370, minwidth=80)
    table.column('amount', width=180, minwidth=80)
    table.grid(row=0, column=0, sticky='nsew')
    scrollbar = ttk.Scrollbar(table_frame, command=table.yview)
    scrollbar.grid(row=0, column=1, sticky='ns')
    table.configure(yscrollcommand=scrollbar.set)
    form = ttk.Frame(body)
    form.grid(row=2, column=0, sticky='ew', pady=10)
    form.columnconfigure(0, weight=2)
    form.columnconfigure(1, weight=1)
    name_var, amount_var = tk.StringVar(), tk.StringVar()
    ttk.Label(form, text='名字').grid(row=0, column=0, sticky='w')
    ttk.Label(form, text='个人金额（元）').grid(row=0, column=1, sticky='w')
    name_entry = ttk.Entry(form, textvariable=name_var, width=24)
    name_entry.grid(row=1, column=0, sticky='ew', padx=(0, 12))
    amount_entry = ttk.Entry(form, textvariable=amount_var, width=18)
    amount_entry.grid(row=1, column=1, sticky='ew')
    operations = ttk.Frame(body)
    operations.grid(row=3, column=0, sticky='ew', pady=(0, 8))
    editor_status = tk.StringVar(value='修改会自动保存；取消或关闭窗口保留已保存的内容。')
    feedback = ttk.Label(body, textvariable=editor_status, wraplength=700)
    feedback.grid(row=4, column=0, sticky='ew', pady=(0, 8))
    footer = ttk.Frame(body)
    footer.grid(row=5, column=0, sticky='ew')
    footer.columnconfigure(1, weight=1)
    state = {'timer': None, 'loading': False, 'dirty': False, 'index': None, 'selection_conflict': False,
             'rows': list(session.rows), 'closed': False}
    # Compatibility for callers supplying a draft; normal new-list creation
    # already persists defaults and opens without this argument.
    if initial_amount is not None:
        state['rows'] = [Person(p.name, initial_amount if not p.name and p.amount == -1 else p.amount)
                         for p in state['rows']]

    def cancel_timer():
        if state['timer'] is not None:
            app.root.after_cancel(state['timer'])
            state['timer'] = None

    def guard():
        if (app.store is not original_store or app.busy or app.observing or app.closing
                or app.pending_change is not None or app.pending_login or app.interrupting
                or app.cleanup_failed or app.audit_failed or app.single_revoke_id is not None):
            raise GuardError('账本或运行状态已变化，请关闭窗口后重新编辑。')

    def show_fields(index):
        state['loading'] = True
        state['index'] = index
        person = state['rows'][index] if index is not None else Person('', app.table_defaults.amount)
        name_var.set(person.name)
        amount_var.set(amount_text(person.amount))
        state['loading'] = False
        state['dirty'] = False
        state['selection_conflict'] = False

    def render(index=None):
        state['loading'] = True
        table.delete(*table.get_children())
        for i, person in enumerate(state['rows']):
            table.insert('', 'end', iid=str(i), values=(person.name, amount_text(person.amount)))
        index = min(index or 0, len(state['rows']) - 1) if state['rows'] else None
        if index is not None:
            table.selection_set(str(index))
            table.see(str(index))
        state['loading'] = False
        show_fields(index)
        undo_button.configure(state='normal' if session.can_undo else 'disabled')

    def saved(action):
        app.validated_hash = None
        app._invalidate_collection()
        app.check_people(show_dialog=False)
        app._diagnostic('editor_saved', action=action, count=len(session.rows))
        app.status_var.set('人员表已自动保存。请重新读取费用，再启动全自动管理。')
        editor_status.set('已自动保存。撤销可退回上一步；不保存本次修改可恢复打开窗口前的内容。')
        undo_button.configure(state='normal' if session.can_undo else 'disabled')

    def failure(exc, notify=False):
        editor_status.set('修改未完整保存或确认：' + safe_error(exc))
        app._diagnostic('editor_save_failed', error_type=type(exc).__name__)
        if notify:
            messagebox.showerror('保存未完整确认', safe_error(exc), parent=dialog)

    def commit(notify=False, selection_change=False):
        cancel_timer()
        if not state['dirty']:
            return True
        try:
            guard()
            if state['selection_conflict']:
                raise GuardError('选中行尚未显示时输入发生变化；请先撤销这段输入，再重新选行填写。')
            selected = table.selection()
            selected_index = int(selected[0]) if selected else None
            if not selection_change and selected_index != state['index']:
                raise GuardError('选中行正在变化，请等待选中行内容显示后再修改。')
            person = Person(clean_name(name_var.get()), money(amount_var.get().strip(), allow_skip=True))
            index = state['index']
            if index is None:
                raise GuardError('请先添加新行或选中要编辑的行。')
            rows = list(state['rows'])
            rows[index] = person
            changed = session.replace(rows)
            state['rows'] = list(session.rows)
            state['dirty'] = False
            # Keep cursor and typed formatting intact during debounce saves.
            table.item(str(index), values=(person.name, amount_text(person.amount)))
            if changed:
                saved('update')
            else:
                editor_status.set('内容已保存，无新增修改。')
            return True
        except Exception as exc:
            failure(exc, notify)
            return False

    def changed(*_):
        if state['loading'] or state['closed']:
            return
        state['dirty'] = True
        selected = table.selection()
        selected_index = int(selected[0]) if selected else None
        if selected_index != state['index']:
            state['selection_conflict'] = True
        cancel_timer()
        undo_button.configure(state='normal')
        editor_status.set('正在编辑，停顿后自动保存……')
        state['timer'] = app.root.after(600, commit)

    def select(_event=None):
        if state['loading']:
            return
        selected = table.selection()
        index = int(selected[0]) if selected else None
        if index == state['index']:
            return
        if not commit(notify=True, selection_change=True):
            if state['index'] is not None:
                table.selection_set(str(state['index']))
            return
        show_fields(index)

    def add():
        if not commit(notify=True):
            return
        try:
            guard()
            session.replace(state['rows'] + [Person('', app.table_defaults.amount)])
            state['rows'] = list(session.rows)
            render(len(state['rows']) - 1)
            saved('add')
            name_entry.focus_set()
        except Exception as exc:
            failure(exc, True)

    def remove():
        cancel_timer()
        chosen = table.selection()
        index = int(chosen[0]) if chosen else None
        if index is None:
            return
        try:
            guard()
            if state['selection_conflict']:
                raise GuardError('请先撤销选行期间尚未保存的输入，再移除需要的行。')
            rows = list(state['rows'])
            del rows[index]
            session.replace(rows)
            state['rows'] = list(session.rows)
            render(index)
            saved('remove')
        except Exception as exc:
            failure(exc, True)

    def undo():
        cancel_timer()
        try:
            guard()
            if state['dirty']:
                state['rows'] = list(session.rows)
                render(state['index'])
                editor_status.set('已撤销尚未保存的输入；之前已保存的内容保留。')
            elif session.undo():
                state['rows'] = list(session.rows)
                render(state['index'])
                saved('undo')
                editor_status.set('已撤销上一次操作并保存；最多可连续撤销最近 50 次操作。')
        except Exception as exc:
            failure(exc, True)

    def close():
        # Closing is not a write operation; preserve only completed saves.
        state['closed'] = True
        cancel_timer()
        app._action_state('edit', '已关闭')
        app.status_var.set('人员表已关闭，已保存的修改保留。' if not state['dirty'] else
                           '人员表已关闭，已保存的修改保留；窗口关闭时未执行额外保存。')
        dialog.destroy()

    def discard():
        cancel_timer()
        try:
            guard()
            if session.discard():
                saved('discard')
            state['closed'] = True
            app._action_state('edit', '已关闭')
            app.status_var.set('已恢复打开编辑窗口前的人员表；更早保存的内容和处理记录保留。')
            dialog.destroy()
        except Exception as exc:
            failure(exc, True)

    ttk.Button(operations, text='更新选中行', command=app._button_command('editor_update', lambda: commit(True))).pack(side='left')
    ttk.Button(operations, text='添加新行', command=app._button_command('editor_add', add)).pack(side='left', padx=8)
    ttk.Button(operations, text='移除选中行', command=app._button_command('editor_remove', remove)).pack(side='left')
    ttk.Button(footer, text='不保存本次修改', command=app._button_command('editor_discard', discard)).grid(row=0, column=0, sticky='w')
    undo_button = ttk.Button(footer, text='撤销', command=app._button_command('editor_undo', undo))
    undo_button.grid(row=0, column=1)
    ttk.Button(footer, text='取消', command=app._button_command('editor_cancel', close)).grid(row=0, column=2, sticky='e')
    table.bind('<<TreeviewSelect>>', select)
    name_var.trace_add('write', changed)
    amount_var.trace_add('write', changed)
    for entry in (name_entry, amount_entry):
        entry.bind('<Return>', lambda _event: commit(True))
    dialog.protocol('WM_DELETE_WINDOW', close)
    dialog.bind('<Destroy>', lambda event: cancel_timer() if event.widget is dialog else None)
    render()
    apply_theme(app.root, app.current_theme)
    dialog.update_idletasks()
    scale = float(dialog.tk.call('tk', 'scaling'))
    width = min(max(760, int(560 * scale)), max(480, dialog.winfo_screenwidth() - 80))
    height = min(max(560, int(420 * scale)), max(400, dialog.winfo_screenheight() - 140))
    dialog.geometry(f'{width}x{height}')
    # The only expanding row is the table; the footer is always allocated.
    dialog.minsize(min(width, 680), min(height, body.winfo_reqheight()))
    def rewrap(event):
        if event.widget is body:
            hint.configure(wraplength=max(250, event.width - 32))
            feedback.configure(wraplength=max(250, event.width - 32))
    body.bind('<Configure>', rewrap)
    app._action_state('edit', '编辑中')
    app.status_var.set('人员表已打开。有效输入自动保存，撤销退回上一步，取消仅关闭窗口。')
