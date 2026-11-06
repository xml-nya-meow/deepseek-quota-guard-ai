"""Real App acceptance of interrupted membership plans with synthetic accounts.

Local member writes can survive a rolled-back shared transaction.  The tests
exercise actual App startup recovery and actual automatic/force permits, while
the browser transport remains an in-memory, counted fake.  No platform is used.
"""
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from quota_guard.browser_service import key_fingerprint
from quota_guard.core import DataStore, GuardError, Person, TZ
from quota_guard.responsibility_gate import (
    ANCHOR_KEY, ResponsibilityGate, _roster_hash, action_history, ledger_anchor, ledger_id,
)
from quota_guard.responsibility_registry import ResponsibilityRegistry
from quota_guard.revocation_permit import DeletePermit
from tests.test_responsibility_workflow import OfflineResponsibilityApp


TEMP_ROOT = Path(__file__).resolve().parents[2] / 'TEMP'


class MembershipRecoveryWorkflowTests(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='membership-app-', dir=TEMP_ROOT)
        self.base = Path(self.temporary.name).resolve()
        identity = uuid4().hex
        self.keys = [{'name': f'Synthetic {name}', 'tracking_id': f'membership-{identity}-{name}'}
                     for name in ('X', 'Y', 'Z', 'control')]
        self.account = key_fingerprint(self.keys)
        self.inventory = list(self.keys)
        self.stores = {}
        for name, key in zip(('X', 'Y', 'Z'), self.keys):
            store = DataStore(self.base / name)
            store.initialize(2, '100', datetime.now(TZ).date().isoformat())
            store.save_people([Person(key['name'], Decimal(0))])
            store.bind_keys(self.keys, self.account)
            self.stores[name] = store
        self.directory = self.base / 'synthetic-control' / 'responsibility'
        self.gate = ResponsibilityGate(self.directory)
        self.drivers = []
        self.grant_events = []
        actual_decide = DeletePermit.decide

        def counted_decide(permit, allowed):
            accepted = actual_decide(permit, allowed)
            if allowed is True and accepted:
                self.grant_events.append(permit.target_tracking_id)
            return accepted

        self.permit_patch = patch.object(DeletePermit, 'decide', counted_decide)
        self.permit_patch.start()

    def tearDown(self):
        try:
            for driver in reversed(self.drivers):
                if driver.app is not None:
                    driver.close(fixture_cleanup=True)
        finally:
            self.permit_patch.stop()
            self.temporary.cleanup()

    def key(self, name):
        return next(key for key in self.keys if key['name'] == 'Synthetic ' + name)

    def registry(self):
        return ResponsibilityRegistry(self.directory)

    def open(self, name, *, folder=None, outcome='unknown'):
        driver = OfflineResponsibilityApp(folder or self.stores[name].folder, self.directory,
            self.inventory, self.key(name)['tracking_id'], self.account, outcome=outcome)
        self.drivers.append(driver)
        return driver

    def interrupt_after_first_member(self, operation):
        # A real first member transaction/export succeeds.  The next callback
        # fails before touching that member; shared execute transaction rolls
        # back, while the already-committed local witness remains available.
        from quota_guard import membership_recovery
        actual_apply = membership_recovery._apply_member
        applied = []

        def cut(plan, member):
            if applied:
                raise GuardError('synthetic membership cut before second member')
            result = actual_apply(plan, member)
            applied.append(member['ledger_id'])
            return result

        with patch.object(membership_recovery, '_apply_member', side_effect=cut):
            with self.assertRaises(GuardError):
                operation()
        self.assertEqual(len(applied), 1)
        state = self.registry().snapshot()
        record = self.registry().membership_plan()
        self.assertEqual(state['phase'], 'initializing')
        self.assertEqual(record['state'], 'pending')
        self.assertEqual(self.grant_events, [])
        first = next(store for store in self.stores.values() if ledger_id(store) == applied[0])
        self.assertIsNotNone(ledger_anchor(first))
        return record, applied[0]

    def assert_ready(self, record, names):
        current = self.registry().membership_plan()
        self.assertEqual(current['plan_id'], record['plan_id'])
        self.assertEqual(current['payload'], record['payload'])
        self.assertEqual(current['state'], 'complete')
        self.assertEqual(self.registry().snapshot()['phase'], 'ready')
        members = self.registry().registered_ledgers()
        self.assertEqual({member['ledger_id'] for member in members},
                         {ledger_id(self.stores[name]) for name in names})
        roster = _roster_hash(members)
        for name in names:
            anchor = ledger_anchor(self.stores[name])
            self.assertEqual(anchor['roster_hash'], roster)
            registered = next(m for m in members if m['ledger_id'] == ledger_id(self.stores[name]))
            self.assertEqual(anchor['checkpoint'], registered['checkpoint'])
            planned = next(m for m in record['payload']['members'] if m['ledger_id'] == registered['ledger_id'])
            audits = [event for event in self.stores[name].events(10000)
                      if event['kind'] == 'membership_recovery' and event['plan_id'] == record['plan_id']]
            self.assertEqual(len(audits), 1)
            self.assertEqual(audits[0]['before_anchor'], planned['before_anchor'])
            self.assertEqual(audits[0]['target_anchor'], planned['target_anchor'])
        return current

    def assert_readonly_reopen(self, driver):
        self.assertEqual(driver.browser.calls, [])
        self.assertEqual(driver.browser.sends, 0)
        self.assertEqual(driver.browser.consumptions, 0)

    def success(self, name, mode):
        driver = self.open(name, outcome='success')
        before = len(self.grant_events)
        driver.launch(mode)
        driver.finish_or_block()
        self.assertEqual(len(self.grant_events) - before, 1)
        self.assertEqual(driver.browser.observed_grants, 1)
        self.assertEqual(driver.browser.consumptions, 1)
        self.assertEqual(driver.browser.sends, 1)
        history = action_history(driver.app.store)
        self.assertEqual([event['status'] for event in history], ['pending', 'requested', 'absent_verified'])
        operations = {event['operation_id'] for event in history}
        self.assertEqual(len(operations), 1)
        self.assertEqual(driver.browser.trace[-1]['event'], 'synthetic_send')
        self.assertEqual(self.registry().open_obligations(), [])
        self.inventory = list(driver.browser.inventory)
        driver.close()
        return operations.pop()

    def blocked(self, name, mode, *, folder=None):
        driver = self.open(name, folder=folder)
        before_history = action_history(driver.app.store)
        before_grants = len(self.grant_events)
        driver.launch(mode)
        driver.finish_or_block()
        self.assertEqual(driver.browser.sends, 0)
        self.assertEqual(driver.browser.consumptions, 0)
        self.assertNotIn('revoke', driver.browser.calls)
        self.assertEqual(len(self.grant_events), before_grants)
        self.assertEqual(action_history(driver.app.store), before_history)
        driver.close()

    def test_interrupted_initialization_reopens_original_plan_then_two_entries_send_once(self):
        record, first_id = self.interrupt_after_first_member(
            lambda: self.gate.initialize([self.stores['X'], self.stores['Y']]))
        self.assertEqual(self.registry().registered_ledgers(), [])
        first_store = next(store for store in self.stores.values() if ledger_id(store) == first_id)
        first_events = first_store.events(10000)
        reopened = self.open('X')
        self.assert_readonly_reopen(reopened)
        self.assert_ready(record, ('X', 'Y'))
        self.assertEqual(first_store.events(10000), first_events,
                         'A committed member witness must not acquire a duplicate audit on resume')
        self.assertEqual(self.grant_events, [])
        reopened.close()
        first_op = self.success('X', 'automatic')
        second_op = self.success('Y', 'force')
        self.assertNotEqual(first_op, second_op)
        self.assertEqual(len(self.grant_events), 2)

    def test_interrupted_enrollment_reopens_new_member_and_keeps_three_member_roster(self):
        self.gate.initialize([self.stores['X'], self.stores['Y']])
        initial = self.registry().membership_plan()
        record, _ = self.interrupt_after_first_member(lambda: self.gate.check(self.stores['Z']))
        self.assertNotEqual(record['plan_id'], initial['plan_id'])
        self.assertEqual(len(record['payload']['members']), 3)
        self.assertEqual({m['ledger_id'] for m in self.registry().registered_ledgers()},
                         {ledger_id(self.stores[name]) for name in ('X', 'Y')})
        reopened = self.open('Z')
        self.assert_readonly_reopen(reopened)
        self.assert_ready(record, ('X', 'Y', 'Z'))
        self.assertEqual(action_history(reopened.app.store), [])
        self.assertEqual(self.grant_events, [])
        reopened.close()
        first_op = self.success('X', 'automatic')
        second_op = self.success('Y', 'force')
        self.assertNotEqual(first_op, second_op)
        self.assertEqual({m['ledger_id'] for m in self.registry().registered_ledgers()},
                         {ledger_id(self.stores[name]) for name in ('X', 'Y', 'Z')})

    def test_unknown_member_witness_is_not_overwritten_and_both_entries_stay_blocked(self):
        self.gate.initialize([self.stores['X'], self.stores['Y']])
        record, _ = self.interrupt_after_first_member(lambda: self.gate.check(self.stores['Z']))
        bad = {**ledger_anchor(self.stores['Y']), 'roster_hash': 'f' * 64}
        with self.stores['Y']._connect() as db:
            db.execute('UPDATE meta SET value=? WHERE key=?', (json.dumps(bad), ANCHOR_KEY))
        # The automatic entry may collect a fresh read-only cost snapshot before
        # the membership gate refuses writes.  Preserve every other event and
        # every member anchor; a snapshot is not a recovery or deletion permit.
        histories = {name: [event for event in store.events(10000) if event['kind'] != 'snapshot']
                     for name, store in self.stores.items()}
        anchors = {name: ledger_anchor(store) for name, store in self.stores.items()}
        self.blocked('X', 'automatic')
        self.blocked('Z', 'force')
        self.assertEqual(ledger_anchor(self.stores['Y']), bad)
        self.assertEqual(self.registry().membership_plan(), record)
        self.assertEqual(self.registry().snapshot()['phase'], 'initializing')
        self.assertEqual({name: [event for event in store.events(10000) if event['kind'] != 'snapshot']
                          for name, store in self.stores.items()}, histories)
        self.assertEqual({name: ledger_anchor(store) for name, store in self.stores.items()}, anchors)
        self.assertEqual(self.grant_events, [])

    def test_copy_or_missing_original_path_cannot_take_over_pending_plan(self):
        record, _ = self.interrupt_after_first_member(
            lambda: self.gate.initialize([self.stores['X'], self.stores['Y']]))
        copied = self.base / 'X-copy'
        shutil.copytree(self.stores['X'].folder, copied)
        self.blocked('X', 'force', folder=copied)
        self.assertEqual(self.registry().membership_plan(), record)
        original, renamed = self.stores['X'].folder.resolve(), (self.base / 'X-moved').resolve()
        self.assertTrue(original.is_relative_to(self.base) and renamed.is_relative_to(self.base))
        original.rename(renamed)
        self.blocked('Y', 'automatic')
        self.assertFalse(original.exists(), 'Recovery must not fabricate the missing original ledger')
        self.assertEqual(self.registry().membership_plan(), record)
        self.assertEqual(self.registry().snapshot()['phase'], 'initializing')
        self.assertEqual(self.grant_events, [])

    def test_old_unresolved_operations_survive_membership_recovery_until_readonly_absence(self):
        originals = {}
        for name in ('X', 'Y'):
            store, key, operation = self.stores[name], self.key(name), uuid4().hex
            store.record_action(key['name'], key['tracking_id'], 'pending', 'synthetic legacy intent',
                                operation_id=operation)
            store.record_action(key['name'], key['tracking_id'], 'requested', 'synthetic legacy requested',
                operation_id=operation, recovery_evidence={'schema': 1, 'account_id': self.account,
                    'target_tracking_id': key['tracking_id'],
                    'known_tracking_ids': [item['tracking_id'] for item in self.keys],
                    'source': 'prepared_http_inventory_v1'})
            store.record_action(key['name'], key['tracking_id'], 'unknown_result', 'synthetic legacy uncertainty',
                                operation_id=operation, continuation=True)
            originals[name] = action_history(store)
        record, _ = self.interrupt_after_first_member(
            lambda: self.gate.initialize(list(self.stores.values())))
        reopened = self.open('Z')
        self.assert_readonly_reopen(reopened)
        self.assert_ready(record, ('X', 'Y', 'Z'))
        self.assertEqual({ob['receipt']['operation_id'] for ob in self.registry().open_obligations()},
                         {history[-1]['operation_id'] for history in originals.values()})
        for name in ('X', 'Y'):
            self.assertEqual(action_history(self.stores[name]), originals[name])
        reopened.close()
        self.blocked('Z', 'automatic')
        for index, name in enumerate(('X', 'Y')):
            target = self.key(name)['tracking_id']
            self.inventory = [key for key in self.inventory if key['tracking_id'] != target]
            verifier = self.open(name)
            verifier.verify()
            current = action_history(verifier.app.store)
            self.assertEqual(current[:-1], originals[name])
            self.assertEqual(current[-1]['status'], 'absent_verified')
            self.assertEqual(current[-1]['operation_id'], originals[name][-1]['operation_id'])
            self.assertEqual(verifier.browser.sends, 0)
            self.assertEqual(verifier.browser.consumptions, 0)
            self.assertEqual(self.grant_events, [])
            verifier.close()
            self.assertEqual(len(self.registry().open_obligations()), 1 - index)
            if index == 0:
                self.blocked('Z', 'force')
        self.success('Z', 'automatic')
        self.assertEqual(len(self.grant_events), 1)
        self.assertEqual(self.inventory, [self.keys[3]])


if __name__ == '__main__':
    unittest.main()
