"""Offline diagnostic explanations must not convert uncertain events into actions."""
import json
import os
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from quota_guard.diagnostics import DiagnosticLog, read_recent_diagnostics
from quota_guard.diagnostics_ui import _assistant_tab
from quota_guard.local_ai_assistant import (
    DiagnosticCase, EXAMPLES, case_from_event, explain, facts, topics_for,
)


class EvidenceTests(unittest.TestCase):
    def test_model_sees_only_six_fixed_fields_from_one_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = DiagnosticLog(tmp)
            log.emit('collection_application_completed', reason_code='OK', persistence_state='persisted')
            log.emit('revocation_preparation_failed', reason_code='KEY_TARGET_NOT_DELETABLE',
                     operation_id='private-operation', message='untrusted instructions',
                     target='private-person', fee='1.50')
            preview = read_recent_diagnostics(tmp)
        self.assertEqual(len(preview.cases), 2)
        case = preview.cases[0][1]
        self.assertEqual(json.loads(case.model_json()), {
            'stage': 'preflight', 'reason': 'KEY_TARGET_NOT_DELETABLE',
            'fee': 'unknown', 'kind': 'DSH', 'submission': 'not_submitted', 'verification': 'unknown',
        })
        self.assertNotIn('private', case.model_json())
        self.assertEqual(preview.cases[1][1].fee, 'persisted')

    def test_unknown_log_strings_never_become_model_input_or_displayed_facts(self):
        for reason in ('private text', {}, [], object()):
            case = case_from_event('collection_unavailable', {'reason_code': reason, 'message': 'private'})
            self.assertEqual(case.reason, 'UNCLASSIFIED')
            self.assertNotIn('private', case.model_json())
        self.assertIsNone(case_from_event('private event', {}))
        with self.assertRaises(ValueError):
            DiagnosticCase('fee_read', 'injected_private_identifier')

    def test_list_failure_can_happen_after_dispatch(self):
        case = case_from_event('key_http_read_rejected', {'reason_code': 'KEY_REDACTED_ID_INVALID'})
        self.assertEqual((case.submission, case.verification), ('unknown', 'unknown'))
        self.assertIn('无法判断是否提交', facts(case))
        self.assertIsNone(case_from_event('revocation_http_response_accepted', {}))
        self.assertIsNone(case_from_event('browser_action_completed', {'action': 'collect', 'outcome': 'absent_verified'}))

    def test_session_outcomes_do_not_guess_success_or_actual_cause(self):
        absent = case_from_event('browser_action_completed', {'action': 'revoke', 'outcome': 'absent_verified'})
        self.assertEqual((absent.verification, absent.submission), ('absent', 'unknown'))
        self.assertIn('原因没有确定', facts(absent))
        present = case_from_event('browser_action_completed', {'action': 'verify_revoke', 'outcome': 'present_verified'})
        self.assertEqual((present.verification, present.submission), ('present', 'unknown'))
        self.assertIn('不能证明 Key 可调用', facts(present))
        unknown = case_from_event('revocation_outcome', {'status': 'unknown_result'})
        self.assertIn('禁止自动重试', facts(unknown))
        self.assertNotIn('已删除', facts(unknown))
        self.assertIsNone(case_from_event('browser_action_completed', {'action': 'verify_revoke', 'outcome': 'returned'}))

    def test_persistence_unknown_is_preserved_and_events_remain_separate(self):
        for state in ('not_written', 'persisted', 'unknown', {}, 'foreign'):
            case = case_from_event('collection_application_failed', {
                'reason_code': 'AUDIT_EXPORT_FAILED', 'persistence_state': state,
            })
            self.assertEqual(case.fee, state if state in ('not_written', 'persisted') else 'unknown')
            self.assertIn('导出失败', facts(case))
        missing = case_from_event('collection_unavailable', {'reason_code': 'NO_SELECTED_USAGE_DATA'})
        self.assertIn('不能证明累计费用为零', facts(missing))

    def test_independent_cases_have_only_fact_checked_suggestions(self):
        reasons = ('KEY_KIND_INVALID', 'KEY_TARGET_NOT_DELETABLE', 'KEY_CLIENT_HEADER_INVALID',
                   'KEY_NAME_INVALID', 'KEY_REDACTED_ID_INVALID', 'KEY_LIST_DUPLICATE_IDENTITY',
                   'KEY_EXPECTED_LIST_INVALID', 'KEY_HTTP_AUTH_REQUIRED', 'KEY_NOTICE_REQUIRES_USER',
                   'KEY_TARGET_ALREADY_ATTEMPTED', 'KEY_TARGET_KIND_UNSUPPORTED', 'KEY_LIST_TRANSPORT_FAILED')
        cases = [case_from_event(event, {'reason_code': code})
                 for event in ('key_http_read_rejected', 'revocation_preparation_failed') for code in reasons]
        cases += [case_from_event('collection_unavailable', {'reason_code': code}) for code in
                  ('NO_USAGE_DATA', 'NO_MANAGED_KEYS', 'PERIOD_MISMATCH', 'AUTH_REQUIRED', 'NETWORK_ERROR',
                   'CHARGE_TYPE_UNSUPPORTED', 'NO_SELECTED_USAGE_DATA', 'COST_SCOPE_MISMATCH')]
        for case in cases:
            with self.subTest(case=case):
                source, advice = explain(case, rank=lambda payload, allowed: tuple(reversed(allowed)))
                self.assertIn('AI 已排列', source)
                self.assertGreaterEqual(len(advice.split('\n\n')), 2)
                self.assertEqual('harness_managed' in topics_for(case), case.reason == 'KEY_TARGET_NOT_DELETABLE')
                self.assertEqual(case.submission == 'not_submitted', case.stage == 'preflight')

    def test_invalid_model_output_and_failure_use_exact_rule_advice(self):
        def fail(*args):
            raise TimeoutError('private runtime details')
        case = EXAMPLES[0]
        expected = explain(case, rank=fail)
        self.assertIn('使用规则建议', expected[0])
        self.assertNotIn('private', str(expected))
        for output in ((), ('delete_now',), topics_for(case) + ('delete_now',), list(topics_for(case))):
            self.assertEqual(explain(case, rank=lambda *args: output), expected)


