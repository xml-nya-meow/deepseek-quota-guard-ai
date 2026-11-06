"""Offline application checks of shared, durable responsibility ownership.

Real App, SQLite events, DeletePermit and read-only reconciliation are used.
The transport is synthetic. Recreating an App is not an OS-crash experiment.
Every registry is explicitly injected below the test's temporary directory.
"""
from concurrent.futures import Future
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
import shutil
import tempfile
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from quota_guard.browser_service import BrowserService, key_fingerprint
from quota_guard.core import DataStore, GuardError, Person, TZ
from quota_guard.responsibility_gate import ResponsibilityGate
from quota_guard.responsibility_registry import ResponsibilityRegistry
from quota_guard.ui import App


class ResponsibilityBrowser:
    """Count the synthetic send separately from readiness and real permit use."""
    def __init__(self, store, inventory, target, account, *, outcome='unknown', prepare=True):
        self.store, self.inventory, self.target, self.account = store, list(inventory), target, account
        self.outcome, self.auto_prepare = outcome, prepare
        self.calls, self.trace = [], []
        self.future = self.permit = None
        self.observed_grants = self.consumptions = self.sends = 0

    def event(self, event, **fields):
        self.trace.append({'sequence': len(self.trace) + 1, 'event': event, **fields})

    def submit(self, action, **kwargs):
        self.calls.append(action)
        future = Future()
        if action == 'collect':
            start = self.store.load().start_date
            names = set(kwargs['managed_names'])
            requested = [key['tracking_id'] for key in self.inventory if key['name'] in names]
            future.set_result({'keys': list(self.inventory), 'account_id': self.account,
                'totals': {key: Decimal(0) for key in requested},
                'requested_tracking_ids': requested, 'source_id': 'synthetic-duty-source',
                'start_date': start, 'end_date': start,
                'collection_started_wall': time.time(), 'collection_started_monotonic': time.monotonic()})
        elif action == 'read_keys':
            future.set_result({'keys': list(self.inventory), 'account_id': self.account})
        elif action == 'revoke':
            assert kwargs['tracking_id'] == self.target
            assert self.future is None, 'A target must not get a second synthetic submission job.'
            self.future, self.permit = future, kwargs['permit']
            self.event('preparation_entered', target=self.target)
            if self.auto_prepare:
                self.prepared()
        elif action == 'verify_revoke':
            self.event('readonly_verification', target=kwargs['tracking_id'])
            service = SimpleNamespace(_keys=lambda: list(self.inventory), known_keys=set(), anchor=None)
            try:
                result = BrowserService._verify_revoke(service, kwargs['name'], kwargs['tracking_id'],
                    kwargs['known_keys'], http_keys=list(self.inventory))
                future.set_result(result)
            except Exception as exc:
                future.set_exception(exc)
        else:
            raise AssertionError('Unexpected synthetic action: ' + action)
        return future

    def prepared(self):
        self.permit.set_prepared_inventory(self.target, [key['tracking_id'] for key in self.inventory])
        self.permit.ready.set()

    def tick(self):
        if self.future is None or self.future.done() or not self.permit.decided.is_set():
            return
        if not self.permit.allowed:
            self.future.set_result({'cancelled': True})
            return
        original = self.store.latest_action_record(self.target)
        assert original['status'] == 'requested'
        assert original['operation_id']
        receipt = original['responsibility']
        assert receipt['operation_id'] == original['operation_id']
        assert receipt['target_hash'] == sha256(self.target.encode('utf-8')).hexdigest()
        assert original['recovery_evidence']['target_tracking_id'] == self.target
        assert set(original['recovery_evidence']['known_tracking_ids']) == {
            key['tracking_id'] for key in self.inventory}
        self.observed_grants += 1
        self.event('allowed_permit_observed', target=self.target, operation_id=original['operation_id'])
        self.permit.consume_for_click()
        self.consumptions += 1
        self.event('real_permit_consumed', target=self.target, operation_id=original['operation_id'])
        self.sends += 1
        self.event('synthetic_send', target=self.target, operation_id=original['operation_id'])
        if self.outcome == 'unknown':
            self.future.set_exception(GuardError('synthetic send response lost'))
        elif self.outcome == 'success':
            self.inventory = [key for key in self.inventory if key['tracking_id'] != self.target]
            self.future.set_result({'absent': True})
        else:
            raise AssertionError('Invalid synthetic outcome')

    def close(self):
        pass

    def interrupt(self):
        future = Future()
        future.set_result(None)
        return future


