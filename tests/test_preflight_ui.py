"""Read-only management preflight cannot become a deletion or renew fee evidence."""
from decimal import Decimal
import unittest

import test_workflow as fixtures
from quota_guard.core import Person


class PreflightUITests(unittest.TestCase):
    setUp = fixtures.WorkflowTests.setUp
    tearDown = fixtures.WorkflowTests.tearDown
    pump = fixtures.WorkflowTests.pump
    valid_collection = fixtures.WorkflowTests.valid_collection
    empty_collection = fixtures.WorkflowTests.empty_collection
    prime_verified_session = fixtures.WorkflowTests.prime_verified_session

    def result(self, **changes):
        return dict(preflight_passed=True, name='测试甲', tracking_id='test-key-a',
                    key_count=2, **changes)

    def test_read_only_preflight_accepts_below_quota_without_actions_or_fee_refresh(self):
        self.store.save_people([Person('测试甲', Decimal('0.2')), Person('测试乙', Decimal(80))])
        self.app.check_people(show_dialog=False)
        self.prime_verified_session()
        before = self.store.events()
        clocks = (self.app.collection_observed_wall, self.app.collection_observed_monotonic)
        self.app.browser = fixtures.OfflineBrowser({'inspect_revoke': self.result()})
        self.app.tree.selection_set('0')
        self.app.inspect_revoke(); self.pump()
        self.assertEqual([a for a, _ in self.app.browser.calls], ['inspect_revoke'])
        kwargs = self.app.browser.calls[0][1]
        self.assertNotIn('permit', kwargs)
        self.assertNotIn('target_lock_names', kwargs)
        self.assertEqual(self.store.events(), before)
        self.assertEqual((self.app.collection_observed_wall, self.app.collection_observed_monotonic), clocks)
        self.assertIsNone(self.app.delete_permit)
        self.assertFalse(self.app.automatic)
        self.assertEqual(self.app.action_states['inspect'], '准备通过')

    def test_preflight_does_not_start_during_automatic_management(self):
        self.prime_verified_session()
        self.app.browser = fixtures.ControlledBrowser()
        self.app.tree.selection_set('0')
        self.app.automatic = self.app.observing = True
        self.app.inspect_revoke()
        self.assertEqual(self.app.browser.calls, [])

    def test_changed_workbook_discards_preflight_success(self):
        self.prime_verified_session()
        browser = fixtures.ControlledBrowser()
        self.app.browser = browser
        self.app.tree.selection_set('0')
        self.app.inspect_revoke()
        self.store.save_people([Person('测试甲', Decimal('0.2')), Person('测试乙', Decimal(80))])
        browser.futures[0].set_result(self.result()); self.pump()
        self.assertNotEqual(self.app.action_states.get('inspect'), '准备通过')
        self.assertFalse(any(event['kind'] == 'action' for event in self.store.events()))

    def test_mismatched_result_cannot_claim_ready(self):
        self.prime_verified_session()
        result = self.result(); result['tracking_id'] = 'different-target'
        self.app.browser = fixtures.OfflineBrowser({'inspect_revoke': result})
        self.app.tree.selection_set('0')
        self.app.inspect_revoke(); self.pump()
        self.assertNotEqual(self.app.action_states.get('inspect'), '准备通过')
        self.assertIsNone(self.app.delete_permit)
        self.assertFalse(any(event['kind'] == 'action' for event in self.store.events()))

    def test_previous_session_late_success_is_discarded(self):
        self.prime_verified_session()
        browser = fixtures.ControlledBrowser()
        self.app.browser = browser
        self.app.tree.selection_set('0')
        self.app.inspect_revoke()
        self.app.session_generation += 1
        self.app._invalidate_collection(session=True)
        browser.futures[0].set_result(self.result()); self.pump()
        self.assertNotEqual(self.app.action_states.get('inspect'), '准备通过')
        self.assertFalse(self.app.session_ready)
        self.assertIsNone(self.app.delete_permit)
        self.assertFalse(any(event['kind'] == 'action' for event in self.store.events()))
