"""Decode observed console costs without making requests or writing a ledger.

Production sums model and function bucket costs through the same strict leaf
validator. The saved official client (4179.a95866c86d.js) maps both leaves at
character offset 521149, maps function_series at 566854 and sums both categories
at 570517. No usage-count pricing or guessed function names are involved.
Supporting this observed schema is not a guarantee of complete server billing
or real-account acceptance; local identity and action guards remain separate.
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
import re
from urllib.parse import parse_qs, urlparse

from .core import GuardError, TZ, money
from .platform_http import CostReadError, is_cost_endpoint, normalize_cost_scope


_KEY_ID = re.compile(r'[A-Za-z0-9_-]{8,200}')
_MODEL_SERIES = 'series'
_FUNCTION_SERIES = 'function_series'


class NoUsageData(CostReadError):
    """A valid but empty report may be checked again without inventing costs."""
    def __init__(self, message):
        super().__init__(message, reason_code='NO_USAGE_DATA', retryable=True)


@dataclass(frozen=True)
class OfflineFunctionCostCandidate:
    """Observed cumulative amounts for offline comparison, never a write permit.

    Missing keys are explicitly absent from totals, not filled with zero. Even
    when none are missing, production_validated remains False: a syntactically
    valid fixture is not a real-account acceptance result. This field does not
    enable or disable the production parser, which uses the same leaf rules.
    """
    totals: dict[str, Decimal]
    category_totals: dict[str, dict[str, Decimal]]
    missing_tracking_ids: frozenset[str]
    production_validated: bool = field(default=False, init=False)


def _valid_key(value):
    return (isinstance(value, str) and _KEY_ID.fullmatch(value) is not None
            and not value.startswith('sk-'))


def _accumulate_series(series, category, totals, seen, *, left, right, begin,
                       finish, seconds, expected_keys, scoped=False):
    """One leaf validator for model and function costs; never infer unit prices."""
    if not isinstance(series, list):
        raise CostReadError('费用计费分类不是列表，未应用数据。', reason_code='SCHEMA_UNSUPPORTED')
    for entry in series:
        key = entry['api_key']['tracking_id']
        if not _valid_key(key):
            raise CostReadError('费用中缺少合法 Tracking ID。', reason_code='KEY_ID_INVALID')
        if expected_keys is not None and key not in expected_keys:
            if scoped:
                raise CostReadError('平台返回了指定受管范围之外的费用，未应用本次结果；不会退回全账户查询。',
                                    reason_code='COST_SCOPE_MISMATCH')
            raise CostReadError('离线样本含未明确绑定的 Tracking ID，不分摊其费用。',
                                reason_code='KEY_ID_UNBOUND')
        model = entry['model']
        if not isinstance(model, str) or not model.strip():
            raise CostReadError('费用中缺少计费分类。', reason_code='SCHEMA_UNSUPPORTED')
        if not isinstance(entry['buckets'], list):
            raise CostReadError('费用分桶明细不是列表，未应用数据。', reason_code='SCHEMA_UNSUPPORTED')
        for bucket in entry['buckets']:
            stamp = bucket['time']
            if type(stamp) is not int or stamp < left or stamp + seconds > right:
                raise CostReadError('费用时间超出响应范围。', reason_code='PERIOD_MISMATCH')
            if (stamp - begin) % seconds:
                raise CostReadError('费用分桶与 UTC+8 统计边界不对齐，未应用数据。',
                                    reason_code='BUCKET_MISALIGNED')
            identity = (category, key, model, stamp)
            if identity in seen:
                raise CostReadError('发现重复费用分桶，停止汇总以免重复计费。',
                                    reason_code='DUPLICATE_COST_BUCKET')
            seen.add(identity)
            try:
                cost = money(bucket['cost'])
            except GuardError:
                raise CostReadError('费用金额格式或精度不合法，未应用数据。',
                                    reason_code='AMOUNT_INVALID') from None
            if begin <= stamp < finish:
                totals[key] = totals.get(key, Decimal(0)) + cost


def _parse_categories(payload, url, start_date, end_date, *, expected_keys=None, tracking_ids=None):
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    if not is_cost_endpoint(url):
        raise CostReadError('不是预期的平台费用来源。', reason_code='SOURCE_MISMATCH')
    if query.get('tz') != ['28800']:
        raise CostReadError('平台费用时区不是 UTC+8，本次费用未应用。', reason_code='TIMEZONE_MISMATCH')
    if tracking_ids is not None:
        expected_keys = frozenset(normalize_cost_scope(tracking_ids))
        requested = query.get('api_key_tracking_id', [])
        if len(requested) != len(expected_keys) or set(requested) != expected_keys:
            raise CostReadError('费用请求与本次受管 Key 范围不一致，未应用数据。',
                                reason_code='COST_SCOPE_MISMATCH')
    begin = int(datetime.fromisoformat(start_date).replace(tzinfo=TZ).timestamp())
    finish = int((datetime.fromisoformat(end_date).replace(tzinfo=TZ)
                  + timedelta(days=1)).timestamp())
    try:
        if type(payload['code']) is not int or type(payload['data']['biz_code']) is not int:
            raise CostReadError('平台费用状态结构变化，未更新费用。', reason_code='SCHEMA_UNSUPPORTED')
        if payload['code'] != 0 or payload['data']['biz_code'] != 0:
            raise CostReadError('平台拒绝本次费用查询，具体业务原因尚未确认；原费用保留，请核对统计日期与诊断记录。',
                                reason_code='PLATFORM_REJECTED')
        data = payload['data']['biz_data']
        left, right = data['start'], data['end']
        if type(left) is not int or type(right) is not int or left > begin or right < finish:
            raise CostReadError('平台返回范围没有覆盖完整预算周期，不能替代累计消费。',
                                reason_code='PERIOD_INCOMPLETE')
        interval = data['bucket']
        if type(interval) is int and interval in (3600, 86400):
            seconds = interval
        elif isinstance(interval, str) and interval in ('1h', '1d'):
            seconds = {'1h': 3600, '1d': 86400}[interval]
        else:
            raise CostReadError('暂不支持该费用时间分桶，未应用数据。', reason_code='BUCKET_UNSUPPORTED')
        if query.get('start') != [str(left)] or query.get('end') != [str(right)]:
            raise CostReadError('平台请求与返回时间范围不一致。', reason_code='PERIOD_MISMATCH')
        function_metadata = data.get('functions', [])
        if (not isinstance(function_metadata, list)
                or any(not isinstance(value, str) or not value.strip() for value in function_metadata)):
            raise CostReadError('平台函数计费分类结构变化，未应用不完整金额。',
                                reason_code='SCHEMA_UNSUPPORTED')
        groups = data['data']
        if not isinstance(groups, list):
            raise CostReadError('费用结构不是列表，未应用数据。', reason_code='SCHEMA_UNSUPPORTED')
        if not groups:
            raise NoUsageData('费用结果为空；不能仅凭空数组给所有人员记零消费。')
        categories = {_MODEL_SERIES: {}, _FUNCTION_SERIES: {}}
        seen = set()
        with localcontext() as context:
            context.prec = 50
            for group in groups:
                if group['currency'] != 'CNY':
                    raise CostReadError('存在非人民币费用，不能混合相加。', reason_code='CURRENCY_UNSUPPORTED')
                if function_metadata and _FUNCTION_SERIES not in group:
                    raise CostReadError('平台声明了函数计费分类，但缺少对应明细，未应用不完整金额。',
                                        reason_code='SCHEMA_UNSUPPORTED')
                if any(isinstance(name, str) and name.endswith('_series')
                       and name != _FUNCTION_SERIES for name in group):
                    raise CostReadError('费用包含未识别的明细类别，未应用不完整金额。',
                                        reason_code='CHARGE_TYPE_UNSUPPORTED')
                functions = group.get(_FUNCTION_SERIES, [])
                if not isinstance(functions, list):
                    raise CostReadError('平台函数费用明细不是列表，未应用不完整金额。',
                                        reason_code='CHARGE_TYPE_UNSUPPORTED')
                for category, series in ((_MODEL_SERIES, group[_MODEL_SERIES]),
                                         (_FUNCTION_SERIES, functions)):
                    _accumulate_series(series, category, categories[category], seen,
                                       left=left, right=right, begin=begin, finish=finish,
                                       seconds=seconds, expected_keys=expected_keys,
                                       scoped=tracking_ids is not None)
        if not any(categories.values()):
            raise NoUsageData('所选预算周期没有可核验的逐 Key 费用记录。')
        return categories
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        if isinstance(exc, CostReadError):
            raise
        raise CostReadError('平台费用结构变化，已停止应用；请保留原始账单核查。',
                            reason_code='SCHEMA_UNSUPPORTED') from None


def _sum_categories(categories):
    totals = {}
    with localcontext() as context:
        context.prec = 50
        for subtotal in categories.values():
            for key, amount in subtotal.items():
                totals[key] = totals.get(key, Decimal(0)) + amount
    return totals


def parse_cost_response(payload, url, start_date, end_date, *, tracking_ids=None):
    """Sum actual model/function costs; malformed parts reject the whole report."""
    return _sum_categories(_parse_categories(payload, url, start_date, end_date,
                                             tracking_ids=tracking_ids))


def parse_cost_response_with_functions_offline(payload, url, start_date, end_date,
                                               *, expected_tracking_ids):
    """Return category detail for explicit offline samples, not an action permit.

    The caller must supply all known identity bindings for this offline sample.
    Unknown identities are rejected, missing identities remain missing, and the
    result is deliberately not the production collection-result dictionary.
    Function category names come from each observed leaf's model field; no
    guessed Search/Fetch labels, usage counts, total fields, or quoted prices
    are added. Every result replaces a previous cumulative sample, not a delta.
    """
    if (not isinstance(expected_tracking_ids, (list, tuple, set, frozenset))
            or not expected_tracking_ids or not all(_valid_key(key) for key in expected_tracking_ids)
            or len(set(expected_tracking_ids)) != len(expected_tracking_ids)):
        raise CostReadError('离线候选需要明确且唯一的 Tracking ID 绑定。', reason_code='KEY_ID_INVALID')
    expected = frozenset(expected_tracking_ids)
    categories = _parse_categories(payload, url, start_date, end_date, expected_keys=expected)
    totals = _sum_categories(categories)
    return OfflineFunctionCostCandidate(totals, categories, expected.difference(totals))
