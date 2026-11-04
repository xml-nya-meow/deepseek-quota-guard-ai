"""Portable ledger selection against real, isolated SQLite/workbook fixtures."""
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from quota_guard.core import DataStore, GuardError, TZ
from quota_guard.workspace import POINTER, current_ledger, new_ledger_path, save_current_ledger


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.home = self.root / "portable-app"
        self.start = datetime.now(TZ).date().isoformat()
        self.original = self.make_ledger(self.home)

    def tearDown(self):
        self.temp.cleanup()

    def make_ledger(self, folder):
        store = DataStore(folder)
        store.initialize(2, "160", self.start)
        return store

    def test_no_pointer_opens_existing_home_without_creating_a_period(self):
        period = self.original.load().period_id
        self.assertEqual(current_ledger(self.home), self.home)
        self.assertEqual(DataStore(current_ledger(self.home)).load().period_id, period)
        self.assertFalse((self.home / POINTER).exists())

    def test_selected_child_ledger_restores_and_moves_with_portable_home(self):
        selected = self.make_ledger(new_ledger_path(self.home))
        selected_period = selected.load().period_id
        save_current_ledger(self.home, selected.folder)
        pointer = json.loads((self.home / POINTER).read_text(encoding="utf-8"))
        self.assertFalse(Path(pointer["folder"]).is_absolute())
        self.assertEqual(pointer["period_id"], selected_period)
        # A fresh resolver/reader represents application restart, with no
        # dependence on the previous DataStore instance or working directory.
        self.assertEqual(current_ledger(self.home), selected.folder)
        self.assertEqual(DataStore(current_ledger(self.home)).load().period_id, selected_period)
        relocated = self.root / "copied-portable-app"
        shutil.copytree(self.home, relocated)
        moved_ledger = current_ledger(relocated)
        self.assertEqual(moved_ledger, relocated / selected.folder.relative_to(self.home))
        self.assertEqual(DataStore(moved_ledger).load().period_id, selected_period)
        self.assertNotEqual(moved_ledger, selected.folder)

    def test_corrupt_pointer_never_falls_back_to_the_home_ledger(self):
        original_period = self.original.load().period_id
        pointer = self.home / POINTER
        pointer.write_text("{incomplete", encoding="utf-8")
        with self.assertRaises(GuardError):
            current_ledger(self.home)
        self.assertEqual(pointer.read_text(encoding="utf-8"), "{incomplete")
        self.assertEqual(self.original.load().period_id, original_period)

    def test_missing_selected_directory_does_not_get_recreated(self):
        missing = self.home / "账本" / "missing-period"
        value = {"schema": 1, "folder": str(missing.relative_to(self.home)), "period_id": str(uuid4())}
        pointer = self.home / POINTER
        pointer.write_text(json.dumps(value), encoding="utf-8")
        before = pointer.read_bytes()
        with self.assertRaises(GuardError):
            current_ledger(self.home)
        self.assertFalse(missing.exists())
        self.assertEqual(pointer.read_bytes(), before)

    def test_missing_workbook_opens_original_ledger_for_gui_recovery_without_repair(self):
        selected = self.make_ledger(new_ledger_path(self.home))
        save_current_ledger(self.home, selected.folder)
        period = selected.load().period_id
        selected.workbook_path.unlink()
        self.assertEqual(current_ledger(self.home), selected.folder)
        self.assertFalse(selected.workbook_path.exists())
        with closing(sqlite3.connect(selected.db_path)) as db:
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='period_id'").fetchone()[0], period)

    def test_missing_database_still_blocks_selection_without_reinitializing(self):
        selected = self.make_ledger(new_ledger_path(self.home))
        save_current_ledger(self.home, selected.folder)
        selected.db_path.unlink()
        with self.assertRaises(GuardError):
            current_ledger(self.home)
        self.assertFalse(selected.db_path.exists())

    def test_replaced_period_in_same_directory_is_rejected(self):
        selected = self.make_ledger(new_ledger_path(self.home))
        save_current_ledger(self.home, selected.folder)
        pointer_before = (self.home / POINTER).read_bytes()
        replacement_period = str(uuid4())
        raw = json.loads(selected.config_path.read_text(encoding="utf-8"))
        raw["period_id"] = replacement_period
        selected.config_path.write_text(json.dumps(raw), encoding="utf-8")
        with closing(sqlite3.connect(selected.db_path)) as db, db:
            db.execute("UPDATE meta SET value=? WHERE key='period_id'", (replacement_period,))
        # The replacement is internally valid. It must still fail the persisted
        # selection's expected identity rather than silently accepting a backup.
        self.assertEqual(selected.load().period_id, replacement_period)
        with self.assertRaises(GuardError):
            current_ledger(self.home)
        self.assertEqual((self.home / POINTER).read_bytes(), pointer_before)

    def test_pointer_publish_failure_preserves_previous_selection(self):
        first = self.make_ledger(new_ledger_path(self.home))
        second = self.make_ledger(new_ledger_path(self.home))
        save_current_ledger(self.home, first.folder)
        pointer_before = (self.home / POINTER).read_bytes()
        with patch("quota_guard.core.os.replace", side_effect=PermissionError("fixture publish failure")):
            with self.assertRaises(OSError):
                save_current_ledger(self.home, second.folder)
        self.assertEqual((self.home / POINTER).read_bytes(), pointer_before)
        self.assertEqual(current_ledger(self.home), first.folder)
        self.assertFalse(list(self.home.glob(".quota-*.tmp")))
        self.assertTrue(second.db_path.is_file())

    def test_external_ledger_selection_remains_explicit_absolute_path(self):
        external = self.make_ledger(self.root / "external-ledger")
        save_current_ledger(self.home, external.folder)
        value = json.loads((self.home / POINTER).read_text(encoding="utf-8"))
        self.assertTrue(Path(value["folder"]).is_absolute())
        self.assertEqual(current_ledger(self.home), external.folder)

    def test_new_paths_stay_under_fixed_home_and_are_unique_without_creating_files(self):
        paths = [new_ledger_path(self.home) for _ in range(100)]
        self.assertEqual(len(set(paths)), 100)
        self.assertTrue(all(path.parent == self.home / "账本" for path in paths))
        self.assertTrue(all(path.is_absolute() for path in paths))
        self.assertFalse((self.home / "账本").exists())


if __name__ == "__main__":
    unittest.main()
