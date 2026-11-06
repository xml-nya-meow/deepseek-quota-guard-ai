import json
from pathlib import Path
import tempfile
import unittest

from build import validate_bundle
from tools.verify_portable_candidate import EXPECTED_CASES, check_result, verify


class PortableVerificationTests(unittest.TestCase):
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
