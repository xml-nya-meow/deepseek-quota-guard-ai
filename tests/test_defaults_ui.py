"""New-table defaults and appearance never rewrite active ledger rules."""
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
import tempfile
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

from quota_guard.core import DataStore, GuardError, Person, TZ
from quota_guard.preferences import load_table_defaults, load_poll_interval, save_poll_interval
from isolated_app import App
from quota_guard.ui import prepare_initial_draft


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


class DraftTests(unittest.TestCase):
    def test_only_valid_unnamed_rows_are_converted_to_skip(self):
        people, count = prepare_initial_draft([
            ('  示例甲 ', '0.000000000000000001'), ('', '0'), (' ', '2'), ('', '-1'), ('示例乙', '0')])
        self.assertEqual(count, 2)
        self.assertEqual(people, [Person('示例甲', Decimal('1e-18')),
                                 Person('', Decimal(-1)), Person('', Decimal(-1)),
                                 Person('', Decimal(-1)), Person('示例乙', Decimal(0))])

    def test_blank_names_do_not_hide_invalid_amounts(self):
        for value in ('', '-2', 'NaN', 'Infinity', '1e-19', '1e13', '=1+2'):
            with self.subTest(value=value), self.assertRaises(GuardError):
                prepare_initial_draft([('', value)])


class DefaultsUiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.store = DataStore(self.home)
        self.store.initialize(3, 100, datetime.now(TZ).date().isoformat())
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root, self.home)
        self.app.load_folder()
        self.addCleanup(self.cleanup_app)

    def cleanup_app(self):
        if self.app.target_lock is not None:
            self.app.target_lock.close()
        self.app.folder_lock.close()
        for callback in self.root.tk.call('after', 'info'):
            self.root.after_cancel(callback)
        self.root.destroy()

    def button(self, parent, label):
        return next(widget for widget in descendants(parent)
                    if isinstance(widget, ttk.Button) and str(widget.cget('text')) == label)

    def editor(self):
        return next(widget for widget in self.root.winfo_children() if isinstance(widget, tk.Toplevel))

    def set_editor_fields(self, dialog, name, amount):
        entries = [widget for widget in descendants(dialog) if isinstance(widget, ttk.Entry)]
        self.assertEqual(len(entries), 2)
        for entry, value in zip(entries, (name, amount)):
            entry.delete(0, tk.END)
            entry.insert(0, str(value))

    def radio(self, parent, label):
        return next(widget for widget in descendants(parent)
                    if isinstance(widget, ttk.Radiobutton) and str(widget.cget('text')) == label)

    def set_list_fields(self, dialog, *, count, budget, start):
        entries = [widget for widget in descendants(dialog) if isinstance(widget, ttk.Entry)]
        self.assertEqual(len(entries), 4)
        for entry, value in zip(entries[:3], (count, budget, start)):
            entry.delete(0, tk.END)
            entry.insert(0, str(value))

    def original_ledger_bytes(self):
        return {path.name: path.read_bytes() for path in
                (self.store.workbook_path, self.store.config_path, self.store.db_path)}

    def test_save_defaults_preserves_workbook_rules_interval_and_management(self):
        self.store.save_people([Person('原人员', Decimal(5))])
        before = self.store.workbook_path.read_bytes()
        events = self.store.events()
        save_poll_interval(self.home, 25)
        self.app.kill_line_var.set('0.0000000001')
        self.app.table_fill_var.set('斩杀线')
        self.app.apply_table_defaults()
        defaults, _ = load_table_defaults(self.home)
        self.assertEqual(defaults.amount, Decimal('0.0000000001'))
        self.assertEqual(load_poll_interval(self.home), (25, None))
        self.assertEqual(self.store.workbook_path.read_bytes(), before)
        self.assertEqual(self.store.events(), events)
        self.assertFalse(self.app.observing)
        self.assertFalse(self.app.automatic)

    def test_invalid_or_failed_default_save_keeps_effective_settings(self):
        self.app.kill_line_var.set('-1')
        self.app.table_fill_var.set('斩杀线')
        self.app.apply_table_defaults()
        self.assertEqual(self.app.table_defaults.amount, Decimal(-1))
        self.app.kill_line_var.set('0')
        with patch('quota_guard.ui.save_table_defaults', side_effect=OSError('fixture')):
            self.app.apply_table_defaults()
        self.assertEqual(self.app.table_defaults.amount, Decimal(-1))
        self.assertIn('未保存', self.app.defaults_feedback_var.get())

    def test_draft_prefill_cancel_preserves_canonical_table(self):
        before = self.store.workbook_path.read_bytes()
        self.app.edit_people(initial_amount=Decimal('0.1'))
        dialog = self.editor()
        table = next(w for w in descendants(dialog) if isinstance(w, ttk.Treeview))
        self.assertEqual([table.item(i, 'values')[1] for i in table.get_children()], ['0.1'] * 3)
        self.button(dialog, '取消').invoke()
        self.assertEqual(self.store.workbook_path.read_bytes(), before)
        self.assertFalse(self.app.recovery_mode)

    def test_first_setup_persists_saved_defaults_without_activating_unnamed_rows(self):
        self.app._set_table_defaults('0.0000000001', 'kill_line')
        fresh = self.home / 'fresh-ledger'
        self.app._switch_folder(fresh, reset_session=False)
        self.app.setup()
        self.button(self.editor(), '创建新列表').invoke()
        table = next(w for w in descendants(self.editor()) if isinstance(w, ttk.Treeview))
        self.assertEqual([table.item(i, 'values')[1] for i in table.get_children()],
                         ['0.0000000001'] * 14)
        expected = [Person('', Decimal('0.0000000001'))] * 14
        self.assertEqual(self.app.store.read_people(), expected)
        self.assertEqual(DataStore(fresh).read_people(), expected)
        self.assertTrue(all(row['status'] == 'unassigned' for row in self.app.rows))
        self.assertFalse(self.app.recovery_mode)
        self.assertFalse(self.app.observing)

    def test_new_list_button_enabled_for_existing_ledger_and_cancel_keeps_original(self):
        self.store.save_people([Person('原人员', Decimal('5.123456789012345678'))])
        self.app.load_folder()
        before = self.original_ledger_bytes()
        original_folder, original_config = self.app.folder, self.app.config
        self.assertFalse(self.app.buttons['setup'].instate(['disabled']))
        self.assertTrue(str(self.app.buttons['setup'].cget('text')).startswith('新建列表'))
        with patch('quota_guard.ui.new_ledger_path') as allocate, patch.object(self.app, 'edit_people') as edit:
            self.app.buttons['setup'].invoke()
            dialog = self.editor()
            self.assertEqual(dialog.title(), '新建列表')
            self.button(dialog, '取消').invoke()
        allocate.assert_not_called()
        edit.assert_not_called()
        self.assertEqual(self.app.folder, original_folder)
        self.assertEqual(self.app.config, original_config)
        self.assertEqual(self.original_ledger_bytes(), before)
        self.assertFalse(any(isinstance(widget, tk.Toplevel) for widget in self.root.winfo_children()))

    def test_new_list_defaults_to_inheriting_amounts_with_independent_period_settings(self):
        original_people = [Person('原人员', Decimal('5.123456789012345678')),
                           Person('原跳过人员', Decimal('-1'))]
        self.store.save_people(original_people)
        self.store.bind_keys([{'name': '原人员', 'tracking_id': 'fixture-original'}], 'fixture-account')
        old_config = self.store.load()
        self.store.apply_snapshot({'fixture-original': Decimal('0.2')}, 'fixture-old-cost',
                                  old_config.start_date, old_config.start_date)
        self.app.load_folder()
        self.app._set_table_defaults('0', 'kill_line')
        before = self.original_ledger_bytes()
        destination = self.home / 'new-inherited'
        new_start = (datetime.now(TZ).date() - timedelta(days=1)).isoformat()
        self.app.setup()
        dialog = self.editor()
        self.assertTrue(self.radio(dialog, '延续现有人员与个人额度').instate(['selected']))
        self.set_list_fields(dialog, count=4, budget='17', start=new_start)
        with patch('quota_guard.ui.new_ledger_path', return_value=destination):
            self.button(dialog, '创建新列表').invoke()
        config = self.app.store.load()
        self.assertEqual(self.app.folder, destination.resolve())
        self.assertNotEqual(config.period_id, old_config.period_id)
        self.assertEqual((config.people_count, config.total_budget, config.start_date, config.share),
                         (4, Decimal('17'), new_start, Decimal('4')))
        self.assertEqual(config.account_id, 'fixture-account')
        expected = original_people + [Person('', Decimal(0))] * 2
        self.assertEqual(self.app.store.read_people(), expected)
        table = next(widget for widget in descendants(self.editor()) if isinstance(widget, ttk.Treeview))
        shown = [table.item(item, 'values') for item in table.get_children()]
        self.assertEqual([(values[0], Decimal(values[1])) for values in shown],
                         [(person.name, person.amount) for person in expected])
        self.assertTrue(all(row['spent'] is None for row in self.app.store.evaluate()))
        self.assertEqual(self.original_ledger_bytes(), before)
        self.assertFalse(self.app.observing)
        self.assertFalse(self.app.automatic)
        self.assertIsNone(self.app.browser)

    def test_new_blank_list_persists_saved_defaults_and_preserves_original(self):
        self.store.save_people([Person('保留的旧人员', Decimal('7'))])
        self.app.load_folder()
        self.app._set_table_defaults('0.0000000001', 'kill_line')
        before = self.original_ledger_bytes()
        destination = self.home / 'new-blank'
        self.app.setup()
        dialog = self.editor()
        self.set_list_fields(dialog, count=2, budget='40', start=self.app.config.start_date)
        self.radio(dialog, '建立空白列表（使用上方新表默认值）').invoke()
        with patch('quota_guard.ui.new_ledger_path', return_value=destination):
            self.button(dialog, '创建新列表').invoke()
        expected = [Person('', Decimal('0.0000000001'))] * 2
        self.assertEqual(self.app.store.read_people(), expected)
        table = next(widget for widget in descendants(self.editor()) if isinstance(widget, ttk.Treeview))
        shown = [table.item(item, 'values') for item in table.get_children()]
        self.assertEqual(shown, [('', '0.0000000001')] * 2)
        self.button(self.editor(), '取消').invoke()
        self.assertEqual(self.app.folder, destination.resolve())
        self.assertEqual(self.app.store.read_people(), expected)
        self.assertEqual(DataStore(destination).read_people(), expected)
        self.app.load_folder()
        self.app.edit_people()
        reopened = next(widget for widget in descendants(self.editor()) if isinstance(widget, ttk.Treeview))
        self.assertEqual([reopened.item(item, 'values') for item in reopened.get_children()], shown)
        self.button(self.editor(), '取消').invoke()
        self.assertEqual(self.original_ledger_bytes(), before)
        self.assertFalse(self.app.observing)
        self.assertFalse(self.app.automatic)

    def test_new_blank_list_preserves_unresolved_operation_and_original_evidence(self):
        self.store.save_people([Person('原人员', Decimal('5'))])
        self.store.bind_keys([{'name': '原人员', 'tracking_id': 'fixture-original'}], 'fixture-account')
        evidence = self.store.action_evidence('原人员', 'fixture-original')
        self.store.record_action('原人员', 'fixture-original', 'requested', 'fixture',
                                operation_id='fixture-operation', evidence=evidence)
        self.store.record_action('原人员', 'fixture-original', 'unknown_result', 'fixture',
                                operation_id='fixture-operation', continuation=True)
        self.app.load_folder()
        before = self.original_ledger_bytes()
        self.app.setup()
        dialog = self.editor()
        self.radio(dialog, '建立空白列表（使用上方新表默认值）').invoke()
        with patch('quota_guard.ui.new_ledger_path', return_value=self.home / 'pending-blank'):
            self.button(dialog, '创建新列表').invoke()
        self.assertEqual(self.app.store.load().account_id, 'fixture-account')
        self.assertEqual(self.app.store.active_bindings(), self.store.active_bindings())
        unresolved = self.app.store.unresolved_actions()
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0]['operation_id'], 'fixture-operation')
        self.assertEqual(unresolved[0]['status'], 'unknown_result')
        self.assertEqual(unresolved[0]['evidence'], evidence)
        self.assertEqual(self.original_ledger_bytes(), before)

    def test_new_list_creation_failure_retains_original_list_and_open_dialog(self):
        self.store.save_people([Person('原人员', Decimal('5'))])
        self.app.load_folder()
        before = self.original_ledger_bytes()
        original_folder, original_config = self.app.folder, self.app.config
        destination = self.home / 'failed-create'
        self.app.setup()
        dialog = self.editor()
        with patch('quota_guard.ui.new_ledger_path', return_value=destination), \
                patch.object(DataStore, 'create_list', side_effect=GuardError('fixture creation failure')), \
                patch('quota_guard.ui.messagebox.showerror') as error:
            self.button(dialog, '创建新列表').invoke()
        error.assert_called_once()
        self.assertIn('fixture creation failure', error.call_args.args[1])
        self.assertEqual(self.app.folder, original_folder)
        self.assertEqual(self.app.config, original_config)
        self.assertTrue(dialog.winfo_exists())
        self.assertFalse(destination.exists())
        self.assertEqual(self.original_ledger_bytes(), before)

    def test_new_list_switch_failure_leaves_original_selected_and_complete_successor_preserved(self):
        before = self.original_ledger_bytes()
        original_folder, original_config = self.app.folder, self.app.config
        destination = self.home / 'switch-failed'
        self.app.setup()
        dialog = self.editor()
        with patch('quota_guard.ui.new_ledger_path', return_value=destination), \
                patch.object(self.app, '_switch_folder', side_effect=GuardError('fixture switch failure')), \
                patch('quota_guard.ui.messagebox.showerror') as error:
            self.button(dialog, '创建新列表').invoke()
        error.assert_called_once()
        self.assertEqual(self.app.folder, original_folder)
        self.assertEqual(self.app.config, original_config)
        self.assertEqual(self.original_ledger_bytes(), before)
        self.assertNotEqual(DataStore(destination).load().period_id, original_config.period_id)
        self.assertTrue(dialog.winfo_exists())

    def test_new_list_rejects_original_workbook_changed_while_dialog_open(self):
        self.app.setup()
        dialog = self.editor()
        self.store.save_people([Person('窗口期间新填写', Decimal('9'))])
        modified = self.original_ledger_bytes()
        original_folder = self.app.folder
        with patch('quota_guard.ui.new_ledger_path') as allocate, \
                patch('quota_guard.ui.messagebox.showerror') as error:
            self.button(dialog, '创建新列表').invoke()
        allocate.assert_not_called()
        error.assert_called_once()
        self.assertIn('原列表或会话状态已变化', error.call_args.args[1])
        self.assertEqual(self.app.folder, original_folder)
        self.assertEqual(self.original_ledger_bytes(), modified)

    def test_zero_budget_confirmation_cannot_hide_original_workbook_change(self):
        original_folder, original_config = self.app.folder, self.app.config
        self.app.setup()
        dialog = self.editor()
        self.set_list_fields(dialog, count=3, budget='0', start=original_config.start_date)
        modified = {}

        def change_table_during_confirmation(*args, **kwargs):
            self.store.save_people([Person('确认期间新填写', Decimal('9'))])
            modified.update(self.original_ledger_bytes())
            return True

        with patch('quota_guard.ui.messagebox.askyesno', side_effect=change_table_during_confirmation) as confirm, \
                patch('quota_guard.ui.new_ledger_path') as allocate, \
                patch('quota_guard.ui.messagebox.showerror') as error:
            self.button(dialog, '创建新列表').invoke()
        confirm.assert_called_once()
        self.assertEqual(confirm.call_args.args[0], '零总预算')
        allocate.assert_not_called()
        error.assert_called_once()
        self.assertIn('原列表或会话状态已变化', error.call_args.args[1])
        self.assertEqual(self.app.folder, original_folder)
        self.assertEqual(self.app.config, original_config)
        self.assertEqual(self.original_ledger_bytes(), modified)
        self.assertTrue(dialog.winfo_exists())

    def test_successor_keeps_existing_personal_amounts(self):
        people = [Person('原人员', Decimal(5)), Person('', Decimal(-1))]
        self.store.save_people(people)
        self.app._set_table_defaults('0', 'kill_line')
        successor = self.store.create_successor(self.home / 'successor')
        self.assertEqual(successor.read_people(), people)

    def test_editor_update_immediately_saves_named_zero_and_unassigned_amounts(self):
        self.store.save_people([Person('', Decimal(0))] * 3, allow_unassigned=True)
        self.app.load_folder()
        self.app.edit_people()
        dialog = self.editor()
        self.set_editor_fields(dialog, '新人员', '0')
        with patch('quota_guard.ui.messagebox.askyesno', return_value=True) as ask:
            self.button(dialog, '更新选中行').invoke()
        ask.assert_not_called()
        self.assertEqual(self.store.read_people(), [Person('新人员', Decimal(0)),
                                                   Person('', Decimal(0)), Person('', Decimal(0))])
        self.assertEqual([row['status'] for row in self.app.rows], ['triggered', 'unassigned', 'unassigned'])
        self.assertFalse(self.app.observing)
        self.assertIsNone(self.app.browser)

    def test_created_zero_list_main_table_cancel_and_reopen_all_keep_zero(self):
        self.app._set_table_defaults('0', 'kill_line')
        destination = self.home / 'persisted-zero'
        self.app.setup()
        dialog = self.editor()
        self.radio(dialog, '建立空白列表（使用上方新表默认值）').invoke()
        with patch('quota_guard.ui.new_ledger_path', return_value=destination):
            self.button(dialog, '创建新列表').invoke()
        expected = [Person('', Decimal(0))] * 3
        self.assertEqual(self.app.store.read_people(), expected)
        self.assertEqual([row['status'] for row in self.app.rows], ['unassigned'] * 3)
        main_rows = [self.app.tree.item(item, 'values') for item in self.app.tree.get_children()]
        self.assertEqual([values[1] for values in main_rows], ['0'] * 3)
        self.assertTrue(all('待填写' in values[-1] and '跳过' not in values[-1] for values in main_rows))
        self.button(self.editor(), '取消').invoke()
        self.assertEqual(DataStore(destination).read_people(), expected)
        self.app.load_folder()
        self.assertEqual(self.app.store.read_people(), expected)
        self.assertFalse(self.app.recovery_mode)
        self.app.edit_people()
        table = next(widget for widget in descendants(self.editor()) if isinstance(widget, ttk.Treeview))
        self.assertEqual([table.item(item, 'values')[1] for item in table.get_children()], ['0'] * 3)
        self.button(self.editor(), '取消').invoke()

    def test_all_unnamed_zero_rows_cannot_start_collection_or_automatic_management(self):
        self.store.save_people([Person('', Decimal(0))] * 3, allow_unassigned=True)
        self.app.load_folder()
        self.assertFalse(self.app.check_people(show_dialog=False))
        with patch.object(self.app, '_browser_action') as browser_action, \
                patch('quota_guard.ui.messagebox.askyesno') as confirm:
            self.app.collect()
            self.app.start()
        browser_action.assert_not_called()
        confirm.assert_not_called()
        self.assertFalse(self.app.automatic)
        self.assertFalse(self.app.observing)
        self.assertFalse(any(event['kind'] == 'quota_triggered' for event in self.store.events()))

    def test_persisted_unassigned_amounts_remain_when_one_name_is_filled_and_saved(self):
        amount = Decimal('0.0000000001')
        self.store.save_people([Person('', amount)] * 3, allow_unassigned=True)
        self.app.load_folder()
        self.app.edit_people()
        dialog = self.editor()
        self.set_editor_fields(dialog, '新填写人员', str(amount))
        self.button(dialog, '更新选中行').invoke()
        expected = [Person('新填写人员', amount), Person('', amount), Person('', amount)]
        self.assertEqual(self.store.read_people(), expected)
        self.assertEqual(DataStore(self.home).read_people(), expected)
        self.assertEqual([row['status'] for row in self.app.rows], ['unknown', 'unassigned', 'unassigned'])
        self.assertTrue(self.app.check_people(show_dialog=False))
        self.assertIn('待管理 1 人', self.app.status_var.get())
        self.assertFalse(self.app.automatic)

    def test_existing_editor_does_not_refill_existing_values(self):
        self.store.save_people([Person('已有人员', Decimal(7)), Person('', Decimal(-1))])
        self.app._set_table_defaults('0.1', 'kill_line')
        self.app.edit_people()
        table = next(w for w in descendants(self.editor()) if isinstance(w, ttk.Treeview))
        self.assertEqual([table.item(i, 'values')[1] for i in table.get_children()], ['7', '-1'])

    def test_theme_cycle_and_system_refresh_leave_ledger_unchanged(self):
        before = self.store.workbook_path.read_bytes()
        self.assertEqual(self.app.theme_mode, 'system')
        self.app.cycle_theme()
        self.assertEqual(self.app.theme_mode, 'light')
        self.app.cycle_theme()
        self.assertEqual(self.app.theme_mode, 'dark')
        self.app.cycle_theme()
        with patch('quota_guard.ui.resolve_theme', return_value='dark'):
            self.app._refresh_theme()
        self.assertEqual(self.app.theme_mode, 'system')
        self.assertEqual(self.app.current_theme, 'dark')
        self.assertIn('跟随系统（深色）', self.app.theme_button.cget('text'))
        self.assertEqual(self.store.workbook_path.read_bytes(), before)
        self.assertFalse(self.app.automatic)

    def test_failed_theme_save_leaves_previous_mode(self):
        previous = self.app.theme_mode
        with patch('quota_guard.ui.save_theme_mode', side_effect=OSError('fixture')):
            self.app.cycle_theme()
        self.assertEqual(self.app.theme_mode, previous)
        self.assertIn('外观未保存', self.app.status_var.get())


if __name__ == '__main__':
    unittest.main()
