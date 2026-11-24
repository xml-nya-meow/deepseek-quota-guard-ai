from datetime import datetime
from decimal import Decimal
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import Mock

from quota_guard.core import Person, TZ
from quota_guard.ui import App


class DesktopHomeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.root = tk.Tk()
        self.root.withdraw()
        self.demo = Mock()
        self.app = App(self.root, base / "ledger", responsibility_directory=base / "registry", on_demo=self.demo)
        self.app.load_folder()
        self.addCleanup(self.close_app)

    def close_app(self):
        self.app._pause_lifecycle_timers()
        self.app.folder_lock.close()
        self.root.destroy()

    def test_empty_home_exposes_only_relevant_start_and_independent_demo(self):
        home = self.app.desktop
        self.assertEqual(home.primary_key, "setup")
        self.assertFalse(home.edit.winfo_manager())
        self.assertFalse(home.stop.winfo_manager())
        self.assertFalse(self.app.buttons["revoke"].winfo_manager())
        home.demo.invoke()
        self.demo.assert_called_once_with()
        self.assertFalse(self.app.observing)
        self.assertIsNone(self.app.browser)

    def test_roster_preserves_only_exact_identity_and_clears_vanished_selection(self):
        store = self.app.store
        store.initialize(100, "3000", datetime.now(TZ).date().isoformat())
        people = [Person(f"示例 {n}", Decimal(25)) for n in range(100)]
        store.save_people(people)
        keys = [{"name": p.name, "tracking_id": f"fixture-{n:03}"} for n, p in enumerate(people)]
        store.bind_keys(keys, "offline-ledger")
        self.app.load_folder()
        self.assertEqual(self.app.desktop.primary_key, "login")
        target = keys[62]["tracking_id"]
        self.app.tree.selection_set("62")
        self.app._selection()
        self.assertIn(target, self.app.detail_var.get())
        store.save_people(list(reversed(people)))
        self.app.refresh_rows(readonly=True)
        self.assertEqual(self.app.selected()["tracking_id"], target)
        self.assertEqual(self.app.tree.selection(), ("37",))
        store.save_people([p for p in people if p != people[62]])
        self.app.refresh_rows(readonly=True)
        self.assertEqual(self.app.tree.selection(), ())
        self.assertNotIn(target, self.app.detail_var.get())
        self.assertTrue(self.app.desktop.row_menu.instate(["disabled"]))

    def test_cleanup_and_pending_audit_point_to_existing_guarded_actions(self):
        self.app.cleanup_failed = True
        self.app._update_buttons()
        self.assertEqual(self.app.desktop.primary_key, "interrupt")
        self.app.cleanup_failed = False
        self.app.audit_failed = True
        self.app._update_buttons()
        self.assertEqual(self.app.desktop.primary_key, "save_audit")
        self.assertFalse(self.app.buttons["save_audit"].instate(["disabled"]))
