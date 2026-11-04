"""The date prompt closes safely and remains usable at different sizes."""
import tkinter as tk
from tkinter import ttk
import unittest

from quota_guard.text_prompt import ask_text


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


class TextPromptTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.attributes('-alpha', 0)
        self.root.update()
        self.addCleanup(self.root.destroy)
        original_scaling = self.root.tk.call('tk', 'scaling')
        self.addCleanup(self.root.tk.call, 'tk', 'scaling', original_scaling)

    def run_prompt(self, exercise):
        failures = []

        def manipulate():
            dialog = next(w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel))
            dialog.attributes('-alpha', 0)
            try:
                exercise(dialog)
            except BaseException as exc:
                failures.append(exc)
            finally:
                if dialog.winfo_exists():
                    dialog.destroy()

        self.root.after(0, manipulate)
        result = ask_text('合成统计起点', '新的统计起点（YYYY-MM-DD）：\n保留原账本，从新周期重新读取费用。',
                          initialvalue='2026-10-29', parent=self.root)
        if failures:
            raise failures[0]
        self.assertIsNone(self.root.grab_current())
        return result

    def button(self, dialog, label):
        return next(w for w in descendants(dialog) if isinstance(w, ttk.Button) and w.cget('text') == label)

    def test_confirm_returns_changed_text(self):
        def confirm(dialog):
            entry = next(w for w in descendants(dialog) if isinstance(w, ttk.Entry))
            self.assertEqual(entry.get(), '2026-10-29')
            entry.delete(0, 'end')
            entry.insert(0, '2026-11-01')
            self.button(dialog, '确定').invoke()

        self.assertEqual(self.run_prompt(confirm), '2026-11-01')

    def test_cancel_and_window_close_return_none(self):
        for mode in ('cancel', 'window'):
            with self.subTest(mode=mode):
                def cancel(dialog):
                    if mode == 'cancel':
                        self.button(dialog, '取消').invoke()
                    else:
                        dialog.tk.call(dialog.protocol('WM_DELETE_WINDOW'))
                self.assertIsNone(self.run_prompt(cancel))

    def test_prompt_resizes_entry_and_keeps_footer_visible_at_high_scaling(self):
        self.root.tk.call('tk', 'scaling', 2.5)

        def resize(dialog):
            self.assertEqual(dialog.resizable(), (1, 1))
            entry = next(w for w in descendants(dialog) if isinstance(w, ttk.Entry))
            measured = []
            for width, height in ((360, 220), (900, 500)):
                dialog.geometry(f'{width}x{height}')
                for _ in range(3):
                    self.root.update()
                measured.append(entry.winfo_width())
                for label in ('确定', '取消'):
                    button = self.button(dialog, label)
                    self.assertTrue(button.winfo_ismapped())
                    self.assertGreater(button.winfo_height(), 1)
                    self.assertGreaterEqual(button.winfo_rootx(), dialog.winfo_rootx())
                    self.assertLessEqual(button.winfo_rootx() + button.winfo_width(),
                                         dialog.winfo_rootx() + dialog.winfo_width())
                    self.assertLessEqual(button.winfo_rooty() + button.winfo_height(),
                                         dialog.winfo_rooty() + dialog.winfo_height())
            self.assertGreater(measured[1], measured[0])
            self.button(dialog, '取消').invoke()

        self.assertIsNone(self.run_prompt(resize))


if __name__ == '__main__':
    unittest.main()
