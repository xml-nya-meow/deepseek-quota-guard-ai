"""Read-only explanations of a single diagnostic event, with a closed model vocabulary."""
from dataclasses import asdict, dataclass
import json

from .revocation_permit import PREPARATION_REASONS


# Every suggestion is application-authored. The model only chooses the reading order.
TOPICS = {
    'harness_managed': 'Harness 在登录后管理这类 Key。请在官网确认 App-managed 标记，保留这项绑定。',
    'read_only_check': '“检查删除准备（只读）”核对目标和管理会话。预检本身不会提交撤销。',
    'target_identity': '按完整 Tracking ID 和唯一绑定核对选中目标。列表顺序、名称位置和空行都不能单独确定身份。',
    'verify_platform': '查看处理汇报，选中原目标后使用“核实平台状态”做只读核对。结果不明时请保留原记录，避免重复提交。',
    'inflight_cost': '平台列表的观察有时间点。在途请求和延迟账单仍可能产生费用。',
    'fee_scope': '核对统计起点、UTC+8 日期范围和受管人员绑定。空白或设为 -1 的行会跳过。',
    'baseline': '核对官方账单覆盖的起止日期及账本期间。未知费用保持未知，不能用零元替代。',
    'login': '在软件的独立浏览器核验登录，进入 API keys 列表，点一下页面空白处，再回到软件做只读检查。',
    'local_save': '查看处理汇报，对照本次费用是否写入。若提示待恢复，请先完成本地恢复，保留原账本。',
    'target_lock': '正常关闭占用同一目标的其他管理器，等待它完成清理。保留现有账本和互斥保护。',
    'share_diagnostics': '若问题重现，可向开发者提供程序版本、错误码和复现步骤。分享日志前移除凭证和账户信息。',
}

STAGES = {
    'preflight': '删除准备', 'key_list': '平台 Key 列表核验',
    'fee_read': '读取费用', 'fee_apply': '费用入账', 'revocation': '撤销处理',
    'verification': '平台状态核实',
}
FEE_REASONS = {
    'NO_USAGE_DATA', 'NO_SELECTED_USAGE_DATA', 'NO_MANAGED_KEYS', 'AUTH_REQUIRED',
    'SESSION_UNAVAILABLE', 'NETWORK_ERROR', 'HTTP_ERROR', 'PLATFORM_REJECTED',
    'PERIOD_INCOMPLETE', 'PERIOD_MISMATCH', 'COST_SCOPE_MISMATCH',
    'CHARGE_TYPE_UNSUPPORTED', 'SCHEMA_UNSUPPORTED', 'SOURCE_MISMATCH',
    'TIMEZONE_MISMATCH', 'KEY_ID_INVALID', 'KEY_ID_UNBOUND', 'INVALID_KEY_SCOPE',
    'BUCKET_MISALIGNED', 'BUCKET_UNSUPPORTED', 'DUPLICATE_COST_BUCKET',
    'AMOUNT_INVALID', 'CURRENCY_UNSUPPORTED', 'REDIRECT_REJECTED',
    'BODY_TOO_LARGE', 'CONTENT_TYPE_UNSUPPORTED',
    'TARGET_IN_USE', 'TARGET_LOCK_FAILED', 'NO_MANAGED_BINDINGS',
    'NO_BOUND_COST_MATCH', 'CUMULATIVE_DECREASE', 'SNAPSHOT_CONFLICT',
    'COLLECTION_CONTEXT_CHANGED', 'PERIOD_CONFIG_MISMATCH', 'END_DATE_REGRESSION',
    'STALE_SNAPSHOT_REPLAY', 'LOCAL_CONTEXT_UNAVAILABLE', 'SNAPSHOT_DATA_INVALID',
    'ACCOUNT_NOT_BOUND', 'AUDIT_EXPORT_FAILED', 'POST_COMMIT_REFRESH_FAILED',
    'SNAPSHOT_PERSISTENCE_UNKNOWN', 'LOCAL_APPLICATION_FAILED',
}
REASONS = FEE_REASONS | PREPARATION_REASONS.keys() | {'OK', 'OBSERVED_ABSENT', 'OBSERVED_PRESENT', 'RESULT_UNKNOWN', 'UNCLASSIFIED'}


