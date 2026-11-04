"""Bounded local transport check before Python's Playwright event loop starts.

On Windows the event loop's socketpair fallback may block in accept when local
TCP is unavailable. Do not call socketpair here: exercise only our own IPv4
loopback listener with explicit timeouts and no platform/account traffic.
"""
from contextlib import ExitStack
import errno
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
