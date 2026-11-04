"""Autosave and reversal operate on real synthetic workbooks without a browser."""
from decimal import Decimal
import time
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

import test_defaults_ui as fixtures
from quota_guard.core import DataStore, Person


class EditorAutosaveUiTests(unittest.TestCase):
    cleanup_app = fixtures.DefaultsUiTests.cleanup_app
    button = fixtures.DefaultsUiTests.button
    editor = fixtures.DefaultsUiTests.editor
    set_editor_fields = fixtures.DefaultsUiTests.set_editor_fields

    def setUp(self):
        fixtures.DefaultsUiTests.setUp(self)
        self.errors = self.enterContext(patch('quota_guard.ui.messagebox.showerror'))
        self.enterContext(patch('quota_guard.ui.messagebox.showinfo'))
        self.confirm = self.enterContext(patch('quota_guard.ui.messagebox.askyesno', return_value=True))
        # Consume the constructor's initial load before capturing an edit store.
        self.pump(.08)
        self.initial = [Person('合成甲', Decimal('5')), Person('合成乙', Decimal('9')),
                        Person('', Decimal(0))]
        self.store.save_people(self.initial, allow_unassigned=True)
        self.app.load_folder()
        self.app.edit_people()
        self.dialog = self.editor()
        self.table = next(w for w in fixtures.descendants(self.dialog) if isinstance(w, ttk.Treeview))
        self.root.update_idletasks()

    def pump(self, seconds=.03):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.root.update()
            time.sleep(.005)

    def update(self, name, amount):
        self.set_editor_fields(self.dialog, name, amount)
        self.button(self.dialog, '更新选中行').invoke()

    def stored(self):
        return DataStore(self.home).read_people()

    def assert_not_running(self):
        self.assertIsNone(self.app.browser)
        self.assertIsNone(self.app.delete_permit)
        self.assertFalse(self.app.observing)
        self.assertFalse(self.app.automatic)
        self.assertFalse(any(event['kind'] == 'action' for event in self.store.events()))

    def test_valid_input_saves_after_pause_without_save_or_update_click(self):
        self.set_editor_fields(self.dialog, '合成甲', '0.000000000000000001')
        self.assertEqual(self.stored(), self.initial)
        self.pump(.72)
        expected = [Person('合成甲', Decimal('1e-18')), *self.initial[1:]]
        self.assertEqual(self.stored(), expected)
        self.assertEqual(self.app.rows[0]['amount'], Decimal('1e-18'))
        self.assertTrue(self.dialog.winfo_exists())
        self.confirm.assert_not_called()
        self.assert_not_running()

    def test_cancel_keeps_saved_rows_without_saving_pending_input(self):
        self.update('合成甲', '0')
        self.set_editor_fields(self.dialog, '合成甲改名', '0.0000000001')
        self.button(self.dialog, '取消').invoke()
        self.assertFalse(self.dialog.winfo_exists())
        self.assertEqual(self.stored(), [Person('合成甲', Decimal(0)), *self.initial[1:]])
        self.assert_not_running()

    def test_window_close_keeps_saved_changes(self):
        self.update('合成甲', '0')
        close_callback = self.dialog.protocol('WM_DELETE_WINDOW')
        self.dialog.tk.call(close_callback)
        self.assertFalse(self.dialog.winfo_exists())
        self.assertEqual(self.stored(), [Person('合成甲', Decimal(0)), *self.initial[1:]])
        self.assert_not_running()

    def test_undo_reverts_each_successful_edit_and_keeps_audit_events(self):
        self.update('合成甲', '4')
        self.update('合成甲', '3')
        prior_events = self.store.events()
        self.button(self.dialog, '撤销').invoke()
        self.assertEqual(self.stored()[0].amount, Decimal(4))
        self.button(self.dialog, '撤销').invoke()
        self.assertEqual(self.stored(), self.initial)
        self.assertTrue(self.button(self.dialog, '撤销').instate(['disabled']))
        # Reversal is a new save, not deletion of financial/history rows.
        self.assertGreater(len(self.store.events()), len(prior_events))
        old_ids = {event['id'] for event in prior_events}
        self.assertTrue(old_ids.issubset({event['id'] for event in self.store.events()}))
        self.assert_not_running()

    def test_undo_pending_input_leaves_last_saved_content_and_timer_cancelled(self):
        self.update('合成甲', '4')
        self.set_editor_fields(self.dialog, '尚未保存姓名', '3')
        self.button(self.dialog, '撤销').invoke()
        self.assertEqual(self.stored()[0], Person('合成甲', Decimal(4)))
        self.pump(.65)
        self.assertEqual(self.stored()[0], Person('合成甲', Decimal(4)))
        self.button(self.dialog, '撤销').invoke()
        self.assertEqual(self.stored(), self.initial)

    def test_discard_restores_opening_rows_and_keeps_earlier_saves(self):
        self.update('新姓名', '0')
        self.button(self.dialog, '添加新行').invoke()
        self.button(self.dialog, '不保存本次修改').invoke()
        self.assertFalse(self.dialog.winfo_exists())
        self.assertEqual(self.stored(), self.initial)
        self.assertGreater(len(self.store.events()), 1)
        self.assert_not_running()

    def test_add_and_remove_are_immediate_and_undoable(self):
        self.app._set_table_defaults('0', 'kill_line')
        self.button(self.dialog, '添加新行').invoke()
        self.assertEqual(self.stored(), [*self.initial, Person('', Decimal(0))])
        self.button(self.dialog, '移除选中行').invoke()
        self.assertEqual(self.stored(), self.initial)
        self.button(self.dialog, '撤销').invoke()
        self.assertEqual(self.stored(), [*self.initial, Person('', Decimal(0))])
        self.button(self.dialog, '撤销').invoke()
        self.assertEqual(self.stored(), self.initial)
        self.assert_not_running()

    def test_invalid_input_never_writes_and_close_retains_last_valid_save(self):
        self.update('合成甲', '4')
        expected = self.stored()
        self.set_editor_fields(self.dialog, '合成甲', 'NaN')
        self.pump(.66)
        self.assertEqual(self.stored(), expected)
        self.errors.assert_not_called()  # Background validation uses inline feedback.
        self.button(self.dialog, '取消').invoke()
        self.assertFalse(self.dialog.winfo_exists())
        self.assertEqual(self.stored(), expected)
        self.assertIn('未执行额外保存', self.app.status_var.get())

    def test_external_change_blocks_update_undo_and_discard_without_overwrite(self):
        self.update('合成甲', '4')
        external = [Person('外部合成修改', Decimal('7'))]
        self.store.save_people(external)
        external_bytes = self.store.workbook_path.read_bytes()
        self.update('合成甲', '3')
        self.assertEqual(self.store.workbook_path.read_bytes(), external_bytes)
        self.button(self.dialog, '撤销').invoke()  # Discard only the failed draft.
        self.button(self.dialog, '撤销').invoke()  # This reversal must check the hash.
        self.button(self.dialog, '不保存本次修改').invoke()
        self.assertTrue(self.dialog.winfo_exists())
        self.assertEqual(self.store.workbook_path.read_bytes(), external_bytes)
        self.button(self.dialog, '取消').invoke()
        self.assertFalse(self.dialog.winfo_exists())
        self.assertEqual(self.stored(), external)

    def test_changing_selection_commits_old_row_before_loading_new_row(self):
        self.set_editor_fields(self.dialog, '合成甲', '2')
        self.table.selection_set('1')
        self.table.event_generate('<<TreeviewSelect>>')
        self.pump()
        self.assertEqual(self.stored(), [Person('合成甲', Decimal(2)), *self.initial[1:]])
        entries = [w for w in fixtures.descendants(self.dialog) if isinstance(w, ttk.Entry)]
        self.assertEqual([entry.get() for entry in entries], ['合成乙', '9'])

    def test_all_blank_named_zero_edits_are_unassigned_and_never_delete(self):
        self.update('', '0')
        self.assertEqual(self.stored()[0], Person('', Decimal(0)))
        self.assertEqual(self.app.rows[0]['status'], 'unassigned')
        self.assert_not_running()

    def test_write_failure_keeps_disk_and_previous_undo_history(self):
        self.update('合成甲', '4')
        saved_bytes = self.store.workbook_path.read_bytes()
        with patch.object(self.app.store, 'save_people', side_effect=OSError('synthetic disk failure')):
            self.update('合成甲', '3')
        self.assertEqual(self.store.workbook_path.read_bytes(), saved_bytes)
        self.errors.assert_called_once()
        self.button(self.dialog, '撤销').invoke()  # Failed, unsaved input only.
        self.assertEqual(self.stored()[0].amount, Decimal(4))
        self.button(self.dialog, '撤销').invoke()
        self.assertEqual(self.stored(), self.initial)

    def test_duplicate_name_cannot_replace_last_valid_workbook(self):
        before = self.store.workbook_path.read_bytes()
        self.update('合成乙', '0')
        self.assertEqual(self.store.workbook_path.read_bytes(), before)
        self.errors.assert_called_once()
        self.assertIn('重复', self.errors.call_args.args[1])
        self.button(self.dialog, '取消').invoke()
        self.assertEqual(self.stored(), self.initial)

    def test_editor_entry_does_not_open_during_other_work_or_management(self):
        self.button(self.dialog, '取消').invoke()
        for state in ('busy', 'observing', 'closing'):
            with self.subTest(state=state):
                setattr(self.app, state, True)
                try:
                    self.app.edit_people()
                    self.assertFalse(any(isinstance(w, tk.Toplevel) for w in self.root.winfo_children()))
                finally:
                    setattr(self.app, state, False)
        self.assertEqual(self.stored(), self.initial)

    def test_reopened_editor_discard_keeps_previous_windows_saved_content(self):
        self.update('合成甲', '4')
        self.button(self.dialog, '取消').invoke()
        self.app.edit_people()
        self.dialog = self.editor()
        self.update('合成甲', '3')
        self.button(self.dialog, '不保存本次修改').invoke()
        self.assertEqual(self.stored(), [Person('合成甲', Decimal(4)), *self.initial[1:]])

    def test_remove_uses_actual_selection_before_selection_callback_is_delivered(self):
        self.table.selection_set('1')
        # Do not pump <<TreeviewSelect>>: the old form still shows row 0.
        self.button(self.dialog, '移除选中行').invoke()
        self.assertEqual(self.stored(), [self.initial[0], self.initial[2]])
        self.button(self.dialog, '撤销').invoke()
        self.assertEqual(self.stored(), self.initial)

    def test_update_rejects_unsettled_selection_instead_of_writing_wrong_row(self):
        self.set_editor_fields(self.dialog, '合成甲', '4')
        self.table.selection_set('1')
        self.button(self.dialog, '更新选中行').invoke()
        self.assertEqual(self.stored(), self.initial)
        self.errors.assert_called_once()
        self.pump()
        # Selection processing may finish the old row edit, but must never
        # copy the old form into the newly selected second row.
        self.assertEqual(self.stored()[1], self.initial[1])

    def test_input_during_unsettled_selection_cannot_be_committed_by_later_event(self):
        self.table.selection_set('1')
        # A unique name must not be protected merely by duplicate validation.
        self.set_editor_fields(self.dialog, '选择期间的新姓名', '3')
        self.button(self.dialog, '更新选中行').invoke()
        self.pump(.7)
        self.assertEqual(self.stored(), self.initial)
        self.button(self.dialog, '移除选中行').invoke()
        self.assertEqual(self.stored(), self.initial)
        self.button(self.dialog, '撤销').invoke()
        self.table.selection_set('1')
        self.pump()
        self.update('确认选中后的新姓名', '3')
        self.assertEqual(self.stored(), [self.initial[0], Person('确认选中后的新姓名', Decimal(3)), self.initial[2]])
        self.assert_not_running()

    def test_footer_stays_inside_window_at_high_scaling_and_minimum_size(self):
        self.button(self.dialog, '取消').invoke()
        original_scaling = self.root.tk.call('tk', 'scaling')
        self.root.attributes('-alpha', 0)
        self.root.deiconify()
        self.pump()
        try:
            for scaling in (1.5, 2.0, 2.5):
                with self.subTest(scaling=scaling):
                    self.root.tk.call('tk', 'scaling', scaling)
                    self.app.edit_people()
                    dialog = self.editor()
                    # Map transparently only to exercise the actual geometry manager.
                    dialog.attributes('-alpha', 0)
                    dialog.transient('')
                    dialog.deiconify()
                    min_width, min_height = dialog.minsize()
                    dialog.geometry(f'{min_width}x{min_height}')
                    self.pump()
                    for label in ('不保存本次修改', '撤销', '取消'):
                        button = self.button(dialog, label)
                        self.assertTrue(button.winfo_ismapped())
                        left = button.winfo_rootx() - dialog.winfo_rootx()
                        top = button.winfo_rooty() - dialog.winfo_rooty()
                        self.assertGreaterEqual(left, 0)
                        self.assertGreaterEqual(top, 0)
                        self.assertLessEqual(left + button.winfo_width(), dialog.winfo_width())
                        self.assertLessEqual(top + button.winfo_height(), dialog.winfo_height())
                    self.button(dialog, '取消').invoke()
        finally:
            self.root.withdraw()
            self.root.tk.call('tk', 'scaling', original_scaling)


if __name__ == '__main__':
    unittest.main()
