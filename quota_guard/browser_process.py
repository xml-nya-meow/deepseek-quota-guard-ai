"""Bounded, per-instance process isolation for the visible platform browser.

The parent and its short-lived exit witness hold the Windows Job handles. The
spawned browser interpreter cannot start Playwright until it has joined that
Job and the witness is armed. No browser objects, cookies, or login state cross
the process boundary.
"""
from __future__ import annotations

from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass
import multiprocessing
import os
import re
from pathlib import Path
from queue import Empty, Queue
import signal
import stat
import tempfile
import threading
import time
import uuid

from .core import AccountMismatch, GuardError, _safe_detail
from .revocation_permit import DeletePermit, KeyInspectionError, RevocationNotSubmitted
from .operator_status import AI_COLLECTION_REASONS, OperatorCollectionError
from .target_lock import validate_lock_names
from .profile_recovery import (WindowsLifetime, job_name, ownership, plain_directory, profile_access_directory,
                               read_marker, recover_profiles, remove_profile_tree, resolved_profile_path,
                               witness_main, write_marker)


def _operation_fields(task):
    value = task.kwargs.get('operation_id') if task and task.action == 'collect' else None
    return {'operation_id': value} if isinstance(value, str) and re.fullmatch('[0-9a-f]{32}', value) else {}


_TIMEOUTS = {'login': 45, 'collect': 600, 'read_keys': 45, 'prepare_revoke': 45,
             'inspect_revoke': 90, 'verify_revoke': 45, 'revoke': 240, 'selftest': 30}
_STAGES = {'login': '正在打开登录窗口', 'collect': '正在核对平台费用',
           'read_keys': '正在只读核验账户与 Key 列表',
           'prepare_revoke': '正在定位待处理人员', 'verify_revoke': '正在核实平台状态',
           'inspect_revoke': '正在只读检查删除准备',
           'revoke': '正在核实并处理额度', 'selftest': '正在检查浏览器运行环境'}


class _WindowsJob:
    """A non-inheritable kill-on-close Job; never enumerate named processes."""
    def __init__(self, pid, *, name):
        import ctypes
        from ctypes import wintypes

        class BASIC(ctypes.Structure):
            _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64),
                        ('PerJobUserTimeLimit', ctypes.c_int64),
                        ('LimitFlags', wintypes.DWORD),
                        ('MinimumWorkingSetSize', ctypes.c_size_t),
                        ('MaximumWorkingSetSize', ctypes.c_size_t),
                        ('ActiveProcessLimit', wintypes.DWORD),
                        ('Affinity', ctypes.c_size_t),
                        ('PriorityClass', wintypes.DWORD),
                        ('SchedulingClass', wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ('ReadOperationCount', 'WriteOperationCount', 'OtherOperationCount',
                         'ReadTransferCount', 'WriteTransferCount', 'OtherTransferCount')]

        class EXTENDED(ctypes.Structure):
            _fields_ = [('BasicLimitInformation', BASIC), ('IoInfo', IO),
                        ('ProcessMemoryLimit', ctypes.c_size_t),
                        ('JobMemoryLimit', ctypes.c_size_t),
                        ('PeakProcessMemoryUsed', ctypes.c_size_t),
                        ('PeakJobMemoryUsed', ctypes.c_size_t)]

        class ACCOUNTING(ctypes.Structure):
            _fields_ = [(name, ctypes.c_int64) for name in
                        ('TotalUserTime', 'TotalKernelTime', 'ThisPeriodTotalUserTime', 'ThisPeriodTotalKernelTime')]
            _fields_ += [(name, wintypes.DWORD) for name in
                         ('TotalPageFaultCount', 'TotalProcesses', 'ActiveProcesses', 'TotalTerminatedProcesses')]

        api = ctypes.WinDLL('kernel32', use_last_error=True)
        api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        api.CreateJobObjectW.restype = wintypes.HANDLE
        api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                               ctypes.c_void_p, wintypes.DWORD]
        api.SetInformationJobObject.restype = wintypes.BOOL
        api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        api.OpenProcess.restype = wintypes.HANDLE
        api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        api.AssignProcessToJobObject.restype = wintypes.BOOL
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL
        api.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        api.TerminateJobObject.restype = wintypes.BOOL
        api.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                 ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p]
        api.QueryInformationJobObject.restype = wintypes.BOOL
        self._accounting = ACCOUNTING
        self._ctypes = ctypes
        self.api, self.handle = api, None
        ctypes.set_last_error(0)
        handle = api.CreateJobObjectW(None, name)
        if not handle:
            raise GuardError('无法创建独立浏览器进程隔离，未启动浏览器。')
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS: never adopt it.
            api.CloseHandle(handle)
            raise GuardError('浏览器隔离标识已被占用，未放行浏览器启动。')
        self.name = name
        self.handle = handle
        process = None
        try:
            info = EXTENDED()
            info.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
            if not api.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                raise OSError('SetInformationJobObject')
            process = api.OpenProcess(0x0100 | 0x0001, False, pid)
            if not process or not api.AssignProcessToJobObject(handle, process):
                raise OSError('AssignProcessToJobObject')
        except Exception:
            self.close()
            raise GuardError('无法隔离本次浏览器进程，未放行浏览器启动。') from None
        finally:
            if process:
                api.CloseHandle(process)

    def close(self):
        if self.handle is not None:
            handle = self.handle
            if not self.api.TerminateJobObject(handle, 1):
                raise GuardError('无法终止本次浏览器进程树，清理尚未完成。')
            deadline = time.monotonic() + 1.5
            while True:
                info = self._accounting()
                if not self.api.QueryInformationJobObject(handle, 1, self._ctypes.byref(info),
                                                         self._ctypes.sizeof(info), None):
                    raise GuardError('无法核实本次浏览器进程树已退出，清理尚未完成。')
                if info.ActiveProcesses == 0:
                    break
                if time.monotonic() >= deadline:
                    raise GuardError('本次浏览器进程树未在限定时间内退出，请再次中断清理。')
                time.sleep(.01)
            if not self.api.CloseHandle(handle):
                raise GuardError('无法释放本次浏览器隔离句柄，清理尚未完成。')
            self.handle = None