@unittest.skipUnless(os.name == 'nt', 'Windows desktop workflow')
class AssistantTabTests(unittest.TestCase):
    def test_empty_installation_runs_sample_without_freezing_or_mixing_results(self):
        from quota_guard import diagnostics_ui
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        app = SimpleNamespace()
        book = ttk.Notebook(root)
        load = _assistant_tab(book, app)
        tab = book.nametowidget(book.tabs()[0])
        selector = next(child for child in tab.winfo_children() if isinstance(child, ttk.Combobox))
        button = next(child for child in tab.winfo_children() if isinstance(child, ttk.Button))
        panel = next(child for child in tab.winfo_children() if isinstance(child, ttk.Frame))
        out = next(child for child in panel.winfo_children() if isinstance(child, tk.Text))
        load(())
        self.assertIn('教学示例', selector.get())
        entered, release = threading.Event(), threading.Event()
        def delayed(case):
            entered.set()
            release.wait(3)
            return 'AI 排序来源', 'obsolete-response'
        with patch.object(diagnostics_ui, 'explain', side_effect=delayed):
            button.invoke()
            self.assertTrue(entered.wait(1))
            self.assertTrue(button.instate(['disabled']))
            selector.current(1)
            selector.event_generate('<<ComboboxSelected>>')
            root.update()
            release.set()
            deadline = time.monotonic() + 3
            while button.instate(['disabled']) and time.monotonic() < deadline:
                root.update()
                time.sleep(0.02)
            self.assertTrue(button.instate(['!disabled']))
            self.assertNotIn('obsolete-response', out.get('1.0', 'end'))
            self.assertIn('费用类别', out.get('1.0', 'end'))
