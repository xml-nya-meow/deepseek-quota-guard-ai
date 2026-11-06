"""Real Tk/ledger callbacks, synthetic browser results; no platform requests."""
from contextlib import closing
from decimal import Decimal
import sqlite3
import unittest
from unittest.mock import patch

import test_workflow as fixtures
from quota_guard.core import Person
from quota_guard.forecast_shadow import DATABASE, record_collection


class ForecastShadowWorkflowTests(unittest.TestCase):
    setUp = fixtures.WorkflowTests.setUp
    tearDown = fixtures.WorkflowTests.tearDown
    pump = fixtures.WorkflowTests.pump
    valid_collection = fixtures.WorkflowTests.valid_collection

    def configure_positive_people(self):
        self.store.save_people([Person('测试甲', Decimal('1')), Person('测试乙', Decimal('1'))])
        self.app.load_folder()

    def read_with(self, browser, **changes):
        self.app.browser = browser
        self.app.collect()
        if isinstance(browser, fixtures.ControlledBrowser):
            browser.futures[-1].set_result(self.valid_collection(**changes))
        self.pump()
        self.assertTrue(self.app.collection_has_fees)
        self.assertEqual(self.app.action_states['collect'], '成功')

    def shadow_rows(self):
        with closing(sqlite3.connect(self.folder / DATABASE)) as db:
            return list(db.execute('SELECT status,coverage_end,cumulative FROM observations ORDER BY id'))

    def test_successful_read_writes_shadow_without_mutating_authoritative_ledger(self):
        self.configure_positive_people()
        comparisons = []

        def observe_write(*args, **kwargs):
            before = (self.store.read_evaluation(), self.store.events(limit=10000))
            result = record_collection(*args, **kwargs)
            after = (self.store.read_evaluation(), self.store.events(limit=10000))
            comparisons.append((before, after, kwargs))
            return result

        with patch('quota_guard.forecast_shadow.record_collection', side_effect=observe_write):
            self.read_with(fixtures.ControlledBrowser(),
                           totals={'test-key-a': Decimal('.1'), 'test-key-b': Decimal('.2')})
        self.assertEqual(len(comparisons), 1)
        before, after, arguments = comparisons[0]
        self.assertEqual(before, after)
        self.assertNotIn('data_as_of', arguments)
        self.assertNotIn('interval_seconds', arguments)
        self.assertEqual(arguments['source_id'], self.store.current_snapshot_id())
        self.assertEqual({row[0] for row in self.shadow_rows()}, {'data_time_unknown'})
        self.assertTrue(all(row[1] is None for row in self.shadow_rows()))
        self.assertFalse(self.store.unresolved_actions())
        self.assertFalse(self.app.automatic)

    def test_shadow_failure_during_automatic_poll_does_not_stop_or_reject_fees(self):
        self.configure_positive_people()
        browser = fixtures.ControlledBrowser()
        self.read_with(browser, totals={'test-key-a': Decimal('.1'), 'test-key-b': Decimal('.2')})
        self.app.start()
        self.pump()
        self.assertTrue(self.app.automatic)
        with (patch('quota_guard.forecast_shadow.record_collection', side_effect=OSError('synthetic failure')),
              patch.object(self.app, '_diagnostic') as diagnostic):
            self.app.collect()
            browser.futures[-1].set_result(self.valid_collection(
                totals={'test-key-a': Decimal('.2'), 'test-key-b': Decimal('.3')},
                source_id='next-synthetic-snapshot'))
            self.pump()
        diagnostic.assert_any_call('forecast_shadow_unavailable')
        self.assertTrue(self.app.automatic)
        self.assertTrue(self.app.observing)
        self.assertTrue(self.app._collection_is_current())
        self.assertEqual(self.app.action_states['collect'], '成功')
        self.assertEqual(self.store.read_evaluation()[0]['spent'], Decimal('.2'))
        self.assertFalse(self.store.unresolved_actions())
        self.assertEqual([action for action, _ in browser.calls], ['collect', 'collect'])

    def test_nonapplicable_rows_and_recovery_duty_do_not_poison_managed_shadow(self):
        self.store.save_people([Person('测试甲', Decimal('1')), Person('测试乙', Decimal('-1')),
                                Person('', Decimal('0'))], allow_unassigned=True)
        self.app.load_folder()
        result = self.valid_collection(totals={'test-key-a': Decimal('.1')},
                                       requested_tracking_ids=['test-key-a'])
        browser = fixtures.OfflineBrowser({'collect': result})
        self.read_with(browser)
        self.assertEqual(len(self.shadow_rows()), 1)
        # The real UI appends this shape for unresolved targets removed from the
        # workbook. It is recovery duty, not an eligible forecast observation.
        self.app.rows.append(dict(name='旧操作', tracking_id='old-synthetic-target', amount=None,
                                  spent=None, share=Decimal('107'), status='recovery_pending'))
        self.app._record_forecast_shadow(result)
        self.assertEqual(len(self.shadow_rows()), 1)
        self.assertIn('已更新 1 项', self.app.forecast_summary_var.get())
        self.assertEqual(browser.calls[0][1]['managed_names'], ['测试甲'])

    def test_untrusted_shadow_summary_cannot_choose_target_or_grant_permit(self):
        self.configure_positive_people()
        browser = fixtures.ControlledBrowser()
        summary = dict(count=999, estimated=999, statuses={'estimated': 999},
                       selected_target='test-key-a', eta_at=0, eligible_ids=['test-key-a'])
        with patch('quota_guard.forecast_shadow.record_collection', return_value=summary):
            self.read_with(browser, totals={'test-key-a': Decimal('.1'), 'test-key-b': Decimal('1.2')})
        self.assertIsNone(self.app.delete_permit)
        self.assertFalse(self.store.unresolved_actions())
        self.app.start()
        self.pump()
        self.assertEqual(browser.calls[-1][0], 'revoke')
        args = browser.calls[-1][1]
        self.assertEqual(args['tracking_id'], 'test-key-b')
        permit = args['permit']
        self.assertFalse(permit.allowed)
        self.assertEqual(self.store.latest_action('test-key-b'), 'pending')
        self.assertIsNone(self.store.latest_action('test-key-a'))
        permit.set_prepared_inventory('test-key-b', ['test-key-a', 'test-key-b'])
        permit.ready.set()
        self.pump()
        self.assertTrue(permit.allowed)
        self.assertEqual(self.store.latest_action('test-key-b'), 'requested')
        self.assertIsNone(self.store.latest_action('test-key-a'))
        # Resolve only the offline future; no network or external deletion.
        browser.futures[-1].set_result({'absent': True})
        self.pump()
        self.assertEqual(self.store.latest_action('test-key-b'), 'absent_verified')
        self.assertEqual(self.store.read_evaluation()[0]['status'], 'ok')


if __name__ == '__main__':
    unittest.main()
