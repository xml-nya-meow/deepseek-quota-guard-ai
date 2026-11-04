from pathlib import Path
import tempfile
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import Mock

from quota_guard.ui import App
from quota_guard.window_layout import ScrollableContent, fit_window


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


class WindowLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.attributes('-alpha', 0)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.cleanup_root)

    def cleanup_root(self):
        for callback in self.root.tk.call('after', 'info'):
            self.root.after_cancel(callback)
        self.root.destroy()

    def settle(self):
        for _ in range(3):
            self.root.update()

    def test_small_screen_does_not_receive_an_oversized_minimum(self):
        window = Mock()
        window.winfo_screenwidth.return_value = 800
        window.winfo_screenheight.return_value = 600
        fit_window(window)
        window.resizable.assert_called_once_with(True, True)
        window.minsize.assert_called_once_with(640, 420)
        window.geometry.assert_called_once_with('740x500')
        window.maxsize.assert_not_called()

    def test_scroll_container_keeps_oversized_contents_reachable(self):
        self.root.geometry('400x300')
        area = ScrollableContent(self.root)
        area.pack(fill='both', expand=True)
        ttk.Frame(area.content, width=900, height=1000).pack()
        target = ttk.Button(area.content, text='bottom target')
        target.pack(anchor='e')
        self.root.deiconify()
        self.settle()
        self.assertGreater(area.content.winfo_width(), area.canvas.winfo_width())
        self.assertGreater(area.content.winfo_height(), area.canvas.winfo_height())
        area.canvas.xview_moveto(1)
        area.canvas.yview_moveto(1)
        self.settle()
        self.assertLessEqual(target.winfo_rootx() + target.winfo_width(),
                             area.canvas.winfo_rootx() + area.canvas.winfo_width() + 1)
        self.assertLessEqual(target.winfo_rooty() + target.winfo_height(),
                             area.canvas.winfo_rooty() + area.canvas.winfo_height() + 1)

    def test_main_window_shrinks_with_scroll_fallback_and_grows_table(self):
        app = App(self.root, Path(self.temp.name))
        self.addCleanup(app.folder_lock.close)
        self.root.deiconify()
        self.root.geometry('640x420')
        self.settle()
        area = app.main_content
        self.assertEqual(self.root.resizable(), (1, 1))
        self.assertGreater(area.content.winfo_height(), area.canvas.winfo_height())
        small_height = app.tree.winfo_height()
        # Hosted desktops can cap Toplevel geometry at their screen size.
        # This hidden test must actually supply surplus viewport space before
        # checking expansion; asking for 1600x1400 alone does not guarantee it.
        large_width = max(1600, area.content.winfo_reqwidth() + 200)
        large_height = max(1400, area.content.winfo_reqheight() + 300)
        self.root.maxsize(large_width, large_height)
        self.root.geometry(f'{large_width}x{large_height}')
        self.settle()
        self.assertGreater(area.canvas.winfo_height(), area.content.winfo_reqheight())
        self.assertGreater(app.tree.winfo_height(), small_height)

    def test_new_list_has_fixed_footer_and_expanding_inputs(self):
        app = App(self.root, Path(self.temp.name))
        self.addCleanup(app.folder_lock.close)
        app.load_folder()
        app.setup()
        dialog = next(w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel))
        dialog.attributes('-alpha', 0)
        self.root.deiconify()
        dialog.geometry('440x360')
        self.settle()
        entries = [w for w in descendants(dialog) if isinstance(w, ttk.Entry)]
        width = entries[0].winfo_width()
        buttons = [w for w in descendants(dialog) if isinstance(w, ttk.Button)]
        for button in buttons:
            self.assertGreater(button.winfo_height(), 1)
            self.assertLessEqual(button.winfo_rooty() + button.winfo_height(),
                                 dialog.winfo_rooty() + dialog.winfo_height())
        self.assertEqual(dialog.resizable(), (1, 1))
        dialog.geometry('1100x700')
        self.settle()
        self.assertGreater(entries[0].winfo_width(), width)
        self.assertIsNone(app.config)


if __name__ == '__main__':
    unittest.main()
