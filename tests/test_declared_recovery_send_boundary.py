"""A committed local send witness survives failure of the shared phase write.

Only synthetic on-disk ledgers and an injected registry are used. No GUI,
browser, network or remote deletion occurs. Keep test artifacts for review.
"""
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from quota_guard.core import DataStore, GuardError, Person, TZ
from quota_guard.declared_recovery import SEND_PREFIX, TOMBSTONE_PREFIX
from quota_guard.responsibility_gate import ResponsibilityGate, action_history


TEMP_ROOT = Path(__file__).resolve().parents[2] / 'TEMP'
ACCOUNT = 'synthetic-boundary-account'
TARGET = 'synthetic-boundary-target'
OTHER = 'synthetic-boundary-other'
CONTROL = 'synthetic-boundary-control'
NAME = 'Synthetic boundary original'
OTHER_NAME = 'Synthetic boundary other'


class DeclaredRecoverySendBoundaryTests(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.folder = Path(tempfile.mkdtemp(prefix='declared-send-boundary-', dir=TEMP_ROOT))
        self.keys = [
            {'tracking_id': TARGET, 'name': NAME},
            {'tracking_id': OTHER, 'name': OTHER_NAME},
            {'tracking_id': CONTROL, 'name': 'Synthetic independent control'},
        ]
        self.first = self.make_store('X', NAME)
        self.second = self.make_store('Y', OTHER_NAME)
        self.directory = self.folder / 'responsibility'
        self.gate = ResponsibilityGate(self.directory)
        self.gate.initialize([self.first, self.second])

    def make_store(self, directory, name):
        store = DataStore(self.folder / directory)
        store.initialize(2, '100', datetime.now(TZ).date().isoformat())
        store.save_people([Person(name, Decimal(0))])
        store.bind_keys(self.keys, ACCOUNT)
        return store

    @staticmethod
    def meta(store, key):
        with store._connect() as db:
            row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return None if row is None else json.loads(row[0])

    @staticmethod
    def events(store):
        with store._connect() as db:
            return list(db.execute('SELECT * FROM events ORDER BY id'))

    def test_local_witness_then_shared_publish_failure_only_allows_original_observation(self):
        receipt = self.gate.claim(self.first, uuid4().hex, TARGET)
        self.first.record_action(NAME, TARGET, 'pending', 'synthetic pending',
            operation_id=receipt.operation_id, responsibility=receipt.to_dict())
        original_shared = (self.gate.registry.db_path.read_bytes(),
                           self.gate.registry.anchor_path.read_bytes())

        def requested():
            self.first.record_action(NAME, TARGET, 'requested', 'synthetic requested',
                operation_id=receipt.operation_id,
                recovery_evidence={'schema': 1, 'account_id': ACCOUNT,
                    'target_tracking_id': TARGET, 'known_tracking_ids': [TARGET, OTHER, CONTROL],
                    'source': 'prepared_http_inventory_v1'})

        # requested + audit export + local SEND witness all succeed. Only the
        # subsequent registry publication fails, before replacing its anchor.
        with patch.object(self.gate.registry, '_publish_anchor',
                          side_effect=OSError('synthetic failure before shared publication')):
            with self.assertRaises(GuardError):
                self.gate.possible_send(self.first, receipt, write_requested=requested)
        self.assertEqual((self.gate.registry.db_path.read_bytes(),
                          self.gate.registry.anchor_path.read_bytes()), original_shared)
        self.assertEqual(self.gate.registry.open_obligations()[0]['phase'], 'declared')
        witness = {'schema': 1, 'receipt': receipt.to_dict()}
        self.assertEqual(self.meta(self.first, SEND_PREFIX + receipt.operation_id), witness)
        self.assertEqual([a['status'] for a in action_history(self.first)], ['pending', 'requested'])

        gate = ResponsibilityGate(self.directory)
        original = DataStore(self.first.folder)
        before = self.events(original)
        for recover in (lambda: gate.reconcile(original),
                        lambda: gate.recover_declared(original, receipt)):
            with self.assertRaises(GuardError):
                recover()
            self.assertEqual(self.events(original), before)
            self.assertIsNone(gate.registry.recovery_for(receipt)['recovery_id'])
            self.assertIsNone(self.meta(original, TOMBSTONE_PREFIX + receipt.operation_id))
        with self.assertRaises(GuardError):
            gate.claim(self.second, uuid4().hex, OTHER)

        # These are synthetic independent observation results on the same
        # original operation. Presence cannot discharge uncertain submission.
        self.assertEqual(set(original.recovery_tracking_ids(TARGET, receipt.operation_id)),
                         {TARGET, OTHER, CONTROL})
        original.record_action(NAME, TARGET, 'present_verified', 'synthetic present observation',
            operation_id=receipt.operation_id, continuation=True)
        self.assertFalse(gate.settle(original, receipt))
        self.assertEqual(gate.registry.open_obligations()[0]['receipt'], receipt.to_dict())
        with self.assertRaises(GuardError):
            gate.claim(self.second, uuid4().hex, OTHER)

        original.record_action(NAME, TARGET, 'absent_verified', 'synthetic absent observation',
            operation_id=receipt.operation_id, continuation=True)
        self.assertTrue(gate.settle(original, receipt))
        self.assertEqual(gate.registry.open_obligations(), [])
        self.assertEqual(self.meta(original, SEND_PREFIX + receipt.operation_id), witness)
        history = action_history(original)
        self.assertEqual([a['status'] for a in history],
                         ['pending', 'requested', 'present_verified', 'absent_verified'])
        self.assertEqual({a['operation_id'] for a in history}, {receipt.operation_id})
        self.assertIsNone(self.meta(original, TOMBSTONE_PREFIX + receipt.operation_id))
        next_receipt = gate.claim(self.second, uuid4().hex, OTHER)
        self.assertNotEqual(next_receipt.operation_id, receipt.operation_id)


if __name__ == '__main__':
    unittest.main()
