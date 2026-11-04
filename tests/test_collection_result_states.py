"""History/empty read outcomes and stale-task guards, with no platform access."""
from datetime import date, timedelta
from decimal import Decimal
import json
import unittest
from unittest.mock import patch

import test_workflow as fixtures
from quota_guard.core import GuardError, Person
from quota_guard.ui import App


class CollectionResultStateTests(unittest.TestCase):
    tearDown = fixtures.WorkflowTests.tearDown
    pump = fixtures.WorkflowTests.pump
    valid_collection = fixtures.WorkflowTests.valid_collection
    empty_collection = fixtures.WorkflowTests.empty_collection

    def setUp(self):
        fixtures.WorkflowTests.setUp(self)
        self.store.save_people([Person('测试甲', Decimal(2)), Person('测试乙', Decimal(80))])
        self.app.load_folder()

    def read(self, result):
        self.app.browser = fixtures.OfflineBrowser({'collect': result})
        self.app.collect()
        self.pump()

    def counts(self):
        with self.store._connect() as db:
            return tuple(db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0]
                         for table in ('bindings', 'costs', 'snapshots'))

    def test_empty_response_is_unknown_not_a_successful_zero_charge(self):
        self.read(self.empty_collection())
        self.assertTrue(self.app.session_ready)
        self.assertTrue(self.app._collection_is_current())
        self.assertFalse(self.app.collection_has_fees)
        self.assertEqual(self.counts(), (2, 0, 0))
        self.assertIn('空明细', self.app.collection_summary_var.get())
        self.assertIn('实际费用仍未知', self.app.collection_summary_var.get())
        self.assertIn('费用未知', self.app.buttons['collect'].cget('text'))
        self.assertTrue(all(row['spent'] is None for row in self.app.rows[:2]))

    def test_platform_business_rejection_does_not_claim_account_mismatch(self):
        self.read(self.empty_collection(reason_code='PLATFORM_REJECTED', retryable=False))
        self.assertTrue(self.app.session_ready)
        self.assertFalse(self.app.collection_has_fees)
        self.assertEqual(self.counts(), (2, 0, 0))
        self.assertIn('平台业务层拒绝', self.app.collection_summary_var.get())
        self.assertIn('具体原因未确认', self.app.collection_summary_var.get())
        self.assertEqual(self.app.action_states['login'], '已核验')

    def test_unmatched_fees_show_platform_overlap_without_writing_or_authorizing(self):
        summary = dict(platform_key_count=2, fee_tracking_count=2, matched_platform_count=0,
                       unmatched_fee_count=2, missing_platform_count=2, case_only_match_count=0)
        self.read(self.valid_collection(totals={'synthetic-old-key': Decimal(1),
                                               'synthetic-other-key': Decimal(2)},
                                       coverage_summary=summary))
        self.assertTrue(self.app.session_ready)
        self.assertFalse(self.app._collection_is_current())
        self.assertFalse(self.app.collection_has_fees)
        self.assertEqual(self.counts(), (2, 0, 0))
        self.assertIn('平台已返回费用', self.app.collection_summary_var.get())
        self.assertIn('两者精确匹配 0 枚', self.app.collection_summary_var.get())
        self.assertIn('不代表账户已更换', self.app.collection_summary_var.get())
        self.assertFalse(self.app.observing)
        self.assertIsNone(self.app.delete_permit)

    def test_partial_success_exposes_missing_managed_coverage(self):
        self.read(self.valid_collection(totals={'test-key-a': Decimal('0.01')}))
        self.assertTrue(self.app.collection_has_fees)
        self.assertTrue(self.app._collection_is_current())
        self.assertIn('当前受管 2 枚，本次匹配 1 枚，未覆盖 1 枚',
                      self.app.collection_summary_var.get())
        self.assertIsNone(next(row for row in self.app.rows if row['name'] == '测试乙')['spent'])

    def test_wrong_period_empty_and_nonempty_results_cannot_bind_or_write(self):
        yesterday = (date.fromisoformat(self.start) - timedelta(days=1)).isoformat()
        for totals in (None, {'test-key-a': Decimal(1)}):
            with self.subTest(empty=totals is None), patch.object(self.app, '_claim_targets') as claim:
                before = self.store.events()
                self.read(self.valid_collection(start_date=yesterday, totals=totals))
                claim.assert_not_called()
                self.assertEqual(self.counts(), (0, 0, 0))
                self.assertEqual(self.store.events(), before)
                self.assertFalse(self.app._collection_is_current())
                self.assertEqual(self.app.action_states['collect'], '读取期间设置已变化')

    def test_valid_configuration_changed_during_empty_read_is_rejected_before_binding(self):
        browser = fixtures.ControlledBrowser()
        self.app.browser = browser
        self.app.collect()
        # Simulate an external but internally consistent config update in this
        # temporary ledger while a response is in flight; the workbook is unchanged.
        config = json.loads(self.store.config_path.read_text(encoding='utf8'))
        config['total_budget'] = '1600'
        self.store.config_path.write_text(json.dumps(config), encoding='utf8')
        with self.store._connect() as db:
            db.execute("UPDATE meta SET value='1600' WHERE key='total_budget'")
        self.assertEqual(self.store.load().total_budget, Decimal(1600))
        before = self.store.events()
        with patch.object(self.app, '_claim_targets') as claim:
            browser.futures[0].set_result(self.empty_collection())
            self.pump()
            claim.assert_not_called()
        self.assertEqual(self.counts(), (0, 0, 0))
        self.assertEqual(self.store.events(), before)
        self.assertFalse(self.app._collection_is_current())
        self.assertFalse(self.app.busy)

    def test_context_read_failure_before_dispatch_does_not_start_browser(self):
        browser = fixtures.ControlledBrowser()
        self.app.browser = browser
        with patch.object(self.app, '_collection_context', side_effect=GuardError('synthetic failure')):
            self.app.collect()
        self.assertEqual(browser.calls, [])
        self.assertFalse(self.app.busy)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_unusable_fees_stop_automatic_management_with_a_specific_reason(self):
        with patch.object(self.app, '_diagnostic', wraps=self.app._diagnostic) as diagnostic:
            self.read(self.empty_collection(reason_code='CHARGE_TYPE_UNSUPPORTED', retryable=False))
            self.app.start()
            self.pump()
            self.pump()
        self.assertFalse(self.app.observing)
        self.assertFalse(self.app.automatic)
        self.assertEqual([action for action, _ in self.app.browser.calls], ['collect'])
        diagnostic.assert_any_call('automatic_management_stopped',
                                   reason_code='CHARGE_TYPE_UNSUPPORTED',
                                   stage='automatic_quota_check', count=0,
                                   complete=False, retryable=False)
        self.assertIn('全自动管理已停止', self.app.status_var.get())

    def test_display_summary_rejects_raw_values_and_does_not_change_ids(self):
        self.assertEqual(App._coverage_text({'platform_key_count': 'private-value',
                                           'fee_tracking_count': 1, 'matched_platform_count': 0}), '')
        text = App._coverage_text(dict(platform_key_count=2, fee_tracking_count=2,
                                       matched_platform_count=0, case_only_match_count=2,
                                       arbitrary='private-value'))
        self.assertNotIn('private-value', text)
        self.assertIn('未自动合并', text)


if __name__ == '__main__':
    unittest.main()
