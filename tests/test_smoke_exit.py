"""A windowed self-test must fail without an interactive error dialog."""
from concurrent.futures import Future
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

import main


class SmokeExitTests(unittest.TestCase):
    def run_smoke(self, browser, folder):
        with patch('sys.argv', ['main', '--browser-smoke', '--data-dir', str(folder)]), \
                patch('quota_guard.browser_process.IsolatedBrowserService', return_value=browser), \
                patch('main.messagebox.showerror') as dialog:
            code = main.main()
            dialog.assert_not_called()
        return code, json.loads((folder / 'browser-smoke.json').read_text(encoding='utf-8'))

    def test_launch_failure_writes_fixed_nonsecret_result_and_closes(self):
        with tempfile.TemporaryDirectory() as temporary:
            browser = MagicMock()
            future = Future()
            future.set_exception(RuntimeError('Bearer private-fixture-value'))
            browser.submit.return_value = future
            code, result = self.run_smoke(browser, Path(temporary))
            self.assertEqual(code, 1)
            self.assertEqual(result, {'browser_started': False, 'reason_code': 'BROWSER_SMOKE_FAILED'})
            browser.close.assert_called_once()

    def test_cleanup_failure_cannot_report_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            browser = MagicMock()
            future = Future(); future.set_result({'browser_started': True})
            browser.submit.return_value = future
            browser.close.side_effect = RuntimeError('private cleanup detail')
            code, result = self.run_smoke(browser, Path(temporary))
            self.assertEqual(code, 1)
            self.assertEqual(result['reason_code'], 'BROWSER_SMOKE_CLEANUP_FAILED')

    def test_success_keeps_browser_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            browser = MagicMock()
            future = Future(); future.set_result({'browser_started': True, 'version': 'fixture'})
            browser.submit.return_value = future
            code, result = self.run_smoke(browser, Path(temporary))
            self.assertEqual(code, 0)
            self.assertEqual(result['version'], 'fixture')
