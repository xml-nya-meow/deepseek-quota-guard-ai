"""Bounded creation-to-observation cost reads; no account-wide fallback."""
from datetime import date, datetime, timedelta
from decimal import Decimal, localcontext
from hashlib import sha256
import json
import time

from .core import TZ
from .cost_parser import parse_cost_response
from .platform_http import CostReadError, normalize_cost_scope


CHUNK_DAYS = 30
MAX_REQUESTS = 256
# Leave headroom inside the existing 90-second isolated-worker deadline for
# an in-flight 20-second GET and final metadata/DOM verification.
MAX_READ_SECONDS = 45


def creation_periods(origins, end_date, observed_wall):
    try:
        last = date.fromisoformat(end_date)
        if (not isinstance(origins, dict) or not origins
                or type(observed_wall) not in (int, float)
                or datetime.fromtimestamp(observed_wall, TZ).date() != last):
            raise ValueError()
        normalize_cost_scope(list(origins))
        periods = {}
        for key, stamp in origins.items():
            if type(stamp) is not int or not 0 < stamp <= observed_wall:
                raise ValueError()
            first = datetime.fromtimestamp(stamp, TZ).date()
            if first > last:
                raise ValueError()
            periods[key] = {'created_at': stamp, 'start_date': first.isoformat(),
                            'end_date': last.isoformat()}
        return periods
    except (ValueError, TypeError, OverflowError, OSError):
        raise CostReadError('创建日期证据不完整或无效，未应用创建以来费用。',
                            reason_code='LIFETIME_METADATA_INVALID') from None


def _read_plan(periods):
    groups = {}
    for key, period in periods.items():
        groups.setdefault((period['start_date'], period['end_date']), []).append(key)
    plan = []
    for (first, last), keys in sorted(groups.items()):
        cursor, finish = date.fromisoformat(first), date.fromisoformat(last)
        while cursor <= finish:
            end = min(cursor + timedelta(days=CHUNK_DAYS - 1), finish)
            plan.append((cursor.isoformat(), end.isoformat(), tuple(sorted(keys))))
            if len(plan) > MAX_REQUESTS:
                raise CostReadError('创建以来查询超过本轮分段上限，未截断或应用部分累计。',
                                    reason_code='LIFETIME_QUERY_LIMIT')
            cursor = end + timedelta(days=1)
    return plan


def read_lifetime_costs(reader, periods, *, emit=None, started_monotonic=None):
    """Return only totals whose every requested segment has key evidence.

    An empty successful response is not a zero. Missing any segment removes
    that key's accumulated amount rather than presenting a lower bound as a
    complete lifetime total. A malformed response aborts the whole observation.
    """
    emit = emit or (lambda *_args, **_kwargs: None)
    plan = _read_plan(periods)
    started = time.monotonic() if started_monotonic is None else started_monotonic
    totals = {key: Decimal(0) for key in periods}
    complete = set(periods)
    sources = []
    emit('lifetime_query_planned', count=len(periods), segment_count=len(plan))

    def check_time():
        if time.monotonic() - started >= MAX_READ_SECONDS:
            raise CostReadError('创建以来查询超过本轮时限，未应用部分累计；请保留原账本。',
                                reason_code='LIFETIME_QUERY_TIMEOUT', retryable=True)

    with localcontext() as context:
        context.prec = 50
        for ordinal, (start, end, keys) in enumerate(plan, 1):
            check_time()
            emit('lifetime_segment_started', segment_index=ordinal, segment_count=len(plan),
                 count=len(keys), start_date=start, end_date=end)
            url, payload = reader.read(start, end, tracking_ids=list(keys))
            check_time()
            try:
                amounts = parse_cost_response(payload, url, start, end, tracking_ids=keys)
            except CostReadError as exc:
                if exc.reason_code != 'NO_USAGE_DATA':
                    raise
                amounts = {}
            complete.difference_update(set(keys) - set(amounts))
            for key, amount in amounts.items():
                totals[key] += amount
            sources.append({'start': start, 'end': end, 'scope': keys,
                            'payload_sha256': sha256(json.dumps(payload, sort_keys=True,
                                                              default=str).encode()).hexdigest()})
            emit('lifetime_segment_completed', segment_index=ordinal, segment_count=len(plan),
                 count=len(amounts), missing_count=len(set(keys) - set(amounts)))
        check_time()
    if not complete:
        raise CostReadError('受管 Key 的创建以来费用尚无完整分段证据；未知时段未按零元计算。',
                            reason_code='LIFETIME_PERIOD_INCOMPLETE', retryable=True)
    digest = sha256(json.dumps({'fee_basis': 'key_lifetime', 'key_periods': periods,
                               'segments': sources}, sort_keys=True).encode()).hexdigest()
    return {key: totals[key] for key in sorted(complete)}, digest
