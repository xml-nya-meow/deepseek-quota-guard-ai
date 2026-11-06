"""Evidence-based recovery of this application's interrupted browser profiles.

A short-lived witness holds only its own Job and the original parent process.
It records an empty Job before exiting. A missing named Job is NOT exit proof:
Windows can remove an object's name while references to the object still exist.
Unmarked, incomplete or uncertain directories are deliberately left untouched.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time

from .core import GuardError
from .target_lock import TargetLockReferences, validate_lock_names


MARKER = '.quota-browser-owner.json'
_PRODUCT = 'deepseek-quota-guard'
_SESSION = re.compile(r'[0-9a-f]{32}')


def job_name(session):
    if not isinstance(session, str) or not _SESSION.fullmatch(session):
        raise GuardError('浏览器会话标识无效，未启动浏览器。')
    return 'Local\\DeepSeekQuotaGuard-' + session


def plain_directory(path):
    """Check the full path, including ancestors; do not follow reparse points."""
    path = Path(path).absolute()
    for component in (path, *path.parents):
        info = component.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & 0x400):
            raise GuardError('浏览器临时路径含链接或重解析点，未清理。')
    info = path.lstat()
    if info.st_ino <= 0:
        raise GuardError('浏览器临时目录缺少可靠文件身份，未清理。')
    return [info.st_dev, info.st_ino]


def _plain_file(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or getattr(info, 'st_file_attributes', 0) & 0x400 or info.st_nlink != 1):
        raise GuardError('浏览器所有权记录不是独立普通文件，未清理。')
    return info


def _process_record(value):
    return (isinstance(value, dict) and set(value) == {'pid', 'created'}
            and type(value['pid']) is int and 0 < value['pid'] <= 0xffffffff
            and type(value['created']) is int and value['created'] > 0)


def read_marker(directory):
    directory = Path(directory)
    # Reject a linked candidate before opening anything underneath it.
    root_identity, directory_identity = plain_directory(directory.parent), plain_directory(directory)
    path = directory / MARKER
    info = _plain_file(path)
    if info.st_size > 4096:
        raise GuardError('浏览器所有权记录过大，未清理。')
    with path.open('r', encoding='utf-8') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise GuardError('浏览器所有权记录已变化，未清理。')
        content = stream.read(4097)
        if len(content) > 4096:
            raise GuardError('浏览器所有权记录过大，未清理。')
        value = json.loads(content)
    fields = {'schema', 'product', 'session', 'root_identity', 'directory_identity',
              'parent', 'worker', 'witness', 'job', 'state'}
    if (not isinstance(value, dict) or set(value) != fields or type(value['schema']) is not int
            or value['schema'] != 1
            or value['product'] != _PRODUCT or value['session'] != directory.name
            or value['job'] != job_name(directory.name) or value['state'] not in {'armed', 'settled'}
            or not all(_process_record(value[key]) for key in ('parent', 'worker', 'witness'))
            or value['root_identity'] != root_identity
            or value['directory_identity'] != directory_identity
            or root_identity != plain_directory(directory.parent)
            or directory_identity != plain_directory(directory)):
        raise GuardError('浏览器所有权或目录身份不符，未清理。')
    return value


def write_marker(directory, value, *, previous=None):
    """Publish only in the original ordinary directory, before admitting work."""
    directory = Path(directory)
    if (plain_directory(directory.parent) != value['root_identity']
            or plain_directory(directory) != value['directory_identity']):
        raise GuardError('浏览器临时目录身份已变化，未写入所有权记录。')
    marker = directory / MARKER
    if previous is not None:
        if read_marker(directory) != previous:
            raise GuardError('浏览器所有权记录已变化，未确认清理完成。')
    elif marker.exists() or marker.is_symlink():
        raise GuardError('浏览器所有权记录已存在，未放行会话。')
    staging = directory / (MARKER + '.new')
    # An interrupted staging file is not reused or followed.
    with staging.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=True, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    if (plain_directory(directory.parent) != value['root_identity']
            or plain_directory(directory) != value['directory_identity']):
        raise GuardError('浏览器临时目录身份已变化，未发布所有权记录。')
    if previous is not None and read_marker(directory) != previous:
        raise GuardError('浏览器所有权记录已变化，未发布退出证明。')
    os.replace(staging, marker)


def ownership(directory, parent, worker, witness):
    directory = Path(directory)
    return {'schema': 1, 'product': _PRODUCT, 'session': directory.name,
            'root_identity': plain_directory(directory.parent),
            'directory_identity': plain_directory(directory), 'parent': parent,
            'worker': worker, 'witness': witness, 'job': job_name(directory.name),
            'state': 'armed'}


class WindowsLifetime:
    """Narrow Win32 interface; no process enumeration or foreign termination."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes, self.wintypes = ctypes, wintypes
        api = self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'OpenProcess': ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            'GetProcessTimes': ([wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4, wintypes.BOOL),
            'WaitForSingleObject': ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            'CloseHandle': ([wintypes.HANDLE], wintypes.BOOL),
            'OpenJobObjectW': ([wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
            'TerminateJobObject': ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
            'QueryInformationJobObject': ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                           wintypes.DWORD, ctypes.c_void_p], wintypes.BOOL),
        }
        for name, (arguments, result) in signatures.items():
            getattr(api, name).argtypes = arguments
            getattr(api, name).restype = result

        class Accounting(ctypes.Structure):
            _fields_ = [(name, ctypes.c_int64) for name in
                        ('user', 'kernel', 'period_user', 'period_kernel')]
            _fields_ += [(name, wintypes.DWORD) for name in
                         ('faults', 'total', 'active', 'terminated')]
        self.accounting = Accounting

    def process(self, pid):
        return self.api.OpenProcess(0x00100000 | 0x1000, False, pid)

    def creation(self, handle):
        fields = [self.wintypes.FILETIME() for _ in range(4)]
        if not self.api.GetProcessTimes(handle, *(self.ctypes.byref(value) for value in fields)):
            raise OSError('GetProcessTimes')
        return (fields[0].dwHighDateTime << 32) | fields[0].dwLowDateTime

    def record(self, pid):
        handle = self.process(pid)
        if not handle:
            raise GuardError('无法核对本次浏览器进程身份，未放行会话。')
        try:
            return {'pid': pid, 'created': self.creation(handle)}
        finally:
            self.api.CloseHandle(handle)

    def exited(self, record):
        handle = self.process(record['pid'])
        if not handle:
            # PID 0 is excluded by the marker schema. Other failures, including
            # access denied, do not establish that the original process exited.
            return self.ctypes.get_last_error() == 87  # ERROR_INVALID_PARAMETER
        try:
            if self.creation(handle) != record['created']:
                return False  # PID reuse is conservatively retained.
            return self.api.WaitForSingleObject(handle, 0) == 0
        finally:
            self.api.CloseHandle(handle)

    def settle_job(self, handle):
        if not self.api.TerminateJobObject(handle, 1):
            raise OSError('TerminateJobObject')
        deadline = time.monotonic() + 1.5
        while True:
            info = self.accounting()
            if not self.api.QueryInformationJobObject(handle, 1, self.ctypes.byref(info),
                                                      self.ctypes.sizeof(info), None):
                raise OSError('QueryInformationJobObject')
            if info.active == 0:
                return
            if time.monotonic() >= deadline:
                raise GuardError('浏览器进程树尚未确认退出，保留临时目录。')
            time.sleep(.01)


