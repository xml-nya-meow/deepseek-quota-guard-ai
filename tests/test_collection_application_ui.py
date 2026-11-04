"""Returned synthetic fees are applied separately from browser authentication.

Real SQLite/workbooks live only in temporary folders. The browser double never
launches a browser or contacts the platform; recovery must remain local/read-only.
"""
from decimal import Decimal
import unittest
from unittest.mock import Mock, patch

import test_workflow as fixtures
from quota_guard.core import GuardError, Person, SnapshotApplyError


class CollectionApplicationUiTests(unittest.TestCase):
    tearDown = fixtures.WorkflowTests.tearDown
    pump = fixtures.WorkflowTests.pump
    valid_collection = fixtures.WorkflowTests.valid_collection
    empty_collection = fixtures.WorkflowTests.empty_collection

    def setUp(self):
        fixtures.WorkflowTests.setUp(self)
        # Positive quotas keep fixture reads independent of zero-quota actions.
        self.store.save_people([Person('测试甲', Decimal(2)), Person('测试乙', Decimal(80))])
        self.app.load_folder()
        self.emitted = []
        self.real_diagnostic = self.app._diagnostic

        def record(event, **fields):
            self.emitted.append((event, fields))
            self.real_diagnostic(event, **fields)

        self.app._diagnostic = record

    def read(self, source='sample-one', amount='1', **overrides):
        values = {'source_id': source,
                  'totals': {'test-key-a': Decimal(amount), 'test-key-b': Decimal(5)}}
        values.update(overrides)
        result = self.valid_collection(**values)
        browser = fixtures.OfflineBrowser({'collect': result})
        self.app.browser = browser
        self.app.collect()
        self.pump()
        return browser, result

    def fees(self):
        with self.store._connect() as db:
            snapshots = [tuple(row) for row in db.execute('SELECT * FROM snapshots ORDER BY rowid')]
            costs = [tuple(row) for row in db.execute('SELECT * FROM costs ORDER BY tracking_id')]
        return snapshots, costs

    def failure(self):
        return [fields for event, fields in self.emitted
                if event == 'collection_application_failed'][-1]

    def assert_ineligible(self):
        self.assertFalse(self.app.collection_has_fees)
        self.assertEqual(self.app.collection_source_id, '')
        self.assertIsNone(self.app.collection_context)
        self.assertIsNone(self.app.collection_observed_wall)
        self.assertIsNone(self.app.collection_observed_monotonic)
        self.assertFalse(self.app._collection_is_current())
        self.assertFalse(self.app.observing)
        self.assertFalse(self.app.automatic)
        self.assertIsNone(self.app.delete_permit)
        self.assertIsNone(self.app.pending_start)
        self.assertFalse(self.app.busy)
        self.assertIsNone(self.app.current_action)

    def assert_local_recovery(self, expected_fees, expected_events):
        browser_calls = list(self.app.browser.calls)
        # Reapplying a duplicate could reconcile new trigger events; neither
        # application entry point nor the mutating evaluator is a retry tool.
        with patch.object(self.app.store, 'apply_snapshot_result', side_effect=AssertionError('reapply')), \
                patch.object(self.app.store, 'apply_snapshot', side_effect=AssertionError('legacy reapply')), \
                patch.object(self.app.store, 'evaluate', side_effect=AssertionError('mutating evaluate')):
            self.app.retry_audit()
        self.assertFalse(self.app.audit_failed)
        self.assertEqual(self.app.audit_pending, [])
        self.assertEqual(self.fees(), expected_fees)
        self.assertEqual(self.store.events(), expected_events)
        self.assertEqual(self.app.browser.calls, browser_calls)
        self.assertTrue(self.app.session_ready)
        self.assert_ineligible()
        self.assertIn('重新读取费用', self.app.status_var.get())

    def test_no_overlap_preserves_session_old_fees_and_cancels_all_quota_authority(self):
        self.read()
        before = self.fees()
        events = self.store.events()
        self.app.observing = self.app.automatic = True
        permit = Mock()
        self.app.delete_permit = permit
        browser, _ = self.read(source='foreign-fees', totals={
            'synthetic-historical-one': Decimal(3), 'synthetic-historical-two': Decimal(4)})
        self.assertEqual(self.fees(), before)
        self.assertEqual(self.store.events(), events)
        self.assertTrue(self.app.session_ready)
        self.assertIs(self.app.browser, browser)
        permit.decide.assert_called_with(False)
        self.assert_ineligible()
        self.assertFalse(self.app.audit_failed)
        self.assertEqual(self.app.action_states['collect'], '费用与绑定不匹配')
        self.assertIn('本次费用快照未更新', self.app.status_var.get())
        failure = self.failure()
        self.assertEqual(failure['reason_code'], 'NO_BOUND_COST_MATCH')
        self.assertEqual(failure['persistence_state'], 'not_written')
        self.assertEqual(failure['matched_total_count'], 0)
        self.assertEqual(failure['bound_total_count'], 2)
        self.assertEqual(failure['fee_tracking_count'], 2)
        self.assertFalse(any(event == 'browser_action_failed' for event, _ in self.emitted))
        self.app.start()
        self.assertEqual([action for action, _ in browser.calls], ['collect'])
        self.mocks[1].assert_not_called()

    def test_cumulative_decrease_is_local_rejection_and_keeps_accepted_amount(self):
        self.read(amount='3')
        before = self.fees()
        self.read(source='lower-cost', amount='1')
        self.assertEqual(self.fees(), before)
        self.assertTrue(self.app.session_ready)
        self.assertEqual(self.failure()['reason_code'], 'CUMULATIVE_DECREASE')
        self.assert_ineligible()

    def test_old_source_replay_cannot_refresh_newer_positive_quota_evidence(self):
        self.read(source='S1', amount='1')
        self.read(source='S2', amount='3')
        self.assertTrue(self.app._collection_is_current())
        self.assertEqual(self.app.rows[0]['status'], 'triggered')
        before, events = self.fees(), self.store.events()
        self.read(source='S1', amount='1')
        self.assertEqual(self.store.current_snapshot_id(), 'S2')
        self.assertEqual(self.fees(), before)
        self.assertEqual(self.store.events(), events)
        self.assertTrue(self.app.session_ready)
        self.assertFalse(self.app.audit_failed)
        self.assert_ineligible()
        self.assertEqual(self.failure()['reason_code'], 'STALE_SNAPSHOT_REPLAY')
        self.assertFalse(self.failure()['observed_source_is_effective'])
        with self.assertRaises(GuardError):
            self.app._current_revoke_target(self.app.rows[0])
        self.app.start()
        self.assertFalse(any(event['kind'] == 'action' for event in self.store.events()))

    def test_current_duplicate_refreshes_observation_without_replacing_snapshot(self):
        self.read(source='current', amount='1')
        previous_clock = self.app.collection_observed_wall
        before, events = self.fees(), self.store.events()
        browser, result = self.read(source='current', amount='1')
        self.assertTrue(self.app._collection_is_current())
        self.assertTrue(self.app.collection_has_fees)
        self.assertEqual(self.app.collection_source_id, 'current')
        self.assertEqual(self.app.collection_observed_wall, result['collection_started_wall'])
        self.assertGreater(self.app.collection_observed_wall, previous_clock)
        self.assertEqual(self.fees(), before)
        self.assertEqual(self.store.events(), events)
        completed = [fields for event, fields in self.emitted
                     if event == 'collection_application_completed'][-1]
        self.assertEqual(completed['apply_outcome'], 'current_duplicate')
        operation = browser.calls[0][1]['operation_id']
        self.assertRegex(operation, r'^[0-9a-f]{32}$')
        self.assertEqual(completed['operation_id'], operation)

    def test_snapshot_change_outside_collection_invalidates_positive_evidence(self):
        self.read(source='observed', amount='1')
        self.assertTrue(self.app._collection_is_current())
        self.store.apply_snapshot({'test-key-a': Decimal(3), 'test-key-b': Decimal(5)},
                                  'different-source', self.start, self.start)
        self.assertFalse(self.app._collection_is_current())
        with self.assertRaises(GuardError):
            self.app._current_revoke_target(self.app.rows[0])

    def test_post_commit_export_failure_retries_local_work_without_reapply(self):
        original_export = self.app.store.export_logs

        def export_after_commit_fails():
            if self.app.store.current_snapshot_id() == 'export-pending':
                raise GuardError('synthetic export failure')
            return original_export()

        with patch.object(self.app.store, 'export_logs', side_effect=export_after_commit_fails):
            self.read(source='export-pending', amount='3')
        self.assertEqual(self.store.current_snapshot_id(), 'export-pending')
        self.assertTrue(self.app.audit_failed)
        self.assertTrue(self.app.session_ready)
        self.assert_ineligible()
        self.assertEqual(self.failure()['reason_code'], 'AUDIT_EXPORT_FAILED')
        self.assertEqual(self.failure()['persistence_state'], 'persisted')
        self.assertIn('费用已保存在统计库', self.app.audit_var.get())
        self.assertEqual(self.app.action_states['collect'], '已入账，本地恢复待完成')
        self.assert_local_recovery(self.fees(), self.store.events())

    def test_post_commit_row_refresh_failure_is_saved_and_recovered_readonly(self):
        original_refresh = self.app.refresh_rows

        def refresh(*, readonly=False):
            if readonly:
                raise OSError('synthetic row refresh failure')
            return original_refresh()

        with patch.object(self.app, 'refresh_rows', side_effect=refresh):
            self.read(source='refresh-pending', amount='3')
        self.assertEqual(self.store.current_snapshot_id(), 'refresh-pending')
        self.assertTrue(self.app.audit_failed)
        self.assertEqual(self.failure()['reason_code'], 'POST_COMMIT_REFRESH_FAILED')
        self.assertEqual(self.failure()['stage'], 'post_commit_refresh')
        self.assert_ineligible()
        self.assert_local_recovery(self.fees(), self.store.events())

    def test_post_commit_context_failure_never_publishes_partial_freshness(self):
        original_context = self.app._collection_context

        def context_after_commit_fails():
            if self.store.current_snapshot_id() == 'context-pending':
                raise GuardError('synthetic context failure')
            return original_context()

        with patch.object(self.app, '_collection_context', side_effect=context_after_commit_fails):
            self.read(source='context-pending', amount='3')
        self.assertEqual(self.store.current_snapshot_id(), 'context-pending')
        self.assertTrue(self.app.audit_failed)
        self.assertEqual(self.failure()['stage'], 'post_commit_context')
        self.assert_ineligible()
        self.assert_local_recovery(self.fees(), self.store.events())

    def test_report_refresh_failure_is_not_silently_called_complete(self):
        original_events = self.app.store.events

        def events(*args, **kwargs):
            if self.app.store.current_snapshot_id() == 'report-pending':
                raise GuardError('synthetic report read failure')
            return original_events(*args, **kwargs)

        with patch.object(self.app.store, 'events', side_effect=events):
            self.read(source='report-pending')
        self.assertTrue(self.app.audit_failed)
        self.assertEqual(self.failure()['reason_code'], 'POST_COMMIT_REFRESH_FAILED')
        self.assert_ineligible()
        self.assert_local_recovery(self.fees(), self.store.events())

    def test_unknown_commit_status_is_not_reported_as_not_written(self):
        error = SnapshotApplyError('synthetic uncertain transaction',
                                   reason_code='SNAPSHOT_PERSISTENCE_UNKNOWN',
                                   stage='snapshot_transaction', persistence_state='unknown')
        with patch.object(self.app.store, 'apply_snapshot_result', side_effect=error):
            self.read()
        self.assertFalse(self.app.session_ready)
        self.assertFalse(self.app.audit_failed)
        self.assertEqual(self.failure()['persistence_state'], 'unknown')
        self.assertEqual(self.app.action_states['collect'], '保存结果待核实')
        self.assertIn('不能认定未写入', self.app.status_var.get())
        self.assert_ineligible()

    def test_unclassified_apply_failure_releases_busy_and_keeps_commit_unknown(self):
        with patch.object(self.app.store, 'apply_snapshot_result', side_effect=RuntimeError('synthetic failure')):
            self.read()
        self.assertFalse(self.app.session_ready)
        self.assertEqual(self.failure()['reason_code'], 'SNAPSHOT_PERSISTENCE_UNKNOWN')
        self.assertEqual(self.failure()['persistence_state'], 'unknown')
        self.assert_ineligible()

    def test_true_worker_failure_still_invalidates_session_and_is_not_local_rejection(self):
        self.read()
        browser = fixtures.ControlledBrowser()
        self.app.browser = browser
        self.app.collect()
        browser.futures[0].set_exception(GuardError('synthetic authentication failure'))
        self.pump()
        self.assertFalse(self.app.session_ready)
        self.assert_ineligible()
        self.assertFalse(any(event == 'collection_application_failed' for event, _ in self.emitted))
        self.assertTrue(any(event == 'browser_action_failed' for event, _ in self.emitted))

    def test_explicit_authorization_rejection_clears_identity_only_freshness(self):
        self.read()
        self.read(totals=None, source_id='', reason_code='AUTH_REQUIRED', retryable=False,
                  message='synthetic authentication rejected')
        self.assertFalse(self.app.session_ready)
        self.assert_ineligible()

    def test_empty_fees_preserve_identity_only_zero_quota_rule(self):
        self.store.save_people([Person('测试甲', Decimal(0)), Person('测试乙', Decimal(80))])
        browser, _ = self.read(totals=None, source_id='', reason_code='NO_USAGE_DATA',
                               retryable=True, message='synthetic no usage')
        self.assertTrue(self.app.session_ready)
        self.assertTrue(self.app._collection_is_current())
        self.assertFalse(self.app.collection_has_fees)
        self.assertEqual(self.app.collection_source_id, '')
        self.assertIsNone(self.app.rows[0]['spent'])
        self.assertEqual(self.app.rows[0]['status'], 'triggered')
        self.assertEqual(self.app._current_revoke_target(self.app.rows[0])['amount'], Decimal(0))
        with self.assertRaises(GuardError):
            self.app._current_revoke_target(self.app.rows[1])
        self.assertEqual([action for action, _ in browser.calls], ['collect'])

    def test_application_diagnostics_contain_counts_not_payload_or_names(self):
        self.read(source='different-account-costs', totals={'synthetic-foreign-cost-id': Decimal(1)})
        fields = self.failure()
        self.assertNotIn('totals', fields)
        self.assertNotIn('keys', fields)
        self.assertNotIn('coverage_summary', fields)
        self.assertNotIn('observed_source_id', fields)
        self.assertNotIn('effective_snapshot_id', fields)
        rendered = repr(fields)
        self.assertNotIn('测试甲', rendered)
        self.assertNotIn('synthetic-foreign-cost-id', rendered)
        self.assertNotIn('different-account-costs', rendered)


if __name__ == '__main__':
    unittest.main()
