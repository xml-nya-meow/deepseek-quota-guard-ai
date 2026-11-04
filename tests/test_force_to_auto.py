"""Explicit automatic start can refresh after force deletion without relogin."""
from decimal import Decimal
import unittest
from unittest.mock import patch

import test_workflow as fixtures
from quota_guard.core import GuardError, Person
from quota_guard.browser_service import key_fingerprint


class ForceToAutoTests(unittest.TestCase):
    setUp = fixtures.WorkflowTests.setUp
    tearDown = fixtures.WorkflowTests.tearDown
    pump = fixtures.WorkflowTests.pump
    valid_collection = fixtures.WorkflowTests.valid_collection
    empty_collection = fixtures.WorkflowTests.empty_collection

    def force_first(self, *, second_amount=80, result=None):
        self.store.save_people([Person('测试甲', Decimal(0)), Person('测试乙', Decimal(second_amount))])
        self.keys.append({'name': '表外合成对照', 'tracking_id': 'test-key-control'})
        self.app.load_folder()
        browser = fixtures.ControlledBrowser(identity_result={
            'keys': self.keys, 'account_id': key_fingerprint(self.keys)})
        self.app.browser = browser
        self.app.tree.selection_set('0')
        self.app.revoke_selected_once()
        self.pump()
        self.assertEqual([action for action, _ in browser.calls], ['read_keys', 'revoke'])
        permit = browser.calls[-1][1]['permit']
        permit.ready.set()
        self.pump()
        self.assertTrue(permit.allowed)
        browser.futures[0].set_result({'absent': True} if result is None else result)
        self.pump()
        self.assertFalse(self.app.busy)
        self.assertIsNone(self.app.single_revoke_id)
        self.assertIsNone(self.app.delete_permit)
        self.mocks[2].reset_mock()
        return browser

    def fresh_survivors(self, *, empty=False, **overrides):
        make = self.empty_collection if empty else self.valid_collection
        values = {'keys': self.keys[1:], 'source_id': 'post-force-fixture',
                  'account_id': self.store.load().account_id}
        if not empty:
            values['totals'] = {'test-key-b': Decimal(5)}
        values.update(overrides)
        return make(**values)

    def begin_start_refresh(self, browser):
        self.assertTrue(self.app.start_refresh_required)
        self.assertFalse(self.app._collection_is_current())
        self.assertTrue(self.app.session_ready)
        self.app.start()
        self.assertTrue(self.app.busy)
        self.assertFalse(self.app.automatic)
        self.assertFalse(self.app.observing)
        self.assertIsNotNone(self.app.pending_start)
        self.assertEqual(browser.calls[-1][0], 'collect')
        self.mocks[2].assert_not_called()
        return browser.futures[-1]

    def assert_stopped(self):
        self.assertFalse(self.app.automatic)
        self.assertFalse(self.app.observing)
        self.assertIsNone(self.app.pending_start)

    def test_successful_force_then_start_refreshes_and_manages_without_login(self):
        browser = self.force_first()
        self.assertEqual(self.store.latest_action('test-key-a'), 'absent_verified')
        future = self.begin_start_refresh(browser)
        self.assertFalse(self.app.buttons['stop'].instate(['disabled']))
        self.app.start()  # Repeated presses cannot enqueue another read.
        self.assertEqual([action for action, _ in browser.calls].count('collect'), 1)
        future.set_result(self.fresh_survivors())
        self.pump()
        self.assertTrue(self.app.observing)
        self.assertTrue(self.app.automatic)
        self.assertIsNone(self.app.pending_start)
        self.assertFalse(self.app.start_refresh_required)
        self.assertTrue(self.app._collection_is_current())
        self.mocks[2].assert_called_once()
        self.assertEqual([action for action, _ in browser.calls], ['read_keys', 'revoke', 'collect'])
        self.assertEqual(self.app.rows[0]['status'], 'retired')
        self.assertEqual(self.app.rows[1]['spent'], Decimal(5))

    def test_zero_quota_can_continue_after_verified_empty_fee_refresh(self):
        browser = self.force_first(second_amount=0)
        future = self.begin_start_refresh(browser)
        future.set_result(self.fresh_survivors(empty=True))
        self.pump()
        self.assertTrue(self.app.automatic)
        self.assertFalse(self.app.collection_has_fees)
        self.assertEqual(browser.calls[-1][0], 'revoke')
        self.assertEqual(browser.calls[-1][1]['tracking_id'], 'test-key-b')
        permit = browser.calls[-1][1]['permit']
        permit.ready.set()
        self.pump()
        self.assertTrue(permit.allowed)
        evidence = self.store.latest_action_record('test-key-b')
        self.assertEqual(evidence['status'], 'requested')
        self.assertNotEqual(evidence.get('intent'), 'manual_force')
        browser.futures[-1].set_result({'absent': True})
        self.pump()
        self.assertEqual(self.store.latest_action('test-key-b'), 'absent_verified')
        self.assertFalse(any(action == 'login' for action, _ in browser.calls))

    def test_force_success_alone_does_not_read_or_start_automatic_management(self):
        browser = self.force_first()
        self.pump()
        self.assert_stopped()
        self.assertEqual([action for action, _ in browser.calls], ['read_keys', 'revoke'])
        self.assertTrue(self.app.start_refresh_required)

    def test_stop_during_refresh_preserves_read_but_cancels_automatic_start(self):
        browser = self.force_first()
        future = self.begin_start_refresh(browser)
        self.app.buttons['stop'].invoke()
        self.assertIsNone(self.app.pending_start)
        future.set_result(self.fresh_survivors())
        self.pump()
        self.assert_stopped()
        self.assertTrue(self.app._collection_is_current())
        self.mocks[2].assert_not_called()

    def test_login_during_refresh_cancels_start_and_discards_old_collection(self):
        browser = self.force_first()
        future = self.begin_start_refresh(browser)
        self.app.login()
        future.set_result(self.fresh_survivors())
        self.pump()
        self.assert_stopped()
        self.assertEqual(browser.calls[-1][0], 'login')
        self.assertFalse(self.app.session_ready)
        self.mocks[2].assert_not_called()
        browser.futures[-1].set_result({})
        self.pump()

    def test_workbook_change_during_refresh_cannot_start_or_apply_old_result(self):
        browser = self.force_first()
        future = self.begin_start_refresh(browser)
        self.store.save_people([Person('测试甲', Decimal(0)), Person('测试乙', Decimal(81))])
        changed_bytes = self.store.workbook_path.read_bytes()
        future.set_result(self.fresh_survivors())
        self.pump()
        self.assert_stopped()
        self.assertEqual(self.store.workbook_path.read_bytes(), changed_bytes)
        self.assertIsNone(self.app.collection_context)
        self.mocks[2].assert_not_called()

    def test_refresh_failure_releases_pending_state_without_revoke_or_confirmation(self):
        browser = self.force_first()
        future = self.begin_start_refresh(browser)
        future.set_exception(GuardError('synthetic read failure'))
        self.pump()
        self.assert_stopped()
        self.assertFalse(self.app.busy)
        self.assertFalse(self.app.session_ready)
        self.assertEqual([action for action, _ in browser.calls], ['read_keys', 'revoke', 'collect'])
        self.mocks[2].assert_not_called()

    def test_synchronous_collect_failure_clears_pending_and_allows_fresh_start(self):
        browser = self.force_first()
        with patch.object(self.app, 'collect', side_effect=GuardError('synthetic parameter read failure')):
            self.app.start()
        self.assert_stopped()
        self.assertFalse(self.app.busy)
        self.assertTrue(self.app.start_refresh_required)
        self.assertEqual([action for action, _ in browser.calls], ['read_keys', 'revoke'])
        self.mocks[2].assert_not_called()
        future = self.begin_start_refresh(browser)
        future.set_result(self.fresh_survivors())
        self.pump()
        self.assertTrue(self.app.automatic)
        self.assertTrue(self.app.observing)
        self.assertIsNone(self.app.pending_start)
        self.mocks[2].assert_called_once()

    def test_authentication_rejection_does_not_resume_automatic_start(self):
        browser = self.force_first()
        future = self.begin_start_refresh(browser)
        future.set_result(self.fresh_survivors(empty=True, reason_code='AUTH_REQUIRED', retryable=False))
        self.pump()
        self.assert_stopped()
        self.assertFalse(self.app.session_ready)
        self.assertIsNone(self.app.collection_context)
        self.mocks[2].assert_not_called()

    def test_empty_fee_result_expiring_before_resume_clears_pending_without_start(self):
        browser = self.force_first()
        future = self.begin_start_refresh(browser)
        # The result is fresh when accepted, but its stored evidence is no
        # longer current by continuation time (for example, a date rollover).
        with patch.object(self.app, '_collection_is_current', return_value=False):
            future.set_result(self.fresh_survivors(empty=True))
            self.pump()
        self.assert_stopped()
        self.assertFalse(self.app.busy)
        self.assertTrue(self.app.session_ready)
        self.assertFalse(self.app.collection_has_fees)
        self.assertEqual([action for action, _ in browser.calls], ['read_keys', 'revoke', 'collect'])
        self.mocks[2].assert_not_called()

    def test_unknown_force_result_blocks_new_read_and_auto_retry(self):
        browser = self.force_first(result={'absent': False})
        self.assertEqual(self.store.latest_action('test-key-a'), 'unknown_result')
        self.app.start()
        self.pump()
        self.assert_stopped()
        self.assertEqual([action for action, _ in browser.calls], ['read_keys', 'revoke'])
        self.mocks[2].assert_not_called()

    def test_declining_start_after_refresh_keeps_fresh_read_without_enabling_management(self):
        browser = self.force_first()
        future = self.begin_start_refresh(browser)
        self.mocks[2].return_value = False
        future.set_result(self.fresh_survivors())
        self.pump()
        self.assert_stopped()
        self.assertTrue(self.app._collection_is_current())
        self.assertFalse(self.app.start_refresh_required)
        self.mocks[2].assert_called_once()

    def test_manual_refresh_clears_requirement_and_start_does_not_read_twice(self):
        browser = self.force_first()
        self.app.collect()
        browser.futures[-1].set_result(self.fresh_survivors())
        self.pump()
        self.assertFalse(self.app.start_refresh_required)
        self.app.start()
        self.pump()
        self.assertTrue(self.app.automatic)
        self.assertEqual([action for action, _ in browser.calls].count('collect'), 1)

    def test_interrupt_during_refresh_cancels_pending_start(self):
        browser = self.force_first()
        self.begin_start_refresh(browser)
        self.app.interrupt_operation()
        self.pump()
        self.pump()
        self.assert_stopped()
        self.assertFalse(self.app.busy)
        self.mocks[2].assert_not_called()

    def test_close_during_refresh_cancels_pending_start(self):
        browser = self.force_first()
        self.begin_start_refresh(browser)
        with patch.object(self.root, 'destroy') as destroy:
            self.app.close()
            self.pump()
            self.pump()
            self.assert_stopped()
            self.assertTrue(self.app.closing)
            destroy.assert_called_once()
        self.mocks[2].assert_not_called()

    def test_cancelled_force_confirmation_still_requires_fresh_fees_before_start(self):
        self.app.load_folder()
        browser = fixtures.ControlledBrowser(identity_result={
            'keys': self.keys, 'account_id': key_fingerprint(self.keys)})
        self.app.browser = browser
        self.mocks[2].return_value = False
        self.app.tree.selection_set('0')
        self.app.revoke_selected_once()
        self.pump()
        self.assertEqual([action for action, _ in browser.calls], ['read_keys'])
        self.assertTrue(self.app.start_refresh_required)
        self.mocks[2].reset_mock()
        self.app.start()
        self.assertEqual(browser.calls[-1][0], 'collect')
        self.assertFalse(self.app.automatic)
        self.app.stop(quiet=True)
        browser.futures[-1].set_result(self.valid_collection())
        self.pump()
        self.assert_stopped()


if __name__ == '__main__':
    unittest.main()
