"""Real child-process exits during membership writes; synthetic local files only.

The child exits without Python cleanup while the shared transaction is open.
SQLite must recover its own journal; production code must then finish the same
saved plan. These tests do not simulate power loss or repair mismatched backups.
"""
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from uuid import uuid4

from quota_guard.core import DataStore, Person, TZ
from quota_guard.responsibility_gate import ResponsibilityGate, ledger_anchor, ledger_id


TEMP_ROOT = Path(__file__).resolve().parents[2] / 'TEMP'
ACCOUNT = 'synthetic-membership-process-account'
KEYS = [{'name': name, 'tracking_id': 'synthetic-process-' + name} for name in ('X', 'Y', 'Z')]
CHILD = r'''
import os, sys
from pathlib import Path
from unittest.mock import patch
from quota_guard.core import DataStore
from quota_guard.responsibility_gate import ResponsibilityGate
from quota_guard import membership_recovery as recovery

base, mode, boundary = Path(sys.argv[1]), sys.argv[2], sys.argv[3]
gate = ResponsibilityGate(base / 'shared' / 'responsibility')
original_apply = recovery._apply_member
original_verify = recovery.verify
def exit_after_member(plan, member):
    original_apply(plan, member)
    os._exit(73)
def exit_after_all(plan):
    original_verify(plan)
    os._exit(73)
hook = '_apply_member' if boundary == 'first' else 'verify'
callback = exit_after_member if boundary == 'first' else exit_after_all
with patch.object(recovery, hook, side_effect=callback):
    if mode == 'initialize':
        gate.initialize([DataStore(base / label) for label in ('X', 'Y', 'Z')])
    else:
        gate.check(DataStore(base / 'Z'))
raise AssertionError('The planned crash boundary was not reached')
'''


class MembershipProcessRecoveryTests(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='membership-process-', dir=TEMP_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.stores = []
        for key in KEYS:
            store = DataStore(self.base / key['name'])
            store.initialize(3, '300', datetime.now(TZ).date().isoformat())
            store.save_people([Person(key['name'], Decimal(0))])
            store.bind_keys(KEYS, ACCOUNT)
            self.stores.append(store)
        self.gate = ResponsibilityGate(self.base / 'shared' / 'responsibility')

    def crash_and_recover(self, mode, boundary):
        if mode == 'enroll':
            self.gate.initialize(self.stores[:2])
        result = subprocess.run([sys.executable, '-B', '-c', CHILD, str(self.base), mode, boundary],
                                cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, encoding='utf-8', timeout=25)
        self.assertEqual(result.returncode, 73, result.stdout + result.stderr)
        state = self.gate.registry.snapshot()
        self.assertEqual(state['phase'], 'initializing')
        saved = self.gate.registry.membership_plan()
        self.assertEqual(saved['state'], 'pending')
        self.assertEqual({m['ledger_id'] for m in saved['payload']['members']},
                         {ledger_id(s) for s in self.stores})
        self.assertEqual(len(state['ledgers']), 0 if mode == 'initialize' else 2,
                         'Uncommitted shared membership must be rolled back by SQLite.')
        targets = sum(ledger_anchor(DataStore(m['path'])) == m['target_anchor']
                      for m in saved['payload']['members'])
        self.assertEqual(targets, 1 if boundary == 'first' else 3)
        # New Gate and new DataStore objects recover only the original saved
        # paths. No constructor, raw SQL or test-only reset repairs the state.
        reopened = ResponsibilityGate(self.gate.registry.directory)
        self.assertEqual(reopened.reconcile(DataStore(self.stores[0].folder)), 0)
        current = reopened.registry.membership_plan()
        self.assertEqual(current['plan_id'], saved['plan_id'])
        self.assertEqual(current['state'], 'complete')
        self.assertEqual(current['payload'], saved['payload'])
        self.assertEqual(reopened.registry.snapshot()['phase'], 'ready')
        self.assertEqual(len(reopened.registry.registered_ledgers()), 3)
        for member in current['payload']['members']:
            self.assertEqual(ledger_anchor(DataStore(member['path'])), member['target_anchor'])
        self.assertEqual(reopened.registry.open_obligations(), [])
        # Two ordinary operations can subsequently pass the actual gate. Each
        # produces one original receipt and one possible-send transition; no
        # network request or synthetic deletion is done in this pure test.
        receipts = []
        for store, key in zip(self.stores[:2], KEYS[:2]):
            receipt = reopened.claim(store, uuid4().hex, key['tracking_id'])
            receipts.append(receipt)
            store.record_action(key['name'], key['tracking_id'], 'pending', 'synthetic post-recovery',
                                operation_id=receipt.operation_id, responsibility=receipt.to_dict())
            def requested():
                store.record_action(key['name'], key['tracking_id'], 'requested', 'synthetic prepared',
                    operation_id=receipt.operation_id,
                    recovery_evidence={'schema': 1, 'account_id': ACCOUNT,
                        'target_tracking_id': key['tracking_id'],
                        'known_tracking_ids': [k['tracking_id'] for k in KEYS],
                        'source': 'prepared_http_inventory_v1'})
            self.assertTrue(reopened.possible_send(store, receipt, write_requested=requested))
            store.record_action(key['name'], key['tracking_id'], 'absent_verified', 'synthetic absence',
                                operation_id=receipt.operation_id, continuation=True)
            self.assertTrue(reopened.settle(store, receipt))
        self.assertEqual([r.generation for r in receipts], [1, 2])
        self.assertNotEqual(receipts[0].operation_id, receipts[1].operation_id)
        self.assertEqual(reopened.registry.open_obligations(), [])
        return reopened, saved

    def test_initialization_recovers_after_child_exits_after_first_local_commit(self):
        self.crash_and_recover('initialize', 'first')

    def test_initialization_recovers_after_all_local_commits_before_shared_commit(self):
        self.crash_and_recover('initialize', 'all')

    def test_enrollment_recovers_after_child_exits_during_roster_change(self):
        self.crash_and_recover('enroll', 'first')


if __name__ == '__main__':
    unittest.main()
