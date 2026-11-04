"""The local log viewer remains usable when resized at higher display scaling."""
import time
import tkinter as tk
from tkinter import ttk
import unittest

import test_defaults_ui as fixtures


class DiagnosticsLayoutTests(unittest.TestCase):
    cleanup_app = fixtures.DefaultsUiTests.cleanup_app
    button = fixtures.DefaultsUiTests.button

    def setUp(self):
        fixtures.DefaultsUiTests.setUp(self)
        self.root.attributes('-alpha', 0)
        self.root.deiconify()
        self.pump(.08)

    def pump(self, seconds=.03):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.root.update()
            time.sleep(.005)

    def test_log_controls_and_content_stay_visible_when_resizing(self):
        original_scaling = self.root.tk.call('tk', 'scaling')
        try:
            for scaling in (1.5, 2.0, 2.5):
                with self.subTest(scaling=scaling):
                    self.root.tk.call('tk', 'scaling', scaling)
                    self.app.open_diagnostics()
                    dialog = self.app._diagnostics_window
                    dialog.attributes('-alpha', 0)
                    dialog.deiconify()
                    widgets = list(fixtures.descendants(dialog))
                    content = next(w for w in widgets if isinstance(w, tk.Text))
                    min_width, min_height = dialog.minsize()
                    measured = []
                    for width, height in ((min_width, min_height), (min_width + 250, min_height + 200)):
                        dialog.geometry(f'{width}x{height}')
                        self.pump()
                        for widget in (content, *(self.button(dialog, label) for label in
                                                 ('刷新', '打开日志文件夹', '关闭'))):
                            self.assertTrue(widget.winfo_ismapped())
                            left = widget.winfo_rootx() - dialog.winfo_rootx()
                            top = widget.winfo_rooty() - dialog.winfo_rooty()
                            self.assertGreaterEqual(left, 0)
                            self.assertGreaterEqual(top, 0)
                            self.assertLessEqual(left + widget.winfo_width(), dialog.winfo_width())
                            self.assertLessEqual(top + widget.winfo_height(), dialog.winfo_height())
                        measured.append((content.winfo_width(), content.winfo_height()))
                    self.assertGreater(measured[1][0], measured[0][0])
                    self.assertGreater(measured[1][1], measured[0][1])
                    self.button(dialog, '关闭').invoke()
        finally:
            self.root.withdraw()
            self.root.tk.call('tk', 'scaling', original_scaling)


if __name__ == '__main__':
    unittest.main()
