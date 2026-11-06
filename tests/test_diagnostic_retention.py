from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from quota_guard.diagnostics import (
    DiagnosticLog, PREVIEW_MAX_BYTES, _process_is_alive, read_recent_diagnostics,
)


class DiagnosticRetentionTests(unittest.TestCase):
    def make_log(self, folder, session='111111111111', *, pid=None, days=0, suffix='000000'):
        name = f'diagnostic-20260101T000000{suffix}Z-{session}'
        if pid is not None:
            name += f'-p{pid}'
        path = Path(folder) / (name + '.jsonl')
        path.write_text('{}\n', encoding='utf-8')
        stamp = time.time() - days * 86400
        os.utime(path, (stamp, stamp))
        return path

    def test_age_cleanup_is_specific_and_nonrecursive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = self.make_log(root, days=15)
            recent = self.make_log(root, session='222222222222', days=13)
            protected = [root / '状态.db', root / '运行日志-2020.jsonl',
                         root / 'diagnostic-important.jsonl']
            for path in protected:
                path.write_text('preserve', encoding='utf-8')
            nested = root / 'nested'
            nested.mkdir()
            nested_log = self.make_log(nested, days=100)
            log = DiagnosticLog(root)
            self.assertTrue(log.emit('startup'))
            self.assertFalse(old.exists())
            self.assertTrue(recent.exists())
            self.assertTrue(nested_log.exists())
            for path in protected:
                self.assertEqual(path.read_text(), 'preserve')

    def test_cleanup_runs_on_later_event_without_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = DiagnosticLog(tmp)
            log.emit('first')
            path = log.path
            old = self.make_log(tmp, days=15)
            log.emit('before_interval')
            self.assertTrue(old.exists())
            log.last_cleanup -= 61
            log.emit('after_interval')
            self.assertFalse(old.exists())
            self.assertEqual(log.path, path)

    def test_rotation_respects_file_size_even_with_large_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = DiagnosticLog(tmp, max_bytes=1024, keep_files=3)
            for index in range(15):
                log.emit('event', message='中' * 400, count=index)
            log.emit('中' * 500, **{f'field_{n}': 'x' for n in range(100)})
            files = list(Path(tmp).glob('diagnostic-*.jsonl'))
            self.assertLessEqual(len(files), 3)
            self.assertTrue(all(path.stat().st_size <= 1024 for path in files))
            last = json.loads(log.path.read_text(encoding='utf-8').splitlines()[-1])
            self.assertEqual(last['data']['reason_code'], 'DIAGNOSTIC_RECORD_TOO_LARGE')

    def test_rotation_has_unique_names_when_wall_clock_does_not_advance(self):
        frozen = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            log = DiagnosticLog(tmp, max_bytes=1024, keep_files=30)
            with patch('quota_guard.diagnostics.datetime') as clock:
                clock.now.return_value = frozen
                for index in range(20):
                    self.assertTrue(log.emit('phase', message='x' * 400, count=index))
            files = sorted(Path(tmp).glob('diagnostic-*.jsonl'))
            self.assertGreater(len(files), 1)
            self.assertTrue(all(path.stat().st_size <= 1024 for path in files))
            rows = [json.loads(line) for path in files
                    for line in path.read_text(encoding='utf-8').splitlines()]
            self.assertEqual([row['data']['count'] for row in rows], list(range(20)))

    def test_clock_rollback_and_existing_name_never_append_to_old_file(self):
        frozen = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            log = DiagnosticLog(tmp, max_bytes=1024, keep_files=10)
            stamp = frozen.strftime('%Y%m%dT%H%M%S%fZ')
            existing = Path(tmp) / f'diagnostic-{stamp}-{log.session}-p{os.getpid()}.jsonl'
            existing.write_bytes(b'preserve-existing-file')
            with patch('quota_guard.diagnostics.datetime') as clock:
                clock.now.return_value = frozen
                self.assertTrue(log.emit('first', message='x' * 400))
                first_path = log.path
                clock.now.return_value = datetime(2025, 1, 1, tzinfo=timezone.utc)
                self.assertTrue(log.emit('second', message='x' * 400))
            self.assertNotEqual(existing, first_path)
            self.assertGreater(log.path.name, first_path.name)
            self.assertEqual(existing.read_bytes(), b'preserve-existing-file')
            self.assertEqual(json.loads(first_path.read_text(encoding='utf-8'))['event'], 'first')
            self.assertEqual(json.loads(log.path.read_text(encoding='utf-8'))['event'], 'second')

    def test_active_foreign_writer_latest_is_preserved_but_rotation_is_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = self.make_log(tmp, pid=999999, days=20)
            current = self.make_log(tmp, pid=999999, days=19, suffix='000001')
            other = self.make_log(tmp, session='222222222222', days=1)
            with patch('quota_guard.diagnostics._process_is_alive', return_value=True):
                DiagnosticLog(tmp, keep_files=2).emit('cleanup')
            self.assertFalse(old.exists())
            self.assertTrue(current.exists())
            self.assertFalse(other.exists())

    def test_many_active_writers_temporarily_exceed_cap_then_are_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            active = [self.make_log(tmp, session=f'{n:012x}', pid=999000 + n, days=15)
                      for n in range(1, 4)]
            log = DiagnosticLog(tmp, keep_files=2)
            with patch('quota_guard.diagnostics._process_is_alive', return_value=True):
                log.emit('active')
            self.assertTrue(all(path.exists() for path in active))
            log.last_cleanup -= 61
            with patch('quota_guard.diagnostics._process_is_alive', return_value=False):
                log.emit('exited')
            self.assertTrue(all(not path.exists() for path in active))

    def test_cross_process_current_file_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = [sys.executable, '-c',
                       'import sys; from quota_guard.diagnostics import DiagnosticLog; '
                       'log=DiagnosticLog(sys.argv[1]); log.emit("child"); '
                       'print(log.path.name, flush=True); sys.stdin.readline()', tmp]
            child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True,
                                     cwd=Path(__file__).resolve().parents[1])
            try:
                name = child.stdout.readline().strip()
                self.assertTrue(name.startswith('diagnostic-'), child.stderr.read() if not name else '')
                child_path = Path(tmp) / name
                self.assertTrue(_process_is_alive(child.pid))
                aged = time.time() - 20 * 86400
                os.utime(child_path, (aged, aged))
                log = DiagnosticLog(tmp, keep_files=2)
                log.emit('parent')
                self.assertTrue(child_path.exists())
                child.communicate('\n', timeout=5)
                self.assertFalse(_process_is_alive(child.pid))
                log.last_cleanup -= 61
                log.emit('after_child_exit')
                self.assertFalse(child_path.exists())
            finally:
                if child.poll() is None:
                    child.kill()
                child.communicate(timeout=5)


