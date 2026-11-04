"""A resizable local text prompt with a fixed action footer."""
import tkinter as tk
from tkinter import ttk

from .window_layout import ScrollableContent, fit_window


def ask_text(title, prompt, initialvalue='', *, parent):
    """Return entered text on confirmation, or None on cancel/window close."""
    dialog = tk.Toplevel(parent)
    dialog.withdraw()
    dialog.title(title)
    dialog.transient(parent)
    dialog.columnconfigure(0, weight=1)
    dialog.rowconfigure(0, weight=1)
    area = ScrollableContent(dialog, padding=16)
    area.grid(row=0, column=0, sticky='nsew')
    area.content.columnconfigure(0, weight=1)
    label = ttk.Label(area.content, text=prompt, wraplength=500, justify='left')
    label.grid(row=0, column=0, sticky='ew', pady=(0, 12))
    area.wrap(label, padding=32)
    value = tk.StringVar(dialog, value=initialvalue)
    entry = ttk.Entry(area.content, textvariable=value, width=24)
    entry.grid(row=1, column=0, sticky='ew')
    footer = ttk.Frame(dialog, padding=(16, 8, 16, 12))
    footer.grid(row=1, column=0, sticky='ew')
    footer.columnconfigure(0, weight=1)
    answer = None

    def close(confirm=False):
        nonlocal answer
        if confirm:
            answer = value.get()
        dialog.destroy()
        return 'break'

    ttk.Button(footer, text='确定', command=lambda: close(True)).grid(row=0, column=1, padx=(0, 8))
    ttk.Button(footer, text='取消', command=close).grid(row=0, column=2)
    dialog.protocol('WM_DELETE_WINDOW', close)
    dialog.bind('<Return>', lambda _event: close(True))
    dialog.bind('<Escape>', lambda _event: close())
    fit_window(dialog, preferred=(620, 280), minimum=(360, 220))
    previous_grab = parent.grab_current()
    previous_focus = parent.focus_get()
    dialog.deiconify()
    dialog.grab_set()
    entry.focus_set()
    entry.selection_range(0, 'end')
    try:
        parent.wait_window(dialog)
    finally:
        try:
            if dialog.winfo_exists():
                dialog.destroy()
            if previous_grab is not None and previous_grab.winfo_exists():
                previous_grab.grab_set()
            if previous_focus is not None and previous_focus.winfo_exists():
                previous_focus.focus_set()
        except tk.TclError:
            pass  # The application may have closed while the prompt was open.
    return answer
