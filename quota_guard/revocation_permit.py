"""A short-lived, single-use approval; an expired approval is not cancellation.

Both processes compare the original clocks. IPC never renews the grant, and the
browser must consume it immediately before its single HTTP submission (or the
legacy atomic check/click exercised only by compatibility tests).
"""
from __future__ import annotations

import math
import re
from threading import Event, Lock
import time
import uuid

from .core import GuardError


MAX_GRANT_SECONDS = 5.0
_CLOCK_FIELDS = ('issued_monotonic', 'expires_monotonic', 'issued_wall', 'expires_wall')

KEY_READ_REASONS = {
    'KEY_KIND_INVALID': 'Key HTTP 列表包含尚未适配的 Key 类型，无法完成列表核验。',
    'KEY_TARGET_KIND_UNSUPPORTED': '完整身份列表已核对，但所选 Key 的删除类型尚未适配；未执行删除。请提供本次诊断日志用于适配。',
    'KEY_TARGET_NOT_DELETABLE': '所选 Key 由 Harness 管理，平台不提供普通 Key 删除操作；未执行删除。',
    'KEY_HTTP_AUTH_REQUIRED': 'Key HTTP 请求授权失效或被拒绝，请重新核验登录。',
    'KEY_HTTP_REDIRECTED': 'Key HTTP 请求被重定向或返回地址不符。',
    'KEY_HTTP_FAILED': 'Key HTTP 请求未返回成功状态，请核验网络与平台状态。',
    'KEY_RESPONSE_TYPE_INVALID': 'Key HTTP 响应不是预期 JSON 类型。',
    'KEY_RESPONSE_SIZE_INVALID': 'Key HTTP 响应大小或长度声明不符合读取限制。',
    'KEY_RESPONSE_JSON_INVALID': 'Key HTTP 响应 JSON 无效、含重复字段或非有限数值。',
    'KEY_RESPONSE_STATUS_INVALID': 'Key HTTP 响应状态码字段未通过校验。',
    'KEY_RESPONSE_BUSINESS_INVALID': 'Key HTTP 响应业务状态或数据结构未通过校验。',
    'KEY_LIST_SCHEMA_INVALID': 'Key HTTP 响应未包含符合限制的完整列表。',
    'KEY_NAME_INVALID': 'Key HTTP 列表中的名称字段缺失或格式不符。',
    'KEY_TRACKING_ID_INVALID': 'Key HTTP 列表中的 Tracking ID 缺失或不完整。',
    'KEY_REDACTED_ID_INVALID': 'Key HTTP 列表中的脱敏标识字段 sensitive_id 不符合已观察结构。',
    'KEY_CREATED_AT_INVALID': 'Key HTTP 列表中的创建时间 created_at 不符合已观察的整数秒结构。',
    'KEY_LIST_DUPLICATE_IDENTITY': 'Key HTTP 完整列表存在重复身份。',
    'KEY_EXPECTED_LIST_INVALID': '本地已核验列表或选中目标无效。',
    'KEY_LIST_IDENTITY_MISMATCH': 'HTTP 与网页核验得到的完整 Key 身份列表不一致，请重新读取。',
    'KEY_TARGET_ALREADY_ATTEMPTED': '此会话已尝试提交该目标，禁止重复提交，请先核实结果。',
    'KEY_LIST_TRANSPORT_FAILED': '读取 Key HTTP 列表时发生连接、超时或读取异常。',
}

