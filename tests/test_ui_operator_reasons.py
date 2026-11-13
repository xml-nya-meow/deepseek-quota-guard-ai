"""Offline AI failures retain their own category without fee/identity claims."""
from decimal import Decimal
import json
import unittest
from unittest.mock import patch

import test_workflow as fixtures
import test_browser_process as process_fixtures
import test_lifetime_collection as lifetime_fixtures
from quota_guard.browser_process import _worker_failure
from quota_guard.core import GuardError, Person
from quota_guard.local_ai_assistant import case_from_event, facts, topics_for
from quota_guard.operator_runtime import OperatorError
from quota_guard.operator_status import AI_COLLECTION_LABELS, AI_COLLECTION_REASONS, OperatorCollectionError
from quota_guard.revocation_permit import DeletePermit
from quota_guard.ui import COLLECTION_RESULT_CODES, FEE_STATE_LABELS


class OperatorReasonContractTests(unittest.TestCase):
    def test_every_controlled_operator_error_has_a_collection_label(self):
        from quota_guard.operator_runtime import _MESSAGES
        from quota_guard.web_operator import _REASONS
        self.assertEqual(AI_COLLECTION_REASONS, set(_MESSAGES) | set(_REASONS))
        self.assertLessEqual(AI_COLLECTION_REASONS, COLLECTION_RESULT_CODES)
        for code in AI_COLLECTION_REASONS:
            self.assertEqual(FEE_STATE_LABELS[code], AI_COLLECTION_LABELS[code])
            self.assertNotIn('费用结构', FEE_STATE_LABELS[code])
            self.assertNotIn('费用类别', FEE_STATE_LABELS[code])

    def test_diagnostic_cases_keep_only_controlled_reason_and_uncertainty(self):
        for code in AI_COLLECTION_REASONS:
            with self.subTest(code=code):
                case = case_from_event('collection_unavailable', {
                    'reason_code': code, 'message': 'private page content',
                    'amount': '12345', 'name': 'private person'})
                self.assertEqual(case.reason, code)
                self.assertEqual((case.fee, case.submission, case.verification),
                                 ('unknown', 'unknown', 'unknown'))
                self.assertEqual(len(json.loads(case.model_json())), 6)
                self.assertNotIn('private', case.model_json())
                self.assertIn(AI_COLLECTION_LABELS[code], facts(case))
                self.assertNotIn('账户不匹配', facts(case))
                self.assertNotIn('费用类别', facts(case))
                self.assertNotIn('login', topics_for(case))

    def test_unknown_ai_prefixed_text_is_not_admitted_as_a_reason(self):
        code = 'AI_UNTRUSTED_PRIVATE_REASON'
        self.assertNotIn(code, COLLECTION_RESULT_CODES)
        case = case_from_event('collection_unavailable', {'reason_code': code})
        self.assertEqual(case.reason, 'UNCLASSIFIED')
        self.assertNotIn(code, case.model_json())

    def test_typed_collection_error_rejects_non_literal_reason(self):
        for code in (None, [], {}, 'AI_UNTRUSTED_PRIVATE_REASON'):
            with self.subTest(code=code), self.assertRaises(ValueError):
                OperatorCollectionError(code)


