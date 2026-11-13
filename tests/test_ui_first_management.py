"""First-use convenience must not turn lost responsibility evidence into a new scope."""
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import tempfile
import time
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from quota_guard.core import DataStore, GuardError, Person, TZ
from quota_guard.responsibility_gate import ResponsibilityGate, ledger_anchor
from quota_guard.ui import App, initialize_first_management_scope


class FirstManagementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.store = DataStore(self.base / 'new-ledger')
        self.store.initialize(2, '100', datetime.now(TZ).date().isoformat())
        self.gate = ResponsibilityGate(self.base / 'scope' / 'registry')

    def initialize(self, **overrides):
        flags = {'declared_first_use': True, 'just_created': True, **overrides}
        return initialize_first_management_scope(self.gate, self.store, **flags)

    def test_declared_first_use_creates_ready_membership_with_ledger_witness(self):
        self.assertTrue(self.initialize())
        state = self.gate.registry.snapshot()
        self.assertEqual(state['phase'], 'ready')
        self.assertEqual(len(state['ledgers']), 1)
        self.assertEqual(state['open_obligations'], [])
        self.assertEqual(ledger_anchor(self.store)['epoch'], state['epoch'])

    def test_no_declaration_or_not_just_created_does_not_create_any_registry(self):
        for flags in ({'declared_first_use': False}, {'just_created': False}):
            with self.subTest(flags=flags):
                self.assertFalse(self.initialize(**flags))
                self.assertFalse(self.gate.registry.directory.exists())
                self.assertFalse(self.gate.registry.anchor_path.exists())
                self.assertIsNone(ledger_anchor(self.store))

    def test_bound_account_is_not_first_use(self):
        self.store.save_people([Person('Synthetic first-use member', Decimal(5))])
        self.store.bind_keys([{'name': 'Synthetic first-use member', 'tracking_id': 'synthetic-member'}],
                             'synthetic-account')
        with self.assertRaises(GuardError):
            self.initialize()
        self.assertFalse(self.gate.registry.directory.exists())
        self.assertEqual(self.store.active_tracking_ids(), {'synthetic-member'})

    def test_existing_partial_scope_is_preserved_without_initializing(self):
        self.gate.registry.directory.mkdir(parents=True)
        sentinel = self.gate.registry.directory / 'partial-evidence'
        sentinel.write_bytes(b'synthetic original evidence')
        with self.assertRaises(GuardError):
            self.initialize()
        self.assertEqual(sentinel.read_bytes(), b'synthetic original evidence')
        self.assertEqual(list(self.gate.registry.directory.iterdir()), [sentinel])

    def test_external_anchor_is_not_overwritten(self):
        anchor = self.gate.registry.anchor_path
        anchor.parent.mkdir(parents=True)
        anchor.write_bytes(b'synthetic old witness')
        with self.assertRaises(GuardError):
            self.initialize()
        self.assertEqual(anchor.read_bytes(), b'synthetic old witness')
        self.assertFalse(self.gate.registry.directory.exists())

    def test_new_registry_path_cannot_reinitialize_previously_enrolled_ledger(self):
        self.initialize()
        previous = ledger_anchor(self.store)
        self.gate = ResponsibilityGate(self.base / 'wrong-new-scope' / 'registry')
        with self.assertRaises(GuardError):
            self.initialize()
        self.assertEqual(ledger_anchor(self.store), previous)
        self.assertFalse(self.gate.registry.directory.exists())

    def test_interrupted_first_preparation_is_not_replaced_by_fresh_initialization(self):
        original = self.gate._execute_membership
        with patch.object(self.gate, '_execute_membership', side_effect=OSError('synthetic interruption')):
            with self.assertRaises(OSError):
                self.initialize()
        plan = self.gate.registry.membership_plan()
        with self.assertRaises(GuardError):
            self.initialize()
        self.assertEqual(self.gate.registry.membership_plan()['plan_id'], plan['plan_id'])
        original(plan['plan_id'])
        self.assertEqual(self.gate.registry.snapshot()['phase'], 'ready')

    def management_button_app(self):
        return SimpleNamespace(config=self.store.load(), busy=False, observing=False,
                               closing=False, audit_failed=False, store=self.store,
                               responsibility_gate=self.gate, refresh_rows=Mock(),
                               refresh_events=Mock(), status_var=Mock(), error=Mock())

    def test_management_button_reuses_ready_scope_and_never_initializes_again(self):
        self.initialize()
        app = self.management_button_app()
        before = self.gate.registry.snapshot()['epoch']
        with patch.object(self.gate, 'initialize') as create, \
                patch('quota_guard.ui.messagebox.askyesno') as prompt:
            App.initialize_responsibility(app)
        create.assert_not_called()
        prompt.assert_not_called()
        app.error.assert_not_called()
        self.assertEqual(self.gate.registry.snapshot()['epoch'], before)
        self.assertIn('全自动管理保持停止', app.status_var.set.call_args.args[0])

    def test_management_button_resumes_exact_interrupted_membership_plan(self):
        with patch.object(self.gate, '_execute_membership', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                self.initialize()
        plan_id = self.gate.registry.membership_plan()['plan_id']
        app = self.management_button_app()
        with patch.object(self.gate, 'initialize') as create:
            App.initialize_responsibility(app)
        create.assert_not_called()
        app.error.assert_not_called()
        self.assertEqual(self.gate.registry.snapshot()['phase'], 'ready')
        self.assertEqual(self.gate.registry.membership_plan()['plan_id'], plan_id)

    def test_management_button_missing_original_scope_never_opens_fresh_wizard(self):
        self.initialize()
        self.gate = ResponsibilityGate(self.base / 'missing-original-scope')
        app = self.management_button_app()
        with patch.object(self.gate, 'initialize') as create, \
                patch('quota_guard.ui.messagebox.askyesno') as prompt:
            App.initialize_responsibility(app)
        create.assert_not_called()
        prompt.assert_not_called()
        app.error.assert_called_once()
        self.assertFalse(self.gate.registry.directory.exists())


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


class FirstManagementUiTests(unittest.TestCase):
    def test_fresh_list_checkbox_prepares_scope_without_browser_or_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = tk.Tk()
            root.withdraw()
            app = App(root, base / 'ledger', home_folder=base,
                      responsibility_directory=base / 'scope' / 'registry')
            try:
                root.update_idletasks()
                app.load_folder()
                app.setup()
                dialog = next(w for w in root.winfo_children() if isinstance(w, tk.Toplevel))
                checkbox = next(w for w in descendants(dialog) if isinstance(w, ttk.Checkbutton)
                                and '这是本机首次使用' in str(w.cget('text')))
                self.assertNotIn('selected', checkbox.state())
                checkbox.invoke()
                create = next(w for w in descendants(dialog) if isinstance(w, ttk.Button)
                              and w.cget('text') == '创建新列表')
                with patch.object(app, 'edit_people'), patch('quota_guard.ui.messagebox.showerror') as error:
                    create.invoke()
                error.assert_not_called()
                self.assertIsNotNone(app.config)
                self.assertEqual(app.responsibility_gate.registry.snapshot()['phase'], 'ready')
                self.assertFalse(app.automatic)
                self.assertFalse(app.observing)
                self.assertIsNone(app.browser)
                self.assertIn('新列表与管理记录已准备', app.status_var.get())
            finally:
                root.update_idletasks()
                app.close()
                deadline = time.monotonic() + 5
                while not app.window_destroyed and time.monotonic() < deadline:
                    root.update()
                    time.sleep(0.01)
                if not app.window_destroyed:
                    app.folder_lock.close()
                    root.destroy()
                self.assertTrue(app.window_destroyed, 'production close must finish local cleanup')


if __name__ == '__main__':
    unittest.main()
