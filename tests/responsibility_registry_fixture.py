"""Synthetic plan fixtures for registry-only tests.

The no-op callbacks intentionally make no claim to test real ledger evidence;
Gate/membership integration suites provide that separate verification.
"""
from hashlib import sha256
import json
from uuid import uuid4


def roster(ids):
    return sha256(json.dumps(sorted(ids), separators=(',', ':')).encode('ascii')).hexdigest()


def plan_for(ids, paths, *, operations=(), snapshot=None):
    before = [] if snapshot is None else [
        {key: member[key] for key in ('ledger_id', 'path', 'checkpoint')}
        for member in snapshot['ledgers']]
    epoch = uuid4().hex if snapshot is None else snapshot['epoch']
    base = 0 if snapshot is None else snapshot['generation']
    checkpoints = {member['ledger_id']: member['checkpoint'] for member in before}
    for identity in ids:
        checkpoints.setdefault(identity, 0)
    ops = []
    for index, (identity, opid, target) in enumerate(sorted(operations), start=1):
        ops.append({'ledger_id': identity, 'operation_id': opid, 'target_hash': target,
                    'generation': base + index, 'prior_checkpoint': checkpoints[identity]})
        checkpoints[identity] = base + index
    members = {member['ledger_id']: member for member in before}
    members.update({identity: {'ledger_id': identity, 'path': str(path.resolve())}
                    for identity, path in zip(ids, paths)})
    target_roster = roster(members)
    before_lookup = {member['ledger_id']: member for member in before}
    result = {
        'schema': 1, 'plan_id': uuid4().hex, 'epoch': epoch,
        'mode': 'initialize' if snapshot is None else 'enroll',
        'base_generation': base, 'base_revision': 0 if snapshot is None else snapshot['revision'],
        'before_members': sorted(before, key=lambda member: member['ledger_id']),
        'members': [], 'operations': ops, 'target_roster_hash': target_roster,
    }
    for identity, member in sorted(members.items()):
        previous = before_lookup.get(identity)
        result['members'].append({
            'ledger_id': identity, 'path': member['path'],
            'account_hash': sha256(b'synthetic-account').hexdigest(),
            'history_digest': sha256(b'synthetic-history').hexdigest(),
            'before_anchor': None if previous is None else {
                'epoch': epoch, 'checkpoint': previous['checkpoint'], 'roster_hash': roster(before_lookup)},
            'target_anchor': {'epoch': epoch, 'checkpoint': checkpoints[identity],
                              'roster_hash': target_roster},
        })
    return result


def complete_plan(registry, plan):
    return registry.execute_membership_plan(plan['plan_id'],
        preflight=lambda _: None, apply=lambda _: None, verify=lambda _: None)