# Only these application-owned labels cross the worker boundary. Never forward
# an SDK error, request header, URL or server message as preparation diagnostics.
PREPARATION_REASONS = {
    **KEY_READ_REASONS,
    'PREPARATION_FAILED': '',
    'KEY_SESSION_UNAVAILABLE': '独立浏览器会话尚未就绪，请重新登录。',
    'KEY_SESSION_MISMATCH': '管理授权不属于当前独立浏览器会话。',
    'KEY_PAGE_CHANGED': '页面已离开预期平台路径，请核验登录状态。',
    'KEY_LIST_REQUEST_NOT_OBSERVED': '未观察到本次 Key 列表请求。',
    'KEY_READ_REQUEST_NOT_OBSERVED': '未观察到本次允许的只读授权请求。',
    'KEY_AUTH_HEADER_MISSING': '已观察到只读请求，但缺少管理授权头。',
    'KEY_AUTH_HEADER_INVALID': '已观察到只读请求，但管理授权格式不符合要求。',
    'KEY_CLIENT_HEADER_INVALID': '本程序未通过客户端请求头校验；可在诊断日志查看头名称与拒绝类别。',
    'KEY_HEADER_READ_FAILED': '未能取得本次只读请求的允许头字段。',
    'KEY_NOTICE_REQUIRES_USER': '邮箱提醒或其他弹窗需要先由你处理。',
    'KEY_BOOTSTRAP_TIMEOUT': '建立管理会话超时。',
    'KEY_BOOTSTRAP_FAILED': '建立管理会话失败，请核验网络和登录状态。',
    'KEY_DIAGNOSTIC_FAILED': '管理准备诊断记录未完成。',
    'KEY_LIST_READ_FAILED': '未能核验完整平台 Key 列表。',
    'KEY_ACCOUNT_CHANGED': '账户或目标 Key 列表与已有绑定不一致。',
    'KEY_LAST_TARGET': '缺少另一枚已知 Key，无法独立核实撤销结果。',
    'KEY_TARGET_READ_FAILED': 'HTTP 目标信息未通过完整列表和字段校验。',
    'KEY_ACCOUNT_RECHECK_FAILED': '发送前再次核验账户与 Key 列表失败。',
}


def preparation_reason_code(value):
    return value if isinstance(value, str) and value in PREPARATION_REASONS else 'PREPARATION_FAILED'


class RevocationNotSubmitted(GuardError):
    """A new attempt failed before entering any revocation submission boundary.

    Only the preparatory control flow may create this evidence. Missing logs,
    an old unknown result, or an unconsumed/expired permit are not such proof.
    """
    def __init__(self, message=None, *, reason_code='PREPARATION_FAILED'):
        self.reason_code = preparation_reason_code(reason_code)
        if message is None:
            message = '本次准备失败，尚未进入撤销提交步骤；Key 仍可能可用。'
            detail = PREPARATION_REASONS[self.reason_code]
            if detail:
                message += '\n' + detail + '（' + self.reason_code + '）'
        super().__init__(message)


class KeyInspectionError(GuardError):
    """A read-only preparation check failed; this is not a deletion result."""
    def __init__(self, *, reason_code='PREPARATION_FAILED'):
        self.reason_code = preparation_reason_code(reason_code)
        message = '只读管理预检未通过；本次预检没有执行删除。'
        detail = PREPARATION_REASONS[self.reason_code]
        if detail:
            message += '\n' + detail + '（' + self.reason_code + '）'
        super().__init__(message)


def _validated_grant(value):
    if not isinstance(value, dict) or set(value) != {'token', *_CLOCK_FIELDS}:
        raise GuardError('撤销许可格式无效，未执行最终确认。')
    token = value['token']
    if not isinstance(token, str) or len(token) != 32 or any(c not in '0123456789abcdef' for c in token):
        raise GuardError('撤销许可标识无效，未执行最终确认。')
    for name in _CLOCK_FIELDS:
        number = value[name]
        if isinstance(number, bool) or not isinstance(number, (float, int)) or not math.isfinite(number):
            raise GuardError('撤销许可时间无效，未执行最终确认。')
    for clock in ('monotonic', 'wall'):
        duration = value['expires_' + clock] - value['issued_' + clock]
        if not 0 < duration <= MAX_GRANT_SECONDS:
            raise GuardError('撤销许可期限无效，未执行最终确认。')
    return dict(value)


def _remaining(grant):
    monotonic, wall = time.monotonic(), time.time()
    if monotonic < grant['issued_monotonic'] or wall < grant['issued_wall']:
        return 0.0
    return max(0.0, min(grant['expires_monotonic'] - monotonic,
                        grant['expires_wall'] - wall))