def witness_main(connection, parent, name, directory):
    """Hold this Job until its tree is empty; never delete files in this child."""
    probe = WindowsLifetime()
    parent_handle = job = None
    record = None
    references = None
    try:
        parent_handle = probe.process(parent['pid'])
        if (not parent_handle or probe.creation(parent_handle) != parent['created']
                or probe.api.WaitForSingleObject(parent_handle, 0) != 258):
            return
        job = probe.api.OpenJobObjectW(0x0004 | 0x0008, False, name)
        if not job:
            return
        references = TargetLockReferences()
        connection.send({'kind': 'ready', 'witness': probe.record(os.getpid())})
        while True:
            state = probe.api.WaitForSingleObject(parent_handle, 50)
            if state == 0:
                break
            if state != 258:
                return  # Unknown owner state: finally must prove the Job empty.
            try:
                if not connection.poll():
                    continue
                message = connection.recv()
            except (EOFError, OSError):
                break
            if message.get('kind') == 'arm':
                candidate = read_marker(Path(directory))
                if (candidate != message.get('record') or candidate['parent'] != parent
                        or candidate['witness'] != probe.record(os.getpid()) or candidate['job'] != name):
                    return
                record = candidate
                connection.send({'kind': 'armed'})
            elif message.get('kind') == 'retain_targets':
                if record is None or message.get('session') != record['session']:
                    continue
                task = message.get('task')
                if type(task) is not int or task <= 0:
                    continue
                try:
                    references.retain(validate_lock_names(message.get('names')))
                    retained = True
                except (GuardError, OSError, ValueError, TypeError):
                    retained = False
                connection.send({'kind': 'targets_retained', 'session': record['session'],
                                 'task': task, 'ok': retained})
            elif message.get('kind') == 'finish':
                break
    except (GuardError, OSError, ValueError, TypeError, KeyError):
        # No receipt is preferable to guessing that cleanup succeeded.
        pass
    finally:
        if job:
            # Target references must survive the UI owner until the complete
            # browser tree is gone. A timeout/query failure cannot release them.
            # The parent reports cleanup failure while this helper retries.
            while True:
                try:
                    probe.settle_job(job)
                    break
                except (GuardError, OSError):
                    time.sleep(.25)
            if record is not None:
                try:
                    write_marker(Path(directory), {**record, 'state': 'settled'}, previous=record)
                except (GuardError, OSError, ValueError, TypeError, KeyError):
                    pass
            if references is not None:
                while True:
                    try:
                        references.close()
                        break
                    except (GuardError, OSError):
                        time.sleep(.25)
            probe.api.CloseHandle(job)
        if parent_handle:
            probe.api.CloseHandle(parent_handle)
        connection.close()


