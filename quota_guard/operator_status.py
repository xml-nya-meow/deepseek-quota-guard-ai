"""Closed presentation vocabulary for local operator collection failures.

This module imports no model or browser runtime. Labels describe the recorded
stage only; they cannot grant fee, identity, or deletion authority.
"""

from .core import GuardError

AI_COLLECTION_LABELS = {
    'AI_ASSETS_INVALID': '本地 AI 文件待修复',
    'AI_START_FAILED': '本地 AI 启动失败',
    'AI_START_TIMEOUT': '本地 AI 启动超时',
    'AI_TIMEOUT': 'AI 规划超时',
    'AI_TRANSPORT_FAILED': '本地 AI 通信中断',
    'AI_OUTPUT_INVALID': 'AI 动作未通过校验',
    'AI_CONTEXT_EXCEEDED': '页面信息超出 AI 容量',
    'AI_INPUT_INVALID': 'AI 任务或观察无效',
    'AI_BUSY': '本地 AI 正忙',
    'AI_RUNTIME_CLOSED': '本地 AI 已停止',
    'AI_CLEANUP_PENDING': '本地 AI 退出待确认',
    'AI_OPERATOR_BLOCKED': 'AI 页面操作未完成',
    'AI_OPERATOR_TIMEOUT': 'AI 页面操作超时',
    'AI_OPERATOR_ACTION_LIMIT': 'AI 页面操作达到步数上限',
    'AI_OPERATOR_SCOPE_REJECTED': 'AI 请求范围已阻止',
    'AI_OPERATOR_WRITE_REJECTED': 'AI 写入请求已阻止',
    'AI_OPERATOR_STALE_REFERENCE': 'AI 页面控件已变化',
    'AI_OPERATOR_PAGE_CHANGED': 'AI 操作页面已变化',
    'AI_OPERATOR_ACTION_FAILED': 'AI 页面动作未完成',
    'AI_OPERATOR_UNSUPPORTED': 'AI 操作不支持',
    'AI_OPERATOR_RESPONSE_INVALID': 'AI 取得的页面响应无效',
    'AI_OPERATOR_RESULT_UNKNOWN': 'AI 操作结果待核实',
}

AI_COLLECTION_REASONS = frozenset(AI_COLLECTION_LABELS)


class OperatorCollectionError(GuardError):
    """A closed collection failure; never carries page text or authority."""
    def __init__(self, reason_code):
        if not isinstance(reason_code, str) or reason_code not in AI_COLLECTION_REASONS:
            raise ValueError('Unsupported operator collection reason')
        self.reason_code = reason_code
        super().__init__(AI_COLLECTION_LABELS[reason_code] + '；本次未取得可用费用结果，原金额保留。')
