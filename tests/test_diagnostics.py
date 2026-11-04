import json
from pathlib import Path
import tempfile
import threading
import unittest

from quota_guard.diagnostics import DiagnosticLog, safe_fields


class DiagnosticTests(unittest.TestCase):
    def test_header_metadata_is_closed_and_numbers_are_capped(self):
        fields = {'header_name': 'x-device-model', 'rejection_kind': 'control_char',
                  'value_type': 'str', 'value_length': 1000000, 'source_kind': 'key_list',
                  'matched_request_count': 1000000, 'bearer_format_passed': True}
        self.assertEqual(safe_fields(fields), {**fields, 'value_length': 1025, 'matched_request_count': 10000})
        with tempfile.TemporaryDirectory() as tmp:
            log = DiagnosticLog(tmp)
            self.assertTrue(log.emit('key_http_client_header_rejected', **fields))
            self.assertEqual(json.loads(log.path.read_text(encoding='utf-8'))['data'], safe_fields(fields))

    def test_header_metadata_cannot_carry_arbitrary_values_or_object_text(self):
        class PrivateObject:
            def __str__(self):
                raise AssertionError('No object text may be read')
            def __repr__(self):
                raise AssertionError('No object representation may be read')
        keys = ('header_name', 'rejection_kind', 'value_type', 'source_kind',
                'value_length', 'matched_request_count', 'bearer_format_passed')
        for value in ('private-arbitrary-value', PrivateObject(), {'private': 'value'}, [], -1, None):
            with self.subTest(value_type=type(value).__name__):
                self.assertEqual(safe_fields(dict.fromkeys(keys, value)), {})
        self.assertEqual(safe_fields({'value_length': True, 'matched_request_count': 1.5,
                                      'bearer_format_passed': 1}), {})

    def test_sensitive_data_and_raw_results_never_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = DiagnosticLog(tmp)
            log.emit('action', action='collect', key_count=3,
                     name='Synthetic Person', tracking_id='synthetic-tracking',
                     password='fake-private-password', token='fake-private-token',
                     result={'opaque': 'hidden-account-data', 'count': 9},
                     message='sk-synthetic-credential https://example.test/?token=private')
            text = log.path.read_text(encoding='utf-8')
            for secret in ('Synthetic Person', 'synthetic-tracking', 'fake-private',
                           'hidden-account-data', 'sk-synthetic', '?token=private'):
                self.assertNotIn(secret, text)
            data = json.loads(text)['data']
            self.assertEqual(data['key_count'], 3)
            self.assertEqual(data['result']['count'], 2)

    def test_concurrent_logs_are_complete_json_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = DiagnosticLog(tmp)
            threads = [threading.Thread(target=lambda: [log.emit('phase', count=n) for n in range(20)]) for _ in range(5)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            rows = [json.loads(line) for line in log.path.read_text(encoding='utf-8').splitlines()]
            self.assertEqual(len(rows), 100)

    def test_rotation_only_removes_own_diagnostic_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'keep-ledger.jsonl').write_text('ledger', encoding='utf-8')
            log = DiagnosticLog(root, max_bytes=1024, keep_files=3)
            for index in range(70):
                log.emit('phase', message='x' * 400, count=index)
            self.assertLessEqual(len(list(root.glob('diagnostic-*.jsonl'))), 3)
            self.assertEqual((root / 'keep-ledger.jsonl').read_text(), 'ledger')

    def test_unwritable_path_does_not_break_operation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'file'
            path.write_text('preserve', encoding='utf-8')
            log = DiagnosticLog(path)
            self.assertFalse(log.emit('phase', count=1))
            self.assertTrue(log.disabled)
            self.assertEqual(path.read_text(), 'preserve')

    def test_unknown_payload_keys_are_not_exposed(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = DiagnosticLog(tmp)
            log.emit('phase', payload={'private-person-as-key': {'value': 'hidden'}, 'cookie': 'private-cookie'})
            data = log.path.read_text(encoding='utf-8')
            self.assertNotIn('private-person', data)
            self.assertNotIn('private-cookie', data)


if __name__ == '__main__':
    unittest.main()