class _PrimedConnection:
    """Replay the admitted handshake after setting child-only temporary paths."""
    def __init__(self, connection, first):
        self.connection, self.first = connection, first

    def recv(self):
        if self.first is not None:
            first, self.first = self.first, None
            return first
        return self.connection.recv()

    def __getattr__(self, name):
        return getattr(self.connection, name)


def _bootstrap(connection, target):
    if os.name != 'nt':
        os.setsid()
    gate = connection.recv()
    if not isinstance(gate, dict) or gate.get('kind') != 'start':
        return
    directory = gate.get('temp_dir')
    if not isinstance(directory, str) or not Path(directory).is_dir():
        raise GuardError('本次浏览器临时目录不可用，未启动浏览器。')
    from .browser_startup import owned_temp_path_alias
    transport_directory = owned_temp_path_alias(directory)
    # This interpreter and its descendants alone inherit these settings. Node
    # uses TEMP/TMP on Windows; Python's cached selection is replaced as well.
    # A verified 8.3 alias still names this exact owned directory. Keep the gate
    # and parent ownership/cleanup records in their original canonical form.
    for name in ('TEMP', 'TMP', 'TMPDIR'):
        os.environ[name] = transport_directory
    tempfile.tempdir = transport_directory
    target(_PrimedConnection(connection, gate))


def _worker_failure(exc, action, permit):
    """Preserve definite pre-submission failure only before this worker's gate."""
    from .operator_runtime import OperatorError
    if isinstance(exc, OperatorError):
        code = getattr(exc, 'reason_code', None)
        if action == 'collect' and permit is None and isinstance(code, str) and code in AI_COLLECTION_REASONS:
            safe_error = OperatorCollectionError(code)
            return {'ok': False, 'error_type': 'OperatorCollectionError',
                    'message': str(safe_error), 'reason_code': safe_error.reason_code}
        return {'ok': False, 'error_type': 'GuardError',
                'message': '本地 AI 错误状态与当前操作不一致，未采用本次结果；请保留原记录核查。'}
    if action == 'read_keys':
        return {'ok': False,
                'error_type': 'AccountMismatch' if isinstance(exc, AccountMismatch) and permit is None else 'GuardError',
                'message': '只读 Key 核验未完成；请核实登录账户及已有绑定，本次未取得可用身份。'}
    if action == 'inspect_revoke' and permit is not None:
        return {'ok': False, 'error_type': 'GuardError',
                'message': '只读预检的执行状态不符合约定，未采用本次结果。'}
    if action == 'inspect_revoke':
        safe_error = KeyInspectionError(reason_code=(
            exc.reason_code if isinstance(exc, KeyInspectionError) else 'PREPARATION_FAILED'))
        return {'ok': False, 'error_type': 'KeyInspectionError',
                'message': str(safe_error), 'reason_code': safe_error.reason_code}
    if (isinstance(exc, RevocationNotSubmitted) and action == 'revoke'
            and permit is not None and permit.ready.is_set() is False and permit.allowed is False):
        safe_error = RevocationNotSubmitted(reason_code=getattr(exc, 'reason_code', None))
        return {'ok': False, 'error_type': 'RevocationNotSubmitted',
                'message': str(safe_error), 'reason_code': safe_error.reason_code}
    return {'ok': False,
            'error_type': 'AccountMismatch' if isinstance(exc, AccountMismatch) else 'GuardError',
            'message': (_safe_detail(exc) if isinstance(exc, GuardError)
                        and not isinstance(exc, (RevocationNotSubmitted, KeyInspectionError)) else
                        '平台操作未完成，请重新核实会话；未确认任何删除成功。')}


def _inspection_result(value, expected):
    """Child and parent independently constrain this read-only IPC payload."""
    fields = {'preflight_passed', 'name', 'tracking_id', 'key_count'}
    if (not isinstance(value, dict) or set(value) != fields
            or value.get('preflight_passed') is not True
            or not isinstance(value.get('name'), str) or not 0 < len(value['name']) <= 512
            or not isinstance(value.get('tracking_id'), str) or len(value['tracking_id']) != 36
            or type(value.get('key_count')) is not int or not 2 <= value['key_count'] <= 200
            or value['name'] != expected.get('name')
            or value['tracking_id'] != expected.get('tracking_id')):
        raise KeyInspectionError()
    return {field: value[field] for field in fields}


def _identity_read_result(value, expected):
    """No fee evidence, write result or secret may cross this read-only route."""
    from .browser_service import UUID_PATTERN, check_identity, identity_read_args
    from .core import clean_name
    args = identity_read_args(expected)
    if (not isinstance(value, dict) or set(value) != {'keys', 'account_id'}
            or not isinstance(value.get('keys'), list) or not 1 <= len(value['keys']) <= 200):
        raise GuardError('只读 Key 核验结果不符合约定，未采用本次结果。')
    keys = []
    for key in value['keys']:
        if (not isinstance(key, dict) or set(key) != {'name', 'tracking_id'}
                or not isinstance(key.get('name'), str) or not key['name']
                or clean_name(key['name']) != key['name']
                or not isinstance(key.get('tracking_id'), str)
                or not UUID_PATTERN.fullmatch(key['tracking_id'])):
            raise GuardError('只读 Key 核验结果含无效身份，未采用本次结果。')
        keys.append({'name': key['name'], 'tracking_id': key['tracking_id']})
    if len({key['tracking_id'].lower() for key in keys}) != len(keys):
        raise GuardError('只读 Key 核验结果含重复身份，未采用本次结果。')
    identity = check_identity(keys, **args)
    if value.get('account_id') != identity:
        raise AccountMismatch('只读 Key 核验的账户标识不一致，未采用本次结果。')
    return {'keys': keys, 'account_id': identity}


