"""Offline isolation/IPC checks. No browser, credentials, or network is used."""
from concurrent.futures import Future
from collections import deque
import ctypes
from ctypes import wintypes
import json
import multiprocessing
import os
from pathlib import Path
from queue import Queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import Mock, patch

from quota_guard.browser_process import (
    IsolatedBrowserService, _Task, _WindowsJob, _bound_grant_to_task, _inspection_result, _worker_failure,
)
from quota_guard.revocation_permit import DeletePermit, KeyInspectionError, RevocationNotSubmitted
from quota_guard.core import AccountMismatch, GuardError
from quota_guard.profile_recovery import WindowsLifetime, read_marker, recover_profiles
from quota_guard.target_lock import TargetLock


def fixture_profile_owner(connection, folder):
    """A synthetic owner for later Windows force-kill acceptance; no browser."""
    service = IsolatedBrowserService(diagnostics_dir=Path(folder) / 'diagnostics',
                                     _worker_target=fixture_worker)
    target_lock = TargetLock(['synthetic-owner-' + uuid.uuid4().hex])
    try:
        profile = service.submit('selftest', mode='tempfiles',
                                 target_lock_names=target_lock.names).result(timeout=15)
        tree = service.submit('selftest', mode='child').result(timeout=10)
        marker = read_marker(Path(profile['temp_dir']))
        connection.send({**profile, 'child_pid': tree['child_pid'],
                         'witness_pid': service._witness.pid,
                         'cleanup_records': [marker['worker'], tree['child_record'], marker['witness']]})
        connection.recv()
    finally:
        service.close()
        target_lock.close()
        connection.close()


def fixture_worker(connection):
    """A spawn-safe process which implements only the public IPC test protocol."""
    gate = connection.recv()
    if gate.get('kind') != 'start':
        raise AssertionError('The parent must admit this process before it runs.')
    session = gate['session']
    connection.send({'kind': 'ready', 'session': session})
    children = []
    try:
        while True:
            message = connection.recv()
            if message.get('kind') != 'action':
                continue
            task = message['task']
            if message['session'] != session:
                raise AssertionError('An action crossed a process session.')
            args = message.get('kwargs', {})
            mode = args.get('mode', 'echo')

            def send(kind, **fields):
                connection.send({'kind': kind, 'session': session, 'task': task, **fields})

            if mode == 'crash':
                send('progress', stage='模拟即将崩溃')
                time.sleep(.3)
                os._exit(19)
            if mode == 'fail':
                send('result', ok=False, error_type=args.get('error_type', 'GuardError'),
                     message='模拟账户连续性检查失败')
                continue
            if mode == 'stale':
                connection.send({'kind': 'result', 'session': 'expired-session',
                                 'task': task, 'ok': True, 'result': {'stale': True}})
                connection.send({'kind': 'result', 'session': session,
                                 'task': task - 1, 'ok': True, 'result': {'stale': True}})
                connection.send({'kind': 'progress', 'session': 'expired-session',
                                 'task': task, 'stage': '过期进度不应显示'})
            if mode in ('permit', 'permit_hang'):
                send('permit_ready', prepared_inventory={
                    'target_tracking_id': args['tracking_id'],
                    'known_tracking_ids': [args['tracking_id'], 'synthetic-control']})
                decision = connection.recv()
                if (decision.get('kind'), decision.get('session'), decision.get('task')) != (
                        'permit_decision', session, task):
                    raise AssertionError('The permit decision must belong to this exact action.')
                if not decision.get('allowed'):
                    send('result', ok=True, result={'cancelled': True})
                    continue
                permit = DeletePermit()
                permit.accept_grant(decision.get('grant'))
                permit.consume_for_click()
                if mode == 'permit':
                    send('result', ok=True, result={'absent': True})
                    continue
            if mode in ('hang', 'permit_hang'):
                send('progress', stage='模拟已提交后挂起' if mode == 'permit_hang' else '模拟挂起')
                time.sleep(60)
            if mode == 'delay':
                send('progress', stage='模拟延迟操作')
                time.sleep(args.get('delay', .2))
            result = {'pid': os.getpid(), 'session': session, 'task': task,
                      'value': args.get('value')}
            if mode == 'tempfiles':
                session_directory = Path(gate['temp_dir'])
                fixture_file = session_directory / 'sessionfile' / 'profile' / 'cookie-fixture.txt'
                fixture_file.parent.mkdir(parents=True, exist_ok=True)
                fixture_file.write_text('synthetic browser profile; contains no credentials', encoding='utf-8')
                result.update({'temp_dir': str(session_directory), 'gettempdir': tempfile.gettempdir(),
                               'env': {key: os.environ.get(key) for key in ('TEMP', 'TMP', 'TMPDIR')},
                               'fixture_file': str(fixture_file)})
            if mode == 'child':
                flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, creationflags=flags)
                children.append(child)
                result['child_pid'] = child.pid
                if os.name == 'nt':
                    # Popen retains the original process handle, so identity
                    # capture cannot accidentally follow a reused PID.
                    result['child_record'] = {'pid': child.pid,
                                              'created': WindowsLifetime().creation(child._handle)}
            send('result', ok=True, result=result)
    except (EOFError, BrokenPipeError, OSError):
        pass
    finally:
        connection.close()


def process_alive(pid):
    if os.name != 'nt':
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        kernel.CloseHandle(handle)


def cleanup_fixture_process(record):
    """Terminate only a still-matching synthetic Windows process; otherwise retain it."""
    if (os.name != 'nt' or not isinstance(record, dict)
            or any(type(record.get(key)) is not int or record[key] <= 0
                   for key in ('pid', 'created'))):
        return
    probe = WindowsLifetime()
    kernel = probe.api
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.TerminateProcess.restype = wintypes.BOOL
    handle = kernel.OpenProcess(0x00100000 | 0x1000 | 1, False, record['pid'])
    if handle:
        try:
            # Keep the same handle for identity, liveness and termination;
            # never reopen the historical PID after checking its identity.
            if (probe.creation(handle) == record['created']
                    and kernel.WaitForSingleObject(handle, 0) == 258):
                kernel.TerminateProcess(handle, 99)
        except OSError:
            pass  # Unknown identity/liveness must never become permission to kill.
        finally:
            kernel.CloseHandle(handle)


