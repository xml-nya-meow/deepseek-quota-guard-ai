"""Regression for portable recovery. This test uses scripted model choices."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from quota_guard import operator_acceptance
from quota_guard.operator_acceptance import _offline_worker
from quota_guard.operator_runtime import OperatorRuntime
from tests.test_web_operator import ScriptedPublicOperator


def scripted_worker(connection, *, config):
    choices = ScriptedPublicOperator()
    with patch.object(OperatorRuntime, 'start'), patch.object(OperatorRuntime, 'decide', choices.decide):
        _offline_worker(connection, config=config)


@unittest.skipUnless(os.name == 'nt', 'uses Windows desktop and browser job ownership')
class PortableRecoveryTests(unittest.TestCase):
    def test_restart_roundtrip_uses_original_operation_and_one_backend_request(self):
        with patch.object(operator_acceptance, 'run_operator_acceptance') as runner:
            operator_acceptance.run_portable_operator_acceptance('operator-acceptance-fixture')
        cases = runner.call_args.kwargs['cases']
        self.assertEqual([case['name'] for case in cases],
                         ['read-only-fee-review', 'positive-quota-crossing', 'response-drop-restart'])
        with sync_playwright() as playwright:
            chromium = Path(playwright.chromium.executable_path)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.object(operator_acceptance, '_offline_worker', scripted_worker):
                result = operator_acceptance._one_case(root, cases[-1], root, chromium)
            self.assertTrue(result['passed'], result.get('error', result))
            self.assertTrue(result['cleanup_passed'])
            self.assertEqual(result['actual_post_count'], 1)
            self.assertEqual(result['actual_cost_queries'], 2)
            self.assertEqual(result['open_obligations'], [])
            self.assertEqual([event['status'] for event in result['history']],
                             ['pending', 'requested', 'unknown_result', 'absent_verified'])
            evidence = result['recovery']
            self.assertEqual(evidence['kind'], 'desktop_window_reopen')
            self.assertTrue(evidence['first_window_closed'])
            for key in ('before_operation_id', 'reopened_operation_id', 'verified_operation_id'):
                self.assertEqual(evidence[key], result['unknown_operation'])
            for key in ('post_count_before_close', 'post_count_after_reopen', 'post_count_after_verification'):
                self.assertEqual(evidence[key], 1)
            self.assertGreaterEqual(evidence['fresh_list_receipts'], 1)
            truth = json.loads((root / cases[-1]['name'] / 'backend-truth.json').read_text(encoding='utf-8'))
            self.assertEqual(truth['keys'], [k for k in truth['initial_keys']
                                             if k['tracking_id'] != evidence['target_id']])