class DeletePermit:
    """Compatibility events plus an immutable, once-exported approval lease.

``allowed`` records that approval happened; it deliberately stays true after
expiry. Code must use ``consume_for_click`` rather than treat that flag as a
current authorization. This preserves the audit obligation after approval.
The historical method name also gates the native HTTP submission.
"""
    def __init__(self, *, ttl_seconds=MAX_GRANT_SECONDS, target_tracking_id=None):
        if (isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, (int, float))
                or not math.isfinite(ttl_seconds) or not 0 < ttl_seconds <= MAX_GRANT_SECONDS):
            raise ValueError('撤销许可期限必须大于零且不超过五秒。')
        self.ready = Event()
        self.decided = Event()
        self._lock = Lock()
        self._ttl = float(ttl_seconds)
        self._allowed = False
        self._grant = None
        self._exported = False
        self._consumed = False
        self._prepared_inventory = None
        self._target_tracking_id = target_tracking_id

    @property
    def target_tracking_id(self):
        return self._target_tracking_id

    def set_prepared_inventory(self, target_tracking_id, known_tracking_ids):
        """Carry bounded public identity evidence, never an approval or credential.

        The browser sets this only after HTTP/DOM preparation; IPC validates it
        again before exposing readiness. Once set it cannot change target/set.
        """
        def valid(value):
            return (isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{8,200}', value)
                    and not value.lower().startswith('sk-'))
        if (not valid(target_tracking_id)
                or self.target_tracking_id is not None and self.target_tracking_id != target_tracking_id
                or not isinstance(known_tracking_ids, (list, tuple, set, frozenset))
                or not 2 <= len(known_tracking_ids) <= 200
                or any(not valid(key) for key in known_tracking_ids)
                or len(set(known_tracking_ids)) != len(known_tracking_ids)
                or target_tracking_id not in known_tracking_ids):
            raise GuardError('撤销准备的身份对照证据无效，未授权提交。')
        value = (target_tracking_id, tuple(sorted(known_tracking_ids)))
        with self._lock:
            if self._prepared_inventory is not None and self._prepared_inventory != value:
                raise GuardError('同一撤销准备的对照证据发生变化，未授权提交。')
            self._prepared_inventory = value

    def prepared_inventory(self, target_tracking_id):
        with self._lock:
            value = self._prepared_inventory
            if value is None or value[0] != target_tracking_id:
                raise GuardError('缺少本次目标已核验的恢复对照证据，未授权提交。')
            return {'target_tracking_id': value[0], 'known_tracking_ids': list(value[1])}

    @property
    def allowed(self):
        with self._lock:
            return self._allowed

    def decide(self, allowed):
        with self._lock:
            if self.decided.is_set():
                return False
            self._allowed = allowed is True
            if self._allowed:
                monotonic, wall = time.monotonic(), time.time()
                self._grant = {'token': uuid.uuid4().hex,
                               'issued_monotonic': monotonic, 'expires_monotonic': monotonic + self._ttl,
                               'issued_wall': wall, 'expires_wall': wall + self._ttl}
            self.decided.set()
            return True

    def export_grant(self):
        """The parent exports an approval at most once, without renewing it."""
        with self._lock:
            if not self._allowed or self._grant is None or self._exported or self._consumed:
                raise GuardError('撤销许可未批准或已使用，禁止重复提交。')
            self._exported = True
            if _remaining(self._grant) <= 0:
                raise GuardError('撤销许可已过期或时钟变化，结果待核实；禁止自动重试。')
            return dict(self._grant)

    def accept_grant(self, value):
        """Receive the original lease once; False means expired or duplicate.

An expired, correctly formed approval still sets ``allowed``/``decided`` so the
browser raises on consumption instead of reporting pre-approval cancellation.
"""
        grant = _validated_grant(value)
        with self._lock:
            if self.decided.is_set():
                return False
            self._grant, self._allowed, self._exported = grant, True, True
            self.decided.set()
            return _remaining(grant) > 0

    def consume_for_click(self):
        """Check both clocks and irreversibly consume the final click attempt.

Sample the browser's performance.now()/Date.now() before calling this method.
The final DOM task must check both the original wall-clock bounds and elapsed
browser monotonic time against remaining_ms. Sampling before this calculation
makes the browser deadline conservative; it never renews the original lease.
"""
        with self._lock:
            if not self._allowed or self._grant is None or self._consumed:
                raise GuardError('撤销许可未批准或已使用，未执行最终确认。')
            self._consumed = True
            remaining = _remaining(self._grant)
            if remaining <= 0:
                raise GuardError('撤销许可已过期或时钟变化，结果待核实；禁止自动重试。')
            return {'token': self._grant['token'],
                    'issued_wall_ms': self._grant['issued_wall'] * 1000,
                    'expires_wall_ms': self._grant['expires_wall'] * 1000,
                    'remaining_ms': remaining * 1000}
