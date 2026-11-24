import json
from pathlib import Path
import tempfile
import unittest

from build import validate_bundle
from tools.verify_portable_candidate import EXPECTED_CASES, check_operator_result, check_result, verify


class PortableVerificationTests(unittest.TestCase):
    def test_operator_evidence_must_come_from_same_exe_and_settle_original_operation_once(self):
        recovery = dict(kind='desktop_window_reopen', before_operation_id='original',
                        reopened_operation_id='original', verified_operation_id='original',
                        first_window_closed=True, fresh_list_receipts=1, target_id='target', control_id='control',
                        post_count_before_close=1, post_count_after_reopen=1, post_count_after_verification=1)
        cases = [dict(name=name, passed=True, cleanup_passed=True, actual_post_count=posts,
                      actual_cost_queries=posts + 1, permit_consumptions=posts)
                 for name, posts in [('read-only-fee-review', 0), ('positive-quota-crossing', 1),
                                     ('response-drop-restart', 1)]]
        cases[-1]['recovery'] = recovery
        report = dict(passed=True, version='test', exe_sha256='fixture-hash', real_requests=0,
                      real_platform='not_performed', cases=cases)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            def write():
                (folder / 'summary.json').write_text(json.dumps(report), encoding='utf-8')
            write()
            self.assertTrue(check_operator_result(folder, 'test', 'fixture-hash')['passed'])
            with self.assertRaises(ValueError):
                check_operator_result(folder, 'test', 'different')
            for key, value in [('verified_operation_id', 'replacement'), ('post_count_after_reopen', 2),
                               ('post_count_after_reopen', True), ('fresh_list_receipts', 0),
                               ('control_id', 'target'), ('first_window_closed', False)]:
                with self.subTest(key=key):
                    cases[-1]['recovery'] = {**recovery, key: value}
                    write()
                    with self.assertRaises(ValueError):
                        check_operator_result(folder, 'test', 'fixture-hash')

    def test_workflow_requires_all_cases_matching_version_and_synthetic_transport(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            valid = {'passed': True, 'version': 'fixture', 'transport': 'synthetic',
                     'real_requests': 0, 'send_boundary_checks': 2, 'cases': sorted(EXPECTED_CASES)}
            changes = ({'passed': False}, {'cases': []}, {'version': 'old'},
                       {'transport': 'live'}, {'real_requests': 1}, {'send_boundary_checks': 0})
            for change in changes:
                with self.subTest(change=change):
                    (folder / 'acceptance.json').write_text(json.dumps({**valid, **change}))
                    with self.assertRaises(ValueError):
                        check_result('offline-acceptance', folder, 'fixture')
            (folder / 'acceptance.json').write_text(json.dumps(valid))
            self.assertEqual(check_result('offline-acceptance', folder, 'fixture'), valid)

    def test_ui_marker_alone_is_not_success_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            (folder / 'OFFLINE-FIXTURE.txt').write_text('fixture')
            with self.assertRaises(FileNotFoundError):
                check_result('smoke', folder, 'fixture')
            (folder / 'ui-smoke.json').write_text(json.dumps({'passed': False, 'callback_errors': 1}))
            with self.assertRaises(ValueError):
                check_result('smoke', folder, 'fixture')

    def test_evidence_inside_bundle_is_rejected_before_creating_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            bundle = Path(temporary)
            target = bundle / 'evidence'
            with self.assertRaises(ValueError):
                verify(bundle, target)
            self.assertFalse(target.exists())

    def test_bundle_rejects_ledger_and_acceptance_residue(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            for name in ('状态.db', 'responsibility.sqlite3', 'OFFLINE-FIXTURE.txt', 'acceptance.json'):
                with self.subTest(name=name):
                    file = folder / name
                    file.write_text('fixture')
                    try:
                        with self.assertRaises(RuntimeError):
                            validate_bundle(folder)
                    finally:
                        file.unlink()


if __name__ == '__main__':
    unittest.main()