class OperatorFailureBoundaryTests(unittest.TestCase):
    pending_service = process_fixtures.ExpiredDeliveryTests.pending_service
    deliver_failure = process_fixtures.ExpiredDeliveryTests.deliver_failure

    def test_worker_preserves_start_failure_only_as_fixed_collection_error(self):
        for code in ('AI_START_TIMEOUT', 'AI_ASSETS_INVALID', 'AI_RUNTIME_CLOSED'):
            with self.subTest(code=code):
                error = OperatorError(code)
                error.args = ('private runtime text',)
                failure = _worker_failure(error, 'collect', None)
                self.assertEqual(failure, {'ok': False, 'error_type': 'OperatorCollectionError',
                    'reason_code': code, 'message': str(OperatorCollectionError(code))})
                self.assertNotIn('private', str(failure))

    def test_worker_rejects_corrupt_reason_or_wrong_action_without_raw_text(self):
        for action, permit, code in (('collect', None, []), ('collect', None, 'AI_PRIVATE'),
                ('collect', DeletePermit(), 'AI_TIMEOUT'), ('revoke', DeletePermit(), 'AI_TIMEOUT'),
                ('login', None, 'AI_START_FAILED')):
            with self.subTest(action=action, code=code):
                error = OperatorError('AI_TIMEOUT')
                error.reason_code = code
                error.args = ('private runtime text',)
                failure = _worker_failure(error, action, permit)
                self.assertEqual(failure['error_type'], 'GuardError')
                self.assertNotIn('reason_code', failure)
                self.assertNotIn('private', str(failure))

    def test_untyped_error_cannot_claim_ai_failure_type(self):
        error = GuardError('AI_START_TIMEOUT')
        error.reason_code = 'AI_START_TIMEOUT'
        failure = _worker_failure(error, 'collect', None)
        self.assertEqual(failure['error_type'], 'GuardError')
        self.assertNotIn('reason_code', failure)

    def test_parent_retains_allowlisted_reason_and_ignores_worker_message(self):
        for code in AI_COLLECTION_REASONS:
            with self.subTest(code=code):
                service, future, queued = self.pending_service()
                self.deliver_failure(service, error_type='OperatorCollectionError', reason_code=code)
                self.assertIsInstance(future.exception(), OperatorCollectionError)
                self.assertEqual(future.exception().reason_code, code)
                self.assertEqual(str(future.exception()), str(OperatorCollectionError(code)))
                self.assertNotIn('untrusted', str(future.exception()))
                self.assertFalse(queued.done())

    def test_parent_rejects_unknown_reason_action_and_permission_states(self):
        cases = [('collect', {}, reason) for reason in (None, [], 'AI_PRIVATE')]
        cases += [('collect', {field: value}, 'AI_TIMEOUT') for field, value in (
            ('permit', DeletePermit()), ('permit_ready', True), ('decision_sent', True),
            ('decision_allowed', True), ('permit_ready', None))]
        cases += [('revoke', {}, 'AI_TIMEOUT'), ('inspect_revoke', {}, 'AI_TIMEOUT')]
        for action, changes, reason in cases:
            with self.subTest(action=action, changes=changes, reason=reason):
                service, future, _ = self.pending_service(action)
                for field, value in changes.items():
                    setattr(service._active, field, value)
                self.deliver_failure(service, error_type='OperatorCollectionError', reason_code=reason)
                self.assertIsInstance(future.exception(), GuardError)
                self.assertNotIsInstance(future.exception(), OperatorCollectionError)
                self.assertNotIn('untrusted', str(future.exception()))

    def test_actual_service_initial_reader_failure_reaches_fixed_worker_reason(self):
        # Both collection routes fail before a usable evidence result exists.
        # Reuse only synthetic setup; no browser, model or platform is started.
        for basis in ('period', 'key_lifetime'):
            with self.subTest(basis=basis):
                fixture = lifetime_fixtures.LifetimeServiceTests()
                fixture.setUp()
                with patch('quota_guard.platform_keys.ConsoleKeyManager', return_value=fixture.manager), \
                        patch.object(fixture.service, '_create_cost_reader',
                                     side_effect=OperatorError('AI_START_TIMEOUT')):
                    with self.assertRaises(OperatorError) as caught:
                        fixture.service._collect('2026-11-10', managed_names=['Alpha'], fee_basis=basis)
                failure = _worker_failure(caught.exception, 'collect', None)
                self.assertEqual(failure['reason_code'], 'AI_START_TIMEOUT')
                self.assertNotIn('keys', failure)
                self.assertNotIn('totals', failure)
                fixture.reader.read.assert_not_called()
                fixture.manager.read_creation_times.assert_not_called()


