"""Machine-wide exclusion for verified platform targets, independent of ledgers.

Only the caller can establish that a full Tracking ID belongs to its verified
platform session. These locks are not account identity or cross-device locks.
Keep them on the creating thread until all browser activity and action records
have settled. A session witness retains object references beyond owner exit
until its browser Job is empty, so a new instance cannot enter that exit gap.
"""
from dataclasses import dataclass
from hashlib import sha256
import os
import re
import threading

from .core import GuardError


_PREFIX = "Global\\DeepSeekQuotaGuard.VerifiedTarget.v1."
_EXISTS = 183
_NO_INITIAL_TARGETS = object()


class TargetLockError(GuardError):
    """A known competing owner, distinct from access/cleanup failures."""
    reason_code = 'TARGET_IN_USE'

    def __init__(self):
        super().__init__('同一平台目标正由本机另一个管理器或其退出清理进程占用；'
                         '请正常关闭旧版本或其他管理器，等待清理完成后重新读取费用。')


class TargetLockCleanupError(GuardError):
    """Keep the cleanup owner reachable for legacy single-step construction.

    GUI code uses two-phase acquisition instead. This local object must never
    cross IPC; close it on its creating thread after browser cleanup settles.
    """
    def __init__(self, cleanup_owner):
        self.cleanup_owner = cleanup_owner
        super().__init__('目标互斥清理未完成，请保持停止并关闭窗口重试清理。')


class _AcquireCleanupError(GuardError):
    def __init__(self, handle):
        self.handle = handle
        super().__init__('无法关闭冲突目标的互斥句柄，请退出本程序后重试。')


def _targets(values):
    if isinstance(values, (str, bytes)):
        raise GuardError("目标互斥需要完整 Tracking ID 集合。")
    try:
        values = tuple(values)
    except (TypeError, ValueError):
        raise GuardError("目标互斥需要完整 Tracking ID 集合。") from None
    if not values or any(not isinstance(value, str)
                         or not re.fullmatch(r"[A-Za-z0-9_-]{8,200}", value)
                         or value.lower().startswith("sk-") for value in values):
        raise GuardError("目标互斥缺少有效的完整 Tracking ID，已停止操作。")
    return frozenset(values)


def _name(tracking_id):
    # Never include a ledger path, account fingerprint, user name or raw ID.
    return _PREFIX + sha256(tracking_id.encode("ascii")).hexdigest()


def validate_lock_names(names):
    """Accept only this protocol's nonempty, fixed-namespace reference list."""
    if isinstance(names, (str, bytes)):
        raise GuardError("目标保护引用需要名称集合。")
    try:
        values = tuple(names)
    except (TypeError, ValueError):
        raise GuardError("目标保护引用需要名称集合。") from None
    if not values or any(not isinstance(name, str)
                         or not name.startswith(_PREFIX)
                         or not re.fullmatch(r"[0-9a-f]{64}", name[len(_PREFIX):])
                         for name in values):
        raise GuardError("目标保护引用名称无效，未放行平台操作。")
    return tuple(sorted(set(values)))


@dataclass
class _Handle:
    value: object
    owned: bool = True


class _WindowsMutexBackend:
    def __init__(self):
        if os.name != "nt":
            raise GuardError("此平台不支持 Windows 目标互斥，已停止管理。")
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.api = ctypes.WinDLL("kernel32", use_last_error=True)
        self.api.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.api.CreateMutexW.restype = wintypes.HANDLE
        self.api.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
        self.api.OpenMutexW.restype = wintypes.HANDLE
        self.api.ReleaseMutex.argtypes = [wintypes.HANDLE]
        self.api.ReleaseMutex.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.api.CloseHandle.restype = wintypes.BOOL

    def acquire(self, name):
        # Initial ownership applies only to a newly created object. Treat any
        # existing object as occupied, including an object owned by this same
        # thread: Windows mutex recursion must not admit a second ledger.
        self.ctypes.set_last_error(0)
        handle = self.api.CreateMutexW(None, True, name)
        error = self.ctypes.get_last_error()
        if not handle:
            raise GuardError("无法取得目标互斥；可能已有其他实例占用或系统拒绝访问。")
        if error == _EXISTS:
            # This reference never owns the competing mutex. If closing fails,
            # transfer it to TargetLock's rollback/close path, not the GC.
            try:
                closed = self.api.CloseHandle(handle)
            except Exception:
                closed = False
            if not closed:
                raise _AcquireCleanupError(_Handle(handle, owned=False)) from None
            raise TargetLockError()
        return _Handle(handle)

    def release(self, handle):
        # Preserve each stage on failure, so a later close can retry without
        # releasing a mutex twice or silently discarding an open handle.
        if handle.owned:
            if not self.api.ReleaseMutex(handle.value):
                raise GuardError("目标互斥尚未释放，请保持停止并退出本程序。")
            handle.owned = False
        if not self.api.CloseHandle(handle.value):
            raise GuardError("目标互斥句柄尚未关闭，请保持停止并退出本程序。")


    def open_reference(self, name):
        # A SYNCHRONIZE handle retains the named object without waiting for or
        # taking ownership. Never create a missing lock in the witness.
        handle = self.api.OpenMutexW(0x00100000, False, name)
        if not handle:
            raise GuardError("未能保留已取得的目标互斥引用，未放行平台操作。")
        return handle

    def close_reference(self, handle):
        if not self.api.CloseHandle(handle):
            raise GuardError("目标保护引用尚未关闭，请保留退出见证并重试。")


