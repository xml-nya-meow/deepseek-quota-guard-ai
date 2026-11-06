"""Local evidence and forward-only writes for a durable membership plan.

The registry owns the exclusive cross-process transaction around these callbacks.
This module never opens that registry, discovers paths, grants permits, or contacts
a platform. A partially applied plan is resumed only from its exact saved intent.
"""
from hashlib import sha256
import json
import os
from pathlib import Path
from uuid import uuid4

from .core import DataStore, GuardError, _UNRESOLVED_ACTIONS
from .responsibility_registry import RegistryError, _plain, validate_membership_plan, validate_receipt


ANCHOR_KEY = 'responsibility_anchor_v1'
AUDIT_KIND = 'membership_recovery'


def _digest(value):
    return sha256(json.dumps(value, ensure_ascii=True, sort_keys=True,
                             separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _ledger_id(period_id):
    return sha256(period_id.encode('utf-8')).hexdigest()[:32]


def _roster(members):
    return sha256(json.dumps(sorted(m['ledger_id'] for m in members),
                             separators=(',', ':')).encode('ascii')).hexdigest()


def _canonical_path(value):
    path = Path(value).absolute()
    _plain(path, directory=True)
    resolved = path.resolve(strict=True)
    if os.path.normcase(str(path)) != os.path.normcase(str(resolved)):
        raise RegistryError('REGISTRY_LEDGER_MISMATCH')
    _plain(resolved / DataStore.CONFIG)
    _plain(resolved / DataStore.DATABASE)
    return str(resolved)


def _anchor(db):
    row = db.execute('SELECT value FROM meta WHERE key=?', (ANCHOR_KEY,)).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row[0])
        if (not isinstance(value, dict) or set(value) != {'epoch', 'checkpoint', 'roster_hash'}
                or type(value['checkpoint']) is not int or value['checkpoint'] < 0
                or not isinstance(value['epoch'], str) or len(value['epoch']) != 32
                or not isinstance(value['roster_hash'], str) or len(value['roster_hash']) != 64):
            raise ValueError()
        return value
    except (TypeError, ValueError):
        raise RegistryError('REGISTRY_LEDGER_MISMATCH') from None


def _unresolved(history):
    """Compute every operation, not the GUI's one-recovery-row-per-target view."""
    targets = {}
    for event in history:
        targets.setdefault(event['tracking_id'], []).append(event)
    result = []
    for target, events in targets.items():
        cutoff = max((e['id'] for e in events if e['status'] == 'absent_verified'), default=-1)
        operations = {}
        for event in events:
            if event['id'] > cutoff:
                operations.setdefault(event.get('operation_id'), []).append(event)
        pending = []
        for operation_id, records in operations.items():
            if DataStore._read_only_operation(records):
                continue
            latest = records[-1]
            if latest['status'] not in _UNRESOLVED_ACTIONS | {'present_verified'}:
                continue
            if (not operation_id or any(r.get('inherited_from_period') for r in records)):
                raise GuardError('旧责任缺少原操作编号或属于继承副本；成员计划不能补造或复制责任。')
            target_hash = sha256(target.encode('utf-8')).hexdigest()
            validate_receipt({'epoch': '0' * 32, 'ledger_id': '0' * 32,
                'operation_id': operation_id, 'target_hash': target_hash, 'generation': 1})
            pending.append({'operation_id': operation_id, 'target_hash': target_hash})
        if len(pending) > 1:
            raise GuardError('同一原账本的同一目标存在多笔未决操作；保持阻断，不能只取最后一笔责任。')
        result.extend(pending)
    if len({item['operation_id'] for item in result}) != len(result):
        raise GuardError('旧操作编号关联多个目标；成员计划不能合并或猜测责任。')
    return sorted(result, key=lambda item: item['operation_id'])


def _state(store, db):
    config = store.load(require_workbook=False)
    meta = dict(db.execute('SELECT key,value FROM meta'))
    if meta.get('period_id') != config.period_id or meta.get('account_id') != config.account_id:
        raise RegistryError('REGISTRY_LEDGER_MISMATCH')
    history = store._action_history(db)
    return {'ledger_id': _ledger_id(config.period_id),
            'account_hash': sha256(config.account_id.encode('utf-8')).hexdigest(),
            'history_digest': _digest(history), 'anchor': _anchor(db), 'history': history,
            'meta_keys': set(meta)}


def _capture(store):
    path = _canonical_path(store.folder)
    with store._connect() as db:
        db.execute('BEGIN')
        state = _state(store, db)
    return {'ledger_id': state['ledger_id'], 'path': path,
            'account_hash': state['account_hash'], 'history_digest': state['history_digest'],
            'before_anchor': state['anchor']}, state


def prepare_initial_plan(stores):
    """Capture an explicit original inventory before any shared/local writes."""
    stores = list(stores)
    if not stores or len(stores) > 1000:
        raise RegistryError('REGISTRY_LEDGER_MISMATCH')
    epoch = uuid4().hex
    members, operations = [], []
    for store in stores:
        member, state = _capture(store)
        if (state['anchor'] is not None or any(a.get('responsibility') for a in state['history'])
                or any(key.startswith(('declared_recovery_v1:', 'responsibility_send_boundary_v1:'))
                       for key in state['meta_keys'])):
            raise RegistryError('REGISTRY_LEDGER_MISMATCH')
        members.append(member)
        operations.extend({'ledger_id': member['ledger_id'], **item}
                          for item in _unresolved(state['history']))
    members.sort(key=lambda member: member['ledger_id'])
    if len({m['ledger_id'] for m in members}) != len(members):
        raise RegistryError('REGISTRY_LEDGER_MISMATCH')
    operations.sort(key=lambda item: (item['ledger_id'], item['operation_id']))
    if len({item['operation_id'] for item in operations}) != len(operations):
        raise GuardError('多个原账本使用同一旧操作编号；保持阻断，不能复制或合并责任。')
    checkpoints = {m['ledger_id']: 0 for m in members}
    for generation, operation in enumerate(operations, 1):
        operation['prior_checkpoint'] = checkpoints[operation['ledger_id']]
        operation['generation'] = generation
        checkpoints[operation['ledger_id']] = generation
    roster = _roster(members)
    for member in members:
        member['target_anchor'] = {'epoch': epoch,
            'checkpoint': checkpoints[member['ledger_id']], 'roster_hash': roster}
    return validate_membership_plan({'schema': 1, 'plan_id': uuid4().hex, 'epoch': epoch,
        'mode': 'initialize', 'base_generation': 0, 'base_revision': 0, 'before_members': [],
        'members': members, 'operations': operations, 'target_roster_hash': roster})


def prepare_enrollment_plan(snapshot, new_store):
    """The caller validates/synchronizes trusted successor references first."""
    if snapshot.get('phase') != 'ready' or snapshot.get('open_obligations'):
        raise RegistryError('REGISTRY_INITIALIZING')
    before = [{key: item[key] for key in ('ledger_id', 'path', 'checkpoint')}
              for item in snapshot['ledgers']]
    before.sort(key=lambda item: item['ledger_id'])
    old_roster = _roster(before)
    members = []
    for registered in before:
        member, state = _capture(DataStore(registered['path']))
        expected = {'epoch': snapshot['epoch'], 'checkpoint': registered['checkpoint'],
                    'roster_hash': old_roster}
        if member['ledger_id'] != registered['ledger_id'] or state['anchor'] != expected:
            raise RegistryError('REGISTRY_LEDGER_MISMATCH')
        if _unresolved(state['history']):
            raise GuardError('已登记成员仍有未决责任；不得通过扩员绕过原操作。')
        members.append(member)
    member, state = _capture(new_store)
    if (state['anchor'] is not None or member['ledger_id'] in {m['ledger_id'] for m in members}
            or _unresolved(state['history'])):
        raise RegistryError('REGISTRY_LEDGER_MISMATCH')
    members.append(member)
    members.sort(key=lambda item: item['ledger_id'])
    roster = _roster(members)
    for member in members:
        old = member['before_anchor']
        member['target_anchor'] = {'epoch': snapshot['epoch'],
            'checkpoint': 0 if old is None else old['checkpoint'], 'roster_hash': roster}
    return validate_membership_plan({'schema': 1, 'plan_id': uuid4().hex, 'epoch': snapshot['epoch'],
        'mode': 'enroll', 'base_generation': snapshot['generation'], 'base_revision': snapshot['revision'],
        'before_members': before, 'members': members, 'operations': [], 'target_roster_hash': roster})


def _audit(plan, member):
    return {'schema': 1, 'plan_id': plan['plan_id'], 'plan_hash': _digest(plan),
            **{key: member[key] for key in ('ledger_id', 'account_hash', 'history_digest',
                                            'before_anchor', 'target_anchor')}}


def _check_member(plan, member, store, db, *, require_target=False):
    if _canonical_path(store.folder) != member['path']:
        raise RegistryError('REGISTRY_LEDGER_MISMATCH')
    state = _state(store, db)
    if any(state[key] != member[key] for key in ('ledger_id', 'account_hash', 'history_digest')):
        raise RegistryError('REGISTRY_LEDGER_MISMATCH')
    expected_ops = sorted((op['operation_id'], op['target_hash']) for op in plan['operations']
                          if op['ledger_id'] == member['ledger_id'])
    actual_ops = sorted((op['operation_id'], op['target_hash']) for op in _unresolved(state['history']))
    if actual_ops != expected_ops:
        raise RegistryError('REGISTRY_RECEIPT_MISMATCH')
    audits = []
    for row in db.execute('SELECT payload FROM events WHERE kind=? ORDER BY id', (AUDIT_KIND,)):
        try:
            value = json.loads(row[0])
        except (TypeError, ValueError):
            raise RegistryError('REGISTRY_LEDGER_MISMATCH') from None
        if not isinstance(value, dict):
            raise RegistryError('REGISTRY_LEDGER_MISMATCH')
        if value.get('plan_id') == plan['plan_id']:
            audits.append(value)
    if state['anchor'] == member['target_anchor']:
        if audits != [_audit(plan, member)]:
            raise RegistryError('REGISTRY_LEDGER_MISMATCH')
        return 'target'
    if require_target or state['anchor'] != member['before_anchor'] or audits:
        raise RegistryError('REGISTRY_LEDGER_MISMATCH')
    return 'before'


def preflight(plan):
    """Validate every intended member before allowing any local plan writes."""
    plan = validate_membership_plan(plan)
    counts = {'before': 0, 'target': 0}
    for member in plan['members']:
        store = DataStore(member['path'])
        with store._connect() as db:
            db.execute('BEGIN')
            counts[_check_member(plan, member, store, db)] += 1
    return counts


def _apply_member(plan, member):
    """Fault-injection boundary: one exact member transaction, no shared calls."""
    store = DataStore(member['path'])
    with store._connect() as db:
        db.execute('BEGIN IMMEDIATE')
        state = _check_member(plan, member, store, db)
        if state == 'before':
            db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',
                       (ANCHOR_KEY, json.dumps(member['target_anchor'])))
            store._event(db, AUDIT_KIND, _audit(plan, member), dedup='membership:' + plan['plan_id'])
            _check_member(plan, member, store, db, require_target=True)
    # A committed witness survives an export failure. Replaying this same step
    # verifies its exact audit and retries export; it never duplicates the event.
    try:
        store.export_logs()
    except RegistryError:
        raise
    except (GuardError, OSError):
        # export_logs translates filesystem errors to GuardError (ValueError).
        # Preserve the local failure boundary instead of letting the registry
        # transaction misreport it as damage to the shared SQLite/anchor pair.
        raise RegistryError('REGISTRY_MEMBER_EXPORT_FAILED') from None
    return state == 'before'


def apply(plan):
    """Move only exact before members forward; registry must hold its lock."""
    plan = validate_membership_plan(plan)
    preflight(plan)
    changed = sum(bool(_apply_member(plan, member)) for member in plan['members'])
    verify(plan)
    return {'applied': changed, 'already_target': len(plan['members']) - changed}


def verify(plan):
    """Prove all local targets and exact audits; never accepts before states."""
    plan = validate_membership_plan(plan)
    for member in plan['members']:
        store = DataStore(member['path'])
        with store._connect() as db:
            db.execute('BEGIN')
            _check_member(plan, member, store, db, require_target=True)
    return {'verified': len(plan['members'])}
