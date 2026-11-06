"""Same-directory short aliases and safe Chromium path failures, offline only.

All synthetic directories stay in workspace TEMP. The process probe exits
normally and launches only bundled Node filesystem code, never a browser/site.
"""
import ctypes
import errno
import json
import multiprocessing
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from quota_guard.browser_process import _bootstrap, _worker_failure
from quota_guard.browser_service import BrowserService
from quota_guard.core import GuardError
from quota_guard.browser_startup import (
    BrowserTempPathError, _windows_short_path, is_temp_path_too_long, owned_temp_path_alias,
)
from quota_guard.profile_recovery import (
    MARKER, ownership, profile_access_directory, recover_profiles, resolved_profile_path, write_marker,
)


TEMP_ROOT = Path(__file__).resolve().parents[2] / 'TEMP'


def _node_temp_probe(connection):
    gate = connection.recv()
    import playwright
    node = Path(playwright.__file__).parent / 'driver' / 'node.exe'
    script = """
const fs = require('fs'), os = require('os'), path = require('path');
const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'playwright_chromiumdev_profile-'));
fs.writeFileSync(path.join(profile, 'synthetic.txt'), 'synthetic offline fixture');
process.stdout.write(JSON.stringify({temp: os.tmpdir(), profile}));
"""
    result = subprocess.run([str(node), '-e', script], capture_output=True, text=True,
                            timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
    connection.send({'returncode': result.returncode, 'gate_temp': gate['temp_dir'],
                     'python_temp': tempfile.gettempdir(),
                     'env': {name: os.environ[name] for name in ('TEMP', 'TMP', 'TMPDIR')},
                     'result': json.loads(result.stdout) if result.returncode == 0 else None})
    connection.close()


@unittest.skipUnless(os.name == 'nt', 'Windows existing 8.3 alias')
class BrowserTempAliasTests(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.folder = Path(tempfile.mkdtemp(prefix='browser-temp-path-', dir=TEMP_ROOT)).resolve()
        self.owned = self.folder / 'owned-browser-session-with-a-long-descriptive-name'
        self.owned.mkdir()
        self.directory = str(self.owned)

    def test_actual_alias_is_same_owned_directory_and_no_external_path_is_created(self):
        before = set(self.folder.iterdir())
        actual = _windows_short_path(self.directory)
        selected = owned_temp_path_alias(self.directory)
        self.assertTrue(os.path.samefile(selected, self.directory))
        self.assertTrue(Path(selected).is_dir())
        self.assertEqual(set(self.folder.iterdir()), before)
        if actual and len(actual) < len(self.directory):
            self.assertEqual(selected, actual)
        else:
            self.assertEqual(selected, self.directory)

    def test_no_alias_same_or_longer_name_and_api_failure_all_keep_original_path(self):
        for value in (None, '', self.directory, self.directory + '-longer', 'relative-alias'):
            with self.subTest(alias=value), patch(
                    'quota_guard.browser_startup._windows_short_path', return_value=value):
                self.assertEqual(owned_temp_path_alias(self.directory), self.directory)
        for failure in (OSError('private API failure'), ValueError('private API failure')):
            with self.subTest(failure=type(failure)), patch(
                    'quota_guard.browser_startup._windows_short_path', side_effect=failure):
                self.assertEqual(owned_temp_path_alias(self.directory), self.directory)

    def test_different_existing_directory_is_never_accepted_as_short_alias(self):
        unrelated = self.folder / 'other'
        unrelated.mkdir()
        sentinel = unrelated / 'keep.txt'
        sentinel.write_text('preserve unrelated synthetic directory', encoding='utf-8')
        with patch('quota_guard.browser_startup._windows_short_path', return_value=str(unrelated)):
            self.assertEqual(owned_temp_path_alias(self.directory), self.directory)
        self.assertEqual(sentinel.read_text(encoding='utf-8'), 'preserve unrelated synthetic directory')
        self.assertEqual(list(self.owned.iterdir()), [])

    def test_samefile_failure_keeps_original_and_never_guesses_alias(self):
        alias = _windows_short_path(self.directory)
        if alias is None or len(alias) >= len(self.directory):
            self.skipTest('This volume has no shorter existing 8.3 alias')
        for outcome in (False, OSError('private identity lookup failure')):
            with self.subTest(outcome=type(outcome).__name__), patch(
                    'quota_guard.browser_startup._windows_short_path', return_value=alias), patch(
                    'quota_guard.browser_startup.os.path.samefile',
                    **({'side_effect': outcome} if isinstance(outcome, Exception) else {'return_value': outcome})):
                self.assertEqual(owned_temp_path_alias(self.directory), self.directory)

    def test_windows_api_uses_actual_required_buffer_size_and_rejects_incomplete_result(self):
        calls = []
        alias = str(self.folder / 'EXISTI~1')
        def api(path, buffer, size):
            calls.append((path, buffer is None, size))
            if buffer is None:
                return len(alias) + 1
            buffer.value = alias
            return len(alias)
        with patch.object(ctypes, 'WinDLL', return_value=SimpleNamespace(GetShortPathNameW=api)):
            self.assertEqual(_windows_short_path(self.directory), alias)
        self.assertEqual(calls, [(self.directory, True, 0), (self.directory, False, len(alias) + 1)])
        bad = Mock(side_effect=[40, 40])
        with patch.object(ctypes, 'WinDLL', return_value=SimpleNamespace(GetShortPathNameW=bad)):
            self.assertIsNone(_windows_short_path(self.directory))

    def test_bootstrap_changes_only_worker_temp_settings_not_original_gate_path(self):
        gate = {'kind': 'start', 'temp_dir': self.directory}
        connection = Mock()
        connection.recv.return_value = gate
        captured = {}
        def target(primed):
            captured['gate'] = primed.recv()
            captured['env'] = {name: os.environ[name] for name in ('TEMP', 'TMP', 'TMPDIR')}
            captured['python_temp'] = tempfile.gettempdir()
        original_env = {name: os.environ.get(name) for name in ('TEMP', 'TMP', 'TMPDIR')}
        with patch.dict(os.environ), patch.object(tempfile, 'tempdir', tempfile.tempdir):
            _bootstrap(connection, target)
        self.assertIs(captured['gate'], gate)
        self.assertEqual(gate['temp_dir'], self.directory)
        self.assertTrue(all(os.path.samefile(value, self.directory) for value in captured['env'].values()))
        self.assertTrue(os.path.samefile(captured['python_temp'], self.directory))
        self.assertEqual({name: os.environ.get(name) for name in original_env}, original_env)

    def test_real_spawned_worker_and_node_create_temp_only_under_original_long_owned_directory(self):
        alias = owned_temp_path_alias(self.directory)
        if alias == self.directory:
            self.skipTest('This volume has no shorter existing 8.3 alias')
        original_env = {name: os.environ.get(name) for name in ('TEMP', 'TMP', 'TMPDIR')}
        context = multiprocessing.get_context('spawn')
        parent, child = context.Pipe()
        process = context.Process(target=_bootstrap, args=(child, _node_temp_probe))
        try:
            process.start()
            child.close()
            parent.send({'kind': 'start', 'temp_dir': self.directory})
            self.assertTrue(parent.poll(25), 'Synthetic Node filesystem probe did not return')
            result = parent.recv()
            process.join(timeout=5)
            self.assertEqual(process.exitcode, 0)
            self.assertEqual(result['returncode'], 0)
            self.assertEqual(result['gate_temp'], self.directory)
            self.assertEqual(result['python_temp'], alias)
            self.assertEqual(set(result['env'].values()), {alias})
            self.assertTrue(os.path.samefile(result['result']['temp'], self.owned))
            profile = Path(result['result']['profile']).resolve(strict=True)
            self.assertEqual(profile.parent, self.owned)
            self.assertEqual((profile / 'synthetic.txt').read_text(), 'synthetic offline fixture')
        finally:
            parent.close()
            child.close()
            if process.pid is not None:
                if process.is_alive():
                    process.terminate()  # Only this test's freshly spawned worker on failed assertions.
                    process.join(timeout=5)
                process.close()
        self.assertEqual({name: os.environ.get(name) for name in original_env}, original_env)

    def recovery_fixture(self):
        root = self.folder / 'profiles'
        root.mkdir()
        session = root / uuid4().hex
        session.mkdir()
        access = profile_access_directory(session)
        if str(access) == str(session):
            self.skipTest('This volume has no shorter existing 8.3 alias')
        deep = (access / 'synthetic-profile' / 'nested-cache-section-for-testing'
                / 'persistent-cache-section-for-testing' / 'synthetic-deep-file.txt')
        deep.parent.mkdir(parents=True)
        deep.write_text('synthetic deep profile content', encoding='utf-8')
        records = [{'pid': pid, 'created': pid * 100} for pid in (111, 222, 333)]
        record = ownership(session, *records)
        write_marker(session, record)
        write_marker(session, {**record, 'state': 'settled'}, previous=record)
        unproven = root / uuid4().hex
        unproven.mkdir()
        (unproven / 'keep.txt').write_text('no ownership proof', encoding='utf-8')
        neighbor = self.folder / 'neighbor.txt'
        neighbor.write_text('unrelated synthetic neighbor', encoding='utf-8')
        return root, session, deep, unproven, neighbor

    def test_deep_profile_recovery_uses_same_alias_and_keeps_unproven_session_and_neighbor(self):
        root, session, deep, unproven, neighbor = self.recovery_fixture()
        self.assertTrue(deep.is_file())
        self.assertTrue(resolved_profile_path(deep).is_relative_to(resolved_profile_path(session)))
        probe = Mock()
        probe.exited.return_value = True
        self.assertEqual(recover_profiles(root, probe=probe), {'removed': 1, 'retained': 1})
        self.assertFalse(session.exists())
        self.assertEqual((unproven / 'keep.txt').read_text(), 'no ownership proof')
        self.assertEqual(neighbor.read_text(), 'unrelated synthetic neighbor')
        self.assertEqual(probe.exited.call_count, 3)

    def test_wrong_access_alias_rejects_cleanup_of_owned_and_other_directories(self):
        root, session, deep, unproven, neighbor = self.recovery_fixture()
        probe = Mock()
        probe.exited.return_value = True
        with patch('quota_guard.browser_startup.owned_temp_path_alias', return_value=str(unproven)):
            self.assertEqual(recover_profiles(root, probe=probe), {'removed': 0, 'retained': 2})
        self.assertTrue((session / MARKER).is_file())
        self.assertEqual(deep.read_text(), 'synthetic deep profile content')
        self.assertEqual((unproven / 'keep.txt').read_text(), 'no ownership proof')
        self.assertEqual(neighbor.read_text(), 'unrelated synthetic neighbor')

    def test_deep_profile_without_all_exit_proofs_is_preserved_despite_valid_short_alias(self):
        root, session, deep, unproven, neighbor = self.recovery_fixture()
        probe = Mock()
        probe.exited.return_value = False
        self.assertEqual(recover_profiles(root, probe=probe), {'removed': 0, 'retained': 2})
        self.assertTrue((session / MARKER).is_file())
        self.assertEqual(deep.read_text(), 'synthetic deep profile content')
        self.assertEqual((unproven / 'keep.txt').read_text(), 'no ownership proof')
        self.assertEqual(neighbor.read_text(), 'unrelated synthetic neighbor')

    def test_only_resolved_dos_and_unc_extended_names_are_normalized_for_comparison(self):
        cases = ((r'\\?\C:\synthetic\deep', r'C:\synthetic\deep'),
                 (r'\\?\UNC\synthetic-server\share\deep', r'\\synthetic-server\share\deep'))
        for supplied, expected in cases:
            with self.subTest(supplied=supplied), patch.object(Path, 'resolve', return_value=Path(supplied)) as resolve:
                self.assertEqual(resolved_profile_path(self.owned), Path(expected))
                resolve.assert_called_once_with(strict=True)
        for supplied in (r'\\?\GLOBALROOT\Device\Synthetic', r'\\?\Volume{synthetic}\deep'):
            with self.subTest(supplied=supplied), patch.object(Path, 'resolve', return_value=Path(supplied)):
                with self.assertRaises(GuardError):
                    resolved_profile_path(self.owned)


class BrowserTempLaunchTests(unittest.TestCase):
    def setUp(self):
        self.service = object.__new__(BrowserService)
        self.service.pw = self.service.context = None
        self.service.page = self.service.browser = None
        self.service.anchor = None
        self.service.known_keys = set()
        self.service._stage = Mock()
        self.service._log = Mock()
        self.page, self.context, self.browser, self.driver = Mock(), Mock(), Mock(), Mock()
        self.context.new_page.return_value = self.page
        self.browser.new_context.return_value = self.context
        self.driver.chromium.launch.return_value = self.browser
        self.entry = SimpleNamespace(start=Mock(return_value=self.driver))
        network = patch('socket.socket', side_effect=AssertionError('No real sockets in this test'))
        network.start()
        self.addCleanup(network.stop)

    def ensure_browser(self):
        with patch('quota_guard.browser_service.check_browser_loopback'), \
                patch('playwright.sync_api.sync_playwright', return_value=self.entry), \
                patch('quota_guard.browser_service.install_optional_notice_handler'):
            self.service._ensure_browser()

    def test_only_explicit_os_or_node_path_error_is_recognized(self):
        windows = OSError('private detail')
        windows.winerror = 206
        for error in (OSError(errno.ENAMETOOLONG, 'private path'), windows,
                      Exception("BrowserType.launch: ENAMETOOLONG: name too long, mkdtemp 'private'")):
            self.assertTrue(is_temp_path_too_long(error))
        for error in (Exception('path too long'), OSError(errno.EACCES, 'private path'),
                      Exception(r'C:\ENAMETOOLONG\private'), Exception('NOT_ENAMETOOLONG: private')):
            self.assertFalse(is_temp_path_too_long(error))

    def test_chromium_path_failure_is_fixed_safe_message_before_page_or_platform(self):
        self.driver.chromium.launch.side_effect = Exception(
            "BrowserType.launch: ENAMETOOLONG: name too long, mkdtemp 'private-secret-account-directory'")
        with self.assertRaises(BrowserTempPathError) as error:
            self.ensure_browser()
        self.assertEqual(error.exception.reason_code, 'BROWSER_TEMP_PATH_TOO_LONG')
        self.assertIn('较短路径的数据目录', str(error.exception))
        self.assertNotIn('private', str(error.exception))
        self.driver.chromium.launch.assert_called_once_with(headless=False, timeout=30000)
        self.browser.new_context.assert_not_called()
        self.page.goto.assert_not_called()
        self.assertIsNone(self.service.browser)
        self.assertIsNone(self.service.context)
        self.assertIsNone(self.service.page)
        self.service._log.assert_called_once_with('browser_startup_failed', phase='chromium',
            reason_code='BROWSER_TEMP_PATH_TOO_LONG', error_code='ENAMETOOLONG')
        result = _worker_failure(error.exception, 'login', None)
        self.assertEqual(result['error_type'], 'GuardError')
        self.assertIn('较短路径的数据目录', result['message'])
        self.assertNotIn('private', result['message'])

    def test_other_launch_error_is_not_reclassified_and_retry_remains_possible(self):
        original = Exception('synthetic unrelated browser launch failure')
        self.driver.chromium.launch.side_effect = [original, self.browser]
        with self.assertRaises(Exception) as caught:
            self.ensure_browser()
        self.assertIs(caught.exception, original)
        self.service._log.assert_not_called()
        self.ensure_browser()
        self.assertIs(self.service.page, self.page)
        self.page.goto.assert_not_called()


if __name__ == '__main__':
    unittest.main()
