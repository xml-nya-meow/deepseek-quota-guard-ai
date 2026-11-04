"""Resizable Tk layouts with screen-bounded startup sizes and scroll fallback."""
import tkinter as tk
from tkinter import ttk


def fit_window(window, *, preferred=(1180, 820), minimum=(640, 420)):
    """Do not make a minimum size that is larger than the current screen."""
    available = (max(200, window.winfo_screenwidth() - 60),
                 max(160, window.winfo_screenheight() - 100))
    low = tuple(min(value, bound) for value, bound in zip(minimum, available))
    size = tuple(max(floor, min(value, bound))
                 for value, floor, bound in zip(preferred, low, available))
    window.resizable(True, True)
    window.minsize(*low)
    window.geometry(f'{size[0]}x{size[1]}')


class ScrollableContent(ttk.Frame):
    """Expand content on large windows; keep controls reachable on small ones.

    The content's natural requested size is the scrollable minimum, rather
    than a forced large Toplevel minimum. Mouse wheel bindings are scoped to
    this Toplevel and do not take scrolling away from a table or text widget.
    """
    def __init__(self, parent, *, padding=0):
        super().__init__(parent)
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        background = ttk.Style(self).lookup('TFrame', 'background') or '#f0f0f0'
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0, background=background)
        self.vertical = ttk.Scrollbar(self, orient='vertical', command=self.canvas.yview)
        self.horizontal = ttk.Scrollbar(self, orient='horizontal', command=self.canvas.xview)
        self.canvas.configure(yscrollcommand=self.vertical.set, xscrollcommand=self.horizontal.set)
        self.canvas.grid(row=0, column=0, sticky='nsew')
        self.vertical.grid(row=0, column=1, sticky='ns')
        self.horizontal.grid(row=1, column=0, sticky='ew')
        self.content = ttk.Frame(self.canvas, padding=padding)
        self.item = self.canvas.create_window((0, 0), window=self.content, anchor='nw')
        self._pending = None
        self._wrapping = []
        self.canvas.bind('<Configure>', self._schedule, add=True)
        self.content.bind('<Configure>', self._schedule, add=True)
        self.top = self.winfo_toplevel()
        self.scheduler = self._root()
        self._wheel_binding = self.top.bind('<MouseWheel>', self._wheel, add=True)
        self.bind('<Destroy>', self._destroy, add=True)

    def wrap(self, label, *, padding=40):
        self._wrapping.append((label, padding))
        self._schedule()
        return label

    def _schedule(self, _event=None):
        if self._pending is None:
            self._pending = self.scheduler.after_idle(self._layout)

    def _layout(self):
        self._pending = None
        width, height = max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())
        for label, padding in self._wrapping:
            if label.winfo_exists():
                wrap = max(160, width - padding)
                if int(label.cget('wraplength')) != wrap:
                    label.configure(wraplength=wrap)
        content_width = max(width, self.content.winfo_reqwidth())
        content_height = max(height, self.content.winfo_reqheight())
        self.canvas.itemconfigure(self.item, width=content_width, height=content_height)
        self.canvas.configure(scrollregion=(0, 0, content_width, content_height))

    def _wheel(self, event):
        if isinstance(event.widget, (ttk.Treeview, tk.Text, ttk.Combobox)):
            return
        if not event.delta:
            return
        units = -max(1, abs(event.delta) // 120) if event.delta > 0 else max(1, abs(event.delta) // 120)
        (self.canvas.xview_scroll if event.state & 1 else self.canvas.yview_scroll)(units, 'units')

    def _destroy(self, event):
        if event.widget is not self:
            return
        if self._pending is not None:
            self.scheduler.after_cancel(self._pending)
        try:
            self.top.unbind('<MouseWheel>', self._wheel_binding)
        except tk.TclError:
            pass
