"""Bounded local development diagnostics, separate from financial audit records.

No browser headers, cookies, response bodies or credentials are serialized.
Unknown complex inputs are described by structure. This log is disposable;
ledger events are not, and must never depend on this writer succeeding.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import threading
import time
import uuid

from .local_ai_assistant import DiagnosticCase, case_from_event


DEFAULT_MAX_BYTES = 2_000_000
DEFAULT_KEEP_FILES = 12
DEFAULT_RETENTION_DAYS = 14
CLEANUP_INTERVAL_SECONDS = 60
PREVIEW_MAX_RECORDS = 300
PREVIEW_MAX_BYTES = 256 * 1024
_LOG_NAME = re.compile(
    r'diagnostic-\d{8}T\d{12}Z-(?P<session>[0-9a-f]{12})(?:-p(?P<pid>[1-9]\d{0,9}))?\.jsonl'
)


def _candidates(folder, *, raise_errors=False):
    """Only direct, regular files with an exact application-owned name."""
    result = []
    try:
        if Path(folder).is_symlink():
            return []
        with os.scandir(folder) as entries:
            for entry in entries:
                match = _LOG_NAME.fullmatch(entry.name)
                if not match or not entry.is_file(follow_symlinks=False):
                    continue
                try:
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                result.append((Path(entry.path), stat.st_mtime, stat.st_size, match))
    except OSError:
        if raise_errors:
            raise
        return []
    return sorted(result, key=lambda item: (item[1], item[0].name))


def _process_is_alive(pid):
    """Read-only liveness; uncertainty preserves the file rather than deleting it."""
    if not 1 <= pid <= 0xffffffff:
        return True
    if pid == os.getpid():
        return True
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER: no such PID
        try:
            code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


@dataclass(frozen=True)
class DiagnosticPreview:
    text: str
    record_count: int = 0
    skipped_lines: int = 0
    truncated: bool = False
    unavailable: bool = False
    # Timestamps are display-only. Only a selected case's enum fields reach the model.
    cases: tuple[tuple[str, DiagnosticCase], ...] = ()


def read_recent_diagnostics(folder, *, max_records=PREVIEW_MAX_RECORDS,
                            max_bytes=PREVIEW_MAX_BYTES):
    """Read a bounded tail snapshot; never return raw or malformed log content.

    Both bytes read from disk and UTF-8 bytes returned are capped. A refresh
    creates no long-lived cache or background thread. Older format names remain
    readable, and every displayed field is sanitized again on read.
    """
    max_records = max(1, min(int(max_records), PREVIEW_MAX_RECORDS))
    max_bytes = max(1, min(int(max_bytes), PREVIEW_MAX_BYTES))
    folder = Path(folder)
    try:
        candidates = _candidates(folder, raise_errors=True)
    except FileNotFoundError:
        return DiagnosticPreview('')
    except OSError:
        return DiagnosticPreview('', unavailable=True)
    if not candidates:
        return DiagnosticPreview('', unavailable=folder.exists() and not folder.is_dir())
    remaining = max_bytes
    rows = []
    skipped = 0
    truncated = False
    unavailable = False
    for path, _, _, _ in reversed(candidates):
        if remaining <= 0:
            truncated = True
            break
        try:
            # A candidate can disappear during rotation; no write or retry loop.
            if path.is_symlink():
                continue
            with path.open('rb') as stream:
                size = stream.seek(0, 2)
                start = max(0, size - remaining)
                stream.seek(start)
                raw = stream.read(remaining)
            remaining -= len(raw)
            if start:
                truncated = True
                # The first fragment may be only the tail of a JSON record.
                raw = raw.partition(b'\n')[2]
            lines = raw.splitlines()
            for line in lines:
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                    if not isinstance(value, dict) or not isinstance(value.get('data'), dict):
                        raise ValueError('invalid record')
                    stamp = value.get('time', '')
                    if not isinstance(stamp, str) or len(stamp) > 40:
                        raise ValueError('invalid time')
                    parsed = datetime.fromisoformat(stamp)
                    if parsed.tzinfo is None:
                        raise ValueError('missing timezone')
                    event = value.get('event')
                    if not isinstance(event, str):
                        raise ValueError('invalid event')
                    data = safe_fields(value['data'])
                    elapsed = value.get('elapsed_ms')
                    if type(elapsed) is int and elapsed >= 0:
                        data.setdefault('elapsed_ms', elapsed)
                    # Keep a writer's pre-hashed identity recognizable without
                    # trusting arbitrary nested objects from an edited log.
                    for key, field in value['data'].items():
                        if (_IDENTITY.fullmatch(key) and isinstance(field, dict)
                                and set(field) == {'fingerprint'}
                                and isinstance(field['fingerprint'], str)
                                and re.fullmatch('[0-9a-f]{12}', field['fingerprint'])):
                            data[key] = field
                    session = value.get('session', '')
                    session = session if isinstance(session, str) and re.fullmatch('[0-9a-f]{12}', session) else ''
                    local_stamp = parsed.astimezone().isoformat(timespec='seconds')
                    display = (f"{local_stamp}  "
                               f"{safe_text(event)}  [{session}]\n"
                               f"{json.dumps(data, ensure_ascii=False, allow_nan=False)}\n")
                    rows.append((parsed.timestamp(), display, local_stamp, case_from_event(event, data)))
                except (ValueError, TypeError, OverflowError, RecursionError):
                    skipped += 1
        except OSError:
            unavailable = True
    rows.sort(key=lambda item: item[0], reverse=True)
    if len(rows) > max_records:
        truncated = True
    chosen = []
    cases = []
    output_size = 0
    for _, display, stamp, case in rows[:max_records]:
        size = len(display.encode('utf-8'))
        if output_size + size > max_bytes:
            truncated = True
            break
        chosen.append(display)
        if case is not None:
            cases.append((stamp, case))
        output_size += size
    return DiagnosticPreview(''.join(reversed(chosen)),
                             len(chosen), skipped, truncated, unavailable, tuple(cases))


_SECRET = re.compile(r'password|passwd|secret|token|cookie|authorization|otp|verification|验证码|密码', re.I)
_IDENTITY = re.compile(r'^(name|person|tracking_id|account_id|known_keys|keys|target|identity)$', re.I)
_RAW = re.compile(r'^(body|payload|response|headers|args|kwargs|html|dom|raw|input|result)$', re.I)
# Header diagnostics are a separate closed vocabulary, not arbitrary scalar
# strings. These fields never contain the observed value or its representation.
_HEADER_ENUMS = {
    'header_name': frozenset({
        'x-client-bundle-id', 'x-client-platform', 'x-client-version',
        'x-client-locale', 'x-client-timezone-offset', 'x-device-id', 'x-device-model',
    }),
    'rejection_kind': frozenset({'empty', 'type', 'too_long', 'control_char'}),
    'value_type': frozenset({'str', 'bool', 'int', 'float', 'bytes', 'list', 'tuple', 'dict', 'none', 'other'}),
    'source_kind': frozenset({'key_list', 'cost_read'}),
}
_HEADER_COUNTER_LIMITS = {'value_length': 1025, 'matched_request_count': 10000,
                          'http_row_ordinal': 1000}
_HEADER_BOOLEANS = frozenset({'bearer_format_passed', 'is_selected_target',
                              'dom_identity_matched', 'redacted_format_valid', 'created_at_valid'})
_KEY_ENUMS = {
    'key_field': frozenset({'name', 'tracking_id', 'sensitive_id', 'created_at', 'key_type'}),
    # USER remains readable in historical diagnostic records only.
    'key_kind': frozenset({'NORMAL', 'DSH', 'unknown', 'USER'}),
}
_COST_ENUMS = {
    'scope_kind': frozenset({'managed_keys'}),
    'fee_id_source': frozenset({'api_key.tracking_id'}),
    'key_id_source': frozenset({'tracking_id_column'}),
    'bucket_kind': frozenset({'integer', 'text', 'missing', 'unsupported'}),
    'response_message_type': frozenset({'missing', 'null', 'str', 'bool', 'int', 'float',
                                        'dict', 'list', 'decimal', 'other', 'mixed'}),
    'business_message_type': frozenset({'missing', 'null', 'str', 'bool', 'int', 'float',
                                        'dict', 'list', 'decimal', 'other', 'mixed'}),
}
_COST_COUNTER_LIMITS = {
    'response_message_length': 1024, 'business_message_length': 1024,
    'request_span_days': 3652059,
    'bucket_first_time': 253402300799, 'bucket_last_time': 253402300799,
    'function_bucket_first_time': 253402300799, 'function_bucket_last_time': 253402300799,
    'function_bucket_count': 1000000,
    **dict.fromkeys(('platform_key_count', 'matched_platform_count', 'unmatched_fee_count',
                    'missing_platform_count', 'uuid_casefold_match_count', 'case_only_match_count'),
                   1000000),
}
_COST_BOOLEANS = frozenset({'response_message_present', 'business_message_present'})
_SCALARS = {
    'stage', 'action', 'status', 'reason', 'error_type', 'error_code', 'message',
    'generation', 'session', 'task_id', 'action_id', 'pid', 'worker_pid', 'parent_pid',
    'elapsed_ms', 'elapsed', 'duration_ms', 'started_at', 'timeout', 'timeout_ms',
    'count', 'key_count', 'cost_count', 'bytes', 'reply_count', 'queue_size', 'attempt',
    'success', 'cancelled', 'allowed', 'ready', 'automatic', 'busy', 'submitted',
    'complete', 'totals_available', 'start_date', 'end_date', 'http_status', 'version',
    'operation', 'button', 'outcome', 'error', 'label', 'kind', 'event',
    'panels', 'menu_visible', 'remaining_ms', 'retryable', 'cleanup_complete', 'state',
    'reason_code', 'request_start', 'request_end', 'response_start', 'response_end',
    'group_count', 'series_count', 'bucket_count', 'function_series_count',
    'source_id', 'response_code', 'biz_code', 'operation_id', 'click_completed', 'channel', 'phase',
    'app_version', 'build_commit', 'build_id', 'diagnostic_schema_version',
    'persistence_state', 'export_state', 'apply_outcome', 'observed_source_id', 'effective_snapshot_id',
    'observed_source_is_effective', 'source_seen_before', 'payload_digest_matches',
    'bound_total_count', 'active_managed_count', 'fee_tracking_count', 'matched_total_count',
    'matched_active_count', 'matched_historical_count', 'active_missing_count', 'decrease_count',
}


def safe_text(value):
    value = str(value)
    value = re.sub(r'sk-[\w.*-]+', '[credential]', value, flags=re.I)
    value = re.sub(r'(?i)\bBearer\s+[^\s,;]+', 'Bearer [credential]', value)
    value = re.sub(r'(?i)(authorization|cookie|password|access_token|refresh_token)\s*[:=]\s*[^\r\n]+', r'\1=[redacted]', value)
    value = re.sub(r'\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[credential]', value)
    value = re.sub(r'https?://[^\s<>"\x27]+', '[url]', value)
    value = re.sub(r'[A-Za-z]:[\\/][^\r\n<>"\x27]+', '[local-path]', value)
    value = re.sub(r'[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}', '[email]', value)
    return value[:500]


def shape(value, depth=0):
    """Keep cardinality/types, never copy strings from unknown input objects."""
    if depth >= 3:
        return {'type': type(value).__name__}
    if isinstance(value, dict):
        # Unknown dictionary keys can themselves be tokens or personal IDs.
        fields = {str(k): shape(v, depth + 1) for k, v in value.items() if str(k) in _SCALARS}
        return {'type': 'dict', 'count': len(value), 'fields': fields}
    if isinstance(value, (list, tuple, set)):
        return {'type': type(value).__name__, 'count': len(value),
                'items': [shape(v, depth + 1) for v in list(value)[:3]]}
    if isinstance(value, str):
        return {'type': 'str', 'length': len(value)}
    return {'type': type(value).__name__}


def safe_fields(fields):
    result = {}
    for key, value in fields.items():
        key = str(key)
        if key in _KEY_ENUMS:
            if type(value) is str and value in _KEY_ENUMS[key]:
                result[key] = value
        elif key == 'field_present':
            if type(value) is bool:
                result[key] = value
        elif key in _COST_ENUMS:
            if type(value) is str and value in _COST_ENUMS[key]:
                result[key] = value
        elif key in _COST_COUNTER_LIMITS:
            if type(value) is int and value >= 0:
                result[key] = min(value, _COST_COUNTER_LIMITS[key])
        elif key in _COST_BOOLEANS:
            if type(value) is bool:
                result[key] = value
        elif key == 'request_tz':
            if type(value) is int and value == 28800:
                result[key] = value
        elif key == 'bucket_seconds':
            if type(value) is int and value in (3600, 86400):
                result[key] = value
        elif key in _HEADER_ENUMS:
            if type(value) is str and value in _HEADER_ENUMS[key]:
                result[key] = value
        elif key in _HEADER_COUNTER_LIMITS:
            if type(value) is int and value >= 0:
                result[key] = min(value, _HEADER_COUNTER_LIMITS[key])
        elif key in _HEADER_BOOLEANS:
            if type(value) is bool:
                result[key] = value
        elif _SECRET.search(key):
            result[safe_text(key)] = '[redacted]'
        elif _IDENTITY.fullmatch(key):
            result[key] = {'fingerprint': sha256(str(value).encode()).hexdigest()[:12]}
        elif _RAW.fullmatch(key) or key not in _SCALARS:
            result[safe_text(key)] = shape(value)
        elif isinstance(value, (bool, int, float)) or value is None:
            result[key] = value
        elif isinstance(value, Decimal):
            result[key] = str(value)
        elif isinstance(value, str):
            result[key] = safe_text(value)
        else:
            result[key] = shape(value)
    return result


class DiagnosticLog:
    """One file per writer; bounded rotation prevents unlimited disk growth."""
    def __init__(self, folder, *, max_bytes=DEFAULT_MAX_BYTES,
                 keep_files=DEFAULT_KEEP_FILES, retention_days=DEFAULT_RETENTION_DAYS):
        self.folder = Path(folder)
        self.max_bytes = max(1024, max_bytes)
        self.keep_files = max(2, keep_files)
        self.retention_days = max(1, retention_days)
        self.lock = threading.Lock()
        self.session = uuid.uuid4().hex[:12]
        self.started = time.monotonic()
        self.last_cleanup = None
        self._file_time = None
        self.path = None
        self.disabled = False

    def _new_file(self):
        self.folder.mkdir(parents=True, exist_ok=True)
        current = datetime.now(timezone.utc)
        if self._file_time is not None and current <= self._file_time:
            current = self._file_time + timedelta(microseconds=1)
        # Windows wall-clock resolution can repeat during fast rotation, and
        # the wall clock can move backwards. Keep names strictly increasing
        # within this writer and reserve them exclusively; never append to a
        # supposedly new file that already exists.
        for _ in range(1000):
            stamp = current.strftime('%Y%m%dT%H%M%S%fZ')
            candidate = self.folder / f'diagnostic-{stamp}-{self.session}-p{os.getpid()}.jsonl'
            try:
                with candidate.open('x', encoding='utf-8', newline='\n'):
                    pass
            except FileExistsError:
                current += timedelta(microseconds=1)
                continue
            self._file_time = current
            self.path = candidate
            return
        raise OSError('Unable to reserve a fresh diagnostic file')

    def _cleanup(self):
        """Best-effort retention with active-writer protection, never recursive.

        The count cap is a steady-state bound: if more than keep_files writers
        are alive, their current files survive until a later cleanup. A reused
        PID can conservatively preserve an extra file. Legacy names lack PID;
        their most recently written session file gets a one-minute grace period.
        """
        now = time.time()
        candidates = _candidates(self.folder)
        latest = {}
        for item in candidates:
            latest[item[3]['session']] = item[0]
        live = {}
        protected = {self.path}
        for path, modified, _, match in candidates:
            if latest[match['session']] != path or match['session'] == self.session:
                continue
            pid = match['pid']
            if pid is None:
                if now - modified < CLEANUP_INTERVAL_SECONDS:
                    protected.add(path)
            else:
                pid = int(pid)
                if pid not in live:
                    live[pid] = _process_is_alive(pid)
                if live[pid]:
                    protected.add(path)
        remaining = len(candidates)
        cutoff = now - self.retention_days * 86400
        for old, modified, _, _ in candidates:
            if old in protected:
                continue
            if modified >= cutoff and remaining <= self.keep_files:
                continue
            try:
                # Recheck links immediately before deleting only this file.
                if old.is_symlink() or not old.is_file():
                    continue
                old.unlink()
                remaining -= 1
            except OSError:
                pass
        self.last_cleanup = time.monotonic()

    def emit(self, event, **fields):
        record = {'time': datetime.now(timezone.utc).isoformat(), 'session': self.session,
                  'elapsed_ms': round((time.monotonic() - self.started) * 1000),
                  'event': safe_text(event), 'data': safe_fields(fields)}
        try:
            serialized = json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n'
            if len(serialized.encode('utf-8')) > self.max_bytes:
                # No single event can defeat rotation by carrying a large set
                # of fields. Keep its time/event and a fixed omission reason.
                record['data'] = {'reason_code': 'DIAGNOSTIC_RECORD_TOO_LARGE', 'count': len(fields)}
                record['event'] = record['event'][:128]
                serialized = json.dumps(record, ensure_ascii=False, allow_nan=False) + '\n'
            size = len(serialized.encode('utf-8'))
            with self.lock:
                rotated = (self.path is None or not self.path.exists()
                           or self.path.stat().st_size + size > self.max_bytes)
                if rotated:
                    self._new_file()
                with self.path.open('a', encoding='utf-8', newline='\n') as stream:
                    stream.write(serialized)
                if (rotated or self.last_cleanup is None
                        or time.monotonic() - self.last_cleanup >= CLEANUP_INTERVAL_SECONDS):
                    self._cleanup()
                self.disabled = False
            return True
        except (OSError, TypeError, ValueError):
            self.disabled = True
            return False
