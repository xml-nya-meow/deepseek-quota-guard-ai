"""Real Tk + disposable ledger, offline controlled browser failures."""
import unittest
from unittest.mock import patch

import test_workflow as fixtures
from quota_guard.revocation_permit import KeyInspectionError


class ManagementFailureStateTests(unittest.TestCase):
    setUp = fixtures.WorkflowTests.setUp
    tearDown = fixtures.WorkflowTests.tearDown
    pump = fixtures.WorkflowTests.pump
    valid_collection = fixtures.WorkflowTests.valid_collection
    empty_collection = fixtures.WorkflowTests.empty_collection

    def prime_fees(self):
        self.app.browser = fixtures.OfflineBrowser({'collect': self.valid_collection()})
        self.app.collect(); self.pump()
        self.assertEqual(self.app.action_states['collect'], '成功')
        self.assertTrue(self.app._collection_is_current())
        self.app.tree.selection_set('0')

    def fail_inspection(self, code='KEY_TARGET_KIND_UNSUPPORTED'):
        browser = fixtures.ControlledBrowser()
        self.app.browser = browser
        self.app.inspect_revoke()
        self.assertEqual(browser.calls[-1][0], 'inspect_revoke')
        browser.futures[-1].set_exception(KeyInspectionError(reason_code=code))
        self.pump()
        return browser

    def test_contract_failure_preserves_saved_fact_without_session_or_action_authority(self):
        self.prime_fees()
        before = self.store.current_snapshot_id()
        prior = dict(self.app.last_saved_fee_fact)
        browser = self.fail_inspection()
        text = self.app.collection_summary_var.get()
        self.assertIn('最近费用已保存', text)
        self.assertIn(self.start, text)
        self.assertIn('此错误不等于平台已退出登录', text)
        self.assertEqual(self.store.current_snapshot_id(), before)
        self.assertEqual(self.app.last_saved_fee_fact, prior)
        self.assertFalse(self.app.session_ready)
        self.assertFalse(self.app._collection_is_current())
        self.assertIsNone(self.app.collection_observed_wall)
        self.assertIsNone(self.app.collection_observed_monotonic)
        self.assertFalse(self.app.observing)
        self.assertFalse(self.app.automatic)
        self.assertEqual([call[0] for call in browser.calls], ['inspect_revoke'])
        self.assertFalse(self.store.unresolved_actions())

    def test_saved_fact_survives_age_without_renewing_fees(self):
        self.prime_fees()
        original = self.app.collection_observed_wall
        with patch('quota_guard.ui.time.time', return_value=original + 1000):
            self.assertFalse(self.app._collection_is_current())
            self.fail_inspection('KEY_REDACTED_ID_INVALID')
        self.assertIn('最近费用已保存', self.app.collection_summary_var.get())
        self.assertEqual(self.app.last_saved_fee_fact['observed_wall'], original)
        self.assertIsNone(self.app.collection_context)

    def test_no_fee_snapshot_never_claims_money_was_saved(self):
        self.app.browser = fixtures.OfflineBrowser({'collect': self.empty_collection()})
        self.app.collect(); self.pump()
        self.app.tree.selection_set('0')
        self.fail_inspection()
        self.assertNotIn('最近费用已保存', self.app.collection_summary_var.get())
        self.assertIn('当前删除资格已失效', self.app.collection_summary_var.get())

    def test_direct_recollection_recovers_without_login_or_automatic_resume(self):
        self.prime_fees()
        self.fail_inspection()
        browser = fixtures.OfflineBrowser({'collect': self.valid_collection(source_id='reread-fixture')})
        self.app.browser = browser
        self.app.collect(); self.pump()
        self.assertEqual([call[0] for call in browser.calls], ['collect'])
        self.assertTrue(self.app.session_ready)
        self.assertTrue(self.app._collection_is_current())
        self.assertFalse(self.app.automatic)
        self.assertFalse(self.app.observing)
        self.assertEqual(self.app.collection_reason_code, 'OK')

    def test_saved_fact_is_not_reused_for_another_folder_period_or_snapshot(self):
        self.prime_fees()
        original = dict(self.app.last_saved_fee_fact)
        for field, wrong in (('folder', self.folder/'other'), ('period_id', 'other'), ('source_id', 'other')):
            self.app.last_saved_fee_fact = dict(original, **{field: wrong})
            with self.subTest(field=field): self.assertEqual(self.app._saved_fee_fact(), '')
        self.app.last_saved_fee_fact = original

    def test_auth_failure_is_not_mislabelled_as_contract_failure(self):
        self.prime_fees()
        self.fail_inspection('KEY_HTTP_AUTH_REQUIRED')
        self.assertNotIn('此错误不等于平台已退出登录', self.app.collection_summary_var.get())
        self.assertFalse(self.app.session_ready)
        self.assertFalse(self.app._collection_is_current())

    def test_old_inspection_failure_cannot_clear_new_generation(self):
        self.prime_fees()
        browser = fixtures.ControlledBrowser()
        self.app.browser = browser
        self.app.inspect_revoke()
        self.app.session_generation += 1
        self.app.collection_summary_var.set('new generation')
        browser.futures[-1].set_exception(KeyInspectionError(reason_code='KEY_TARGET_KIND_UNSUPPORTED'))
        self.pump()
        self.assertEqual(self.app.collection_summary_var.get(), 'new generation')
        self.assertTrue(self.app.session_ready)
        self.assertFalse(self.app.busy)
