"""Rejected fees cannot grant quota permission; explicit force stays independent."""
from decimal import Decimal
import time
import unittest

import test_workflow as fixtures
from quota_guard.core import GuardError, Person
from quota_guard.ui import COLLECTION_MAX_AGE_SECONDS


class FeeFailureEligibilityTests(unittest.TestCase):
    setUp = fixtures.WorkflowTests.setUp
    tearDown = fixtures.WorkflowTests.tearDown
    pump = fixtures.WorkflowTests.pump
    valid_collection = fixtures.WorkflowTests.valid_collection
    empty_collection = fixtures.WorkflowTests.empty_collection
    identity_result = fixtures.WorkflowTests.identity_result
    prime_verified_session = fixtures.WorkflowTests.prime_verified_session

    def persisted_fees(self):
        with self.store._connect() as db:
            snapshots = [tuple(row) for row in db.execute('SELECT * FROM snapshots ORDER BY rowid')]
            costs = [tuple(row) for row in db.execute('SELECT * FROM costs ORDER BY tracking_id')]
        events = [event for event in self.store.events() if event['kind'] == 'snapshot']
        return snapshots, costs, events

    def accept_positive_then_reject_newer_read(self):
        self.store.save_people([Person('测试甲', Decimal('0.0000000001')),
                                Person('测试乙', Decimal(80))])
        self.app.browser = fixtures.OfflineBrowser({'collect': self.valid_collection(
            source_id='accepted-before-functions',
            totals={'test-key-a': Decimal('0.000011'), 'test-key-b': Decimal(5)})})
        self.app.collect(); self.pump()
        accepted_clock = self.app.collection_observed_wall
        before = self.persisted_fees()
        browser = fixtures.OfflineBrowser({'collect': self.empty_collection(
            reason_code='CHARGE_TYPE_UNSUPPORTED', retryable=False,
            message='费用含当前未支持的函数类别。')})
        self.app.browser = browser
        self.app.collect(); self.pump()
        self.assertLess(time.time() - accepted_clock, COLLECTION_MAX_AGE_SECONDS)
        self.assertFalse(self.app.collection_has_fees)
        self.assertTrue(self.app.session_ready)
        self.assertEqual(self.persisted_fees(), before)
        return browser, before

    def test_unsupported_latest_read_blocks_periodic_but_allows_explicit_force(self):
        browser, before = self.accept_positive_then_reject_newer_read()
        current = next(row for row in self.store.evaluate() if row['name'] == '测试甲')
        self.assertEqual(current['spent'], Decimal('0.000011'))
        self.assertEqual(current['status'], 'triggered')  # Historical fee display remains truthful.
        self.app.start(); self.pump(); self.pump()
        self.assertEqual([action for action, _ in browser.calls], ['collect'])
        self.assertIsNone(self.app.delete_permit)
        self.assertFalse(self.app.automatic)
        self.assertFalse(any(event['kind'] == 'action' for event in self.store.events()))
        self.assertEqual(self.persisted_fees(), before)  # Includes original DB snapshot timestamp.

        browser = fixtures.ControlledBrowser(identity_result=self.identity_result())
        self.app.browser = browser
        self.app.tree.selection_set('0'); self.app.revoke_selected_once(); self.pump()
        self.assertEqual([action for action, _ in browser.calls], ['read_keys', 'revoke'])
        permit = browser.calls[1][1]['permit']
        permit.ready.set(); self.pump()
        self.assertTrue(permit.allowed)
        event = self.store.latest_action_record('test-key-a')
        self.assertEqual(event['status'], 'requested')
        self.assertEqual(event['intent'], 'manual_force')
        self.assertEqual(Decimal(event['evidence']['spent']), Decimal('0.000011'))
        self.assertFalse(self.app.collection_has_fees)
        self.assertIsNone(self.app.collection_observed_wall)
        self.assertIsNone(self.app.collection_observed_monotonic)
        self.assertEqual(self.persisted_fees(), before)
        browser.futures[0].set_result({'absent': True}); self.pump()
        self.assertEqual(self.store.latest_action('test-key-a'), 'absent_verified')
        self.assertEqual(self.persisted_fees(), before)

    def test_readonly_preflight_does_not_restore_rejected_positive_fee_eligibility(self):
        _browser, before = self.accept_positive_then_reject_newer_read()
        # collection_* tracks the latest identity/read observation. It is not
        # the persisted timestamp of the last accepted fee snapshot.
        read_clocks = (self.app.collection_observed_wall, self.app.collection_observed_monotonic)
        browser = fixtures.OfflineBrowser({'inspect_revoke': {
            'preflight_passed': True, 'name': '测试甲', 'tracking_id': 'test-key-a', 'key_count': 2}})
        self.app.browser = browser
        self.app.tree.selection_set('0'); self.app.inspect_revoke(); self.pump()
        self.assertEqual(self.app.action_states['inspect'], '准备通过')
        self.assertFalse(self.app.collection_has_fees)
        self.assertEqual((self.app.collection_observed_wall, self.app.collection_observed_monotonic), read_clocks)
        with self.assertRaises(GuardError):
            self.app._current_revoke_target(self.app.rows[0])
        self.assertEqual([action for action, _ in browser.calls], ['inspect_revoke'])
        self.assertIsNone(self.app.delete_permit)
        self.assertEqual(self.persisted_fees(), before)
        self.assertFalse(any(event['kind'] == 'action' for event in self.store.events()))

        # Force obtains its own fresh identity check. Neither preflight nor
        # identity-only reading may refresh the rejected fee observation.
        browser = fixtures.ControlledBrowser(identity_result=self.identity_result())
        self.app.browser = browser
        self.app.tree.selection_set('0'); self.app.revoke_selected_once(); self.pump()
        permit = browser.calls[1][1]['permit']
        permit.ready.set(); self.pump()
        self.assertTrue(permit.allowed)
        self.assertEqual(self.store.latest_action_record('test-key-a')['intent'], 'manual_force')
        self.assertFalse(self.app.collection_has_fees)
        self.assertIsNone(self.app.collection_observed_wall)
        self.assertIsNone(self.app.collection_observed_monotonic)
        self.assertFalse(self.app._collection_is_current())
        self.assertEqual(self.persisted_fees(), before)
        browser.futures[0].set_result({'absent': True}); self.pump()

    def test_unsupported_latest_read_keeps_zero_and_skip_rules_without_new_snapshot(self):
        self.store.save_people([Person('测试甲', Decimal(0)),
                                Person('测试乙', Decimal('0.0000000001')),
                                Person('跳过人员', Decimal(-1))])
        self.keys.append({'name': '跳过人员', 'tracking_id': 'test-key-skip'})
        browser = fixtures.OfflineBrowser({'collect': self.valid_collection(
            totals={'test-key-a': Decimal(0), 'test-key-b': Decimal('0.000011'),
                    'test-key-skip': Decimal(100)}), 'revoke': {'absent': True}})
        self.app.browser = browser
        self.app.collect(); self.pump()
        before = self.persisted_fees()
        browser.results['collect'] = self.empty_collection(
            reason_code='CHARGE_TYPE_UNSUPPORTED', retryable=False)
        self.app.collect(); self.pump()
        self.app.start(); self.pump(); self.pump()
        self.assertEqual([kwargs['tracking_id'] for action, kwargs in browser.calls if action == 'revoke'],
                         ['test-key-a'])
        self.assertIsNone(self.store.latest_action('test-key-b'))
        self.assertIsNone(self.store.latest_action('test-key-skip'))
        self.assertNotIn('intent', self.store.latest_action_record('test-key-a'))
        self.assertEqual(next(row for row in self.store.evaluate() if row['name'] == '跳过人员')['status'], 'skipped')
        self.assertEqual(self.persisted_fees(), before)
        self.assertFalse(self.app.automatic)

    def test_preflight_single_and_periodic_entrypoints_share_inflight_exclusion(self):
        self.prime_verified_session()  # Zero quota is eligible throughout this test.
        browser = fixtures.ControlledBrowser(); self.app.browser = browser
        self.app.tree.selection_set('0'); self.app.inspect_revoke()
        self.assertTrue(self.app.busy)
        self.app.inspect_revoke(); self.app.revoke_selected_once(); self.app.start(); self.app.collect()
        self.assertEqual([action for action, _ in browser.calls], ['inspect_revoke'])
        self.assertIsNone(self.app.delete_permit)
        self.assertFalse(any(event['kind'] == 'action' for event in self.store.events()))
        browser.futures[0].set_result({'preflight_passed': True, 'name': '测试甲',
                                      'tracking_id': 'test-key-a', 'key_count': 2})
        self.pump()

        self.app.tree.selection_set('0'); self.app.revoke_selected_once()
        self.assertTrue(self.app.busy)
        self.app.inspect_revoke(); self.app.revoke_selected_once(); self.app.start(); self.app.collect()
        self.assertEqual([action for action, _ in browser.calls], ['inspect_revoke', 'read_keys'])
        self.assertIsNone(self.app.delete_permit)
        browser.futures[1].set_result(self.identity_result()); self.pump()
        self.assertTrue(self.app.busy)
        self.app.inspect_revoke(); self.app.revoke_selected_once(); self.app.start(); self.app.collect()
        self.assertEqual([action for action, _ in browser.calls], ['inspect_revoke', 'read_keys', 'revoke'])
        self.app.stop(quiet=True)
        browser.futures[2].set_result({'cancelled': True}); self.pump()

        # Force intentionally invalidated quota freshness. A real new fee read
        # is required before periodic management can resume.
        self.app.collect()
        browser.futures[3].set_result(self.empty_collection()); self.pump()
        self.app.start(); self.pump()
        self.assertTrue(self.app.busy)
        self.app.inspect_revoke(); self.app.revoke_selected_once(); self.app.start(); self.app.collect()
        self.assertEqual([action for action, _ in browser.calls],
                         ['inspect_revoke', 'read_keys', 'revoke', 'collect', 'revoke'])
        self.app.stop(quiet=True)
        browser.futures[4].set_result({'cancelled': True}); self.pump()


if __name__ == '__main__':
    unittest.main()