class OfflineResponsibilityApp:
    """Small real-Tk driver shared by tests and the separate-process probe."""
    def __init__(self, folder, directory, inventory, target, account, *, outcome='unknown', prepare=True):
        self.patches = [patch('quota_guard.ui.messagebox.showinfo'),
                        patch('quota_guard.ui.messagebox.showerror'),
                        patch('quota_guard.ui.messagebox.askyesno', return_value=True)]
        self.mocks = [patcher.start() for patcher in self.patches]
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root, Path(folder), responsibility_directory=Path(directory))
        # App opens the ledger from its actual after(60) callback. Do not attach
        # the fake to a not-yet-loaded store or bypass that startup path.
        deadline = time.monotonic() + 4
        while self.app.config is None and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.003)
        if self.app.config is None:
            raise AssertionError('Offline ledger did not load: ' + self.app.status_var.get())
        self.browser = ResponsibilityBrowser(self.app.store, inventory, target, account,
                                             outcome=outcome, prepare=prepare)
        self.app.browser = self.browser
        self.target = target

    def until(self, predicate, *, timeout=6, tick=True):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self.root.update()
            if predicate():
                return
            if tick:
                self.browser.tick()
            time.sleep(.003)
        raise AssertionError('Offline condition timed out: ' + self.app.status_var.get())

    def launch(self, mode):
        if mode == 'automatic':
            self.app.collect()
            self.until(lambda: not self.app.busy)
            self.app.start()
        elif mode == 'force':
            self.app.tree.selection_set('0')
            self.app.revoke_selected_once()
        else:
            raise AssertionError('Unknown entry')

    def finish_or_block(self):
        self.until(lambda: not self.app.busy and (
            self.browser.sends > 0 or not self.app.automatic and not self.app.observing))

    def verify(self):
        self.app.tree.selection_set('0')
        self.app.verify_revoke()
        self.until(lambda: not self.app.busy)

    def report(self):
        return {'calls': list(self.browser.calls), 'trace': list(self.browser.trace),
                'observed_allowed_permits': self.browser.observed_grants,
                'permit_consumptions': self.browser.consumptions, 'synthetic_sends': self.browser.sends,
                'latest': self.app.store.latest_action_record(self.target),
                'unresolved': self.app.store.unresolved_actions(), 'status': self.app.status_var.get()}

    def close(self, *, fixture_cleanup=False):
        if self.app is None:
            return
        closed = False
        try:
            if not fixture_cleanup:
                self.app.close()
                end = time.monotonic() + 8
                while time.monotonic() < end:
                    try:
                        self.root.update()
                        if not self.root.winfo_exists():
                            closed = True
                            break
                    except tk.TclError:
                        closed = True
                        break
                    time.sleep(.005)
                else:
                    raise AssertionError('Cooperative App close did not finish')
            else:
                # Only for an injected audit/storage failure. This releases test
                # UI resources, not durable registry responsibility. It is not
                # recorded as cooperative close or a real process crash.
                self.app.stop(quiet=True)
                if self.app.target_lock is not None:
                    self.app.target_lock.close()
                self.app.folder_lock.close()
                for token in self.root.tk.call('after', 'info'):
                    self.root.after_cancel(token)
                self.root.destroy()
                closed = True
        finally:
            if closed:
                # The real cooperative close has already finished. Remove only
                # Tcl timers left in this test interpreter before constructing
                # another Tk root; this is fixture hygiene, not a product fix.
                try:
                    for token in self.root.tk.call('after', 'info'):
                        self.root.tk.call('after', 'cancel', token)
                except tk.TclError:
                    pass
                self.app = self.root = None
                for patcher in reversed(self.patches):
                    patcher.stop()


def make_ledgers(parent):
    """No current-user registry path or real ledger is used."""
    parent = Path(parent)
    identity = uuid4().hex
    targets = [f'synthetic-duty-{identity}-{suffix}' for suffix in ('T1', 'T2', 'R')]
    keys = [{'name': name, 'tracking_id': target} for name, target in
            zip(('Synthetic X', 'Synthetic Y', 'Synthetic R'), targets)]
    account = key_fingerprint(keys)
    stores = []
    for label, name in (('X', 'Synthetic X'), ('Y', 'Synthetic Y')):
        store = DataStore(parent / label)
        store.initialize(2, '100', datetime.now(TZ).date().isoformat())
        store.save_people([Person(name, Decimal(0))])
        store.bind_keys(keys, account)
        stores.append(store)
    directory = parent / 'shared-control' / 'responsibility'
    ResponsibilityGate(directory).initialize(stores)
    return stores, directory, keys, targets, account


class ResponsibilityWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name).resolve()
        self.stores, self.directory, self.keys, self.targets, self.account = make_ledgers(self.base)
        self.drivers = []

    def tearDown(self):
        for driver in reversed(self.drivers):
            if driver.app is not None:
                driver.close(fixture_cleanup=True)
        self.temporary.cleanup()

    def open(self, which, *, keys=None, outcome='unknown', prepare=True, folder=None):
        driver = OfflineResponsibilityApp(folder or self.base / which, self.directory,
            self.keys if keys is None else keys, self.targets[0 if which == 'X' else 1],
            self.account, outcome=outcome, prepare=prepare)
        self.drivers.append(driver)
        return driver

    def create_unknown(self):
        owner = self.open('X')
        owner.launch('automatic')
        owner.finish_or_block()
        self.assertEqual(owner.browser.sends, 1)
        self.assertEqual(owner.browser.consumptions, 1)
        original = owner.app.store.latest_action_record(self.targets[0])
        self.assertEqual(original['status'], 'unknown_result')
        owner.close()
        return original

    def assert_y_blocked(self, mode):
        other = self.open('Y')
        other.launch(mode)
        other.finish_or_block()
        self.assertEqual(other.browser.sends, 0)
        self.assertEqual(other.browser.consumptions, 0)
        self.assertNotIn('revoke', other.browser.calls)
        self.assertIsNone(other.app.store.latest_action_record(self.targets[1]))
        other.close()

    def cross_ledger(self, mode):
        original = self.create_unknown()
        self.assert_y_blocked(mode)
        self.assertEqual(self.stores[0].unresolved_actions()[0]['operation_id'], original['operation_id'])
        self.assertEqual(len(ResponsibilityRegistry(self.directory).open_obligations()), 1)
        remaining = self.keys[1:]
        recovered = self.open('X', keys=remaining)
        recovered.verify()
        latest = recovered.app.store.latest_action_record(self.targets[0])
        self.assertEqual(latest['operation_id'], original['operation_id'])
        self.assertEqual(latest['status'], 'absent_verified')
        self.assertEqual(recovered.browser.calls, ['verify_revoke'])
        self.assertEqual(recovered.browser.sends, 0)
        self.assertEqual(ResponsibilityRegistry(self.directory).open_obligations(), [])
        recovered.close()
        successor = self.open('Y', keys=remaining, outcome='success')
        successor.launch(mode)
        successor.finish_or_block()
        self.assertEqual(successor.browser.sends, 1)
        self.assertEqual(successor.browser.consumptions, 1)
        final = successor.app.store.latest_action_record(self.targets[1])
        self.assertEqual(final['status'], 'absent_verified')
        self.assertNotEqual(final['operation_id'], original['operation_id'])
        self.assertEqual(ResponsibilityRegistry(self.directory).open_obligations(), [])
        self.assertEqual(successor.browser.inventory, [self.keys[2]])
        successor.close()

    def test_automatic_other_ledger_blocks_after_owner_exit_then_resumes_after_original_absence(self):
        self.cross_ledger('automatic')

    def test_forced_other_ledger_blocks_after_owner_exit_then_resumes_after_original_absence(self):
        self.cross_ledger('force')

    def test_present_observation_does_not_release_shared_original_responsibility(self):
        original = self.create_unknown()
        recovered = self.open('X')
        recovered.verify()
        current = recovered.app.store.latest_action_record(self.targets[0])
        self.assertEqual(current['operation_id'], original['operation_id'])
        self.assertEqual(current['status'], 'present_verified')
        self.assertEqual(len(ResponsibilityRegistry(self.directory).open_obligations()), 1)
        recovered.close()
        self.assert_y_blocked('force')

    def test_pending_event_transaction_failure_leaves_shared_barrier_without_submission(self):
        owner = self.open('X')
        original_event = owner.app.store._event
        def fail_pending(db, kind, payload, *args, **kwargs):
            if kind == 'action' and payload.get('status') == 'pending':
                raise GuardError('synthetic pending transaction failure')
            return original_event(db, kind, payload, *args, **kwargs)
        with patch.object(owner.app.store, '_event', side_effect=fail_pending):
            owner.launch('automatic')
            owner.finish_or_block()
        self.assertEqual(owner.browser.sends, 0)
        self.assertIsNone(owner.app.store.latest_action_record(self.targets[0]))
        self.assertEqual(len(ResponsibilityRegistry(self.directory).open_obligations()), 1)
        owner.close(fixture_cleanup=True)
        self.assert_y_blocked('automatic')

    def test_pending_post_commit_export_failure_still_blocks_other_ledger(self):
        self.export_cut('pending')

    def test_requested_post_commit_export_failure_cannot_grant_or_unblock_other_ledger(self):
        self.export_cut('requested')

    def export_cut(self, phase):
        owner = self.open('X', prepare=False)
        real_export = owner.app.store.export_logs
        def fail_export():
            current = owner.app.store.latest_action_record(self.targets[0])
            if current is not None and current['status'] == phase:
                raise GuardError('synthetic post-commit export failure')
            return real_export()
        with patch.object(owner.app.store, 'export_logs', side_effect=fail_export):
            owner.launch('automatic')
            if phase == 'pending':
                owner.finish_or_block()
            else:
                owner.until(lambda: owner.browser.future is not None)
                owner.browser.prepared()
                owner.until(owner.browser.permit.decided.is_set, tick=False)
                self.assertFalse(owner.browser.permit.allowed)
        latest = owner.app.store.latest_action_record(self.targets[0])
        self.assertEqual(latest['status'], phase)
        self.assertEqual(owner.browser.sends, 0)
        self.assertEqual(len(ResponsibilityRegistry(self.directory).open_obligations()), 1)
        owner.close(fixture_cleanup=True)
        self.assert_y_blocked('force')

    def test_copied_ledger_cannot_settle_original_shared_operation(self):
        original = self.create_unknown()
        copied = self.base / 'X-copy'
        shutil.copytree(self.base / 'X', copied)
        clone = self.open('X', folder=copied, keys=self.keys[1:])
        clone.verify()
        # Read-only facts may still be observed; they cannot settle the original
        # location's responsibility or turn the clone into an owner.
        self.assertEqual(clone.browser.sends, 0)
        self.assertEqual(clone.app.store.latest_action_record(self.targets[0])['operation_id'], original['operation_id'])
        self.assertEqual(len(ResponsibilityRegistry(self.directory).open_obligations()), 1)
        self.assertEqual(self.stores[0].latest_action(self.targets[0]), 'unknown_result')
        clone.close()
        self.assert_y_blocked('automatic')

    def test_renamed_ledger_does_not_free_the_original_owner_slot(self):
        self.create_unknown()
        original, renamed = (self.base / 'X').resolve(), (self.base / 'X-renamed').resolve()
        self.assertTrue(original.is_relative_to(self.base) and renamed.is_relative_to(self.base))
        original.rename(renamed)
        moved = self.open('X', folder=renamed, keys=self.keys[1:])
        moved.verify()
        self.assertEqual(moved.browser.sends, 0)
        self.assertEqual(len(ResponsibilityRegistry(self.directory).open_obligations()), 1)
        moved.close()
        self.assert_y_blocked('force')

    def test_paired_old_registry_and_anchor_replay_cannot_hide_new_local_checkpoint(self):
        registry = ResponsibilityRegistry(self.directory)
        database_before = registry.db_path.read_bytes()
        anchor_before = registry.anchor_path.read_bytes()
        original = self.create_unknown()
        self.assertEqual(len(registry.open_obligations()), 1)
        # Both matching old files are replayed, unlike a one-sided corruption
        # test. All paths are this test's synthetic scope, with no open DB owner.
        for path in (registry.db_path, registry.anchor_path):
            self.assertTrue(path.resolve().is_relative_to(self.base))
        registry.db_path.write_bytes(database_before)
        registry.anchor_path.write_bytes(anchor_before)
        self.assertEqual(registry.snapshot()['open_obligations'], [])
        self.assertEqual(self.stores[0].latest_action_record(self.targets[0])['operation_id'], original['operation_id'])
        with self.assertRaises(GuardError):
            ResponsibilityGate(self.directory).check(self.stores[1])
        for mode in ('automatic', 'force'):
            with self.subTest(entry=mode):
                self.assert_y_blocked(mode)
        self.assertEqual(self.stores[0].latest_action(self.targets[0]), 'unknown_result')

    def test_local_terminal_before_failed_release_can_reconcile_on_reopened_original_ledger(self):
        original = self.create_unknown()
        recovered = self.open('X', keys=self.keys[1:])
        with patch.object(recovered.app.responsibility_gate.registry, 'release',
                          side_effect=GuardError('synthetic registry release unavailable')):
            recovered.verify()
        terminal = recovered.app.store.latest_action_record(self.targets[0])
        self.assertEqual(terminal['operation_id'], original['operation_id'])
        self.assertEqual(terminal['status'], 'absent_verified')
        self.assertEqual(recovered.browser.sends, 0)
        self.assertEqual(len(ResponsibilityRegistry(self.directory).open_obligations()), 1)
        recovered.close()
        self.assert_y_blocked('automatic')
        reopened = self.open('X', keys=self.keys[1:])
        # Actual App.load_folder reconciles the already durable terminal fact;
        # no test-side repair call, login or extra platform observation is used.
        self.assertEqual(reopened.browser.calls, [])
        self.assertEqual(reopened.app.store.latest_action_record(self.targets[0]), terminal)
        self.assertEqual(ResponsibilityRegistry(self.directory).open_obligations(), [])
        reopened.close()
        successor = self.open('Y', keys=self.keys[1:], outcome='success')
        successor.launch('force')
        successor.finish_or_block()
        self.assertEqual(successor.browser.sends, 1)
        self.assertEqual(successor.browser.consumptions, 1)
        self.assertEqual(successor.app.store.latest_action(self.targets[1]), 'absent_verified')
        successor.close()


if __name__ == '__main__':
    unittest.main()