class DiagnosticPreviewTests(unittest.TestCase):
    def write_rows(self, root, rows, *, suffix='000000'):
        path = Path(root) / f'diagnostic-20260101T000000{suffix}Z-abcdef123456.jsonl'
        path.write_text(''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows), encoding='utf-8')
        return path

    def row(self, index, **data):
        return {'time': datetime.fromtimestamp(1767225600 + index, timezone.utc).isoformat(),
                'event': f'event_{index}', 'session': 'abcdef123456', 'elapsed_ms': index,
                'data': data}

    def test_preview_returns_only_latest_300_in_chronological_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_rows(tmp, [self.row(n, count=n) for n in range(350)])
            preview = read_recent_diagnostics(tmp, max_records=100000, max_bytes=10000000)
            self.assertEqual(preview.record_count, 300)
            self.assertTrue(preview.truncated)
            self.assertNotIn('event_49 ', preview.text)
            self.assertIn('event_50 ', preview.text)
            self.assertLess(preview.text.index('event_50 '), preview.text.index('event_349 '))
            self.assertLessEqual(len(preview.text.encode('utf-8')), PREVIEW_MAX_BYTES)

    def test_identical_timestamps_keep_latest_appended_rows_under_tail_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = [self.row(n, count=n) for n in range(3)]
            for row in rows:
                row['time'] = rows[0]['time']
            self.write_rows(tmp, rows)
            preview = read_recent_diagnostics(tmp, max_records=2)
            self.assertEqual(preview.record_count, 2)
            self.assertTrue(preview.truncated)
            self.assertNotIn('event_0 ', preview.text)
            self.assertLess(preview.text.index('event_1 '), preview.text.index('event_2 '))

    def test_preview_read_and_output_are_byte_bounded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_rows(tmp, [self.row(n, message='中' * 200) for n in range(1000)])
            original = Path.open
            calls = []

            class TrackedReader:
                def __init__(self, stream):
                    self.stream = stream
                def __enter__(self):
                    return self
                def __exit__(self, *args):
                    self.stream.close()
                def seek(self, *args):
                    return self.stream.seek(*args)
                def read(self, count):
                    calls.append(count)
                    return self.stream.read(count)

            def tracked_open(candidate, *args, **kwargs):
                stream = original(candidate, *args, **kwargs)
                return TrackedReader(stream) if candidate == path else stream

            with patch.object(Path, 'open', tracked_open):
                preview = read_recent_diagnostics(tmp, max_bytes=4096)
            self.assertEqual(sum(calls), 4096)
            self.assertTrue(preview.truncated)
            self.assertIn('event_999 ', preview.text)
            self.assertLessEqual(len(preview.text.encode('utf-8')), 4096)

    def test_preview_sanitizes_existing_files_including_top_level_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            row = self.row(1, password='private-password', name='private-name',
                           message='sk-synthetic-credential Bearer private-bearer',
                           body={'text': 'private-response'},
                           tracking_id={'fingerprint': '123456abcdef'})
            row['event'] = 'event sk-synthetic-event'
            row['session'] = 'private-session'
            self.write_rows(tmp, [row])
            preview = read_recent_diagnostics(tmp)
            self.assertEqual(preview.record_count, 1)
            for value in ('private-password', 'private-name', 'private-bearer', 'private-response',
                          'private-session', 'sk-synthetic'):
                self.assertNotIn(value, preview.text)
            self.assertIn('123456abcdef', preview.text)

    def test_malformed_partial_and_wrong_shape_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.write_rows(tmp, [self.row(1)])
            with path.open('ab') as stream:
                stream.write(b'{not-json}\n[]\n{"time":"secret", "data":{}}\n{"unfinished"')
            preview = read_recent_diagnostics(tmp)
            self.assertEqual(preview.record_count, 1)
            self.assertEqual(preview.skipped_lines, 4)
            self.assertNotIn('not-json', preview.text)
            self.assertNotIn('secret', preview.text)

    def test_preview_sorts_record_times_across_writers(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.write_rows(tmp, [self.row(10), self.row(30)])
            self.write_rows(tmp, [self.row(20), self.row(40)], suffix='000001')
            preview = read_recent_diagnostics(tmp, max_records=3)
            self.assertEqual(preview.record_count, 3)
            self.assertNotIn('event_10 ', preview.text)
            self.assertLess(preview.text.index('event_20 '), preview.text.index('event_30 '))
            self.assertLess(preview.text.index('event_30 '), preview.text.index('event_40 '))

    def test_preview_does_not_read_unknown_files_or_nested_folders(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'diagnostic-unknown.jsonl').write_text('private', encoding='utf-8')
            (root / 'nested').mkdir()
            self.write_rows(root / 'nested', [self.row(1)])
            preview = read_recent_diagnostics(root)
            self.assertEqual(preview.text, '')
            self.assertEqual(preview.record_count, 0)

    def test_unavailable_folder_is_explicit_and_missing_folder_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'not-folder'
            path.write_text('preserve', encoding='utf-8')
            self.assertTrue(read_recent_diagnostics(path).unavailable)
            missing = read_recent_diagnostics(Path(tmp) / 'missing')
            self.assertFalse(missing.unavailable)
            self.assertEqual(missing.text, '')
            with patch('quota_guard.diagnostics.os.scandir', side_effect=PermissionError):
                self.assertTrue(read_recent_diagnostics(tmp).unavailable)


if __name__ == '__main__':
    unittest.main()
