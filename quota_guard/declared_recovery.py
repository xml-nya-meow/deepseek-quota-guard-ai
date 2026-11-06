"""Local half of recovery for a fenced, never-authorized claim.

The registry serializes the send-boundary witness and recovery preflight. This
module never contacts the platform, manufactures a pending action or grants a
permit. A tombstone is permanent for this exact original operation.
"""
from hashlib import sha256
import json
import re

from .core import GuardError
from .responsibility_registry import RegistryError, validate_receipt

TOMBSTONE_PREFIX = 'declared_recovery_v1:'
SEND_PREFIX = 'responsibility_send_boundary_v1:'
ANCHOR_KEY = 'responsibility_anchor_v1'


class DeclaredRecoveryClosed(GuardError):
    """A stale callback belongs to an already durably recovered operation."""


def reject_closed_operation(db, operation_id):
    if operation_id and db.execute('SELECT 1 FROM meta WHERE key=?',
                                  (TOMBSTONE_PREFIX + operation_id,)).fetchone():
        raise DeclaredRecoveryClosed('原操作已由持久恢复结清；迟到回调不再写入，也不会重新提交。')


def _json_row(db, key):
    row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
    if row is None:
        return None
    try:
        result = json.loads(row[0])
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except (TypeError, ValueError):
        raise RegistryError('REGISTRY_LEDGER_MISMATCH') from None


def _identity(store, receipt):
    config = store.load(require_workbook=False)
    if sha256(config.period_id.encode('utf-8')).hexdigest()[:32] != receipt.ledger_id:
        raise RegistryError('REGISTRY_LEDGER_MISMATCH')


def _records(store, db, receipt):
    records = [a for a in store._action_history(db) if a.get('operation_id') == receipt.operation_id]
    if any(a.get('responsibility') != receipt.to_dict()
           or sha256(a['tracking_id'].encode('utf-8')).hexdigest() != receipt.target_hash
           or a.get('read_only') or a.get('inherited_from_period') for a in records):
        raise RegistryError('REGISTRY_RECEIPT_MISMATCH')
    return records


def _digest(records):
    return sha256(json.dumps(records, sort_keys=True, ensure_ascii=True,
                             separators=(',', ':')).encode('utf-8')).hexdigest()


def mark_local_send_boundary(store, receipt):
    """Called inside registry's exclusive promotion transaction, before mark.

    Persist before the shared phase changes. A paired registry/anchor rollback
    to declared cannot then hide that this operation crossed this boundary.
    A failed subsequent registry commit remains conservative uncertainty.
    """
    receipt = validate_receipt(receipt)
    _identity(store, receipt)
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        reject_closed_operation(db, receipt.operation_id)
        anchor = _json_row(db, ANCHOR_KEY)
        records = _records(store, db, receipt)
        if (not anchor or anchor.get('epoch') != receipt.epoch
                or anchor.get('checkpoint') != receipt.generation
                or not records or records[-1]['status'] != 'requested'
                or _json_row(db, SEND_PREFIX + receipt.operation_id) is not None):
            raise RegistryError('REGISTRY_TRANSITION_REJECTED')
        witness = {'schema': 1, 'receipt': receipt.to_dict()}
        db.execute('INSERT INTO meta VALUES (?,?)',
                   (SEND_PREFIX + receipt.operation_id, json.dumps(witness)))
        store._event(db, 'responsibility_send_boundary', witness)