@dataclass(frozen=True)
class DiagnosticCase:
    stage: str
    reason: str
    fee: str = 'unknown'
    submission: str = 'unknown'
    verification: str = 'unknown'
    kind: str = 'unknown'

    def __post_init__(self):
        domains = ((self.stage, STAGES), (self.reason, REASONS),
                   (self.fee, ('persisted', 'not_written', 'unknown')),
                   (self.submission, ('not_submitted', 'unknown')),
                   (self.verification, ('absent', 'present', 'unknown')),
                   (self.kind, ('unknown', 'DSH')))
        if any(not isinstance(value, str) or value not in domain for value, domain in domains):
            raise ValueError('unsupported diagnostic category')

    def model_json(self):
        return json.dumps(asdict(self), sort_keys=True, separators=(',', ':'))


def case_from_event(event, fields):
    """Only literal enums leave the log reader. Never combine facts from different rows."""
    if not isinstance(fields, dict) or not isinstance(event, str):
        return None
    reason = fields.get('reason_code')
    if not isinstance(reason, str):
        reason = ''
    if event in {'revocation_preparation_failed', 'key_http_read_rejected'}:
        code = reason if reason in PREPARATION_REASONS else 'PREPARATION_FAILED'
        kind = 'DSH' if code == 'KEY_TARGET_NOT_DELETABLE' else 'unknown'
        if event == 'revocation_preparation_failed':
            return DiagnosticCase('preflight', code, submission='not_submitted', kind=kind)
        # A list can also be read *after* a submission. An error here proves no submission state.
        return DiagnosticCase('key_list', code, kind=kind)
    if event in {'collection_unavailable', 'cost_collection_rejected', 'cost_collection_skipped'}:
        return DiagnosticCase('fee_read', reason if reason in FEE_REASONS else 'UNCLASSIFIED')
    if event in {'collection_application_failed', 'collection_application_completed'}:
        persistence = fields.get('persistence_state')
        state = persistence if isinstance(persistence, str) and persistence in {'not_written', 'persisted'} else 'unknown'
        code = 'OK' if event.endswith('_completed') and reason == 'OK' else (
            reason if reason in FEE_REASONS else 'UNCLASSIFIED')
        return DiagnosticCase('fee_apply', code, fee=state)
    if event == 'revocation_outcome' and fields.get('status') == 'unknown_result':
        return DiagnosticCase('revocation', 'RESULT_UNKNOWN')
    if event == 'browser_action_completed' and fields.get('action') in ('verify_revoke', 'revoke'):
        if fields.get('outcome') == 'absent_verified':
            return DiagnosticCase('verification', 'OBSERVED_ABSENT', verification='absent')
        if fields.get('action') == 'verify_revoke' and fields.get('outcome') == 'present_verified':
            return DiagnosticCase('verification', 'OBSERVED_PRESENT', verification='present')
    return None


# Two explicitly marked teaching examples also work on a fresh offline installation.
EXAMPLES = (
    DiagnosticCase('preflight', 'KEY_TARGET_NOT_DELETABLE', submission='not_submitted', kind='DSH'),
    DiagnosticCase('fee_read', 'CHARGE_TYPE_UNSUPPORTED'),
)


