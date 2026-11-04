"""Real GUI/ledger and scheduled callbacks, synthetic fees and browser only.

These tests do not prove live billing latency or actual platform deletion.
"""
from decimal import Decimal
import unittest

import test_workflow as fixtures
from quota_guard.core import Person


class PositiveQuotaPollTests(unittest.TestCase):
    setUp = fixtures.WorkflowTests.setUp
    tearDown = fixtures.WorkflowTests.tearDown
    pump = fixtures.WorkflowTests.pump
    valid_collection = fixtures.WorkflowTests.valid_collection
    empty_collection = fixtures.WorkflowTests.empty_collection

    def begin_below_limit(self):
        self.store.save_people([Person('测试甲', Decimal('0.2')), Person('测试乙', Decimal(-1))])
        self.app.load_folder()
        self.browser = fixtures.ControlledBrowser()
        self.app.browser = self.browser
        self.app.collect()
        self.assertEqual(self.browser.calls[-1][1]['managed_names'], ['测试甲'])
        self.browser.futures[-1].set_result(self.valid_collection(
            totals={'test-key-a': Decimal('0.19')}, requested_tracking_ids=['test-key-a']))
        self.pump()
        self.app.start()
        self.pump()
        self.assertTrue(self.app.automatic)
        self.assertIsNotNone(self.app.timer)
        self.assertEqual([a for a, _ in self.browser.calls], ['collect'])
        self.assertEqual(self.app.rows[1]['status'], 'skipped')

    def fire_scheduled_poll(self):
        # Fire the actual Tk callback without waiting ten seconds. Native
        # cancellation leaves its Python command alive until callit runs it.
        timer = self.app.timer
        self.assertIsNotNone(timer)
        command = self.root.tk.call('after', 'info', timer)[0]
        self.root.tk.call('after', 'cancel', timer)
        self.root.tk.call(command)
        self.pump()
        self.assertTrue(self.app.busy)
        self.assertIsNone(self.app.timer)
        self.assertEqual(self.browser.calls[-1][0], 'collect')
        return self.browser.futures[-1]

    def test_second_cycle_crosses_positive_limit_and_authorizes_once_after_durable_record(self):
        self.begin_below_limit()
        future = self.fire_scheduled_poll()
        self.assertIsNone(self.app.delete_permit)
        future.set_result(self.valid_collection(
            totals={'test-key-a': Decimal('0.20001')}, source_id='synthetic-crossing',
            requested_tracking_ids=['test-key-a']))
        self.pump()
        self.assertEqual([a for a, _ in self.browser.calls], ['collect', 'collect', 'revoke'])
        action, args = self.browser.calls[-1]
        self.assertEqual(args['tracking_id'], 'test-key-a')
        permit = args['permit']
        self.assertFalse(permit.allowed)
        self.assertEqual(self.store.latest_action('test-key-a'), 'pending')
        permit.ready.set()
        self.pump()
        self.assertTrue(permit.allowed)
        self.assertEqual(self.store.latest_action('test-key-a'), 'requested')
        evidence = self.store.latest_action_record('test-key-a')
        self.assertNotEqual(evidence.get('intent'), 'manual_force')
        self.browser.futures[-1].set_result({'absent': True})
        self.pump()
        self.assertEqual(self.store.latest_action('test-key-a'), 'absent_verified')
        self.assertEqual(self.app.rows[0]['status'], 'retired')
        self.assertEqual(self.app.rows[1]['status'], 'skipped')
        self.mocks[2].assert_called_once()  # Only the user's initial start.
        self.assertEqual([a for a, _ in self.browser.calls].count('revoke'), 1)
        self.app.stop(quiet=True)
        self.pump()
        self.assertIsNone(self.app.timer)
        self.assertFalse(self.app.automatic)

    def test_second_cycle_without_fees_does_not_reuse_old_amount_or_revoke_positive_quota(self):
        self.begin_below_limit()
        future = self.fire_scheduled_poll()
        future.set_result(self.empty_collection(requested_tracking_ids=['test-key-a']))
        self.pump()
        self.assertFalse(self.app.collection_has_fees)
        self.assertEqual(self.app.rows[0]['spent'], Decimal('0.19'))
        self.assertEqual([a for a, _ in self.browser.calls], ['collect', 'collect'])
        self.assertIsNone(self.app.delete_permit)
        self.assertIsNotNone(self.app.timer)
        self.assertTrue(self.app.automatic)

    def test_stop_during_second_read_can_apply_read_only_fees_but_cannot_delete(self):
        self.begin_below_limit()
        future = self.fire_scheduled_poll()
        self.app.stop(quiet=True)
        future.set_result(self.valid_collection(
            totals={'test-key-a': Decimal('0.20001')}, source_id='synthetic-after-stop',
            requested_tracking_ids=['test-key-a']))
        self.pump()
        self.assertFalse(self.app.automatic)
        self.assertFalse(self.app.observing)
        self.assertIsNone(self.app.timer)
        self.assertEqual(self.app.rows[0]['spent'], Decimal('0.20001'))
        self.assertEqual([a for a, _ in self.browser.calls], ['collect', 'collect'])
        self.assertIsNone(self.app.delete_permit)
