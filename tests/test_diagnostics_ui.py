"""Opening/refeshing diagnostics stays local and does not accumulate widgets."""
from pathlib import Path
import tempfile
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

from isolated_app import App


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


class DiagnosticsUiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root, Path(self.temp.name))
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.app.folder_lock.close()
        for callback in self.root.tk.call('after', 'info'):
            self.root.after_cancel(callback)
        self.root.destroy()

    def test_viewer_is_internal_singleton_redacted_and_refresh_replaces_content(self):
        self.app.diagnostics.emit('fixture', action='collect', message='sk-fake-fixture-only')
        with patch('quota_guard.diagnostics_ui.os.startfile') as external:
            self.app.open_diagnostics()
            dialog = self.app._diagnostics_window
            widgets = list(descendants(dialog))
            content = next(widget for widget in widgets if isinstance(widget, tk.Text))
            self.assertIn('fixture', content.get('1.0', 'end'))
            self.assertNotIn('sk-fake', content.get('1.0', 'end'))
            self.assertEqual(content.cget('state'), 'disabled')
            self.app.open_diagnostics()
            self.assertIs(dialog, self.app._diagnostics_window)
            refresh = next(widget for widget in widgets if isinstance(widget, ttk.Button)
                           and widget.cget('text') == '刷新')
            refresh.invoke()  # Includes the viewer-open diagnostic emitted after construction.
            before = content.get('1.0', 'end')
            refresh.invoke()
            self.assertEqual(content.get('1.0', 'end'), before)
            self.assertEqual(len(list(descendants(dialog))), len(widgets))
            self.assertIsNone(self.app.browser)
            external.assert_not_called()

    def test_closed_viewer_can_reopen_and_show_failed_writer_status(self):
        self.app.open_diagnostics()
        first = self.app._diagnostics_window
        first.destroy()
        self.app.diagnostics.disabled = True
        self.app.open_diagnostics()
        second = self.app._diagnostics_window
        self.assertIsNot(second, first)
        labels = [widget for widget in descendants(second) if isinstance(widget, ttk.Label)]
        texts = [str(widget.cget('text')) for widget in labels]
        for widget in labels:
            variable = widget.cget('textvariable')
            if variable:
                texts.append(str(self.root.getvar(variable)))
        self.assertTrue(any('诊断写入失败' in text for text in texts))
        self.assertFalse(self.app.observing)
