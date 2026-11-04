"""Synthetic recovery evidence; no platform login, credentials or model calls."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from quota_guard.core import GuardError
from quota_guard.profile_recovery import (
    MARKER, WindowsLifetime, job_name, ownership, read_marker, recover_profiles,
    witness_main, write_marker,
)
from test_browser_process import make_directory_link, remove_directory_link


class ProfileRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / 'application-browser-only'
        self.root.mkdir()
        self.probe = Mock()
        self.probe.exited.return_value = True

    def session(self, name='a' * 32, *, settled=True):
        directory = self.root / name
        directory.mkdir()
        (directory / 'synthetic-profile').mkdir()
        (directory / 'synthetic-profile' / 'cookie-fixture.txt').write_text('synthetic', encoding='utf-8')
        records = [{'pid': pid, 'created': pid * 100} for pid in (111, 222, 333)]
        record = ownership(directory, *records)
        write_marker(directory, record)
        if settled:
            write_marker(directory, {**record, 'state': 'settled'}, previous=record)
        return directory, record

    def test_only_proven_owned_session_is_removed(self):
        directory, _ = self.session()
        legacy = self.root / ('b' * 32)
        legacy.mkdir()
        (legacy / 'keep.txt').write_text('old unmarked profile', encoding='utf-8')
        neighbor = self.root / 'unrelated-directory'
        neighbor.mkdir()
        summary = recover_profiles(self.root, probe=self.probe)
        self.assertEqual(summary, {'removed': 1, 'retained': 1})
        self.assertFalse(directory.exists())
        self.assertTrue((legacy / 'keep.txt').exists())
        self.assertTrue(neighbor.exists())
        self.assertEqual(self.probe.exited.call_count, 3)

    def test_missing_job_name_or_dead_worker_never_replaces_exit_receipt(self):
        directory, _ = self.session(settled=False)
        self.assertEqual(recover_profiles(self.root, probe=self.probe)['removed'], 0)
        self.assertTrue(directory.exists())
        self.probe.exited.assert_not_called()

    def test_each_live_or_unverifiable_owner_preserves_the_entire_directory(self):
        directory, _ = self.session()
        for retained_pid in (111, 222, 333):
            with self.subTest(pid=retained_pid):
                self.probe.exited.side_effect = lambda record: record['pid'] != retained_pid
                self.assertEqual(recover_profiles(self.root, probe=self.probe)['removed'], 0)
                self.assertTrue((directory / 'synthetic-profile' / 'cookie-fixture.txt').exists())

    def test_identity_mismatch_corrupt_marker_and_incomplete_staging_are_retained(self):
        directory, _ = self.session()
        marker = json.loads((directory / MARKER).read_text(encoding='utf-8'))
        marker['directory_identity'] = [0, 0]
        (directory / MARKER).write_text(json.dumps(marker), encoding='utf-8')
        corrupt, _ = self.session('b' * 32)
        (corrupt / MARKER).write_text('{', encoding='utf-8')
        incomplete = self.root / ('c' * 32)
        incomplete.mkdir()
        (incomplete / (MARKER + '.new')).write_text('{}', encoding='utf-8')
        self.assertEqual(recover_profiles(self.root, probe=self.probe), {'removed': 0, 'retained': 3})
        self.assertTrue(all(path.exists() for path in (directory, corrupt, incomplete)))

    def test_existing_recovery_owner_prevents_a_second_scan_from_deleting(self):
        directory, _ = self.session()
        with patch('quota_guard.profile_recovery._recovery_lock', side_effect=OSError('locked')):
            self.assertEqual(recover_profiles(self.root, probe=self.probe)['removed'], 0)
        self.assertTrue(directory.exists())

    def test_locked_profile_keeps_exit_receipt_for_a_later_retry(self):
        directory, _ = self.session()
        with patch('quota_guard.profile_recovery.shutil.rmtree', side_effect=PermissionError('locked')):
            self.assertEqual(recover_profiles(self.root, probe=self.probe)['removed'], 0)
        self.assertEqual(read_marker(directory)['state'], 'settled')
        self.assertEqual(recover_profiles(self.root, probe=self.probe)['removed'], 1)
        self.assertFalse(directory.exists())

    def test_reparse_descendant_preserves_external_and_owned_files(self):
        directory, _ = self.session()
        external = Path(self.temporary.name) / 'external'
        external.mkdir()
        (external / 'keep.txt').write_text('must survive', encoding='utf-8')
        link = directory / 'external-link'
        make_directory_link(link, external)
        try:
            self.assertEqual(recover_profiles(self.root, probe=self.probe)['removed'], 0)
            self.assertEqual((external / 'keep.txt').read_text(encoding='utf-8'), 'must survive')
            self.assertTrue((directory / MARKER).exists())
        finally:
            remove_directory_link(link)

    def witness_fixture(self, *, settle_error=None):
        directory, record = self.session(settled=False)
        probe = Mock()
        probe.process.return_value = 101
        probe.creation.return_value = record['parent']['created']
        probe.api.OpenJobObjectW.return_value = 202
        probe.api.WaitForSingleObject.side_effect = [258, 258, 0]
        probe.record.return_value = record['witness']
        connection = Mock()
        connection.poll.return_value = True
        connection.recv.return_value = {'kind': 'arm', 'record': record}
        attempts = []

        def settle(_handle):
            self.assertEqual(read_marker(directory)['state'], 'armed')
            attempts.append(True)
            if settle_error and len(attempts) == 1:
                raise settle_error

        probe.settle_job.side_effect = settle
        return directory, record, probe, connection

    def test_parent_death_receipt_is_published_only_after_job_is_empty(self):
        directory, record, probe, connection = self.witness_fixture()
        with patch('quota_guard.profile_recovery.WindowsLifetime', return_value=probe), \
                patch('quota_guard.profile_recovery.TargetLockReferences') as references:
            witness_main(connection, record['parent'], job_name(directory.name), str(directory))
        references.return_value.close.assert_called_once()
        probe.settle_job.assert_called_once_with(202)
        self.assertEqual(read_marker(directory)['state'], 'settled')
        self.assertEqual([call.args[0]['kind'] for call in connection.send.call_args_list],
                         ['ready', 'armed'])
        connection.close.assert_called_once()

    def test_witness_job_query_failure_keeps_references_until_a_successful_retry(self):
        directory, record, probe, connection = self.witness_fixture(settle_error=OSError('query failed'))
        with patch('quota_guard.profile_recovery.WindowsLifetime', return_value=probe), \
                patch('quota_guard.profile_recovery.TargetLockReferences') as references, \
                patch('quota_guard.profile_recovery.time.sleep') as delay:
            def still_retained(_seconds):
                self.assertEqual(read_marker(directory)['state'], 'armed')
                references.return_value.close.assert_not_called()
            delay.side_effect = still_retained
            witness_main(connection, record['parent'], job_name(directory.name), str(directory))
        self.assertEqual(probe.settle_job.call_count, 2)
        self.assertEqual(read_marker(directory)['state'], 'settled')
        references.return_value.close.assert_called_once()

    def test_witness_parent_pid_reuse_never_adopts_another_process(self):
        directory, record, probe, connection = self.witness_fixture()
        probe.creation.return_value = record['parent']['created'] + 1
        with patch('quota_guard.profile_recovery.WindowsLifetime', return_value=probe), \
                patch('quota_guard.profile_recovery.TargetLockReferences'):
            witness_main(connection, record['parent'], job_name(directory.name), str(directory))
        probe.api.OpenJobObjectW.assert_not_called()
        probe.settle_job.assert_not_called()
        self.assertEqual(read_marker(directory)['state'], 'armed')


class WindowsExitProofTests(unittest.TestCase):
    def probe(self):
        probe = object.__new__(WindowsLifetime)
        probe.api, probe.ctypes, probe.creation = Mock(), Mock(), Mock()
        return probe

    def test_reused_pid_and_unknown_access_are_not_exit_proof(self):
        probe = self.probe()
        probe.api.OpenProcess.return_value = 123
        probe.creation.return_value = 200
        self.assertFalse(probe.exited({'pid': 111, 'created': 100}))
        probe.api.WaitForSingleObject.assert_not_called()
        probe.api.OpenProcess.return_value = None
        probe.ctypes.get_last_error.return_value = 5
        self.assertFalse(probe.exited({'pid': 111, 'created': 100}))

    def test_matching_signaled_process_and_absent_pid_are_exit_proof(self):
        probe = self.probe()
        probe.api.OpenProcess.return_value = 123
        probe.creation.return_value = 100
        probe.api.WaitForSingleObject.return_value = 0
        self.assertTrue(probe.exited({'pid': 111, 'created': 100}))
        probe.api.OpenProcess.return_value = None
        probe.ctypes.get_last_error.return_value = 87
        self.assertTrue(probe.exited({'pid': 111, 'created': 100}))


if __name__ == '__main__':
    unittest.main()
