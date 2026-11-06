"""Bounded local transport check before Python's Playwright event loop starts.

On Windows the event loop's socketpair fallback may block in accept when local
TCP is unavailable. Do not call socketpair here: exercise only our own IPv4
loopback listener with explicit timeouts and no platform/account traffic.
"""
from contextlib import ExitStack
import errno
import os
import re
import socket
import time

from .core import GuardError


LOOPBACK_TIMEOUT_SECONDS = 2.0
_KNOWN_ERROR_CODES = frozenset({
    errno.EACCES, errno.EPERM, errno.EINVAL, errno.EMFILE, errno.ENFILE,
    errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT, errno.ENETUNREACH,
    errno.EHOSTUNREACH, errno.ECONNREFUSED, errno.ETIMEDOUT,
    10013, 10024, 10047, 10049, 10051, 10060, 10061, 10065,
})


class BrowserStartupError(GuardError):
    reason_code = 'BROWSER_LOOPBACK_UNAVAILABLE'

    def __init__(self, error_code=None):
        self.error_code = (error_code if type(error_code) is int and error_code in _KNOWN_ERROR_CODES
                           else 'TIMEOUT' if error_code == 'TIMEOUT' else 'UNKNOWN')
        super().__init__(
            '独立浏览器未启动：本机回环通信（127.0.0.1）不可用或超时，'
            '浏览器驱动无法建立内部连接。本次未访问平台、未执行删除。'
            '请恢复本机网络访问条件后重新点击“登录”重试；本程序不会修改系统网络设置。')


class BrowserTempPathError(GuardError):
    reason_code = 'BROWSER_TEMP_PATH_TOO_LONG'
    error_code = 'ENAMETOOLONG'

    def __init__(self):
        super().__init__(
            '独立浏览器未启动：Chromium 报告本次临时目录路径过长，无法建立运行目录。'
            '本次未访问平台、未执行删除。建议使用较短路径的数据目录；'
            '已登记账本不要直接移动或清空，请保留原文件并先确认迁移方案。'
            '本程序不会修改系统长路径或短文件名设置。')


def _windows_short_path(directory):
    """Read an existing filesystem alias; never enable or manufacture one."""
    import ctypes
    from ctypes import wintypes

    api = ctypes.WinDLL('kernel32', use_last_error=True).GetShortPathNameW
    api.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    api.restype = wintypes.DWORD
    size = api(directory, None, 0)
    if not size:
        return None
    buffer = ctypes.create_unicode_buffer(size)
    count = api(directory, buffer, size)
    if not count or count >= size:
        return None
    return buffer.value


def owned_temp_path_alias(directory):
    """Use only a verified shorter spelling of the already-owned directory.

    Some Windows volumes do not provide 8.3 aliases. That is not an error and
    must not cause relocation to a shared/system temporary directory. The
    canonical ownership/cleanup path remains with the parent process.
    """
    if os.name != 'nt':
        return directory
    try:
        if not os.path.isabs(directory) or not os.path.isdir(directory):
            return directory
        alias = _windows_short_path(directory)
        if (not isinstance(alias, str) or not alias or len(alias) >= len(directory)
                or not os.path.isabs(alias) or not os.path.isdir(alias)
                or not os.path.samefile(directory, alias)):
            return directory
        return alias
    except (OSError, ValueError, OverflowError):
        return directory


def is_temp_path_too_long(exc):
    """Recognize actual OS/Node error codes, never infer from a path length."""
    if isinstance(exc, OSError) and (
            getattr(exc, 'errno', None) == errno.ENAMETOOLONG
            or getattr(exc, 'winerror', None) == 206):
        return True
    # Playwright carries Node's filesystem failure in its exception message.
    # Require the exact error-code token followed by ':' rather than matching
    # generic 'path too long' text or a directory containing that word.
    return re.search(r'(?<![A-Za-z0-9_])ENAMETOOLONG\s*:', str(exc)) is not None


def check_browser_loopback():
    """Connect and accept on one private listener under a shared two-second budget."""
    deadline = time.monotonic() + LOOPBACK_TIMEOUT_SECONDS

    def bounded(sock):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        sock.settimeout(remaining)

    try:
        with ExitStack() as stack:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            stack.callback(listener.close)
            bounded(listener)
            listener.bind(('127.0.0.1', 0))
            listener.listen(1)
            client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            stack.callback(client.close)
            bounded(client)
            client.connect(listener.getsockname())
            bounded(listener)
            accepted, _peer = listener.accept()
            stack.callback(accepted.close)
    except OSError as exc:
        code = 'TIMEOUT' if isinstance(exc, TimeoutError) else getattr(exc, 'winerror', None)
        if code is None:
            code = getattr(exc, 'errno', None)
        raise BrowserStartupError(code) from None
