"""Creation-based UI flow with real ledgers and a synthetic browser only."""
from datetime import datetime, timedelta
from decimal import Decimal
import unittest
from unittest.mock import patch

import test_workflow as fixtures
from quota_guard.core import TZ, Person


class LifetimeUiTests(unittest.TestCase):
    tearDown = fixtures.WorkflowTests.tearDown
    pump = fixtures.WorkflowTests.pump
    valid_collection = fixtures.WorkflowTests.valid_collection

    def setUp(self):
        fixtures.WorkflowTests.setUp(self)
        self.store.save_people([Person('测试甲', Decimal(2)), Person('测试乙', Decimal(80))])
        self.old_store = self.store
        self.store = self.old_store.create_successor(self.folder / 'lifetime', fee_basis='key_lifetime')
        self.app._switch_folder(self.store.folder, reset_session=False)
        self.created = int((datetime.now(TZ) - timedelta(days=3)).timestamp())

    def result(self, **overrides):
        periods = {key['tracking_id']: {'created_at': self.created,
            'start_date': datetime.fromtimestamp(self.created, TZ).date().isoformat(),
            'end_date': self.start} for key in self.keys}
        values = dict(fee_basis='key_lifetime', key_periods=periods,
                      requested_tracking_ids=[k['tracking_id'] for k in self.keys])
        values.update(overrides)
        return self.valid_collection(**values)

    def read(self, **overrides):
        browser = fixtures.OfflineBrowser({'collect': self.result(**overrides)})
        self.app.browser = browser
        self.app.collect()
        self.pump()
        return browser

    def test_creation_dates_earlier_than_ledger_are_applied_and_explicitly_displayed(self):
        browser = self.read()
        self.assertEqual(browser.calls[0][1]['fee_basis'], 'key_lifetime')
        self.assertTrue(self.app._collection_is_current())
        self.assertTrue(self.app.collection_has_fees)
        self.assertIn('创建以来', self.app.collection_summary_var.get())
        self.assertIn('创建以来', self.app.summary_var.get())
        self.assertEqual(self.app.rows[1]['spent'], Decimal(5))
        self.assertFalse(self.app.automatic)

    def test_old_period_result_cannot_populate_lifetime_ledger(self):
        self.read(fee_basis='period')
        self.assertFalse(self.app._collection_is_current())
        self.assertFalse(self.app.collection_has_fees)
        self.assertEqual(self.app.store.current_snapshot_id(), '')
        self.assertIn('统计方式不匹配', self.app.action_states['collect'])

    def test_missing_creation_evidence_never_grants_zero_quota_management(self):
        self.app.store.save_people([Person('测试甲', Decimal(0)), Person('测试乙', Decimal(80))])
        self.read(totals=None, key_periods={}, reason_code='LIFETIME_METADATA_INVALID')
        self.assertFalse(self.app._collection_is_current())
        self.assertFalse(self.app.automatic)
        self.assertEqual(self.app.action_states['collect'], '创建日期未核验')

    def test_valid_creation_evidence_with_unknown_cost_keeps_original_zero_quota_rule(self):
        self.app.store.save_people([Person('测试甲', Decimal(0)), Person('测试乙', Decimal(80))])
        self.read(totals=None, source_id='', reason_code='NO_SELECTED_USAGE_DATA')
        self.assertTrue(self.app._collection_is_current())
        self.assertFalse(self.app.collection_has_fees)
        self.assertEqual(self.app.rows[0]['status'], 'triggered')
        self.assertIsNone(self.app.rows[1]['spent'])

    def test_missing_requested_scope_cannot_publish_fees(self):
        value = self.result()
        del value['requested_tracking_ids']
        self.app.browser = fixtures.OfflineBrowser({'collect': value})
        self.app.collect()
        self.pump()
        self.assertFalse(self.app._collection_is_current())
        self.assertEqual(self.app.store.current_snapshot_id(), '')

    def test_legacy_switch_preserves_original_and_uses_new_ledger(self):
        self.app._switch_folder(self.old_store.folder, reset_session=False)
        self.old_store.bind_keys(self.keys, 'synthetic-account')
        self.old_store.record_action('测试甲', 'test-key-a', 'unknown_result', 'synthetic unresolved')
        before = {p: p.read_bytes() for p in
                  (self.old_store.config_path, self.old_store.workbook_path, self.old_store.db_path)}
        destination = self.folder / 'explicit-switch'
        with patch('quota_guard.ui.messagebox.askyesnocancel', return_value=True), \
                patch('quota_guard.ui.new_ledger_path', return_value=destination):
            self.app.change_statistics_mode()
            self.pump()
        self.assertEqual(self.app.folder, destination.resolve())
        self.assertEqual(self.app.config.fee_basis, 'key_lifetime')
        self.assertEqual(self.app.config.account_id, 'synthetic-account')
        self.assertEqual(self.app.store.active_bindings(), self.old_store.active_bindings())
        self.assertEqual(self.app.store.unresolved_actions()[0]['tracking_id'], 'test-key-a')
        self.assertFalse(self.app.automatic)
        self.assertFalse(self.app.session_ready)
        self.assertEqual({p: p.read_bytes() for p in before}, before)

    def test_invalid_metadata_during_management_stops_without_immediate_read_loop(self):
        self.read()
        self.app.observing = self.app.automatic = True
        browser = self.read(totals=None, key_periods={}, reason_code='LIFETIME_METADATA_INVALID', retryable=True)
        self.pump()
        self.assertFalse(self.app.observing)
        self.assertFalse(self.app.automatic)
        self.assertEqual([action for action, _ in browser.calls], ['collect'])
        self.assertFalse(self.app._collection_is_current())

    def test_unknown_cost_cannot_change_previously_saved_creation_identity(self):
        self.read()
        periods = self.result()['key_periods']
        periods['test-key-a']['created_at'] -= 1
        self.read(totals=None, key_periods=periods, reason_code='NO_SELECTED_USAGE_DATA')
        self.assertFalse(self.app._collection_is_current())
        self.assertFalse(self.app.automatic)

    def test_lifetime_date_button_only_explains_and_cannot_truncate_history(self):
        folder = self.app.folder
        with patch('quota_guard.ui.messagebox.showinfo') as info, \
                patch('quota_guard.ui.ask_text') as prompt:
            self.app.change_start_date()
        info.assert_called_once()
        prompt.assert_not_called()
        self.assertEqual(self.app.folder, folder)


if __name__ == '__main__':
    unittest.main()