def facts(case):
    """Keep the evidence above any AI-ranked help, including when the runner fails."""
    if case.stage == 'preflight':
        reason = PREPARATION_REASONS.get(case.reason) or '删除准备未通过。'
        return reason + ' 本条记录发生在提交之前。'
    if case.stage == 'key_list':
        return '该次列表读取被拒绝。只凭这条记录无法判断是否提交过撤销，也无法判断 Key 当前是否存在。'
    if case.stage == 'verification':
        if case.verification == 'present':
            return '该次只读核验时目标仍在平台列表中。此观察不能证明 Key 可调用，也不能排除旧请求随后生效。'
        return '在该次核验中，目标已从平台列表消失，原因没有确定。'
    if case.stage == 'revocation':
        return '该次撤销结果未知。后续操作应先核实平台状态，禁止自动重试。'
    if case.stage == 'fee_read':
        if case.reason == 'CHARGE_TYPE_UNSUPPORTED':
            return '平台返回了当前程序未识别的费用类别。本次没有可核验的新金额，不能据此认定账户不匹配。'
        if case.reason == 'NO_MANAGED_KEYS':
            return '本次没有受管 Key，程序跳过了费用查询。'
        if case.reason in {'NO_USAGE_DATA', 'NO_SELECTED_USAGE_DATA'}:
            return '这次查询没有受管 Key 的有效明细。空明细不能证明累计费用为零。'
        return '该次读取没有取得可用的费用结果；旧金额不代表该次查询已成功入账。'
    detail = {
        'TARGET_IN_USE': '读取结果已返回，本机目标互斥被占用。',
        'TARGET_LOCK_FAILED': '无法建立目标互斥；原因尚未确认。',
        'NO_BOUND_COST_MATCH': '本次费用与本账本受管 Key 的绑定没有交集。',
        'STALE_SNAPSHOT_REPLAY': '本次来源比已采用的费用快照旧。',
        'CUMULATIVE_DECREASE': '新累计低于原记录，请核对统计期间及平台账单修订。',
        'AUDIT_EXPORT_FAILED': '账本与导出文件是两个写入步骤；本次记录导出失败。',
    }.get(case.reason, '')
    return detail + {
        'persisted': '该事件记录费用快照已写入账本。当前是否可用于自动管理，仍要看本轮核验。',
        'not_written': '该事件记录本次费用没有写入账本，原费用仍应保留。',
        'unknown': '该事件无法确认费用是否已经写入账本，请对照处理汇报。',
    }[case.fee]


def topics_for(case):
    if case.reason == 'KEY_TARGET_NOT_DELETABLE':
        return ('harness_managed', 'read_only_check', 'target_identity')
    if case.stage in {'verification', 'revocation'}:
        return ('verify_platform', 'inflight_cost')
    if case.reason in {'TARGET_IN_USE', 'TARGET_LOCK_FAILED'}:
        return ('target_lock', 'local_save', 'share_diagnostics')
    if case.reason in {'AUTH_REQUIRED', 'SESSION_UNAVAILABLE', 'KEY_HTTP_AUTH_REQUIRED',
                       'KEY_AUTH_HEADER_MISSING', 'KEY_SESSION_UNAVAILABLE', 'KEY_READ_REQUEST_NOT_OBSERVED'}:
        return ('login', 'share_diagnostics')
    if case.stage in {'key_list', 'preflight'}:
        return ('target_identity', 'read_only_check', 'share_diagnostics')
    if case.stage == 'fee_apply':
        return ('local_save', 'fee_scope', 'baseline')
    return ('fee_scope', 'baseline', 'share_diagnostics')


def explain(case, *, rank=None):
    """Return (source label, help text). Raw model prose and raw errors never enter the UI."""
    allowed = topics_for(case)
    try:
        if rank is None:
            from .local_ai_runtime import rank_topics
            rank = rank_topics
        ordered = rank(case.model_json(), allowed)
        if not isinstance(ordered, tuple) or len(ordered) != len(allowed) or set(ordered) != set(allowed):
            raise ValueError('invalid topic order')
        source = '本地 AI 已排列阅读建议'
    except Exception:
        ordered = allowed
        source = '本次未取得有效的 AI 排序，已使用规则建议。可检查是否解压了完整 Windows 包，包括 local-ai 文件夹。'
    return source, '\n\n'.join(f'{index}. {TOPICS[topic]}' for index, topic in enumerate(ordered, start=1))
