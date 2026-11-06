"""Bounded public identities retained for one operation's read-only recovery.

This value is neither a deletion permit nor a platform account identifier.
Only the prepared HTTP inventory may supply it before the original grant.
"""
from __future__ import annotations

import re

from .core import GuardError


RECOVERY_SOURCE = 'prepared_http_inventory_v1'
_FIELDS = {'schema', 'account_id', 'target_tracking_id', 'known_tracking_ids', 'source'}
_TRACKING = re.compile(r'[A-Za-z0-9_-]{8,200}')
_ACCOUNT = re.compile(r'[A-Za-z0-9_-]{1,200}')


def _public_identifier(value, pattern):
    return (isinstance(value, str) and pattern.fullmatch(value) is not None
            and not value.lower().startswith('sk-'))


def validate_recovery_evidence(value, *, target_tracking_id, account_id):
    """Return a fresh canonical value, rejecting extra fields and secret shapes."""
    if (not isinstance(value, dict) or set(value) != _FIELDS
            or type(value.get('schema')) is not int or value['schema'] != 1
            or value.get('source') != RECOVERY_SOURCE):
        raise GuardError('原操作的恢复证据格式无效，不能据此确认平台结果。')
    if (not _public_identifier(target_tracking_id, _TRACKING)
            or not _public_identifier(value.get('target_tracking_id'), _TRACKING)
            or value['target_tracking_id'] != target_tracking_id
            or not _public_identifier(account_id, _ACCOUNT)
            or value.get('account_id') != account_id):
        raise GuardError('原操作的恢复证据与当前账本或精确目标不一致。')
    known = value.get('known_tracking_ids')
    if (not isinstance(known, list) or not 2 <= len(known) <= 200
            or any(not _public_identifier(key, _TRACKING) for key in known)
            or len(set(known)) != len(known) or target_tracking_id not in known):
        raise GuardError('原操作的恢复证据缺少唯一完整目标或既有对照身份。')
    return {'schema': 1, 'account_id': account_id, 'target_tracking_id': target_tracking_id,
            'known_tracking_ids': sorted(known), 'source': RECOVERY_SOURCE}