@contextmanager
def _recovery_lock(root):
    """Coordinate only scanners of this root, across application instances."""
    path = root / '.quota-recovery.lock'
    if path.exists() or path.is_symlink():
        _plain_file(path)
    with path.open('a+b') as stream:
        info = _plain_file(path)
        opened = os.fstat(stream.fileno())
        if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
            raise GuardError('临时目录回收锁已变化，保留遗留文件。')
        if os.name == 'nt':
            import msvcrt
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def resolved_profile_path(path):
    """One canonical comparison spelling after strict filesystem resolution.

    Python may retain the Win32 extended prefix only for long descendants.
    Normalize that namespace spelling for boundary comparison, not for I/O.
    """
    resolved = Path(path).resolve(strict=True)
    if os.name != 'nt':
        return resolved
    value = str(resolved)
    if value[:8].upper() == '\\\\?\\UNC\\':
        return Path('\\\\' + value[8:])
    if value.startswith('\\\\?\\'):
        ordinary = value[4:]
        if (len(ordinary) >= 3 and ordinary[0].isascii() and ordinary[0].isalpha()
                and ordinary[1:3] == ':\\'):
            return Path(ordinary)
        raise GuardError('浏览器临时路径命名空间不受支持，未清理。')
    return resolved


def profile_access_directory(directory):
    """Shorter access spelling, with original identity/boundary still checked.

    Node can create deep profile files through an 8.3 session alias even when
    Windows APIs cannot open their full long spelling. Never relocate content
    or substitute an unrelated directory merely to make cleanup succeed.
    """
    from .browser_startup import owned_temp_path_alias

    directory = Path(directory).absolute()
    identity = plain_directory(directory)
    boundary = resolved_profile_path(directory)
    access = Path(owned_temp_path_alias(str(directory)))
    if (plain_directory(access) != identity or resolved_profile_path(access) != boundary
            or not os.path.samefile(directory, access)
            or plain_directory(directory) != identity):
        raise GuardError('浏览器临时目录访问别名与原目录身份不符，未清理。')
    return access


def _safe_tree(directory):
    directory = Path(directory)
    # Reject linked ancestors before canonicalizing. A legal Windows 8.3 alias
    # has the same identity but cannot be compared lexically with resolved children.
    access = profile_access_directory(directory)
    boundary = resolved_profile_path(directory)
    pending = [access]
    while pending:
        current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                if (stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                        or not resolved_profile_path(entry.path).is_relative_to(boundary)):
                    raise GuardError('遗留临时目录含链接或越界路径，未清理。')
                if stat.S_ISDIR(info.st_mode):
                    pending.append(Path(entry.path))


def remove_profile_tree(directory):
    """Keep the receipt until all profile contents are gone, so retry is safe."""
    directory = Path(directory)
    parent_identity, identity = plain_directory(directory.parent), plain_directory(directory)
    _safe_tree(directory)
    access = profile_access_directory(directory)
    for child in access.iterdir():
        if child.name == MARKER:
            continue
        if (plain_directory(directory.parent) != parent_identity
                or plain_directory(directory) != identity):
            raise GuardError('临时目录在清理期间已变化，未继续删除。')
        info = child.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise GuardError('临时文件含链接或重解析点，未继续删除。')
        if stat.S_ISDIR(info.st_mode):
            shutil.rmtree(child)
        else:
            child.unlink()
    if (plain_directory(directory.parent) != parent_identity
            or plain_directory(directory) != identity):
        raise GuardError('临时目录在清理期间已变化，未移除所有权记录。')
    marker = access / MARKER
    if marker.exists() or marker.is_symlink():
        _plain_file(marker)
        marker.unlink()
    access.rmdir()


def recover_profiles(root, *, probe=None):
    """Scan this exact application root only; receipt + dead owners are required."""
    root = Path(root).absolute()
    summary = {'removed': 0, 'retained': 0}
    identity = plain_directory(root)
    candidates = [path for path in root.iterdir() if _SESSION.fullmatch(path.name)]
    if not candidates:
        return summary
    if probe is None:
        if os.name != 'nt':
            return summary
        probe = WindowsLifetime()
    try:
        with _recovery_lock(root):
            for directory in candidates:
                try:
                    record = read_marker(directory)
                    if record['state'] != 'settled' or not all(
                            probe.exited(record[key]) for key in ('parent', 'worker', 'witness')):
                        summary['retained'] += 1
                        continue
                    _safe_tree(directory)
                    if plain_directory(root) != identity or read_marker(directory) != record:
                        raise GuardError('遗留临时目录身份已变化，未清理。')
                    remove_profile_tree(directory)
                    summary['removed'] += 1
                except (GuardError, OSError, ValueError, TypeError, KeyError):
                    summary['retained'] += 1
    except (GuardError, OSError):
        summary['retained'] += 1
    return summary