def _browser_worker(connection):
    """IPC runs on this thread; all Playwright calls stay on its own worker."""
    from .browser_service import BrowserService
    gate = connection.recv()
    if not isinstance(gate, dict) or gate.get('kind') != 'start':
        return
    session = gate['session']
    service = BrowserService(diagnostics_dir=gate.get('diagnostics_dir'))
    service._log('isolated_session_started', session=session, generation=gate.get('generation'),
                 parent_pid=os.getppid(), pid=os.getpid())
    active = None
    active_action = None
    permit = None
    ready_sent = False
    stage = None
    connection.send({'kind': 'ready', 'session': session})
    try:
        while True:
            if connection.poll(.03):
                message = connection.recv()
                if not isinstance(message, dict) or message.get('session') != session:
                    continue
                if message.get('kind') == 'action':
                    if active is not None:
                        raise RuntimeError('Concurrent browser command')
                    action, task = message['action'], message['task']
                    active_action = action
                    service._log('isolated_action_started', session=session, generation=gate.get('generation'),
                                 task_id=task, action=action, pid=os.getpid())
                    kwargs = message['kwargs'] if action == 'read_keys' else dict(message['kwargs'])
                    permit, ready_sent = None, False
                    if action == 'read_keys':
                        from .browser_service import identity_read_args
                        try:
                            kwargs = identity_read_args(kwargs)
                        except GuardError as exc:
                            connection.send({'kind': 'result', 'session': session, 'task': task,
                                             **_worker_failure(exc, action, None)})
                            active_action = None
                            continue
                    if action == 'revoke':
                        permit = DeletePermit(target_tracking_id=kwargs['tracking_id'])
                        kwargs['permit'] = permit
                    stage = _STAGES.get(action, '正在操作平台')
                    connection.send({'kind': 'progress', 'session': session,
                                     'task': task, 'stage': stage})
                    active = (task, service.submit(action, **kwargs))
                elif (message.get('kind') == 'permit_decision' and active is not None
                      and message.get('task') == active[0] and permit is not None
                      and ready_sent):
                    if message.get('allowed') is True:
                        fresh = permit.accept_grant(message.get('grant'))
                        if not fresh:
                            service._log('revocation_permit_expired_or_duplicate', session=session,
                                         task_id=active[0])
                    else:
                        permit.decide(False)
            if active is None:
                continue
            task, future = active
            if permit is not None and permit.ready.is_set() and not ready_sent:
                ready_sent = True
                connection.send({'kind': 'permit_ready', 'session': session, 'task': task,
                                 'prepared_inventory': permit.prepared_inventory(kwargs['tracking_id'])})
            current = getattr(service, 'progress', None)
            if isinstance(current, dict) and isinstance(current.get('stage'), str) and current['stage'] != stage:
                stage = current['stage']
                connection.send({'kind': 'progress', 'session': session, 'task': task, 'stage': stage})
            if not future.done():
                continue
            try:
                result = future.result()
                if active_action == 'inspect_revoke':
                    result = _inspection_result(result, kwargs)
                elif active_action == 'read_keys':
                    result = _identity_read_result(result, kwargs)
                message = {'ok': True, 'result': result}
            except Exception as exc:
                message = _worker_failure(exc, active_action, permit)
            connection.send({'kind': 'result', 'session': session, 'task': task, **message})
            active, active_action, permit, stage = None, None, None, None
    except (EOFError, BrokenPipeError, OSError):
        pass
    finally:
        if permit is not None:
            permit.decide(False)
        connection.close()
        # The owner closes the Job and all descendants. Do not wait here for a
        # potentially blocked Playwright thread or transfer browser ownership.


@dataclass
class _Task:
    number: int
    action: str
    kwargs: dict
    future: Future
    permit: object = None
    started: float = 0
    started_wall: float = 0
    permit_ready: bool = False
    decision_sent: bool = False
    decision_allowed: bool = False
    target_lock_names: tuple = ()
    lock_refs_requested: bool = False
    lock_refs_retained: bool = False
    dispatched: bool = False


def _bound_grant_to_task(grant, task, timeout):
    """The final DOM lease may never outlive either original task deadline."""
    bounded = dict(grant)
    for clock, started, now in (('monotonic', task.started, time.monotonic()),
                                ('wall', task.started_wall, time.time())):
        issued = bounded['issued_' + clock]
        expires = min(bounded['expires_' + clock], started + timeout)
        if not started or now < started or now < issued or expires <= now or expires <= issued:
            raise GuardError('撤销任务期限已到或时钟变化，结果待核实；禁止自动重试。')
        bounded['expires_' + clock] = expires
    return bounded


