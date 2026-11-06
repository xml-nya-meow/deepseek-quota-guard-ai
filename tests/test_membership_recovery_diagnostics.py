"""Local recovery failures must not masquerade as a damaged shared registry.

Real DataStore/Gate/SQLite and export filesystem operations run in workspace
TEMP. Synthetic files are preserved; no Tk, network or user registry is used.
"""
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

from quota_guard.core import DataStore, GuardError, Person, TZ
from quota_guard import membership_recovery as membership
from quota_guard.responsibility_gate import ResponsibilityGate, ledger_anchor, ledger_id
from quota_guard.responsibility_registry import RegistryError


TEMP_ROOT = Path(__file__).resolve().parents[2] / 'TEMP'
ACCOUNT = 'synthetic-diagnostics-account'
KEYS = [{'name': name, 'tracking_id': 'synthetic-diagnostics-' + name} for name in ('X', 'Y')]
PRIVATE_DETAIL = 'synthetic-private-path-account-token-must-not-reach-the-message'


class MembershipRecoveryDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.folder = Path(tempfile.mkdtemp(prefix='membership-diagnostics-', dir=TEMP_ROOT))
        self.stores = []
        for key in KEYS:
            store = DataStore(self.folder / key['name'])
            store.initialize(2, '100', datetime.now(TZ).date().isoformat())
            store.save_people([Person(key['name'], Decimal(1))])
            store.bind_keys(KEYS, ACCOUNT)
            self.stores.append(store)
        self.gate = ResponsibilityGate(self.folder / 'shared' / 'responsibility')

    def assert_reason(self, code, callback):
        with self.assertRaises(RegistryError) as raised:
            callback()
        error = raised.exception
        self.assertEqual(error.reason_code, code)
        self.assertNotIn(PRIVATE_DETAIL, str(error))
        self.assertNotIn(str(self.folder), str(error))
        self.assertNotIn(ACCOUNT, str(error))
        return error

    @staticmethod
    def audits(store):
        with store._connect() as db:
            return [json.loads(row[0]) for row in db.execute(
                'SELECT payload FROM events WHERE kind=? ORDER BY id', (membership.AUDIT_KIND,))]

    def pending_plan(self):
        plan = membership.prepare_initial_plan(self.stores)
        self.gate.registry.initialize(membership_plan=plan, before_plan=membership.preflight)
        return plan

    def block_real_export(self, store):
        """Preserve the existing log and make real atomic replacement fail."""
        with store._connect() as db:
            day = db.execute('SELECT substr(created,1,10) FROM events ORDER BY id LIMIT 1').fetchone()[0]
        destination = store.folder / ('运行日志-' + day + '.jsonl')
        destination.relative_to(self.folder)
        backup = destination.with_name(destination.name + '.preserved')
        destination.rename(backup)
        destination.mkdir()

        def restore():
            # Only these exact synthetic paths move; artifacts are never deleted.
            destination.rename(destination.with_name(destination.name + '.blocking-directory'))
            backup.rename(destination)
        return destination, restore

    def recover_same_plan(self, saved):
        reopened = ResponsibilityGate(self.gate.registry.directory)
        self.assertEqual(reopened.reconcile(DataStore(self.stores[0].folder)), 0)
        record = reopened.registry.membership_plan()
        self.assertEqual(record['plan_id'], saved['plan_id'])
        self.assertEqual(record['payload'], saved['payload'])
        self.assertEqual(record['state'], 'complete')
        self.assertEqual(reopened.registry.snapshot()['phase'], 'ready')
        self.assertEqual(reopened.registry.open_obligations(), [])
        for member in record['payload']['members']:
            store = DataStore(member['path'])
            self.assertEqual(ledger_anchor(store), member['target_anchor'])
            audits = [a for a in self.audits(store) if a['plan_id'] == record['plan_id']]
            self.assertEqual(audits, [membership._audit(record['payload'], member)])
        # Recovery does not itself create any send authority. A later ordinary
        # claim remains a fresh declared receipt with no possible-send phase.
        receipt = reopened.claim(DataStore(self.stores[0].folder), uuid4().hex, KEYS[0]['tracking_id'])
        self.assertEqual(receipt.generation, 1)
        self.assertEqual(reopened.registry.open_obligations()[0]['phase'], 'declared')
        return reopened

    def test_real_initial_export_io_failure_keeps_plan_and_reopens_without_duplicate_audit(self):
        store = self.stores[0]
        destination, restore = self.block_real_export(store)
        # Prove this is the actual export wrapper around a filesystem failure,
        # not an injected exception pretending that export_logs was reached.
        with self.assertRaises(GuardError) as raw:
            store.export_logs()
        self.assertIsInstance(raw.exception.__cause__, OSError)
        self.assertTrue(destination.is_dir())

        error = self.assert_reason('REGISTRY_MEMBER_EXPORT_FAILED', lambda: self.gate.initialize(self.stores))
        self.assertIn('原迁移计划保留', str(error))
        saved = self.gate.registry.membership_plan()
        self.assertEqual(saved['state'], 'pending')
        self.assertEqual(self.gate.registry.snapshot()['ledgers'], [])
        self.assertEqual(self.gate.registry.open_obligations(), [])
        member = next(m for m in saved['payload']['members'] if m['ledger_id'] == ledger_id(store))
        self.assertEqual(ledger_anchor(store), member['target_anchor'])
        before = self.audits(store)
        self.assertEqual(len(before), 1)
        self.assert_reason('REGISTRY_MEMBER_EXPORT_FAILED', lambda: self.gate.claim(
            store, uuid4().hex, KEYS[0]['tracking_id']))
        self.assertEqual(self.audits(store), before)
        self.assertEqual(self.gate.registry.membership_plan(), saved)
        self.assertEqual(self.gate.registry.open_obligations(), [])
        restore()
        self.recover_same_plan(saved)

    def test_real_enrollment_export_failure_preserves_original_roster_until_same_plan_recovers(self):
        self.gate.initialize(self.stores[:1])
        old = self.gate.registry.snapshot()
        _, restore = self.block_real_export(self.stores[1])
        self.assert_reason('REGISTRY_MEMBER_EXPORT_FAILED', lambda: self.gate.check(self.stores[1]))
        saved = self.gate.registry.membership_plan()
        state = self.gate.registry.snapshot()
        self.assertEqual(saved['payload']['mode'], 'enroll')
        self.assertEqual(saved['state'], 'pending')
        self.assertEqual(state['phase'], 'initializing')
        self.assertEqual(state['ledgers'], old['ledgers'])
        self.assertEqual(state['generation'], old['generation'])
        self.assertEqual(state['open_obligations'], [])
        restore()
        self.recover_same_plan(saved)

    def test_real_local_config_read_failure_reports_local_preflight_and_retries_original_plan(self):
        plan = self.pending_plan()
        path = self.stores[1].folder / DataStore.CONFIG
        original = path.read_bytes()
        path.write_text('{' + PRIVATE_DETAIL, encoding='utf-8')
        before = self.gate.registry.membership_plan()
        self.assert_reason('REGISTRY_MEMBER_READ_FAILED', lambda: self.gate.reconcile(self.stores[0]))
        self.assertEqual(self.gate.registry.membership_plan(), before)
        self.assertEqual(before['plan_id'], plan['plan_id'])
        self.assertTrue(all(ledger_anchor(s) is None for s in self.stores))
        self.assertTrue(all(not self.audits(s) for s in self.stores))
        path.write_bytes(original)
        self.recover_same_plan(before)

    def test_raw_local_write_and_final_verify_errors_are_stage_specific_and_sanitized(self):
        plan = self.pending_plan()
        saved = self.gate.registry.membership_plan()
        with patch.object(membership, '_apply_member', side_effect=OSError(PRIVATE_DETAIL)):
            self.assert_reason('REGISTRY_MEMBER_APPLY_FAILED', lambda: self.gate.reconcile(self.stores[0]))
        self.assertEqual(self.gate.registry.membership_plan(), saved)
        self.assertTrue(all(ledger_anchor(s) is None for s in self.stores))

        # The outer verification callback is reached only after real local
        # apply (including its internal verification and logs) completes.
        def failing_final(_):
            raise GuardError(PRIVATE_DETAIL)
        self.assert_reason('REGISTRY_MEMBER_VERIFY_FAILED', lambda:
            self.gate.registry.execute_membership_plan(plan['plan_id'], preflight=membership.preflight,
                apply=membership.apply, verify=failing_final))
        self.assertEqual(self.gate.registry.membership_plan(), saved)
        self.assertEqual(membership.preflight(plan), {'before': 0, 'target': 2})
        self.recover_same_plan(saved)

    def test_initial_before_plan_failure_never_claims_a_durable_plan_exists(self):
        with patch.object(membership, 'preflight', side_effect=OSError(PRIVATE_DETAIL)):
            error = self.assert_reason('REGISTRY_MEMBER_PRECHECK_FAILED', lambda: self.gate.initialize(self.stores))
        self.assertIn('本次计划尚未保存', str(error))
        self.assertNotIn('原迁移计划保留', str(error))
        self.assertTrue(self.gate.registry.anchor_path.exists())
        self.assertTrue(self.gate.registry.db_path.exists())
        with sqlite3.connect(self.gate.registry.db_path) as db:
            self.assertEqual(db.execute('SELECT name FROM sqlite_master WHERE type="table"').fetchall(), [])
        self.assertTrue(all(ledger_anchor(s) is None for s in self.stores))
        # Existing conservative initialization behaviour is unchanged: this
        # incomplete creation is not silently reset or called a retriable plan.
        self.assert_reason('REGISTRY_INVALID', lambda: self.gate.registry.snapshot())
        self.assert_reason('REGISTRY_INVALID', lambda: self.gate.initialize(self.stores))

    def test_enrollment_before_plan_failure_keeps_previous_complete_plan_and_can_retry(self):
        self.gate.initialize(self.stores[:1])
        before = self.gate.registry.snapshot()
        old_audits = self.audits(self.stores[0])
        with patch.object(membership, 'preflight', side_effect=GuardError(PRIVATE_DETAIL)):
            error = self.assert_reason('REGISTRY_MEMBER_PRECHECK_FAILED', lambda: self.gate.check(self.stores[1]))
        self.assertIn('本次计划尚未保存', str(error))
        self.assertNotIn('原迁移计划保留', str(error))
        self.assertEqual(self.gate.registry.snapshot(), before)
        self.assertEqual(self.audits(self.stores[0]), old_audits)
        self.assertIsNone(ledger_anchor(self.stores[1]))
        reopened = ResponsibilityGate(self.gate.registry.directory)
        reopened.check(DataStore(self.stores[1].folder))
        self.assertEqual(reopened.registry.snapshot()['phase'], 'ready')
        self.assertEqual(len(reopened.registry.registered_ledgers()), 2)
        self.assertNotEqual(reopened.registry.membership_plan()['plan_id'], before['membership_plan']['plan_id'])

    def test_known_identity_mismatch_keeps_original_reason_and_never_writes_target(self):
        self.pending_plan()
        with self.stores[1]._connect() as db:
            db.execute('UPDATE meta SET value=? WHERE key=?', ('synthetic-other-account', 'account_id'))
        saved = self.gate.registry.membership_plan()
        self.assert_reason('REGISTRY_LEDGER_MISMATCH', lambda: self.gate.reconcile(self.stores[0]))
        self.assertEqual(self.gate.registry.membership_plan(), saved)
        self.assertTrue(all(ledger_anchor(s) is None for s in self.stores))
        self.assertEqual(self.gate.registry.open_obligations(), [])

    def test_actual_registry_structure_and_anchor_damage_keep_invalid_code_without_callbacks(self):
        self.pending_plan()
        anchor_path = self.gate.registry.anchor_path
        original = anchor_path.read_bytes()
        damaged = json.loads(original)
        damaged['revision'] += 1
        anchor_path.write_text(json.dumps(damaged), encoding='utf-8')
        callbacks = Mock(side_effect=AssertionError('No callback may run against a damaged registry'))
        with patch.object(membership, 'preflight', callbacks):
            self.assert_reason('REGISTRY_INVALID', lambda: self.gate.reconcile(self.stores[0]))
        callbacks.assert_not_called()
        anchor_path.write_bytes(original)
        with sqlite3.connect(self.gate.registry.db_path) as db:
            db.execute('DROP TABLE obligation_recovery')
        with patch.object(membership, 'preflight', callbacks):
            self.assert_reason('REGISTRY_INVALID', lambda: self.gate.reconcile(self.stores[0]))
        callbacks.assert_not_called()
        self.assertTrue(all(ledger_anchor(s) is None for s in self.stores))

    def test_external_anchor_publish_failure_remains_registry_error_not_local_callback_error(self):
        plan = self.pending_plan()
        saved = self.gate.registry.membership_plan()
        with patch.object(self.gate.registry, '_publish_anchor', side_effect=OSError(PRIVATE_DETAIL)):
            self.assert_reason('REGISTRY_INVALID', lambda: self.gate.reconcile(self.stores[0]))
        self.assertEqual(self.gate.registry.membership_plan(), saved)
        self.assertEqual(membership.preflight(plan), {'before': 0, 'target': 2})
        self.recover_same_plan(saved)


if __name__ == '__main__':
    unittest.main()
