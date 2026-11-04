"""Automatic table edits, undo and discard use real guarded workbook writes."""
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from quota_guard.core import DataStore, GuardError, Person, TZ
from quota_guard.people_editor import PeopleEditorSession


class PeopleEditorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = DataStore(Path(self.temp.name) / 'ledger')
        self.store.initialize(3, '100', datetime.now(TZ).date().isoformat(), initial_amount=0)
        self.session = PeopleEditorSession(self.store)

    def person(self, name='测试甲', amount='1'):
        return Person(name, Decimal(amount))

    def assert_disk(self, rows):
        self.assertEqual(DataStore(self.store.folder).read_people(), list(rows))

    def test_open_is_read_only_and_closing_needs_no_save(self):
        original = self.store.workbook_path.read_bytes()
        self.assertEqual(self.session.baseline, (self.person('', '0'),) * 3)
        self.assertFalse(self.session.changed)
        self.assertFalse(self.session.can_undo)
        self.assertEqual(self.store.workbook_path.read_bytes(), original)
        self.session.update(0, self.person(amount='0'))
        saved = self.session.rows
        del self.session  # Normal editor close does not discard.
        self.assert_disk(saved)

    def test_update_add_remove_persist_immediately_and_multiple_undo_restores_steps(self):
        baseline = self.session.rows
        self.assertTrue(self.session.update(0, self.person(amount='0.000000000000000001')))
        updated = self.session.rows
        self.assert_disk(updated)
        self.assertTrue(self.session.add(self.person('测试乙', '-1')))
        added = self.session.rows
        self.assert_disk(added)
        self.assertTrue(self.session.remove(1))
        self.assert_disk(self.session.rows)
        self.assertEqual(self.session.history_depth, 3)
        for expected in (added, updated, baseline):
            self.assertTrue(self.session.undo())
            self.assertEqual(self.session.rows, expected)
            self.assert_disk(expected)
        self.assertFalse(self.session.changed)
        self.assertFalse(self.session.undo())

    def test_discard_restores_opening_values_and_clears_undo_without_restoring_old_bytes(self):
        baseline = self.session.baseline
        self.session.update(0, self.person('目标', '0'))
        self.session.remove(1)
        self.assertTrue(self.session.discard())
        self.assert_disk(baseline)
        self.assertFalse(self.session.changed)
        self.assertFalse(self.session.can_undo)
        self.assertFalse(self.session.discard())

    def test_noop_does_not_write_or_add_history(self):
        original = self.store.workbook_path.read_bytes()
        with patch.object(self.store, 'save_people', wraps=self.store.save_people) as save:
            self.assertFalse(self.session.replace(self.session.rows))
            self.assertFalse(self.session.undo())
            self.assertFalse(self.session.discard())
            save.assert_not_called()
        self.assertEqual(self.store.workbook_path.read_bytes(), original)
        self.assertEqual(self.session.history_depth, 0)

    def test_normalizes_names_and_retains_empty_zero_and_precise_amounts(self):
        rows = [Person('  测试甲  ', '0'), Person('', '0.000000000000000001'), self.person('', '-1')]
        self.session.replace(rows)
        expected = (self.person('测试甲', '0'), self.person('', '0.000000000000000001'), self.person('', '-1'))
        self.assertEqual(self.session.rows, expected)
        self.assert_disk(expected)
        self.assertEqual([row['status'] for row in self.store.evaluate()][1:], ['unassigned', 'unassigned'])

    def test_invalid_and_duplicate_rows_preserve_disk_and_history(self):
        self.session.update(0, self.person())
        old = (self.session.rows, self.session.expected_hash, self.session.history_depth)
        for rows in ([self.person('重复'), self.person('重复')],
                     [Person('错误', 'NaN')], [self.person(str(i)) for i in range(4)]):
            with self.subTest(rows=rows), self.assertRaises(GuardError):
                self.session.replace(rows)
            self.assertEqual((self.session.rows, self.session.expected_hash, self.session.history_depth), old)
            self.assert_disk(old[0])

    def test_invalid_index_cannot_modify_other_rows(self):
        for index in (-1, 3, True, 0.0):
            with self.subTest(index=index):
                with self.assertRaises(GuardError):
                    self.session.update(index, self.person())
                with self.assertRaises(GuardError):
                    self.session.remove(index)
        self.assert_disk(self.session.baseline)

    def test_save_failure_leaves_state_history_and_disk_unchanged(self):
        self.session.update(0, self.person())
        old = (self.session.rows, self.session.expected_hash, self.session.history_depth)
        with patch.object(self.store, 'save_people', side_effect=GuardError('合成占用故障')):
            for action in (lambda: self.session.add(self.person('乙')), self.session.undo, self.session.discard):
                with self.assertRaises(GuardError):
                    action()
                self.assertEqual((self.session.rows, self.session.expected_hash, self.session.history_depth), old)
        self.assert_disk(old[0])

    def test_external_change_blocks_edit_undo_discard_and_noop(self):
        self.session.update(0, self.person())
        old = (self.session.rows, self.session.expected_hash, self.session.history_depth)
        external = [self.person('外部已保存', '9')]
        self.store.save_people(external)
        for action in (lambda: self.session.update(0, self.person('另改')),
                       self.session.undo, self.session.discard,
                       lambda: self.session.replace(self.session.rows)):
            with self.subTest(action=action), self.assertRaisesRegex(GuardError, '其他操作修改'):
                action()
            self.assert_disk(external)
            self.assertEqual((self.session.rows, self.session.expected_hash, self.session.history_depth), old)

    def test_config_change_blocks_save_even_when_workbook_has_not_changed(self):
        self.session.update(0, self.person())
        config = json.loads(self.store.config_path.read_text(encoding='utf-8'))
        config['total_budget'] = '200'
        self.store.config_path.write_text(json.dumps(config), encoding='utf-8')
        old = (self.session.rows, self.session.expected_hash, self.session.history_depth)
        for action in (lambda: self.session.add(self.person('乙')), self.session.undo, self.session.discard):
            with self.assertRaises(GuardError):
                action()
        self.assertEqual((self.session.rows, self.session.expected_hash, self.session.history_depth), old)

    def test_failed_audit_after_write_is_not_claimed_as_rollback_and_blocks_next_overwrite(self):
        old = (self.session.rows, self.session.expected_hash, self.session.history_depth)
        with patch.object(self.store, '_event', side_effect=OSError('合成审计故障')):
            with self.assertRaises(OSError):
                self.session.update(0, self.person())
        self.assertEqual((self.session.rows, self.session.expected_hash, self.session.history_depth), old)
        written = (self.person(), *self.session.baseline[1:])
        self.assert_disk(written)
        with self.assertRaisesRegex(GuardError, '其他操作修改'):
            self.session.discard()
        self.assert_disk(written)

    def test_audit_and_binding_are_not_rewound_by_undo_or_discard(self):
        self.session.update(0, self.person())
        self.store.bind_keys([{'name': '测试甲', 'tracking_id': 'fixture-key'}], 'fixture-account')
        # Account binding is a config change, so open a new editing session.
        session = PeopleEditorSession(self.store)
        existing = self.store.events()
        binding = self.store.active_bindings()
        session.update(0, self.person(amount='2'))
        session.undo()
        session.update(0, self.person(amount='3'))
        session.discard()
        self.assertEqual(self.store.active_bindings(), binding)
        events = self.store.events()
        self.assertGreater(len(events), len(existing))
        self.assertTrue(all(event in events for event in existing))
        self.assert_disk(session.baseline)

    def test_history_is_bounded_but_discard_always_has_opening_baseline(self):
        for index in range(55):
            self.session.update(0, self.person(amount=str(index)))
        self.assertEqual(self.session.history_depth, PeopleEditorSession.MAX_HISTORY)
        self.assertTrue(self.session.discard())
        self.assert_disk(self.session.baseline)

    def test_read_race_is_rejected_without_creating_an_inconsistent_baseline(self):
        original = self.store.read_people

        def changed_during_read():
            rows = original()
            self.store.save_people([self.person('并发修改')])
            return rows

        with patch.object(self.store, 'read_people', side_effect=changed_during_read):
            # Do not recurse through save_people.evaluate in this synthetic race.
            with patch.object(self.store, 'evaluate'):
                with self.assertRaisesRegex(GuardError, '读取期间表格已改变'):
                    PeopleEditorSession(self.store)

    def test_account_bound_after_open_invalidates_the_edit_session(self):
        self.session.update(0, self.person())
        self.store.bind_keys([{'name': '测试甲', 'tracking_id': 'fixture-key'}], 'fixture-account')
        with self.assertRaisesRegex(GuardError, '账本设置已改变'):
            self.session.undo()
        self.assert_disk(self.session.rows)

    def test_post_save_external_replacement_does_not_become_the_accepted_hash(self):
        original_save = self.store.save_people
        old = (self.session.rows, self.session.expected_hash, self.session.history_depth)
        external = [self.person('保存后其他操作', '9')]

        def replace_after_save(*args, **kwargs):
            original_save(*args, **kwargs)
            original_save(external)

        with patch.object(self.store, 'save_people', side_effect=replace_after_save):
            with self.assertRaisesRegex(GuardError, '保存后表格内容再次改变'):
                self.session.update(0, self.person())
        self.assertEqual((self.session.rows, self.session.expected_hash, self.session.history_depth), old)
        self.assert_disk(external)
        with self.assertRaisesRegex(GuardError, '其他操作修改'):
            self.session.discard()


if __name__ == '__main__':
    unittest.main()