class TargetLock:
    """Nonblocking, all-or-nothing target acquisition on one owning thread.

    ``update`` acquires additions before releasing removals. A failed addition
    rolls back only new locks and preserves the original set. A cleanup failure
    disables coverage checks and further updates; ``close`` may still be retried.
    No destructor releases mutexes from an arbitrary garbage-collector thread.
    """
    def __init__(self, tracking_ids=_NO_INITIAL_TARGETS):
        targets = None if tracking_ids is _NO_INITIAL_TARGETS else _targets(tracking_ids)
        self._owner = threading.current_thread()
        self._handles = {}
        self._closed = False
        self._broken = False
        try:
            self._backend = _WindowsMutexBackend()
        except GuardError:
            raise
        except Exception:
            raise GuardError("无法建立 Windows 目标互斥，已停止管理。") from None
        if targets is not None:
            try:
                self.update(targets)
            except BaseException:
                if self._handles:
                    # Older callers can explicitly retry cleanup through the
                    # exception. New callers must hold self before update().
                    raise TargetLockCleanupError(self) from None
                raise

    def _check_thread(self):
        if threading.current_thread() is not self._owner:
            raise GuardError("目标互斥必须由建立它的界面线程持有和释放。")

    def _check_usable(self):
        self._check_thread()
        if self._closed or self._broken:
            raise GuardError("目标互斥已关闭或状态不明，请停止管理并退出本程序。")

    @property
    def tracking_ids(self):
        self._check_thread()
        return frozenset(self._handles)

    @property
    def names(self):
        """Export current protected object names for a session exit witness."""
        self._check_usable()
        return tuple(sorted(_name(target) for target in self._handles))

    def covers(self, tracking_ids):
        self._check_usable()
        return _targets(tracking_ids).issubset(self._handles)

    def _release(self, handles, targets):
        failed = False
        for target in sorted(targets):
            try:
                self._backend.release(handles[target])
            except Exception:
                failed = True
            else:
                del handles[target]
        return not failed

    def update(self, tracking_ids):
        self._check_usable()
        targets = _targets(tracking_ids)
        additions = {}
        try:
            for target in sorted(targets.difference(self._handles)):
                additions[target] = self._backend.acquire(_name(target))
        except BaseException as exc:
            if isinstance(exc, _AcquireCleanupError):
                additions[target] = exc.handle
            if not self._release(additions, tuple(additions)):
                self._handles.update(additions)
                self._broken = True
                raise GuardError("目标互斥回滚未完成，请停止管理并退出本程序。") from None
            if isinstance(exc, GuardError) or not isinstance(exc, Exception):
                raise
            raise GuardError("无法取得完整目标集合的互斥，已保留原有保护。") from None
        self._handles.update(additions)
        if not self._release(self._handles, set(self._handles).difference(targets)):
            self._broken = True
            raise GuardError("旧目标互斥未能完整释放，请停止管理并退出本程序。")

    def close(self):
        self._check_thread()
        if self._closed:
            return
        if not self._release(self._handles, tuple(self._handles)):
            self._broken = True
            raise GuardError("目标互斥未能完整关闭，请停止管理并退出本程序。")
        self._closed = True

    def __enter__(self):
        self._check_usable()
        return self

    def __exit__(self, *_args):
        self.close()


class TargetLockReferences:
    """Keep target names alive until the caller proves its browser Job empty.

    The witness must retain the complete requested set and acknowledge it before
    a protected task is dispatched. References are additive for that session;
    closing is permitted only after its process tree is confirmed empty. This
    object cannot establish that fact itself and never takes mutex ownership.
    """
    def __init__(self):
        self._owner = threading.current_thread()
        self._handles = {}
        self._closed = False
        self._broken = False
        try:
            self._backend = _WindowsMutexBackend()
        except GuardError:
            raise
        except Exception:
            raise GuardError("无法建立目标保护引用，未放行平台操作。") from None

    def _check_thread(self):
        if threading.current_thread() is not self._owner:
            raise GuardError("目标保护引用必须由建立它的见证线程维护。")

    def _check_usable(self):
        self._check_thread()
        if self._closed or self._broken:
            raise GuardError("目标保护引用已关闭或状态不明，未放行平台操作。")

    @property
    def names(self):
        self._check_usable()
        return tuple(sorted(self._handles))

    def _release(self, handles):
        failed = False
        for name in tuple(handles):
            try:
                self._backend.close_reference(handles[name])
            except Exception:
                failed = True
            else:
                del handles[name]
        return not failed

    def retain(self, names):
        self._check_usable()
        names = validate_lock_names(names)
        additions = {}
        try:
            for name in names:
                if name not in self._handles:
                    additions[name] = self._backend.open_reference(name)
        except BaseException as exc:
            if not self._release(additions):
                self._handles.update(additions)
                self._broken = True
                raise GuardError("目标保护引用回滚未完成，未放行平台操作。") from None
            if isinstance(exc, GuardError) or not isinstance(exc, Exception):
                raise
            raise GuardError("无法保留完整目标集合，未放行平台操作。") from None
        self._handles.update(additions)
        return self.names

    def close(self):
        """Caller must first confirm Job ActiveProcesses == 0; retry on failure."""
        self._check_thread()
        if self._closed:
            return
        if not self._release(self._handles):
            self._broken = True
            raise GuardError("目标保护引用未能完整关闭，请保留见证并重试。")
        self._closed = True