def make_directory_link(link, target):
    """Create a test-owned directory reparse point; never touch existing paths."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except OSError:
        if os.name != 'nt':
            raise unittest.SkipTest('This host cannot create a synthetic directory symlink.')
    # Junctions do not need the Windows symlink privilege. Both paths are under
    # this test's newly created temporary directory, and neither contains secrets.
    result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(link), str(target)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=subprocess.CREATE_NO_WINDOW, timeout=5)
    if result.returncode or not link.exists():
        raise unittest.SkipTest('This host cannot create a synthetic symlink or junction.')


def remove_directory_link(link):
    """Remove only the link itself, without recursive deletion or target traversal."""
    if link.is_symlink():
        link.unlink()
    elif link.exists():
        os.rmdir(link)


class BrowserProcessTests(unittest.TestCase):
    def service(self, **kwargs):
        service = IsolatedBrowserService(_worker_target=fixture_worker, **kwargs)
        if os.name == 'nt':
            target_lock = TargetLock(['synthetic-' + uuid.uuid4().hex])
            self.addCleanup(target_lock.close)
            original_submit = service.submit

            def submit(action, **arguments):
                if action in {'revoke', 'prepare_revoke', 'verify_revoke'}:
                    arguments.setdefault('target_lock_names', target_lock.names)
                return original_submit(action, **arguments)

            service.submit = submit
        self.addCleanup(service.close)
        return service

    def until(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(.01)
        self.fail('The isolated service did not reach the expected state in time.')

    def warm(self, service):
        return service.submit('selftest').result(timeout=10)

    def test_submit_returns_future_and_progress_is_a_detached_snapshot(self):
        service = self.service()
        self.warm(service)
        started = time.monotonic()
        future = service.submit('collect', mode='delay', delay=.25, value='fixture')
        self.assertIsInstance(future, Future)
        self.until(lambda: service.progress.get('stage') == '模拟延迟操作')
        self.assertTrue(future.running())
        progress = service.progress
        self.assertGreaterEqual(progress['started_at'], started)
        try:
            progress['stage'] = '外部修改'
        except TypeError:
            pass  # An immutable mapping is also a valid read-only snapshot.
        self.assertNotEqual(service.progress['stage'], '外部修改')
        self.assertEqual(future.result(timeout=5)['value'], 'fixture')

    def test_running_future_cannot_be_cancelled_but_queued_future_can(self):
        service = self.service()
        self.warm(service)
        active = service.submit('collect', mode='delay', delay=.35)
        self.until(lambda: service.progress.get('stage') == '模拟延迟操作')
        queued = service.submit('selftest', value='must not run')
        self.assertFalse(active.cancel())
        self.assertTrue(queued.cancel())
        self.assertTrue(active.result(timeout=5))
        self.assertTrue(queued.cancelled())
        self.assertEqual(service.submit('selftest', value='still usable').result(timeout=5)['value'],
                         'still usable')

    def test_action_deadline_settles_hang_and_next_submit_rebuilds_session(self):
        service = self.service(action_timeouts={'collect': .3})
        before = self.warm(service)
        future = service.submit('collect', mode='hang')
        with self.assertRaises(GuardError):
            future.result(timeout=5)
        after = self.warm(service)
        self.assertNotEqual(before['session'], after['session'])
        self.assertNotEqual(before['pid'], after['pid'])

    def test_worker_crash_settles_active_and_queued_operations(self):
        service = self.service()
        self.warm(service)
        active = service.submit('collect', mode='crash')
        self.until(lambda: service.progress.get('stage') == '模拟即将崩溃')
        queued = service.submit('selftest', value='queued before crash')
        with self.assertRaises(GuardError):
            active.result(timeout=5)
        with self.assertRaises(GuardError):
            queued.result(timeout=5)
        self.assertTrue(self.warm(service))

    def test_interrupt_returns_promptly_settles_queue_and_allows_new_session(self):
        service = self.service()
        before = self.warm(service)
        active = service.submit('collect', mode='hang')
        self.until(lambda: service.progress.get('stage') == '模拟挂起')
        queued = service.submit('selftest')
        started = time.monotonic()
        interruption = service.interrupt()
        self.assertIsInstance(interruption, Future)
        self.assertLess(time.monotonic() - started, .5)
        interruption.result(timeout=5)
        self.assertFalse(process_alive(before['pid']), 'Interrupt acknowledged before its process exited.')
        for future in (active, queued):
            self.assertTrue(future.done(), 'Interrupt acknowledged before its old action was settled.')
            with self.assertRaises(GuardError):
                future.result(timeout=1)
        after = self.warm(service)
        self.assertNotEqual(before['session'], after['session'])

    def test_repeated_interrupts_complete_and_do_not_poison_replacement(self):
        service = self.service()
        self.warm(service)
        active = service.submit('collect', mode='hang')
        self.until(lambda: service.progress.get('stage') == '模拟挂起')
        first, second = service.interrupt(), service.interrupt()
        first.result(timeout=5)
        second.result(timeout=5)
        with self.assertRaises(GuardError):
            active.result(timeout=1)
        self.assertTrue(self.warm(service))

    def test_failed_cleanup_settles_commands_blocks_work_and_can_be_retried(self):
        service = self.service()
        before = self.warm(service)
        active = service.submit('collect', mode='hang')
        self.until(lambda: service.progress.get('stage') == '模拟挂起')
        queued = service.submit('selftest', value='must not run before cleanup')
        original = service._kill_session
        attempts = []

        def fail_once():
            attempts.append(True)
            if len(attempts) == 1:
                raise GuardError('模拟进程清理失败')
            return original()

        with patch.object(service, '_kill_session', side_effect=fail_once):
            with self.assertRaisesRegex(GuardError, '清理失败'):
                service.interrupt().result(timeout=5)
            self.assertTrue(service.cleanup_failed)
            self.assertTrue(process_alive(before['pid']))
            for future in (active, queued):
                self.assertTrue(future.done())
                with self.assertRaises(GuardError):
                    future.result(timeout=1)
            with self.assertRaises(GuardError):
                service.submit('selftest').result(timeout=1)
            service.interrupt().result(timeout=5)
            self.assertFalse(service.cleanup_failed)
            self.assertFalse(process_alive(before['pid']))
            after = self.warm(service)
            self.assertNotEqual(before['session'], after['session'])

    def test_session_tempfiles_are_isolated_and_cleanup_preserves_unrelated_files(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder).resolve()
            service = self.service(diagnostics_dir=home / '诊断日志')
            try:
                first = service.submit('selftest', mode='tempfiles').result(timeout=10)
                directory = Path(first['temp_dir'])
                self.assertEqual(service._temp_root, home / '临时浏览器')
                self.assertEqual(directory, service._session_directory)
                self.assertEqual(directory.parent, service._temp_root)
                self.assertEqual(Path(first['gettempdir']), directory)
                self.assertEqual({Path(value) for value in first['env'].values()}, {directory})
                self.assertTrue(Path(first['fixture_file']).is_file())
                neighbor = home / 'unrelated-file.txt'
                neighbor.write_text('preserve this unrelated file', encoding='utf-8')
                sibling = service._temp_root / 'unrelated-session'
                sibling.mkdir()
                sibling_file = sibling / 'keep.txt'
                sibling_file.write_text('preserve this sibling directory', encoding='utf-8')

                service.interrupt().result(timeout=5)
                self.assertFalse(directory.exists())
                self.assertEqual(neighbor.read_text(encoding='utf-8'), 'preserve this unrelated file')
                self.assertEqual(sibling_file.read_text(encoding='utf-8'), 'preserve this sibling directory')

                second = service.submit('selftest', mode='tempfiles').result(timeout=10)
                replacement = Path(second['temp_dir'])
                self.assertNotEqual(replacement, directory)
                self.assertTrue(Path(second['fixture_file']).exists())
                service.close()
                self.assertFalse(replacement.exists())
                self.assertTrue(neighbor.exists())
                self.assertTrue(sibling_file.exists())
            finally:
                service.close()

    def test_default_temp_roots_are_dedicated_to_each_service(self):
        first, second = self.service(), self.service()
        one = first.submit('selftest', mode='tempfiles').result(timeout=10)
        two = second.submit('selftest', mode='tempfiles').result(timeout=10)
        self.assertNotEqual(first._temp_root, second._temp_root)
        self.assertNotEqual(one['temp_dir'], two['temp_dir'])
        self.assertEqual(Path(one['temp_dir']).parent, first._temp_root)
        self.assertEqual(Path(two['temp_dir']).parent, second._temp_root)
        first.interrupt().result(timeout=5)
        self.assertFalse(Path(one['fixture_file']).exists())
        self.assertTrue(Path(two['fixture_file']).exists())

    def test_failed_profile_removal_retains_path_and_blocks_replacement_until_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            service = self.service(diagnostics_dir=Path(folder) / 'diagnostics')
            try:
                result = service.submit('selftest', mode='tempfiles').result(timeout=10)
                directory = Path(result['temp_dir'])
                original = service._remove_session_directory
                attempts = []

                def fail_once(*args, **kwargs):
                    attempts.append(True)
                    if len(attempts) == 1:
                        raise GuardError('模拟临时目录删除失败')
                    return original(*args, **kwargs)

                with patch.object(service, '_remove_session_directory', side_effect=fail_once):
                    with self.assertRaisesRegex(GuardError, '删除失败'):
                        service.interrupt().result(timeout=5)
                    self.assertTrue(service.cleanup_failed)
                    self.assertEqual(service._session_directory, directory)
                    self.assertTrue(Path(result['fixture_file']).exists())
                    self.assertFalse(process_alive(result['pid']))
                    with self.assertRaises(GuardError):
                        service.submit('selftest').result(timeout=1)
                    service.interrupt().result(timeout=5)
                    self.assertFalse(service.cleanup_failed)
                    self.assertFalse(directory.exists())
                replacement = service.submit('selftest', mode='tempfiles').result(timeout=10)
                self.assertNotEqual(replacement['temp_dir'], result['temp_dir'])
            finally:
                service.close()

    def test_profile_reparse_point_refuses_cleanup_without_touching_external_files(self):
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            external = home / 'external-synthetic-data'
            external.mkdir()
            external_file = external / 'keep.txt'
            external_file.write_text('synthetic external data must survive', encoding='utf-8')
            service = self.service(diagnostics_dir=home / 'diagnostics')
            link = None
            try:
                result = service.submit('selftest', mode='tempfiles').result(timeout=10)
                directory = Path(result['temp_dir'])
                link = directory / 'linked-external'
                make_directory_link(link, external)
                with self.assertRaises(GuardError):
                    service.interrupt().result(timeout=5)
                self.assertTrue(service.cleanup_failed)
                self.assertEqual(service._session_directory, directory)
                self.assertTrue(directory.exists())
                self.assertEqual(external_file.read_text(encoding='utf-8'),
                                 'synthetic external data must survive')
                with self.assertRaises(GuardError):
                    service.submit('selftest').result(timeout=1)
                remove_directory_link(link)
                service.interrupt().result(timeout=5)
                self.assertFalse(service.cleanup_failed)
                self.assertFalse(directory.exists())
                self.assertEqual(external_file.read_text(encoding='utf-8'),
                                 'synthetic external data must survive')
            finally:
                if link is not None:
                    remove_directory_link(link)
                service.close()

    def test_job_close_failure_retains_handle_for_retry(self):
        class Accounting(ctypes.Structure):
            _fields_ = [('ActiveProcesses', ctypes.c_uint32)]

        job = object.__new__(_WindowsJob)
        job.handle = 987654321  # Synthetic value: every Win32 call is mocked.
        job.api = Mock()
        job.api.TerminateJobObject.return_value = True
        job._accounting, job._ctypes = Accounting, ctypes

        def no_live_processes(_handle, _kind, information, _size, _length):
            information._obj.ActiveProcesses = 0
            return True

        job.api.QueryInformationJobObject.side_effect = no_live_processes
        job.api.CloseHandle.return_value = False
        with self.assertRaisesRegex(GuardError, '句柄'):
            job.close()
        self.assertEqual(job.handle, 987654321)
        job.api.CloseHandle.assert_called_once_with(987654321)
        job.api.CloseHandle.return_value = True
        job.close()
        self.assertIsNone(job.handle)

    @unittest.skipUnless(os.name == 'nt', 'Windows process cleanup branch')
    def test_process_surviving_join_and_kill_is_retained_as_cleanup_failure(self):
        service = object.__new__(IsolatedBrowserService)
        process = Mock()
        process.pid = 987654321  # Never passed to a real OS call.
        process.is_alive.return_value = True
        service._process = process
        service._job = Mock()  # Job termination itself is also only a mock.
        service._connection, service._outbox = Mock(), Mock()
        service._session, service._ready = 'synthetic-session', True
        with self.assertRaisesRegex(GuardError, '仍在运行'):
            service._kill_session()
        self.assertIs(service._process, process)
        self.assertEqual(service._session, 'synthetic-session')
        process.kill.assert_called_once_with()
        process.close.assert_not_called()

    def test_action_done_callback_can_interrupt_again_without_losing_acknowledgement(self):
        service = self.service()
        self.warm(service)
        active = service.submit('collect', mode='hang')
        self.until(lambda: service.progress.get('stage') == '模拟挂起')
        callback_ran = threading.Event()
        nested_interruptions = []

        def interrupted_again(_future):
            nested_interruptions.append(service.interrupt())
            callback_ran.set()

        active.add_done_callback(interrupted_again)
        initial = service.interrupt()
        initial.result(timeout=5)
        self.assertTrue(callback_ran.wait(timeout=1))
        self.assertTrue(active.done())
        self.assertEqual(len(nested_interruptions), 1)
        nested_interruptions[0].result(timeout=5)
        self.assertTrue(self.warm(service))

    def test_interrupt_returns_immediately_even_while_process_start_is_blocked(self):
        service = self.service()
        entered, release = threading.Event(), threading.Event()

        def blocked_spawn():
            entered.set()
            release.wait(timeout=2)

        with patch.object(service, '_spawn', side_effect=blocked_spawn):
            active = service.submit('selftest')
            self.assertTrue(entered.wait(timeout=5))
            try:
                started = time.monotonic()
                interrupted = service.interrupt()
                self.assertLess(time.monotonic() - started, .5)
            finally:
                release.set()
            interrupted.result(timeout=5)
            self.assertTrue(active.done())
            with self.assertRaises(GuardError):
                active.result(timeout=1)
        self.assertTrue(self.warm(service))

    def test_stale_session_and_task_messages_cannot_complete_current_action(self):
        service = self.service()
        self.warm(service)
        result = service.submit('collect', mode='stale', value='current').result(timeout=5)
        self.assertEqual(result['value'], 'current')
        self.assertNotIn('stale', result)
        self.assertNotEqual(service.progress.get('stage'), '过期进度不应显示')

    def test_revoke_waits_for_explicit_parent_permit(self):
        service = self.service()
        self.warm(service)
        permit = DeletePermit()
        future = service.submit('revoke', name='fixture', tracking_id='fixture-target',
                                permit=permit, mode='permit')
        self.assertTrue(permit.ready.wait(timeout=5))
        time.sleep(.15)
        self.assertFalse(future.done(), 'A ready event is not deletion authorization.')
        permit.decide(True)
        self.assertEqual(future.result(timeout=5), {'absent': True})

    def test_denied_parent_permit_cannot_approve_revoke(self):
        service = self.service()
        self.warm(service)
        permit = DeletePermit()
        future = service.submit('revoke', name='fixture', tracking_id='fixture-target',
                                permit=permit, mode='permit')
        self.assertTrue(permit.ready.wait(timeout=5))
        permit.decide(False)
        self.assertEqual(future.result(timeout=5), {'cancelled': True})

    def test_timeout_after_approved_revoke_is_unknown_not_cancelled(self):
        service = self.service(action_timeouts={'revoke': .5})
        self.warm(service)
        permit = DeletePermit()
        future = service.submit('revoke', name='fixture', tracking_id='fixture-target',
                                permit=permit, mode='permit_hang')
        self.assertTrue(permit.ready.wait(timeout=5))
        permit.decide(True)
        with self.assertRaisesRegex(GuardError, '未知|不明'):
            future.result(timeout=5)

    def test_interrupt_after_approved_revoke_is_unknown_not_cancelled(self):
        service = self.service()
        self.warm(service)
        permit = DeletePermit()
        future = service.submit('revoke', name='fixture', tracking_id='fixture-target',
                                permit=permit, mode='permit_hang')
        self.assertTrue(permit.ready.wait(timeout=5))
        permit.decide(True)
        self.until(lambda: service.progress.get('stage') == '模拟已提交后挂起')
        service.interrupt().result(timeout=5)
        with self.assertRaisesRegex(GuardError, '未知|不明'):
            future.result(timeout=1)

    def test_account_mismatch_type_survives_ipc(self):
        service = self.service()
        with self.assertRaises(AccountMismatch):
            service.submit('collect', mode='fail', error_type='AccountMismatch').result(timeout=10)

    def test_real_about_blank_executor_shares_diagnostic_session_with_parent(self):
        # Exercise the production child executor and bundled Chromium, without
        # opening the platform, logging in, reading credentials, or making API calls.
        with tempfile.TemporaryDirectory() as folder:
            service = IsolatedBrowserService(diagnostics_dir=folder,
                                             action_timeouts={'selftest': 30})
            try:
                result = service.submit('selftest').result(timeout=40)
                self.assertIs(result.get('browser_started'), True)
                self.assertTrue(result.get('version'))
                session_directory = service._session_directory
                entries = list(session_directory.iterdir())
                self.assertTrue(entries, 'The real browser did not use its scoped temporary directory.')
                self.assertTrue(any(entry.is_dir() and entry.name.startswith('playwright_chromiumdev_profile')
                                    for entry in entries), [entry.name for entry in entries])
            finally:
                service.close()
            self.assertTrue(service.closed)
            self.assertFalse(session_directory.exists())
            files = list(Path(folder).glob('diagnostic-*.jsonl'))
            self.assertGreaterEqual(len(files), 2)
            entries = [(path.name, json.loads(line)) for path in files
                       for line in path.read_text(encoding='utf-8').splitlines()]
            parent_file, parent = next((name, record['data']) for name, record in entries
                                       if record['event'] == 'browser_process_started')
            child_file, child = next((name, record['data']) for name, record in entries
                                     if record['event'] == 'isolated_session_started')
            self.assertNotEqual(parent_file, child_file)
            self.assertEqual(parent['session'], child['session'])
            self.assertEqual(parent['generation'], child['generation'])
            self.assertEqual(parent['worker_pid'], child['pid'])
            self.assertEqual(parent['pid'], os.getpid())
            self.assertEqual(child['parent_pid'], os.getpid())
            self.assertNotEqual(child['pid'], os.getpid())

    def test_close_is_bounded_and_rejects_further_work(self):
        service = self.service()
        before = self.warm(service)
        active = service.submit('collect', mode='hang')
        self.until(lambda: service.progress.get('stage') == '模拟挂起')
        started = time.monotonic()
        service.close()
        self.assertLess(time.monotonic() - started, 5)
        with self.assertRaises(GuardError):
            active.result(timeout=1)
        with self.assertRaises(GuardError):
            service.submit('selftest').result(timeout=1)
        self.until(lambda: not process_alive(before['pid']))

    @unittest.skipUnless(os.name == 'nt', 'Windows Job Object process-tree guarantee')
    def test_interrupt_kills_own_tree_and_preserves_other_service(self):
        first, other = self.service(), self.service()
        other_before = self.warm(other)
        owned = first.submit('selftest', mode='child').result(timeout=10)
        self.addCleanup(cleanup_fixture_process, owned['child_record'])
        self.assertTrue(process_alive(owned['child_pid']))
        first.interrupt().result(timeout=5)
        self.until(lambda: not process_alive(owned['pid']))
        self.until(lambda: not process_alive(owned['child_pid']))
        self.assertTrue(process_alive(other_before['pid']))
        other_after = self.warm(other)
        self.assertEqual(other_before['pid'], other_after['pid'])
        self.assertEqual(other_before['session'], other_after['session'])

    @unittest.skipUnless(os.name == 'nt', 'Windows parent death and Job exit witness')
    def test_force_killed_owner_leaves_recoverable_receipt_without_touching_live_service(self):
        with tempfile.TemporaryDirectory() as folder:
            context = multiprocessing.get_context('spawn')
            connection, child = context.Pipe(duplex=True)
            owner = context.Process(target=fixture_profile_owner, args=(child, folder))
            owner.start()
            child.close()
            live = self.service(diagnostics_dir=Path(folder) / 'diagnostics')
            live_profile = live.submit('selftest', mode='tempfiles').result(timeout=15)
            try:
                self.assertTrue(connection.poll(20), 'Synthetic owner did not become ready')
                owned = connection.recv()
                for record in owned['cleanup_records']:
                    self.addCleanup(cleanup_fixture_process, record)
                directory = Path(owned['temp_dir'])
                self.assertEqual(read_marker(directory)['state'], 'armed')
                owner.terminate()  # Explicitly skip the synthetic owner's finally block.
                owner.join(timeout=5)
                self.assertFalse(owner.is_alive())
                self.until(lambda: not process_alive(owned['witness_pid']), timeout=8)
                self.assertFalse(process_alive(owned['pid']))
                self.assertFalse(process_alive(owned['child_pid']))
                self.assertEqual(read_marker(directory)['state'], 'settled')
                result = recover_profiles(directory.parent)
                self.assertEqual(result['removed'], 1)
                self.assertFalse(directory.exists())
                self.assertTrue(Path(live_profile['fixture_file']).exists())
                self.assertTrue(process_alive(live_profile['pid']))
            finally:
                if owner.is_alive():
                    owner.terminate()
                    owner.join(timeout=5)
                owner.close()
                connection.close()
                live.close()


@unittest.skipUnless(os.name == 'nt', 'Windows fixture cleanup identity checks')
class FixtureCleanupTests(unittest.TestCase):
    def probe(self):
        probe = Mock()
        probe.api.OpenProcess.return_value = 444
        probe.creation.return_value = 123456
        probe.api.WaitForSingleObject.return_value = 258
        return probe

    def test_cleanup_rejects_missing_or_invalid_identity(self):
        with patch(__name__ + '.WindowsLifetime') as factory:
            for record in (123, None, {}, {'pid': 123}, {'pid': 123, 'created': 0},
                           {'pid': True, 'created': 123456}):
                with self.subTest(record=record):
                    cleanup_fixture_process(record)
            factory.assert_not_called()

    def test_cleanup_retains_reused_pid_and_closes_handle(self):
        probe = self.probe()
        probe.creation.return_value = 999999
        with patch(__name__ + '.WindowsLifetime', return_value=probe):
            cleanup_fixture_process({'pid': 123, 'created': 123456})
        probe.api.TerminateProcess.assert_not_called()
        probe.api.CloseHandle.assert_called_once_with(444)

    def test_cleanup_retains_when_creation_cannot_be_read(self):
        probe = self.probe()
        probe.creation.side_effect = OSError('synthetic access failure')
        with patch(__name__ + '.WindowsLifetime', return_value=probe):
            cleanup_fixture_process({'pid': 123, 'created': 123456})
        probe.api.TerminateProcess.assert_not_called()
        probe.api.CloseHandle.assert_called_once_with(444)

    def test_cleanup_retains_when_open_fails(self):
        probe = self.probe()
        probe.api.OpenProcess.return_value = 0
        with patch(__name__ + '.WindowsLifetime', return_value=probe):
            cleanup_fixture_process({'pid': 123, 'created': 123456})
        probe.creation.assert_not_called()
        probe.api.TerminateProcess.assert_not_called()
        probe.api.CloseHandle.assert_not_called()

    def test_cleanup_does_not_terminate_exited_or_unknown_process(self):
        for status in (0, 0xFFFFFFFF):
            with self.subTest(status=status):
                probe = self.probe()
                probe.api.WaitForSingleObject.return_value = status
                with patch(__name__ + '.WindowsLifetime', return_value=probe):
                    cleanup_fixture_process({'pid': 123, 'created': 123456})
                probe.api.TerminateProcess.assert_not_called()
                probe.api.CloseHandle.assert_called_once_with(444)

    def test_cleanup_terminates_matching_live_fixture_through_same_handle(self):
        probe = self.probe()
        with patch(__name__ + '.WindowsLifetime', return_value=probe):
            cleanup_fixture_process({'pid': 123, 'created': 123456})
        probe.api.OpenProcess.assert_called_once_with(0x00100000 | 0x1000 | 1, False, 123)
        probe.creation.assert_called_once_with(444)
        probe.api.WaitForSingleObject.assert_called_once_with(444, 0)
        probe.api.TerminateProcess.assert_called_once_with(444, 99)
        probe.api.CloseHandle.assert_called_once_with(444)


class WorkerSubmissionFailureTests(unittest.TestCase):
    def test_inspection_failure_crosses_only_fixed_reason_and_never_original_text(self):
        for code in ('KEY_AUTH_HEADER_MISSING', 'Bearer private-value', [], None):
            with self.subTest(code=code):
                failure = KeyInspectionError(reason_code=code)
                failure.args = ('private worker exception text',)
                # Simulate a corrupt attribute after construction as well.
                failure.reason_code = code
                result = _worker_failure(failure, 'inspect_revoke', None)
                expected = code if code == 'KEY_AUTH_HEADER_MISSING' else 'PREPARATION_FAILED'
                self.assertEqual(result['error_type'], 'KeyInspectionError')
                self.assertEqual(result['reason_code'], expected)
                self.assertEqual(result['message'], str(KeyInspectionError(reason_code=expected)))
                self.assertNotIn('private', str(result))

    def test_untyped_inspection_failure_and_wrong_action_do_not_leak_or_claim_success(self):
        failure = GuardError('private worker page data')
        failure.reason_code = 'KEY_AUTH_HEADER_MISSING'
        result = _worker_failure(failure, 'inspect_revoke', None)
        self.assertEqual(result['reason_code'], 'PREPARATION_FAILED')
        self.assertNotIn('private worker', str(result))
        typed = KeyInspectionError(reason_code='KEY_AUTH_HEADER_MISSING')
        typed.args = ('private typed message',)
        for action, permit in (('revoke', DeletePermit()), ('collect', None), ('inspect_revoke', DeletePermit())):
            with self.subTest(action=action):
                result = _worker_failure(typed, action, permit)
                self.assertEqual(result['error_type'], 'GuardError')
                self.assertNotIn('private typed', str(result))

    def test_inspection_success_payload_rejects_sensitive_extras_wrong_target_and_boolean_count(self):
        expected = {'name': 'Synthetic target', 'tracking_id': '00000000-0000-0000-0000-000000000001'}
        clean = {**expected, 'preflight_passed': True, 'key_count': 2}
        result = _inspection_result(clean, expected)
        self.assertEqual(result, clean)
        self.assertIsNot(result, clean)
        for bad in ({**clean, 'sensitive_id': 'private-value'}, {**clean, 'name': 'Wrong target'},
                    {**clean, 'preflight_passed': False}, {**clean, 'key_count': True},
                    {**clean, 'key_count': 1}, {**clean, 'tracking_id': None}):
            with self.subTest(bad=bad):
                with self.assertRaises(KeyInspectionError) as caught:
                    _inspection_result(bad, expected)
                self.assertNotIn('private-value', str(caught.exception))

    def test_only_revoke_before_worker_ready_keeps_definite_non_submission_type(self):
        error = RevocationNotSubmitted()
        error.args = ('untrusted detail must not cross the typed boundary',)
        result = _worker_failure(error, 'revoke', DeletePermit())
        self.assertEqual(result, {'ok': False, 'error_type': 'RevocationNotSubmitted',
                                  'message': str(RevocationNotSubmitted()),
                                  'reason_code': 'PREPARATION_FAILED'})

    def test_only_allowlisted_reason_crosses_worker_boundary(self):
        for code in ('KEY_AUTH_HEADER_MISSING', 'Bearer private-value', [], None):
            with self.subTest(code=code):
                error = RevocationNotSubmitted('untrusted detail', reason_code=code)
                result = _worker_failure(error, 'revoke', DeletePermit())
                expected = code if code == 'KEY_AUTH_HEADER_MISSING' else 'PREPARATION_FAILED'
                self.assertEqual(result['reason_code'], expected)
                self.assertEqual(result['message'], str(RevocationNotSubmitted(reason_code=expected)))
                self.assertNotIn('private-value', str(result))
                self.assertNotIn('untrusted detail', str(result))

    def test_ready_granted_missing_and_unknown_worker_states_are_generic(self):
        ready, granted = DeletePermit(), DeletePermit()
        ready.ready.set()
        granted.decide(True)
        unknown = Mock()
        unknown.ready.is_set.return_value = False
        unknown.allowed = None
        unknown_ready = Mock(allowed=False)
        unknown_ready.ready.is_set.return_value = None
        for action, permit in (('revoke', ready), ('revoke', granted), ('revoke', None),
                               ('collect', DeletePermit()), (None, DeletePermit()),
                               ('revoke', unknown), ('revoke', unknown_ready)):
            with self.subTest(action=action, permit=permit):
                result = _worker_failure(RevocationNotSubmitted(), action, permit)
                self.assertEqual(result['error_type'], 'GuardError')
                self.assertNotEqual(result['message'], str(RevocationNotSubmitted()))

    def test_untyped_worker_failure_is_never_promoted_from_its_text(self):
        result = _worker_failure(GuardError(str(RevocationNotSubmitted())), 'revoke', DeletePermit())
        self.assertEqual(result['error_type'], 'GuardError')


class ExpiredDeliveryTests(unittest.TestCase):
    """Deterministic message delivery checks without creating any process."""
    def pending_service(self, action='collect', *, approved=False):
        service = object.__new__(IsolatedBrowserService)
        service._lock = threading.RLock()
        service._closed = service._interrupting = False
        service._cleanup_error = None
        service._session, service._worker_pid = 'fixture-session', 123
        service._timeouts = {'collect': 90, 'revoke': 65, 'inspect_revoke': 90}
        service._progress = {}
        service._log = Mock()
        service._kill_session = Mock()
        service._interrupt_waiters = []
        future = Future()
        future.set_running_or_notify_cancel()
        service._active = _Task(1, action, {}, future, started=100,
                                started_wall=1000, decision_allowed=approved)
        queued = Future()
        service._pending = deque([_Task(2, 'collect', {}, queued)])
        return service, future, queued

    def deliver(self, service, *, monotonic, wall, result=None):
        with patch('quota_guard.browser_process.time.monotonic', return_value=monotonic), \
                patch('quota_guard.browser_process.time.time', return_value=wall):
            service._message('fixture-session', {'kind': 'result', 'session': 'fixture-session',
                             'task': 1, 'ok': True, 'result': result or {'totals': 'fixture'}})

    def deliver_failure(self, service, *, monotonic=101, wall=1001,
                        error_type='RevocationNotSubmitted', session='fixture-session', task=1,
                        reason_code=None):
        with patch('quota_guard.browser_process.time.monotonic', return_value=monotonic), \
                patch('quota_guard.browser_process.time.time', return_value=wall):
            service._message(session, {'kind': 'result', 'session': session, 'task': task,
                                      'ok': False, 'error_type': error_type,
                                      'reason_code': reason_code,
                                      'message': 'untrusted detail must not authorize a retry'})

    def test_parent_preserves_matching_preparation_failure_with_fixed_message(self):
        service, future, queued = self.pending_service('revoke')
        service._active.permit = DeletePermit()
        self.deliver_failure(service)
        self.assertIsInstance(future.exception(), RevocationNotSubmitted)
        self.assertEqual(str(future.exception()), str(RevocationNotSubmitted()))
        self.assertFalse(queued.done())

    def test_inspection_submit_has_no_permit_or_lock_and_rejects_mutation_inputs(self):
        service, _, _ = self.pending_service('inspect_revoke')
        service._wake, service._counter = Mock(), 0
        args = {'name': 'Synthetic target', 'tracking_id': '00000000-0000-0000-0000-000000000001'}
        future = service.submit('inspect_revoke', **args)
        queued = service._pending[-1]
        self.assertIs(queued.future, future)
        self.assertIsNone(queued.permit)
        self.assertEqual(queued.target_lock_names, ())
        self.assertEqual(queued.kwargs, args)
        count = len(service._pending)
        for unsafe in ({'permit': DeletePermit()}, {'target_lock_names': ()}, {'extra': 'value'}):
            with self.subTest(unsafe=unsafe):
                rejected = service.submit('inspect_revoke', **args, **unsafe)
                self.assertIsInstance(rejected.exception(), KeyInspectionError)
                self.assertEqual(len(service._pending), count)

    def test_parent_inspection_success_is_only_matching_safe_result_and_never_a_permit(self):
        args = {'name': 'Synthetic target', 'tracking_id': '00000000-0000-0000-0000-000000000001'}
        result = {**args, 'preflight_passed': True, 'key_count': 2}
        service, future, _ = self.pending_service('inspect_revoke')
        service._active.kwargs = args
        self.deliver(service, monotonic=101, wall=1001, result=result)
        self.assertEqual(future.result(), result)
        for bad in ({**result, 'name': 'Wrong target'}, {**result, 'authorization': 'private-value'}):
            with self.subTest(bad=bad):
                service, future, _ = self.pending_service('inspect_revoke')
                service._active.kwargs = args
                self.deliver(service, monotonic=101, wall=1001, result=bad)
                self.assertIsInstance(future.exception(), KeyInspectionError)
                self.assertNotIn('private-value', str(future.exception()))
        service, future, _ = self.pending_service('inspect_revoke')
        service._active.kwargs, service._active.permit = args, DeletePermit()
        self.deliver(service, monotonic=101, wall=1001, result=result)
        self.assertIs(type(future.exception()), GuardError)

    def test_parent_inspection_failure_reconstructs_fixed_code_for_correct_task_only(self):
        for error_type, code, expected in (
            ('KeyInspectionError', 'KEY_TARGET_READ_FAILED', 'KEY_TARGET_READ_FAILED'),
            ('KeyInspectionError', 'Bearer private-value', 'PREPARATION_FAILED'),
            ('KeyInspectionError', {'private': 'value'}, 'PREPARATION_FAILED'),
            ('GuardError', 'KEY_TARGET_READ_FAILED', 'PREPARATION_FAILED'),
            ('RevocationNotSubmitted', 'KEY_TARGET_READ_FAILED', 'PREPARATION_FAILED'),
        ):
            with self.subTest(error_type=error_type, code=code):
                service, future, _ = self.pending_service('inspect_revoke')
                self.deliver_failure(service, error_type=error_type, reason_code=code)
                self.assertIsInstance(future.exception(), KeyInspectionError)
                self.assertEqual(future.exception().reason_code, expected)
                self.assertEqual(str(future.exception()), str(KeyInspectionError(reason_code=expected)))
                self.assertNotIn('untrusted detail', str(future.exception()))
        service, future, _ = self.pending_service('inspect_revoke')
        self.deliver_failure(service, error_type='KeyInspectionError', session='stale-session')
        self.deliver_failure(service, error_type='KeyInspectionError', task=2)
        self.assertFalse(future.done())

    def test_inspection_failure_cannot_reclassify_an_active_revocation(self):
        service, future, _ = self.pending_service('revoke', approved=True)
        service._active.permit = DeletePermit()
        self.deliver_failure(service, error_type='KeyInspectionError', reason_code='KEY_AUTH_HEADER_MISSING')
        self.assertIs(type(future.exception()), GuardError)
        self.assertNotIn('预检没有执行删除', str(future.exception()))

    def test_late_inspection_success_cannot_outlive_its_read_only_deadline(self):
        service, future, queued = self.pending_service('inspect_revoke')
        args = {'name': 'Synthetic target', 'tracking_id': '00000000-0000-0000-0000-000000000001'}
        service._active.kwargs = args
        result = {**args, 'preflight_passed': True, 'key_count': 2}
        self.deliver(service, monotonic=191, wall=1091, result=result)
        self.assertIs(type(future.exception()), GuardError)
        self.assertIn('超时', str(future.exception()))
        self.assertIsNotNone(queued.exception())
        service._kill_session.assert_called_once()

    def test_parent_reconstructs_reason_and_rejects_unknown_codes(self):
        for code in ('KEY_HEADER_READ_FAILED', 'Bearer private-value', {'secret': 'value'}):
            with self.subTest(code=code):
                service, future, _ = self.pending_service('revoke')
                service._active.permit = DeletePermit()
                self.deliver_failure(service, reason_code=code)
                expected = code if code == 'KEY_HEADER_READ_FAILED' else 'PREPARATION_FAILED'
                self.assertIsInstance(future.exception(), RevocationNotSubmitted)
                self.assertEqual(future.exception().reason_code, expected)
                self.assertEqual(str(future.exception()), str(RevocationNotSubmitted(reason_code=expected)))

    def test_known_reason_does_not_downgrade_after_permit_ready(self):
        service, future, _ = self.pending_service('revoke')
        service._active.permit = DeletePermit()
        service._active.permit_ready = True
        self.deliver_failure(service, reason_code='KEY_AUTH_HEADER_MISSING')
        self.assertIs(type(future.exception()), GuardError)
        self.assertIn('未知', str(future.exception()))

    def test_parent_downgrades_claim_after_any_ready_send_or_approval_evidence(self):
        for condition in ('permit_ready', 'decision_sent', 'decision_allowed', 'allowed',
                          'missing_permit', 'unknown_allowed', 'unknown_ready', 'unknown_sent',
                          'unknown_decision'):
            with self.subTest(condition=condition):
                service, future, _ = self.pending_service('revoke')
                task = service._active
                task.permit = DeletePermit()
                if condition == 'allowed':
                    task.permit.decide(True)
                elif condition == 'missing_permit':
                    task.permit = None
                elif condition == 'unknown_allowed':
                    task.permit = Mock(allowed=None)
                elif condition.startswith('unknown_'):
                    field = {'unknown_ready': 'permit_ready', 'unknown_sent': 'decision_sent',
                             'unknown_decision': 'decision_allowed'}[condition]
                    setattr(task, field, None)
                else:
                    setattr(task, condition, True)
                self.deliver_failure(service)
                self.assertIs(type(future.exception()), GuardError)
                self.assertIn('结果未知', str(future.exception()))

    def test_other_actions_and_unknown_error_types_cannot_claim_non_submission(self):
        for action, error_type in (('collect', 'RevocationNotSubmitted'),
                                   ('revoke', 'GuardError'), ('revoke', 'UnknownFailure')):
            with self.subTest(action=action, error_type=error_type):
                service, future, _ = self.pending_service(action)
                service._active.permit = DeletePermit()
                self.deliver_failure(service, error_type=error_type)
                self.assertIs(type(future.exception()), GuardError)

    def test_non_submission_claim_must_match_current_session_and_task(self):
        service, future, _ = self.pending_service('revoke')
        service._active.permit = DeletePermit()
        self.deliver_failure(service, session='stale-session')
        self.deliver_failure(service, task=2)
        self.assertFalse(future.done())
        self.deliver_failure(service)
        self.assertIsInstance(future.exception(), RevocationNotSubmitted)

    def test_late_or_interrupted_non_submission_claim_remains_unknown(self):
        service, future, _ = self.pending_service('revoke')
        service._active.permit = DeletePermit()
        self.deliver_failure(service, monotonic=165)
        self.assertIs(type(future.exception()), GuardError)
        self.assertIn('未知', str(future.exception()))
        service, future, _ = self.pending_service('revoke')
        service._active.permit = DeletePermit()
        service._interrupting = True
        self.deliver_failure(service)
        self.assertFalse(future.done())
        service._abort('操作已中断')
        self.assertIs(type(future.exception()), GuardError)
        self.assertIn('未知', str(future.exception()))

    def test_cancelled_result_cannot_clear_an_approved_revoke(self):
        for condition in ('decision_allowed', 'permit_allowed', 'denied'):
            with self.subTest(condition=condition):
                service, future, _ = self.pending_service('revoke')
                task = service._active
                task.permit = DeletePermit()
                if condition == 'decision_allowed':
                    task.decision_allowed = True
                elif condition == 'permit_allowed':
                    task.permit.decide(True)
                else:
                    task.permit.decide(False)
                self.deliver(service, monotonic=101, wall=1001, result={'cancelled': True})
                if condition == 'denied':
                    self.assertEqual(future.result(), {'cancelled': True})
                else:
                    self.assertIs(type(future.exception()), GuardError)
                    self.assertIn('未知', str(future.exception()))
    def test_same_task_expired_read_cannot_win_before_deadline_check(self):
        service, future, queued = self.pending_service()
        self.deliver(service, monotonic=190, wall=1001)
        with self.assertRaisesRegex(GuardError, '超时'):
            future.result()
        self.assertTrue(queued.done())
        service._kill_session.assert_called_once()

    def test_wall_expiry_rejects_read_when_monotonic_did_not_advance_during_sleep(self):
        service, future, queued = self.pending_service()
        self.deliver(service, monotonic=100.1, wall=1090)
        with self.assertRaisesRegex(GuardError, '超时'):
            future.result()
        self.assertTrue(queued.done())

    def test_backwards_wall_change_invalidates_read(self):
        service, future, _ = self.pending_service()
        self.deliver(service, monotonic=100.1, wall=999)
        with self.assertRaises(GuardError):
            future.result()

    def test_late_approved_revoke_remains_unknown_and_settles_audit_before_interrupt_ack(self):
        service, future, queued = self.pending_service('revoke', approved=True)
        audit = []
        future.add_done_callback(lambda completed: audit.append(completed.exception()))
        interrupted = Future()
        service._interrupt_waiters.append(interrupted)
        completion_order = []
        interrupted.add_done_callback(lambda completed: completion_order.append(
            (future.done(), queued.done(), len(audit))))
        self.deliver(service, monotonic=165, wall=1001, result={'absent': True})
        with self.assertRaisesRegex(GuardError, '未知.*禁止自动重试'):
            future.result()
        self.assertIsInstance(audit[0], GuardError)
        self.assertIsNone(interrupted.result())
        self.assertEqual(completion_order, [(True, True, 1)])
        self.deliver(service, monotonic=166, wall=1002, result={'cancelled': True})
        self.assertEqual(len(audit), 1)

    def test_fresh_matching_read_still_completes(self):
        service, future, queued = self.pending_service()
        self.deliver(service, monotonic=101, wall=1001)
        self.assertEqual(future.result(), {'totals': 'fixture'})
        self.assertFalse(queued.done())
        service._kill_session.assert_not_called()

    def test_final_grant_is_clipped_to_both_task_deadlines_without_renewal(self):
        task = _Task(1, 'revoke', {}, Future(), started=100, started_wall=1000)
        grant = {'token': 'a' * 32, 'issued_monotonic': 164, 'expires_monotonic': 169,
                 'issued_wall': 1064, 'expires_wall': 1069}
        with patch('quota_guard.browser_process.time.monotonic', return_value=164.5), \
                patch('quota_guard.browser_process.time.time', return_value=1064.75):
            bounded = _bound_grant_to_task(grant, task, 65)
        self.assertEqual(bounded['expires_monotonic'], 165)
        self.assertEqual(bounded['expires_wall'], 1065)
        self.assertEqual(bounded['issued_monotonic'], 164)
        self.assertEqual(bounded['issued_wall'], 1064)
        self.assertEqual(grant['expires_monotonic'], 169, 'Do not mutate the historical approval')

    def test_task_expiring_between_export_and_send_rejects_the_grant(self):
        task = _Task(1, 'revoke', {}, Future(), started=100, started_wall=1000)
        grant = {'token': 'a' * 32, 'issued_monotonic': 164, 'expires_monotonic': 169,
                 'issued_wall': 1064, 'expires_wall': 1069}
        for monotonic, wall in ((165, 1064.1), (164.1, 1065), (99, 1064.1), (164.1, 999)):
            with self.subTest(monotonic=monotonic, wall=wall), \
                    patch('quota_guard.browser_process.time.monotonic', return_value=monotonic), \
                    patch('quota_guard.browser_process.time.time', return_value=wall):
                with self.assertRaisesRegex(GuardError, '待核实'):
                    _bound_grant_to_task(grant, task, 65)

    def test_protected_action_waits_for_matching_witness_ack_before_dispatch(self):
        service, _, _ = self.pending_service('revoke')
        task = service._active
        task.target_lock_names = ('Global\\DeepSeekQuotaGuard.VerifiedTarget.v1.' + 'a' * 64,)
        service._witness_outbox, service._outbox = Queue(), Queue()
        service._send_action()
        self.assertTrue(service._outbox.empty())
        self.assertFalse(task.dispatched)
        request = service._witness_outbox.get_nowait()
        self.assertEqual(request['names'], task.target_lock_names)
        response = {'kind': 'targets_retained', 'session': service._session, 'task': 1, 'ok': True}
        with patch('quota_guard.browser_process.time.monotonic', return_value=101), \
                patch('quota_guard.browser_process.time.time', return_value=1001):
            service._message(service._session, response)  # Browser channel cannot acknowledge it.
            self.assertTrue(service._outbox.empty())
            service._message(('witness', service._session), {**response, 'task': 2})
            self.assertTrue(service._outbox.empty())
            service._message(('witness', service._session), response)
        action = service._outbox.get_nowait()
        self.assertEqual(action['kind'], 'action')
        self.assertNotIn('target_lock_names', action['kwargs'])
        self.assertTrue(task.dispatched)

    def test_interrupt_before_dispatch_keeps_acknowledged_action_unsent(self):
        service, _, _ = self.pending_service('revoke')
        service._active.lock_refs_retained = True
        service._witness_outbox, service._outbox = Queue(), Queue()
        service._interrupting = True
        service._send_action()
        self.assertTrue(service._outbox.empty())
        self.assertFalse(service._active.dispatched)


if __name__ == '__main__':
    unittest.main()