def _validated_state(store, db, fence):
    if (not isinstance(fence, dict)
            or set(fence) != {'receipt', 'prior_checkpoint', 'recovery_id'}
            or not isinstance(fence['recovery_id'], str)
            or re.fullmatch('[0-9a-f]{32}', fence['recovery_id']) is None):
        raise RegistryError('REGISTRY_RECEIPT_MISMATCH')
    receipt = validate_receipt(fence['receipt'])
    prior = fence['prior_checkpoint']
    if type(prior) is not int or not 0 <= prior < receipt.generation:
        raise GuardError('旧责任缺少发送前恢复协议与精确原检查点；保留责任，请只读核实原操作。')
    anchor = _json_row(db, ANCHOR_KEY)
    if (not anchor or anchor.get('epoch') != receipt.epoch
            or type(anchor.get('checkpoint')) is not int):
        raise RegistryError('REGISTRY_LEDGER_MISMATCH')
    if db.execute('SELECT 1 FROM meta WHERE key=?', (SEND_PREFIX + receipt.operation_id,)).fetchone():
        raise GuardError('原操作已有可能提交边界见证；不能按未发送恢复，请保留责任并只读核实。')
    records = _records(store, db, receipt)
    old = _json_row(db, TOMBSTONE_PREFIX + receipt.operation_id)
    if old is not None:
        if (set(old) != {'schema', 'receipt', 'recovery_id', 'prior_checkpoint',
                         'checkpoint_before', 'checkpoint_after', 'origin', 'history_digest'}
                or old['schema'] != 1 or any(old.get(k) != fence[k] for k in fence)
                or anchor['checkpoint'] != receipt.generation
                or old['checkpoint_after'] != receipt.generation):
            raise RegistryError('REGISTRY_RECEIPT_MISMATCH')
        if old['origin'] == 'claim_without_action':
            valid = (not records and old['checkpoint_before'] == prior
                     and old['history_digest'] == _digest([]))
        else:
            valid = (old['origin'] == 'recorded_intent' and len(records) >= 2
                     and old['checkpoint_before'] == receipt.generation
                     and records[-1]['status'] == 'cancelled'
                     and records[-1].get('responsibility_recovery_id') == fence['recovery_id']
                     and _digest(records[:-1]) == old['history_digest'])
        audits = [json.loads(row[0]) for row in db.execute(
            "SELECT payload FROM events WHERE kind='responsibility_recovery' ORDER BY id")]
        matching = [a for a in audits if a.get('receipt', {}).get('operation_id') == receipt.operation_id]
        if not valid or matching != [old]:
            raise RegistryError('REGISTRY_RECEIPT_MISMATCH')
        return receipt, anchor, records, old
    if not records:
        if anchor['checkpoint'] != prior:
            raise RegistryError('REGISTRY_LEDGER_MISMATCH')
    elif (anchor['checkpoint'] != receipt.generation
          or records[0]['status'] != 'pending'
          or any(a['status'] not in {'pending', 'requested', 'prepared', 'failed', 'unknown',
                     'unknown_result', 'cancelled', 'not_submitted', 'present_verified',
                     'observation_failed'} for a in records)):
        raise RegistryError('REGISTRY_TRANSITION_REJECTED')
    return receipt, anchor, records, None


def check_local_recoverable(store, fence):
    """Preflight under registry's exclusive lock, before persisting the fence."""
    _identity(store, validate_receipt(fence['receipt']))
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        _validated_state(store, db, fence)


def recover_local_declared(store, fence):
    """Append once, atomically advance the exact checkpoint, then export.

    A caller must first persist this exact recovery fence in the registry.
    Export failure leaves the same tombstone/audit for a bounded local retry.
    """
    _identity(store, validate_receipt(fence['receipt']))
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        receipt, anchor, records, audit = _validated_state(store, db, fence)
        if audit is None:
            audit = {'schema': 1, **fence, 'checkpoint_before': anchor['checkpoint'],
                     'checkpoint_after': receipt.generation,
                     'origin': 'recorded_intent' if records else 'claim_without_action',
                     'history_digest': _digest(records)}
            if records:
                payload = {k: v for k, v in records[-1].items() if k not in {'id', 'created', 'kind'}}
                payload.update(status='cancelled', responsibility_recovery_id=fence['recovery_id'],
                    detail='共享责任仍在提交前阶段，原操作已持久隔离；未签发删除许可。保留原提交意图，取消本次操作。')
                store._event(db, 'action', payload)
            db.execute('UPDATE meta SET value=? WHERE key=?',
                       (json.dumps({**anchor, 'checkpoint': receipt.generation}), ANCHOR_KEY))
            db.execute('INSERT INTO meta VALUES (?,?)',
                       (TOMBSTONE_PREFIX + receipt.operation_id, json.dumps(audit)))
            store._event(db, 'responsibility_recovery', audit)
    store.export_logs()
    return audit
