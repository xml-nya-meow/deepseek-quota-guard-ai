"""Read-only console HTTP adapter for the application's own login session.

These are observed console endpoints, not DeepSeek's public model API contract.
The adapter only sends GET requests to the fixed cost endpoint. It never reads
a browser profile, cookies, local storage, or a model API key. Scoped reads
observe the bearer on the exact Key-list GET of the app's own isolated console
page; legacy callers observe the cost GET. The bearer stays in worker memory
and is discarded when the session is replaced.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import json
import re
import time
from urllib.parse import urlencode, urlsplit

from .core import GuardError


BASE = 'https://platform.deepseek.com'
COST_PATH = '/api/v0/usage/by_api_key/cost'
KEY_LIST_URL = BASE + '/api/v0/users/get_api_keys'
TZ = timezone(timedelta(hours=8))
MAX_BODY_BYTES = 10_000_000
HTTP_TIMEOUT_MS = 20_000
BOOTSTRAP_TIMEOUT_MS = 15_000


class CostReadError(GuardError):
    """Safe client-owned message and stable code; no server text or headers."""
    def __init__(self, message, *, reason_code, retryable=False):
        super().__init__(message)
        self.reason_code = reason_code
        self.retryable = bool(retryable)


def is_cost_endpoint(url):
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == 'https' and parsed.netloc == 'platform.deepseek.com'
                and parsed.path == COST_PATH and not parsed.fragment)
    except (TypeError, ValueError):
        return False


def normalize_cost_scope(tracking_ids):
    """An explicit empty scope must never turn into an account-wide query."""
    if (not isinstance(tracking_ids, (list, tuple, set, frozenset))
            or not 0 < len(tracking_ids) <= 200
            or any(not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,200}', key)
                   or key.lower().startswith('sk-') for key in tracking_ids)
            or len(set(tracking_ids)) != len(tracking_ids)):
        raise CostReadError('费用查询需要明确且唯一的受管 Key 范围。', reason_code='INVALID_KEY_SCOPE')
    return tuple(sorted(tracking_ids))


def cost_url(start_date, end_date, *, tracking_ids=None):
    """UI dates are inclusive; the observed HTTP interval is [start, end)."""
    try:
        first, last = date.fromisoformat(start_date), date.fromisoformat(end_date)
        if first > last:
            raise ValueError('reversed range')
        begin = int(datetime.combine(first, datetime.min.time(), TZ).timestamp())
        finish = int(datetime.combine(last + timedelta(days=1), datetime.min.time(), TZ).timestamp())
    except (TypeError, ValueError, OverflowError):
        raise CostReadError('统计日期范围无效，请修改统计起点。',
                            reason_code='INVALID_DATE_RANGE') from None
    query = {'start': begin, 'end': finish, 'tz': 28800}
    if tracking_ids is not None:
        # Official client serializes array values as repeated query parameters.
        query['api_key_tracking_id'] = normalize_cost_scope(tracking_ids)
    return BASE + COST_PATH + '?' + urlencode(query, doseq=True)


def _message_metadata(mapping, names, prefix):
    """Describe only known message slots; never stringify server-controlled data."""
    values = [mapping[name] for name in names if name in mapping]
    kinds = {('null' if value is None else
              type(value).__name__ if type(value) in (str, bool, int, float, dict, list) else
              'decimal' if isinstance(value, Decimal) else 'other') for value in values}
    return {
        prefix + '_message_present': bool(values),
        prefix + '_message_type': ('missing' if not kinds else
                                  next(iter(kinds)) if len(kinds) == 1 else 'mixed'),
        prefix + '_message_length': min(1024, max(
            (len(value) for value in values if type(value) is str), default=0)),
    }


def cost_metadata(payload):
    """Bounded structural metadata, never response text, identities or costs."""
    fields = {}
    if not isinstance(payload, dict):
        return fields
    fields.update(_message_metadata(payload, ('msg', 'message'), 'response'))
    if type(payload.get('code')) is int:
        fields['response_code'] = payload['code']
    envelope = payload.get('data')
    if not isinstance(envelope, dict):
        return fields
    fields.update(_message_metadata(envelope, ('biz_msg', 'msg', 'message'), 'business'))
    if type(envelope.get('biz_code')) is int:
        fields['biz_code'] = envelope['biz_code']
    data = envelope.get('biz_data')
    if not isinstance(data, dict):
        return fields
    for source, destination in (('start', 'response_start'), ('end', 'response_end')):
        if type(data.get(source)) is int:
            fields[destination] = data[source]
    interval = data.get('bucket')
    if type(interval) is int and interval in (3600, 86400):
        fields['bucket_kind'] = 'integer'
        fields['bucket_seconds'] = interval
    elif type(interval) is str and interval in ('1h', '1d'):
        fields['bucket_kind'] = 'text'
        fields['bucket_seconds'] = {'1h': 3600, '1d': 86400}[interval]
    else:
        fields['bucket_kind'] = 'missing' if 'bucket' not in data else 'unsupported'
    groups = data.get('data')
    if not isinstance(groups, list):
        return fields
    fields.update(group_count=len(groups), series_count=0, bucket_count=0,
                  function_series_count=0, function_bucket_count=0)
    boundaries = {'': [None, None], 'function_': [None, None]}
    for group in groups:
        if not isinstance(group, dict):
            continue
        for name, prefix in (('series', ''), ('function_series', 'function_')):
            series = group.get(name)
            if not isinstance(series, list):
                continue
            fields[prefix + 'series_count'] += len(series)
            first_time, last_time = boundaries[prefix]
            for entry in series:
                if isinstance(entry, dict) and isinstance(entry.get('buckets'), list):
                    fields[prefix + 'bucket_count'] += len(entry['buckets'])
                    for bucket in entry['buckets']:
                        stamp = bucket.get('time') if isinstance(bucket, dict) else None
                        # Unix seconds up to 9999-12-31; malformed values stay
                        # the parser's responsibility and never enter this log.
                        if type(stamp) is int and 0 <= stamp <= 253402300799:
                            first_time = stamp if first_time is None else min(first_time, stamp)
                            last_time = stamp if last_time is None else max(last_time, stamp)
            boundaries[prefix] = [first_time, last_time]
    for prefix, (first_time, last_time) in boundaries.items():
        if first_time is not None:
            fields[prefix + 'bucket_first_time'] = first_time
            fields[prefix + 'bucket_last_time'] = last_time
    return fields


class ConsoleCostReader:
    """Thread-confined, session-scoped HTTP reader; no implicit network retries."""
    def __init__(self, context, *, emit=None):
        self._context = context
        self._authorization = None
        self._emit = emit or (lambda *_args, **_kwargs: None)

    def clear(self):
        self._authorization = None

    def _observe(self, request):
        # An exact endpoint and method prevent forwarding unrelated credentials.
        if request.method != 'GET' or not is_cost_endpoint(request.url):
            return
        self._observe_authorization(request)

    def _observe_key_list(self, request):
        # Same isolated console session; no model credential or profile reads.
        if request.method == 'GET' and request.url == KEY_LIST_URL:
            self._observe_authorization(request)

    def _observe_authorization(self, request):
        try:
            value = request.header_value('authorization')
        except Exception:
            return
        # Do not accept a model API key or a value with line breaks. The value is
        # deliberately never included in repr, diagnostics, IPC, or exceptions.
        if isinstance(value, str) and len(value) <= 16384 and re.fullmatch(r'Bearer [^\s]+', value):
            if not value[7:].lower().startswith('sk-'):
                self._authorization = value

    def prepare(self, page, *, tracking_ids=None):
        scoped = tracking_ids is not None
        if scoped:
            normalize_cost_scope(tracking_ids)
        if self._authorization is not None:
            return
        # Only initial session bootstrap needs a page navigation. No calendar,
        # filters, UI date selection, or default response supplies ledger costs.
        deadline = time.monotonic() + BOOTSTRAP_TIMEOUT_MS / 1000
        # A scoped collection bootstraps on the Key page, avoiding the usage
        # page's automatic account-wide cost request. Legacy offline callers
        # without a scope retain their original bootstrap path.
        route = '/api_keys' if scoped else '/usage'
        observe = self._observe_key_list if scoped else self._observe
        page.on('request', observe)
        try:
            page.goto(BASE + route, wait_until='domcontentloaded', timeout=BOOTSTRAP_TIMEOUT_MS)
            parsed = urlsplit(page.url)
            if parsed.scheme != 'https' or parsed.netloc != 'platform.deepseek.com' or parsed.path != route:
                raise CostReadError('平台登录已失效或离开预期页面，请重新登录。', reason_code='AUTH_REQUIRED')
            while self._authorization is None and time.monotonic() < deadline:
                parsed = urlsplit(page.url)
                if parsed.scheme != 'https' or parsed.netloc != 'platform.deepseek.com' or parsed.path != route:
                    raise CostReadError('平台登录已失效或离开预期页面，请重新登录。', reason_code='AUTH_REQUIRED')
                page.wait_for_timeout(100)
            if self._authorization is None:
                raise CostReadError('未取得本次登录的费用接口授权，请重新登录后读取。不会改用日期菜单。',
                                    reason_code='AUTH_REQUIRED')
        except CostReadError:
            self.clear()
            raise
        except Exception:
            self.clear()
            raise CostReadError('建立费用读取会话失败，请检查网络后重新读取。',
                                reason_code='SESSION_UNAVAILABLE', retryable=True) from None
        finally:
            page.remove_listener('request', observe)

    def read(self, start_date, end_date, *, tracking_ids=None):
        if self._authorization is None:
            raise CostReadError('费用读取会话尚未授权，请先完成登录。', reason_code='AUTH_REQUIRED')
        url = cost_url(start_date, end_date, tracking_ids=tracking_ids)
        response = None
        try:
            # BrowserContext.request shares only this app's isolated cookie jar.
            # Redirects are disabled so the explicit bearer cannot be forwarded.
            response = self._context.request.get(url, headers={
                'Authorization': self._authorization, 'Accept': 'application/json',
            }, timeout=HTTP_TIMEOUT_MS, max_redirects=0, max_retries=0,
                fail_on_status_code=False, ignore_https_errors=False)
            self._emit('cost_http_response', http_status=response.status)
            if response.status in (401, 403):
                self.clear()
                raise CostReadError('平台费用接口未授权，请重新登录；原费用保留。', reason_code='AUTH_REQUIRED')
            if 300 <= response.status < 400 or response.url != url:
                self.clear()
                raise CostReadError('费用接口发生跳转，未发送后续请求，请重新登录。', reason_code='REDIRECT_REJECTED')
            if response.status != 200:
                raise CostReadError('费用接口返回错误，本次没有更新费用。', reason_code='HTTP_ERROR',
                                    retryable=response.status == 429 or response.status >= 500)
            length = response.headers.get('content-length')
            if length is not None and (not length.isdigit() or int(length) > MAX_BODY_BYTES):
                raise CostReadError('费用响应大小不符合读取限制，未更新费用。', reason_code='RESPONSE_TOO_LARGE')
            media_type = response.headers.get('content-type', '').partition(';')[0].strip().lower()
            if media_type != 'application/json':
                raise CostReadError('费用接口未返回 JSON 账单，可能需要重新登录。', reason_code='RESPONSE_NOT_JSON')
            body = response.body()
            if len(body) > MAX_BODY_BYTES:
                raise CostReadError('费用响应超过读取限制，未更新费用。', reason_code='RESPONSE_TOO_LARGE')
            try:
                def reject_constant(_value):
                    raise ValueError('non-finite JSON number')
                payload = json.loads(body, parse_float=Decimal, parse_constant=reject_constant)
            except (ValueError, UnicodeError, RecursionError):
                raise CostReadError('费用响应不是有效 JSON，未更新费用。', reason_code='RESPONSE_NOT_JSON') from None
            self._emit('cost_response_finished', bytes=len(body), http_status=response.status,
                       **cost_metadata(payload))
            return url, payload
        except CostReadError:
            raise
        except Exception:
            # SDK errors can include request headers. Return only fixed text.
            raise CostReadError('费用接口请求超时或网络中断，请稍后重新读取。',
                                reason_code='NETWORK_ERROR', retryable=True) from None
        finally:
            if response is not None:
                try:
                    response.dispose()
                except Exception:
                    pass