class OperatorCollectionPresentationTests(unittest.TestCase):
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

    def stored_fees(self):
        with self.store._connect() as db:
            return ([tuple(row) for row in db.execute('SELECT * FROM snapshots ORDER BY rowid')],
                    [tuple(row) for row in db.execute('SELECT * FROM costs ORDER BY tracking_id')])

    def test_ai_failure_keeps_reason_through_trace_label_and_summary(self):
        for code in ('AI_OPERATOR_SCOPE_REJECTED', 'AI_TIMEOUT', 'AI_START_TIMEOUT',
                     'AI_RUNTIME_CLOSED', 'AI_ASSETS_INVALID', 'AI_OPERATOR_ACTION_LIMIT'):
            with self.subTest(code=code), patch.object(self.app, '_diagnostic') as diagnostic:
                self.read(self.empty_collection(reason_code=code, retryable=False))
                self.assertEqual(self.app.action_states['collect'], AI_COLLECTION_LABELS[code])
                self.assertIn(AI_COLLECTION_LABELS[code], self.app.collection_summary_var.get())
                self.assertEqual(self.app.collection_reason_code, code)
                received = [call.kwargs for call in diagnostic.call_args_list
                            if call.args and call.args[0] == 'collection_result_received']
                unavailable = [call.kwargs for call in diagnostic.call_args_list
                               if call.args and call.args[0] == 'collection_unavailable']
                self.assertEqual(received[-1]['reason_code'], code)
                self.assertEqual(unavailable[-1]['reason_code'], code)
                self.assertTrue(self.app.session_ready)
                self.assertFalse(self.app.collection_has_fees)
                self.assertIsNone(self.app.delete_permit)

    def test_failed_ai_read_preserves_prior_amount_but_cannot_reuse_it_for_quota(self):
        self.read(self.valid_collection(totals={'test-key-a': Decimal(3),
                                                'test-key-b': Decimal(5)}))
        before = self.stored_fees()
        self.read(self.empty_collection(reason_code='AI_TIMEOUT', retryable=False))
        self.assertEqual(self.stored_fees(), before)
        self.assertEqual(next(row for row in self.store.evaluate()
                              if row['name'] == '测试甲')['spent'], Decimal(3))
        self.assertFalse(self.app.collection_has_fees)
        self.app.start(); self.pump(); self.pump()
        self.assertFalse(self.app.observing)
        self.assertFalse(self.app.automatic)
        self.assertIsNone(self.app.delete_permit)
        self.assertEqual([action for action, _ in self.app.browser.calls], ['collect'])
        self.assertEqual(self.stored_fees(), before)

    def test_unknown_empty_result_does_not_accuse_fee_schema_or_log_raw_reason(self):
        marker = 'AI_UNTRUSTED_PRIVATE_REASON'
        with patch.object(self.app, '_diagnostic') as diagnostic:
            self.read(self.empty_collection(reason_code=marker, retryable=False))
        self.assertEqual(self.app.action_states['collect'], '读取未完成')
        self.assertEqual(self.app.collection_reason_code, 'UNKNOWN_COLLECTION_RESULT')
        self.assertNotIn(marker, repr(diagnostic.call_args_list))

    def test_actual_fee_contract_errors_keep_their_distinct_labels(self):
        for code, label in (('CHARGE_TYPE_UNSUPPORTED', '费用类别待适配'),
                            ('SCHEMA_UNSUPPORTED', '费用结构待适配')):
            with self.subTest(code=code):
                self.read(self.empty_collection(reason_code=code, retryable=False))
                self.assertEqual(self.app.action_states['collect'], label)
                self.assertEqual(self.app.collection_reason_code, code)

    def test_initial_ai_failure_invalidates_identity_and_preserves_recorded_amounts(self):
        self.read(self.valid_collection(totals={'test-key-a': Decimal(3), 'test-key-b': Decimal(5)}))
        before = self.stored_fees()
        self.app.browser = fixtures.ControlledBrowser()
        with patch.object(self.app, '_diagnostic') as diagnostic:
            self.app.collect()
            self.app.browser.futures[-1].set_exception(OperatorCollectionError('AI_START_TIMEOUT'))
            self.pump()
        self.assertEqual(self.app.action_states['collect'], AI_COLLECTION_LABELS['AI_START_TIMEOUT'])
        self.assertEqual(self.app.collection_reason_code, 'AI_START_TIMEOUT')
        self.assertIn('身份与费用仍需重新核验', self.app.collection_summary_var.get())
        self.assertNotIn('费用结构', self.app.collection_summary_var.get())
        self.assertFalse(self.app.session_ready)
        self.assertFalse(self.app.collection_has_fees)
        self.assertEqual(self.app.last_keys, [])
        self.assertEqual(self.stored_fees(), before)
        unavailable = [call.kwargs for call in diagnostic.call_args_list
                       if call.args and call.args[0] == 'collection_unavailable']
        self.assertEqual(unavailable[-1]['reason_code'], 'AI_START_TIMEOUT')
        case = case_from_event('collection_unavailable', unavailable[-1])
        self.assertEqual(case.reason, 'AI_START_TIMEOUT')
        self.assertEqual(case.fee, 'unknown')
        self.app.start(); self.pump()
        self.assertFalse(self.app.observing)
        self.assertFalse(self.app.automatic)
        self.assertIsNone(self.app.delete_permit)
        self.assertEqual([action for action, _ in self.app.browser.calls], ['collect'])
        self.assertEqual(self.stored_fees(), before)


if __name__ == '__main__':
    unittest.main()