class IsolatedBrowserService:
    def __init__(self, diagnostics_folder=None, *, diagnostics_dir=None,
                 action_timeouts=None, _worker_target=None):
        self._lock = threading.RLock()
        self._pending = deque()
        self._messages = Queue()
        self._wake = threading.Event()
        self._stopped = threading.Event()
        self._interrupt_waiters = []
        self._interrupting = False
        self._closed = False
        self._cleanup_error = None
        self._counter = 0
        self._generation = 0
        self._active = None
        self._process = self._job = self._connection = self._outbox = None
        self._witness = self._witness_connection = self._ownership = None
        self._witness_outbox = None
        self._job_empty_confirmed = False
        self._worker_pid = None
        self._temp_root = None
        self._temp_root_identity = None
        self._owns_temp_root = False
        self._session_directory = None
        self._session_directory_identity = None
        self._session = None
        self._ready = False
        self._progress = {'stage': '等待操作', 'started_at': time.monotonic()}
        self._timeouts = dict(_TIMEOUTS)
        if action_timeouts:
            for action, seconds in action_timeouts.items():
                if action not in self._timeouts or not isinstance(seconds, (int, float)) or not 0 < seconds <= 600:
                    raise ValueError('浏览器动作超时设置无效。')
                self._timeouts[action] = seconds
        self._target = _worker_target or _browser_worker
        self._logger = None
        folder = diagnostics_dir if diagnostics_dir is not None else diagnostics_folder
        self._diagnostics_dir = str(Path(folder)) if folder is not None else None
        if folder is not None:
            from .diagnostics import DiagnosticLog
            self._logger = DiagnosticLog(Path(folder))
        self._thread = threading.Thread(target=self._monitor, name='isolated-browser-monitor', daemon=True)
        self._thread.start()

    @property
    def progress(self):
        with self._lock:
            return dict(self._progress)

    @property
    def interrupting(self):
        with self._lock:
            return self._interrupting

    @property
    def closed(self):
        with self._lock:
            return self._closed

    @property
    def cleanup_failed(self):
        with self._lock:
            return self._cleanup_error is not None

    def _log(self, event, **fields):
        if self._logger is not None:
            try:
                task = self._active
                context = {'pid': os.getpid(), 'worker_pid': self._worker_pid,
                           'generation': self._generation, 'session': self._session,
                           'action': task.action if task else None, 'task_id': task.number if task else None}
                context.update(_operation_fields(task))
                self._logger.emit(event, **{**context, **fields})
            except Exception:
                pass

    def submit(self, action, **kwargs):
        future = Future()
        with self._lock:
            if self._closed or self._interrupting or self._cleanup_error is not None:
                future.set_exception(GuardError('浏览器正在关闭或清理，请等待完成后重新登录。'))
                return future
            if action not in _TIMEOUTS:
                future.set_exception(GuardError('不支持的浏览器动作。'))
                return future
            if action == 'read_keys':
                from .browser_service import identity_read_args
                try:
                    kwargs = identity_read_args(kwargs)
                except GuardError as exc:
                    future.set_exception(exc)
                    return future
            permit = kwargs.pop('permit', None)
            if action == 'revoke' and not isinstance(permit, DeletePermit):
                future.set_exception(GuardError('撤销缺少父进程许可，未执行操作。'))
                return future
            names = kwargs.pop('target_lock_names', None)
            if action == 'inspect_revoke' and (permit is not None or names is not None
                                               or set(kwargs) != {'name', 'tracking_id'}):
                future.set_exception(KeyInspectionError())
                return future
            try:
                if names is not None or (os.name == 'nt' and action in {'revoke', 'prepare_revoke'}):
                    names = validate_lock_names(names)
                else:
                    names = ()
            except GuardError as exc:
                future.set_exception(exc)
                return future
            self._counter += 1
            self._pending.append(_Task(self._counter, action, kwargs, future, permit,
                                       target_lock_names=names))
        self._wake.set()
        return future

    def interrupt(self):
        future = Future()
        with self._lock:
            if self._stopped.is_set():
                if self._cleanup_error is None:
                    future.set_result(None)
                    return future
                # A monitor failure must not make retained ownership impossible
                # to clean. Explicit interruption may restart only the cleaner.
                self._stopped.clear()
                self._thread = threading.Thread(target=self._monitor, name='isolated-browser-cleaner', daemon=True)
                restart = True
            else:
                restart = False
            self._interrupting = True
            self._interrupt_waiters.append(future)
            if restart:
                self._thread.start()
        self._wake.set()
        return future

    def close(self):
        with self._lock:
            self._closed = True
            # Publish closing and its interrupt together. Otherwise the monitor
            # could observe closed before interrupting and strand command Futures.
            completion = self.interrupt()
        # The monitor never performs blocking IPC. Its only waits are bounded
        # process joins; the Job handle closes before those waits begin.
        if threading.current_thread() is not self._thread:
            try:
                completion.result(timeout=6)
            except TimeoutError:
                raise GuardError('浏览器清理仍未完成，不能确认关闭成功。') from None
            if not self._stopped.wait(1):
                raise GuardError('浏览器监控尚未退出，不能确认关闭成功。')

    def _spawn(self):
        context = multiprocessing.get_context('spawn')
        parent, child = context.Pipe(duplex=True)
        process = context.Process(target=_bootstrap, args=(child, self._target),
                                  name='quota-platform-browser', daemon=True)
        self._connection, self._process = parent, process
        self._session = uuid.uuid4().hex
        self._job_empty_confirmed = False
        self._generation += 1
        self._ready = False
        try:
            self._prepare_session_directory()
            process.start()
            self._worker_pid = process.pid
            child.close()
            if os.name == 'nt':
                self._job = _WindowsJob(process.pid, name=job_name(self._session))
                self._start_witness(context)
        except Exception:
            child.close()
            raise
        session = self._session
        outbox = self._outbox = Queue()

        def reader():
            try:
                while True:
                    message = parent.recv()
                    self._messages.put((session, message))
                    self._wake.set()
            except (EOFError, OSError):
                self._messages.put((session, {'kind': 'transport_error', 'session': session}))
                self._wake.set()

        def writer():
            try:
                while True:
                    message = outbox.get()
                    if message is None:
                        return
                    parent.send(message)
            except (EOFError, OSError, TypeError, ValueError):
                self._messages.put((session, {'kind': 'transport_error', 'session': session}))
                self._wake.set()

        threading.Thread(target=reader, name='browser-ipc-reader', daemon=True).start()
        threading.Thread(target=writer, name='browser-ipc-writer', daemon=True).start()
        with self._lock:
            if not self._interrupting and not self._closed:
                outbox.put({'kind': 'start', 'session': session, 'generation': self._generation,
                            'diagnostics_dir': self._diagnostics_dir,
                            'temp_dir': str(self._session_directory)})
        self._log('browser_process_started')

    def _start_witness(self, context):
        """Do not admit the worker until its independent exit witness is armed."""
        deadline = time.monotonic() + 3
        probe = WindowsLifetime()
        parent_record, worker_record = probe.record(os.getpid()), probe.record(self._worker_pid)
        parent, child = context.Pipe(duplex=True)
        self._witness_connection = parent
        witness = self._witness = context.Process(
            target=witness_main, args=(child, parent_record, self._job.name, str(self._session_directory)),
            name='quota-profile-exit-witness', daemon=True)
        try:
            witness.start()
        finally:
            child.close()

        def reply():
            while time.monotonic() < deadline:
                with self._lock:
                    if self._interrupting or self._closed:
                        raise GuardError('浏览器启动已中断，未放行会话。')
                if parent.poll(min(.05, max(0, deadline - time.monotonic()))):
                    return parent.recv()
            raise GuardError('浏览器退出见证进程未就绪，未放行浏览器启动。')

        ready = reply()
        expected = probe.record(witness.pid)
        if ready != {'kind': 'ready', 'witness': expected}:
            raise GuardError('浏览器退出见证进程身份不符，未放行浏览器启动。')
        record = ownership(self._session_directory, parent_record, worker_record, expected)
        write_marker(self._session_directory, record)
        self._ownership = record
        parent.send({'kind': 'arm', 'record': record})
        if reply() != {'kind': 'armed'}:
            raise GuardError('浏览器退出见证记录未确认，未放行浏览器启动。')
        session = self._session
        outbox = self._witness_outbox = Queue()

        def reader():
            try:
                while True:
                    message = parent.recv()
                    self._messages.put((('witness', session), message))
                    self._wake.set()
            except (EOFError, OSError):
                self._messages.put((('witness', session), {'kind': 'transport_error', 'session': session}))
                self._wake.set()

        def writer():
            try:
                while True:
                    message = outbox.get()
                    if message is None:
                        return
                    parent.send(message)
            except (EOFError, OSError, TypeError, ValueError):
                self._messages.put((('witness', session), {'kind': 'transport_error', 'session': session}))
                self._wake.set()

        threading.Thread(target=reader, name='profile-witness-reader', daemon=True).start()
        threading.Thread(target=writer, name='profile-witness-writer', daemon=True).start()

    def _finish_witness(self):
        witness = getattr(self, '_witness', None)
        connection = getattr(self, '_witness_connection', None)
        outbox = getattr(self, '_witness_outbox', None)
        if outbox is not None:
            outbox.put(None)
            self._witness_outbox = None
        if connection is not None:
            # Closing the sole parent endpoint requests finish, including when
            # a bounded monitor cannot wait for a blocked background writer.
            connection.close()
            self._witness_connection = None
        if witness is not None and witness.pid is not None:
            witness.join(timeout=2)
            if witness.is_alive():
                raise GuardError('浏览器退出见证进程尚未结束，保留临时目录并等待再次清理。')
            witness.close()
        self._witness = None
        if getattr(self, '_ownership', None) is not None:
            try:
                record = read_marker(self._session_directory)
                if record == self._ownership and self._job_empty_confirmed:
                    write_marker(self._session_directory, {**record, 'state': 'settled'}, previous=record)
                    record = read_marker(self._session_directory)
                if record != {**self._ownership, 'state': 'settled'}:
                    raise GuardError('浏览器完整退出证明尚未保存，保留临时目录。')
            except (GuardError, OSError, ValueError, TypeError, KeyError):
                if not self._job_empty_confirmed:
                    raise
                # The living parent has observed ActiveProcesses == 0 and joined
                # both children. A receipt-write failure cannot revoke that
                # proof; current-session deletion still checks directory IDs.
                self._log('browser_profile_receipt_unavailable', status='in_memory_exit_proof',
                          message='已确认本次进程退出；持久退出记录不可用，将仅清理已记录的本次目录。')
            # If rmtree partly succeeds, its first removed file may be the
            # receipt. Retrying this same owned directory must remain possible.
            self._ownership = None

    def _send_action(self):
        with self._lock:
            task = self._active
            if (self._closed or self._interrupting or self._cleanup_error is not None
                    or task is None or task.dispatched):
                return
            if task.target_lock_names and not task.lock_refs_retained:
                if self._witness_outbox is None:
                    raise GuardError('目标互斥退出保护不可用，未执行平台动作。')
                if not task.lock_refs_requested:
                    task.lock_refs_requested = True
                    self._witness_outbox.put({'kind': 'retain_targets', 'session': self._session,
                                              'task': task.number, 'names': task.target_lock_names})
                return
            task.dispatched = True
            self._outbox.put({'kind': 'action', 'session': self._session, 'task': task.number,
                              'action': task.action, 'kwargs': task.kwargs})
        self._log('browser_action_started', action=task.action, task_id=task.number)

    @staticmethod
    def _plain_directory(path):
        return tuple(plain_directory(path))

    def _prepare_session_directory(self):
        if self._session_directory is not None:
            raise GuardError('上次浏览器临时文件尚未清理，不能建立新会话。')
        if self._temp_root is None:
            if self._diagnostics_dir is None:
                root = Path(tempfile.mkdtemp(prefix='deepseek-quota-browser-'))
                self._owns_temp_root = True
            else:
                root = Path(self._diagnostics_dir).parent / '临时浏览器'
                root.mkdir(parents=True, exist_ok=True)
            self._plain_directory(root)
            self._temp_root = root.resolve(strict=True)
            self._temp_root_identity = self._plain_directory(self._temp_root)
        root = self._temp_root
        if self._plain_directory(root) != self._temp_root_identity or root.resolve(strict=True) != root:
            raise GuardError('浏览器临时目录根路径已变化，未建立会话。')
        # Retry on every new login: a previous witness may still have been
        # completing when this application first started. Never scan global TEMP.
        recovered = recover_profiles(root)
        self._log('browser_profile_recovery', count=recovered['removed'],
                  status='retained' if recovered['retained'] else 'complete',
                  message=('存在缺少完整退出或路径证明的遗留临时目录，已保留。'
                           if recovered['retained'] else '已核对本程序临时浏览器目录。'))
        directory = root / self._session
        directory.mkdir()
        self._session_directory = directory
        self._session_directory_identity = self._plain_directory(directory)
        if directory.resolve(strict=True).parent != root:
            raise GuardError('浏览器临时目录超出本程序记录范围，未启动浏览器。')

    def _remove_session_directory(self):
        directory, root = self._session_directory, self._temp_root
        if directory is not None:
            try:
                if root is None or directory.parent != root:
                    raise GuardError('浏览器临时目录不在本程序记录范围内，未执行删除。')
                if self._plain_directory(root) != self._temp_root_identity or root.resolve(strict=True) != root:
                    raise GuardError('浏览器临时目录根路径已变化，未执行删除。')
                if (self._plain_directory(directory) != self._session_directory_identity
                        or directory.resolve(strict=True) != directory):
                    raise GuardError('浏览器会话目录已被替换，未执行删除。')
                pending = [profile_access_directory(directory)]
                while pending:
                    current = pending.pop()
                    with os.scandir(current) as entries:
                        for entry in entries:
                            metadata = entry.stat(follow_symlinks=False)
                            if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, 'st_file_attributes', 0) & 0x400:
                                raise GuardError('浏览器临时文件含链接或重解析点，请处理后再次中断清理。')
                            child = Path(entry.path)
                            if not resolved_profile_path(child).is_relative_to(resolved_profile_path(directory)):
                                raise GuardError('浏览器临时文件路径越界，未执行删除。')
                            if stat.S_ISDIR(metadata.st_mode):
                                pending.append(child)
                # Recheck the recorded target and preserve its receipt until
                # profile contents are removed. Global TEMP is never enumerated.
                if (directory.resolve(strict=True).parent != root
                        or self._plain_directory(directory) != self._session_directory_identity):
                    raise GuardError('浏览器会话目录在清理期间发生变化，未执行删除。')
                remove_profile_tree(directory)
                if directory.exists() or directory.is_symlink():
                    raise GuardError('浏览器临时文件仍然存在，清理未完成。')
            except FileNotFoundError:
                # An already removed recorded session is idempotently clean;
                # missing descendants during traversal still require a retry.
                if directory.exists() or directory.is_symlink():
                    raise GuardError('浏览器临时文件清理期间发生变化，请再次中断清理。') from None
            except GuardError:
                raise
            except OSError:
                raise GuardError('本次浏览器临时文件未能删除，请关闭占用后再次中断清理。') from None
            self._session_directory = None
            self._session_directory_identity = None
        if self._owns_temp_root and root is not None:
            try:
                # Only remove our own empty root, never siblings or user files.
                if self._plain_directory(root) == self._temp_root_identity and root.resolve(strict=True) == root:
                    root.rmdir()
                    self._temp_root = self._temp_root_identity = None
            except OSError:
                pass

    def _kill_session(self):
        process, job, connection, outbox = self._process, self._job, self._connection, self._outbox
        # Keep ownership until every check succeeds, so a failed cleanup can be
        # retried and no replacement session can hide a live previous process.
        if connection is not None:
            connection.close()
        if outbox is not None:
            outbox.put(None)
        if job is not None:
            job.close()
            self._job_empty_confirmed = True
            self._job = None
        if process is not None and process.pid is not None:
            if os.name != 'nt':
                try:
                    if self._ready or os.getpgid(process.pid) == process.pid:
                        os.killpg(process.pid, signal.SIGKILL)
                    elif process.is_alive():
                        process.terminate()
                except (ProcessLookupError, PermissionError):
                    pass
            elif job is None and process.is_alive():
                process.terminate()  # No gate was sent if Job assignment failed.
            process.join(timeout=1.5)
            if process.is_alive():
                process.kill()
                process.join(timeout=1)
            if process.is_alive():
                raise GuardError('本次浏览器进程仍在运行，清理尚未完成。')
            process.close()
            self._process = None
        self._finish_witness()
        self._remove_session_directory()
        self._ownership = None
        self._process = self._job = self._connection = self._outbox = None
        self._session, self._ready = None, False

    def _abort(self, reason):
        with self._lock:
            self._interrupting = True
            active = self._active
            self._progress = {'stage': '正在清理本次浏览器进程',
                              'started_at': active.started if active else time.monotonic()}
            if active is not None and active.permit is not None:
                active.permit.decide(False)
        log_context = {'session': self._session, 'worker_pid': self._worker_pid,
                       'action': active.action if active else None, 'task_id': active.number if active else None,
                       **_operation_fields(active)}
        cleanup_error = None
        try:
            self._kill_session()
        except Exception as exc:
            cleanup_error = exc if isinstance(exc, GuardError) else GuardError('本次浏览器进程清理未完成，请再次中断清理。')
        with self._lock:
            self._cleanup_error = cleanup_error
            tasks = ([self._active] if self._active else []) + list(self._pending)
            self._active = None
            self._pending.clear()
            # Finish every command before acknowledging interruption to the UI.
            for task in tasks:
                if not task.future.done():
                    detail = ('撤销结果未知，禁止自动重试；请重新登录后核实平台状态。'
                              if task is active and task.action == 'revoke' else
                              ('本次操作未完成；浏览器清理仍未完成，不能重新登录。' if cleanup_error else
                               '本次操作未完成；浏览器已清理，请重新登录后读取。'))
                    task.future.set_exception(GuardError(reason + '：' + detail))
            self._interrupting = False
            self._progress = {'stage': ('清理失败，请再次中断操作' if cleanup_error else
                                       '浏览器已关闭' if self._closed else '已清理，请重新登录'),
                              'started_at': time.monotonic()}
            # A command's done callback may itself request interruption. Include
            # those requests rather than leaving a waiter on an already reset job.
            waiters, self._interrupt_waiters = self._interrupt_waiters, []
            for future in waiters:
                if cleanup_error is None:
                    future.set_result(None)
                else:
                    future.set_exception(cleanup_error)
        self._log('browser_process_reset', reason=reason, cleanup_complete=cleanup_error is None, **log_context)

    def _message(self, outer_session, message):
        with self._lock:
            if self._interrupting or self._closed or self._cleanup_error is not None:
                return
        witness_message = outer_session == ('witness', self._session)
        if ((outer_session != self._session and not witness_message) or not isinstance(message, dict)
                or message.get('session') != self._session):
            return
        # A queued message can survive suspension longer than the operation's
        # deadline. Inspect it before ready/result delivery, not after draining.
        if self._active is not None and self._task_expired(self._active):
            self._log('browser_expired_message', message_kind=message.get('kind'),
                      approved=self._active.decision_allowed)
            self._abort('平台操作超时，迟到结果未用于更新当前状态')
            return
        kind = message.get('kind')
        if witness_message:
            if kind == 'transport_error':
                self._abort('浏览器退出见证进程失去连接')
                return
            task = self._active
            if (kind == 'targets_retained' and task is not None and task.lock_refs_requested
                    and not task.lock_refs_retained and message.get('task') == task.number):
                if message.get('ok') is not True:
                    self._abort('目标互斥退出保护未确认，未执行平台动作')
                    return
                task.lock_refs_retained = True
                self._send_action()
            return
        if kind == 'transport_error':
            self._abort('浏览器进程失去连接')
            return
        if kind == 'ready':
            if not self._ready:
                self._ready = True
                if self._active is not None:
                    self._send_action()
            return
        task = self._active
        if task is None or message.get('task') != task.number:
            return
        if kind == 'progress' and isinstance(message.get('stage'), str):
            with self._lock:
                self._progress = {'stage': message['stage'][:100], 'started_at': task.started}
            self._log('browser_stage', stage=message['stage'][:100], action=task.action, task_id=task.number,
                      elapsed_ms=round((time.monotonic() - task.started) * 1000))
        elif kind == 'permit_ready' and task.permit is not None:
            try:
                inventory = message.get('prepared_inventory')
                if (task.action != 'revoke' or not isinstance(inventory, dict)
                        or set(inventory) != {'target_tracking_id', 'known_tracking_ids'}
                        or inventory['target_tracking_id'] != task.kwargs['tracking_id']):
                    raise GuardError('撤销准备与当前目标不一致。')
                task.permit.set_prepared_inventory(inventory['target_tracking_id'], inventory['known_tracking_ids'])
            except (KeyError, TypeError, GuardError):
                self._abort('撤销准备证据未通过核验，未授权提交')
                return
            task.permit_ready = True
            task.permit.ready.set()
        elif kind == 'result':
            with self._lock:
                # Linearize result delivery with the user's interruption. A
                # queued success must not override a prior interrupt request.
                if self._interrupting or self._closed:
                    return
                expired = self._task_expired(task)
                if not expired:
                    self._active = None
                    self._progress = {'stage': '等待操作', 'started_at': time.monotonic()}
                    if message.get('ok') is True:
                        result = message.get('result')
                        if task.action == 'read_keys':
                            try:
                                if (task.permit is not None or task.permit_ready is not False
                                        or task.decision_sent is not False or task.decision_allowed is not False
                                        or task.target_lock_names):
                                    raise GuardError('只读 Key 核验的执行状态不符合约定，未采用本次结果。')
                                task.future.set_result(_identity_read_result(result, task.kwargs))
                            except GuardError as exc:
                                task.future.set_exception(exc)
                        elif task.action == 'inspect_revoke':
                            try:
                                if (task.permit is not None or task.permit_ready is not False
                                        or task.decision_sent is not False or task.decision_allowed is not False):
                                    raise GuardError('只读预检的执行状态不符合约定，未采用本次结果。')
                                task.future.set_result(_inspection_result(result, task.kwargs))
                            except GuardError as exc:
                                task.future.set_exception(exc)
                        elif (task.action == 'revoke' and isinstance(result, dict)
                                and result.get('cancelled') is True
                                and (task.decision_allowed is True or
                                     (task.permit is not None and task.permit.allowed is True))):
                            task.future.set_exception(GuardError(
                                '撤销许可已经批准，不能以取消结果解除责任；结果未知，禁止自动重试。'))
                        elif (task.action == 'revoke' and isinstance(result, dict)
                                and result.get('absent') is True and not task.decision_allowed):
                            task.future.set_exception(GuardError('未找到父进程撤销许可，结果未知；禁止自动重试。'))
                        else:
                            task.future.set_result(result)
                    else:
                        if task.action == 'read_keys':
                            error_type = (AccountMismatch if message.get('error_type') == 'AccountMismatch'
                                          and task.permit is None and task.permit_ready is False
                                          and task.decision_sent is False and task.decision_allowed is False
                                          else GuardError)
                            error = error_type('只读 Key 核验未完成；请核实登录账户及已有绑定，本次未取得可用身份。')
                        elif task.action == 'inspect_revoke':
                            # Never accept text from either a typed or untyped
                            # worker exception. This action has no write permit.
                            if (task.permit is not None or task.permit_ready is not False
                                    or task.decision_sent is not False or task.decision_allowed is not False):
                                error = GuardError('只读预检的执行状态不符合约定，未采用本次结果。')
                            else:
                                code = (message.get('reason_code')
                                        if message.get('error_type') == 'KeyInspectionError' else None)
                                error = KeyInspectionError(reason_code=code)
                        elif message.get('error_type') == 'OperatorCollectionError':
                            code = message.get('reason_code')
                            if (task.action == 'collect' and task.permit is None
                                    and task.permit_ready is False and task.decision_sent is False
                                    and task.decision_allowed is False
                                    and isinstance(code, str) and code in AI_COLLECTION_REASONS):
                                error = OperatorCollectionError(code)
                            else:
                                error = GuardError('本地 AI 错误状态与当前操作不一致，未采用本次结果；请保留原记录核查。')
                        elif message.get('error_type') == 'KeyInspectionError':
                            error = GuardError('只读预检结果与当前动作不一致；当前结果未知，禁止自动重试撤销。')
                        elif message.get('error_type') == 'RevocationNotSubmitted':
                            if (task.action == 'revoke' and task.permit_ready is False
                                    and task.decision_sent is False and task.decision_allowed is False
                                    and task.permit is not None and task.permit.allowed is False):
                                error = RevocationNotSubmitted(reason_code=message.get('reason_code'))
                            else:
                                error = GuardError('撤销提交阶段无法确认，结果未知；禁止自动重试，请核实平台状态。')
                        else:
                            error_type = AccountMismatch if message.get('error_type') == 'AccountMismatch' else GuardError
                            error = error_type(_safe_detail(message.get('message', '平台操作未完成。')))
                        task.future.set_exception(error)
            if expired:
                self._abort('平台操作超时，迟到结果未用于更新当前状态')
                return
            self._log('browser_action_finished', action=task.action, task_id=task.number,
                      success=task.future.exception() is None, **_operation_fields(task))

    def _monitor(self):
        try:
            while True:
                self._wake.clear()
                launch = dispatch = False
                with self._lock:
                    interrupt = self._interrupting
                if interrupt:
                    self._abort('操作已中断')
                with self._lock:
                    if self._closed and self._cleanup_error is None:
                        break
                    current = self._active
                if current is not None and self._task_expired(current):
                    # Revoke errors retain the original durable intent through
                    # the UI callback; a late reply must not become cancellation.
                    self._abort('平台操作超时')
                try:
                    while True:
                        self._message(*self._messages.get_nowait())
                except Empty:
                    pass
                with self._lock:
                    if self._active is None and not self._interrupting and self._cleanup_error is None:
                        while self._pending:
                            task = self._pending.popleft()
                            if task.future.set_running_or_notify_cancel():
                                task.started = time.monotonic()
                                task.started_wall = time.time()
                                self._active = task
                                self._progress = {'stage': _STAGES[task.action], 'started_at': task.started}
                                if self._process is None:
                                    launch = True
                                elif self._ready:
                                    dispatch = True
                                break
                    task = self._active
                # Process creation and Job assignment must never hold the public
                # API lock: interrupt() must remain immediate during startup.
                if launch:
                    try:
                        self._spawn()
                    except Exception:
                        self._abort('无法启动独立浏览器进程')
                        task = None
                elif dispatch:
                    with self._lock:
                        if not self._interrupting and not self._closed:
                            self._send_action()
                if task is not None:
                    if self._task_expired(task):
                        self._abort('平台操作超时')
                    elif task.permit_ready and not task.decision_sent and task.permit.decided.is_set():
                        grant = None
                        if task.permit.allowed:
                            try:
                                grant = _bound_grant_to_task(task.permit.export_grant(), task,
                                                            self._timeouts[task.action])
                            except GuardError:
                                self._abort('撤销许可已过期或已使用')
                                continue
                        with self._lock:
                            if not self._interrupting and not self._closed:
                                task.decision_sent = True
                                task.decision_allowed = task.permit.allowed is True
                                self._outbox.put({'kind': 'permit_decision', 'session': self._session,
                                                  'task': task.number, 'allowed': task.decision_allowed,
                                                  'grant': grant})
                if self._process is not None and self._cleanup_error is None and not self._process.is_alive():
                    self._abort('浏览器进程异常结束')
                self._wake.wait(.025)
        except Exception:
            self._abort('浏览器隔离服务异常')
            with self._lock:
                self._closed = True
        finally:
            try:
                self._kill_session()
                self._cleanup_error = None
            except Exception:
                self._cleanup_error = GuardError('本次浏览器清理未完成，不能确认关闭成功。')
            with self._lock:
                waiters, self._interrupt_waiters = self._interrupt_waiters, []
                self._interrupting = False
                self._closed = True
                self._stopped.set()
                for future in waiters:
                    if not future.done():
                        if self._cleanup_error is None:
                            future.set_result(None)
                        else:
                            future.set_exception(self._cleanup_error)

    def _task_expired(self, task):
        # Wall time covers suspend on clocks that exclude it; monotonic covers
        # wall-clock rollback. Any backwards jump invalidates the operation.
        monotonic, wall = time.monotonic(), time.time()
        timeout = self._timeouts[task.action]
        return (not task.started or not task.started_wall
                or not 0 <= monotonic - task.started < timeout
                or not 0 <= wall - task.started_wall < timeout)
