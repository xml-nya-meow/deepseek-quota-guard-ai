"""Acceptance entry isolation and one real local GUI workflow, without HTTP."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from concurrent.futures import Future
from decimal import Decimal
from types import SimpleNamespace

import main
from quota_guard.offline_acceptance import _new_directory, run_offline_acceptance, _Transport


class OfflineAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)

    def test_existing_directory_is_never_touched(self):
        target = self.base / 'offline-acceptance-existing'
        target.mkdir()
        sentinel = target / 'keep.txt'
        sentinel.write_bytes(b'existing-data')
        with self.assertRaises(FileExistsError):
            run_offline_acceptance(target)
        self.assertEqual(list(target.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_bytes(), b'existing-data')

    def test_unmarked_directory_is_rejected_before_creation(self):
        target = self.base / 'real-ledger'
        with self.assertRaises(ValueError):
            _new_directory(target)
        self.assertFalse(target.exists())

    def test_reparse_path_rejected_before_creation(self):
        target = self.base / 'offline-acceptance-linked'
        with patch.object(Path, 'is_symlink', return_value=True):
            with self.assertRaises(ValueError):
                _new_directory(target)
        self.assertFalse(target.exists())

    def test_failure_report_does_not_include_exception_text_or_launch_gui(self):
        target = self.base / 'offline-acceptance-failure'
        with patch('quota_guard.offline_acceptance._exercise', side_effect=OSError('private fixture detail')):
            result = run_offline_acceptance(target)
        self.assertFalse(result['passed'])
        self.assertEqual(result['error_type'], 'OSError')
        self.assertNotIn('private', (target / 'acceptance.json').read_text())

    def test_cli_requires_explicit_directory_and_exclusive_mode(self):
        for args in (['--offline-acceptance'], ['--smoke'], ['--smoke', '--ai-smoke']):
            with self.subTest(args=args), patch('sys.argv', ['main', *args]), patch('sys.stderr'):
                with self.assertRaises(SystemExit) as error:
                    main.main()
                self.assertEqual(error.exception.code, 2)

    def test_cli_unsafe_path_fails_without_dialog_or_normal_startup(self):
        with patch('sys.argv', ['main', '--offline-acceptance', '--data-dir', str(self.base)]), \
                patch('main.tk.Tk') as root, patch('main.messagebox.showerror') as dialog:
            self.assertEqual(main.main(), 1)
            root.assert_not_called()
            dialog.assert_not_called()

    def test_smoke_propagates_tk_callback_failure_to_nonzero_exit(self):
        target = self.base / 'offline-acceptance-smoke'
        root = MagicMock()
        app = SimpleNamespace(window_destroyed=True, store=object(), workbook_timer=None,
                              theme_timer=None, initial_load_timer=None, close=lambda: None)
        root.mainloop.side_effect = lambda: root.report_callback_exception(TypeError, None, None)
        with patch('sys.argv', ['main', '--smoke', '--data-dir', str(target)]), \
                patch('main.tk.Tk', return_value=root), patch('quota_guard.ui.App', return_value=app):
            self.assertEqual(main.main(), 1)
        result = json.loads((target / 'ui-smoke.json').read_text())
        self.assertFalse(result['passed'])
        self.assertEqual(result['callback_errors'], 1)

    def test_missing_or_mismatched_send_evidence_prevents_synthetic_send(self):
        receipt = {'operation_id': 'fixture-operation'}
        store = MagicMock()
        store.latest_action_record.return_value = {'status': 'requested', 'operation_id': 'fixture-operation',
            'responsibility': receipt, 'recovery_evidence': {'target_tracking_id': 'fixture-target'}}
        cases = [([], None), ([{'phase': 'declared', 'receipt': receipt}], None),
                 ([{'phase': 'possible_send', 'receipt': receipt}], None),
                 ([{'phase': 'possible_send', 'receipt': receipt}], (json.dumps({'schema': 1, 'receipt': {}}),))]
        for obligations, row in cases:
            with self.subTest(obligations=obligations, row=row):
                transport = _Transport(store, [], 'fixture-account', self.base, cost=Decimal(0))
                transport.future = Future()
                transport.target = 'fixture-target'
                transport.permit = MagicMock(allowed=True)
                transport.permit.decided.is_set.return_value = True
                store._connect.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = row
                with patch('quota_guard.responsibility_registry.ResponsibilityRegistry') as registry:
                    registry.return_value.open_obligations.return_value = obligations
                    with self.assertRaises(AssertionError):
                        transport.tick()
                self.assertEqual(transport.sends, 0)
                transport.permit.consume_for_click.assert_not_called()

    def test_actual_gui_import_reopen_recovery_and_two_synthetic_entries(self):
        target = self.base / 'offline-acceptance-workflow'
        # Propagate here for actionable local tracebacks; the public CLI sanitizes.
        from quota_guard.offline_acceptance import _exercise
        recorded = {}
        def exercise(base, result):
            try:
                _exercise(base, result)
            except Exception as exc:
                recorded['error'] = exc
                raise
        with patch('quota_guard.offline_acceptance._exercise', side_effect=exercise):
            result = run_offline_acceptance(target)
        if 'error' in recorded:
            raise recorded['error']
        self.assertTrue(result['passed'], result)
        self.assertEqual(len(result['cases']), 6)
        self.assertEqual(result['real_requests'], 0)
        self.assertEqual(result['send_boundary_checks'], 2)
        self.assertEqual(json.loads((target / 'acceptance.json').read_text()), result)


if __name__ == '__main__':
    unittest.main()
