"""Real Tk/ledger recovery callbacks with controlled, offline platform evidence.

Closing/recreating an App here is not a process-kill or filesystem-durability test.
No BrowserService is initialized and no transport submission is performed.
"""
from concurrent.futures import Future
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import tempfile
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from quota_guard.browser_service import BrowserService, key_fingerprint
from quota_guard.core import DataStore, GuardError, Person, TZ
from isolated_app import App


TARGET = 'synthetic-ui-recovery-target'
CONTROL = 'synthetic-ui-recovery-control'
NAME = '合成恢复目标'


class ControlledRecoveryBrowser:
    """Fake preparation/transport; reconciliation runs the actual read-only method."""
    def __init__(self, collection=None, inventory=None):
        self.collection = collection
        self.inventory = [] if inventory is None else inventory
        self.calls = []
        self.revoke_future = None
        self.permit = None

    def submit(self, action, **kwargs):
        self.calls.append((action, kwargs))
        future = Future()
        if action == 'collect':
            assert self.collection is not None
            future.set_result(self.collection)
        elif action == 'revoke':
            self.revoke_future = future
            self.permit = kwargs['permit']
        elif action == 'verify_revoke':
            fake = SimpleNamespace(_keys=lambda: self.inventory, known_keys=set(), anchor=None)
            try:
                result = BrowserService._verify_revoke(fake, kwargs['name'], kwargs['tracking_id'],
                    kwargs['known_keys'], http_keys=self.inventory)
                future.set_result(result)
            except Exception as exc:
                future.set_exception(exc)
        else:
            raise AssertionError('Unexpected offline action: ' + action)
        return future

    def prepared(self, *, include_inventory=True):
        if include_inventory:
            self.permit.set_prepared_inventory(TARGET, [TARGET, CONTROL])
        self.permit.ready.set()

    def close(self):
        pass

    def interrupt(self):
        future = Future()
        future.set_result(None)
        return future


class RecoveryEvidenceWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name).resolve() / 'ledger'
        self.store = DataStore(self.folder)
        self.start = datetime.now(TZ).date().isoformat()
        self.store.initialize(2, '100', self.start)
        self.store.save_people([Person(NAME, Decimal('0'))])
        self.keys = [{'name': NAME, 'tracking_id': TARGET},
                     {'name': 'DeepSeek Harness', 'tracking_id': CONTROL}]
        self.patches = [patch('quota_guard.ui.messagebox.showinfo'),
                        patch('quota_guard.ui.messagebox.showerror'),
                        patch('quota_guard.ui.messagebox.askyesno', return_value=True)]
        self.mocks = [p.start() for p in self.patches]
        self.root = self.app = None
        self.open_app()

    def open_app(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root, self.folder)
        self.until(lambda: self.app.config is not None)

    def close_app(self):
        if self.app is None:
            return
        self.app.stop(quiet=True)
        if self.app.browser is not None:
            self.app.browser.close()
        if self.app.target_lock is not None:
            self.app.target_lock.close()
        self.app.folder_lock.close()
        for callback in self.root.tk.call('after', 'info'):
            self.root.after_cancel(callback)
        self.root.destroy()
        self.app = self.root = None

    def tearDown(self):
        self.close_app()
        for patcher in reversed(self.patches):
            patcher.stop()
        self.temporary.cleanup()

    def until(self, predicate, *, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.root.update()
            if predicate():
                return
            time.sleep(.01)
        self.fail('Offline UI condition timed out')

    def start_pending(self):
        result = {'keys': self.keys, 'account_id': key_fingerprint(self.keys),
                  'totals': {TARGET: Decimal(0)}, 'source_id': 'synthetic-recovery-source',
                  'start_date': self.start, 'end_date': self.start,
                  'collection_started_wall': time.time(),
                  'collection_started_monotonic': time.monotonic()}
        browser = ControlledRecoveryBrowser(collection=result)
        self.app.browser = browser
        self.app.collect()
        self.until(lambda: not self.app.busy)
        self.assertTrue(self.app._collection_is_current(), self.app.status_var.get())
        self.assertEqual(self.store.active_tracking_ids(), {TARGET})
        self.app.start()
        self.until(lambda: browser.revoke_future is not None)
        self.assertEqual(self.store.latest_action_record(TARGET)['status'], 'pending')
        return browser

    def finish_unknown(self, browser):
        browser.revoke_future.set_exception(GuardError('synthetic response outcome unknown'))
        self.until(lambda: not self.app.busy)
        self.assertFalse(self.app.automatic)
        self.assertEqual(self.store.latest_action_record(TARGET)['status'], 'unknown_result')

    def restart_and_verify(self):
        original = self.store.unresolved_actions()[0]
        self.close_app()
        self.store = DataStore(self.folder)
        self.open_app()
        browser = ControlledRecoveryBrowser(inventory=self.keys[1:])
        self.app.browser = browser
        self.app.tree.selection_set('0')
        self.app.verify_revoke()
        self.assertIsNotNone(self.app.target_lock)
        self.assertTrue(self.app.target_lock.covers({TARGET, CONTROL}),
                        'Reopened verification must protect the original unbound survivor too.')
        self.until(lambda: not self.app.busy)
        self.assertEqual([action for action, _ in browser.calls], ['verify_revoke'])
        self.assertEqual(browser.calls[0][1]['known_keys'], {TARGET, CONTROL})
        latest = self.store.latest_action_record(TARGET)
        self.assertEqual(latest['operation_id'], original['operation_id'])
        self.assertEqual(latest['status'], 'absent_verified')
        self.assertEqual(latest['recovery_evidence'], original['recovery_evidence'])
        self.assertEqual(self.store.unresolved_actions(), [])
        self.assertEqual(self.store.active_tracking_ids(), set())

    def test_unbound_harness_survivor_is_saved_before_grant_and_recovers_after_reopen(self):
        browser = self.start_pending()
        browser.prepared()
        self.until(browser.permit.decided.is_set)
        self.assertTrue(browser.permit.allowed)
        requested = DataStore(self.folder).latest_action_record(TARGET)
        self.assertEqual(requested['status'], 'requested')
        self.assertEqual(requested['recovery_evidence']['known_tracking_ids'], sorted([TARGET, CONTROL]))
        self.assertEqual(requested['recovery_evidence']['account_id'], self.store.load().account_id)
        self.finish_unknown(browser)
        self.restart_and_verify()

    def test_unbound_normal_survivor_uses_same_original_operation_after_reopen(self):
        self.keys[1]['name'] = '未受管普通对照'
        browser = self.start_pending()
        browser.prepared()
        self.until(browser.permit.decided.is_set)
        self.assertTrue(browser.permit.allowed)
        self.finish_unknown(browser)
        self.restart_and_verify()

    def test_missing_prepared_inventory_never_grants_or_invents_recovery_evidence(self):
        browser = self.start_pending()
        operation = self.store.latest_action_record(TARGET)['operation_id']
        browser.prepared(include_inventory=False)
        self.until(browser.permit.decided.is_set)
        self.assertFalse(browser.permit.allowed)
        self.assertFalse(self.app.automatic)
        self.assertEqual(self.store.latest_action_record(TARGET)['status'], 'pending')
        self.assertNotIn('recovery_evidence', self.store.latest_action_record(TARGET))
        self.finish_unknown(browser)
        self.assertEqual(self.store.unresolved_actions()[0]['operation_id'], operation)

    def test_evidence_transaction_failure_never_grants_and_keeps_original_intent(self):
        browser = self.start_pending()
        operation = self.store.latest_action_record(TARGET)['operation_id']
        record = self.app.store.record_action
        def fail_requested(name, tracking_id, status, detail, **kwargs):
            if status == 'requested':
                raise GuardError('synthetic intent persistence failure')
            return record(name, tracking_id, status, detail, **kwargs)
        with patch.object(self.app.store, 'record_action', side_effect=fail_requested):
            browser.prepared()
            self.until(browser.permit.decided.is_set)
        self.assertFalse(browser.permit.allowed)
        self.assertEqual(self.store.latest_action_record(TARGET)['status'], 'pending')
        self.finish_unknown(browser)
        self.assertEqual(self.store.unresolved_actions()[0]['operation_id'], operation)
        self.assertNotIn('recovery_evidence', self.store.latest_action_record(TARGET))

    def test_post_commit_export_failure_never_grants_but_durable_context_recovers(self):
        browser = self.start_pending()
        operation = self.store.latest_action_record(TARGET)['operation_id']
        export = self.app.store.export_logs
        def fail_requested_export():
            # evaluate/action_evidence also export. Fail only after requested
            # is committed so this is distinct from a preparation failure.
            if DataStore(self.folder).latest_action_record(TARGET)['status'] == 'requested':
                raise GuardError('synthetic export failure')
            return export()
        with patch.object(self.app.store, 'export_logs', side_effect=fail_requested_export):
            browser.prepared()
            self.until(browser.permit.decided.is_set)
        self.assertFalse(browser.permit.allowed)
        requested = DataStore(self.folder).latest_action_record(TARGET)
        self.assertEqual(requested['status'], 'requested')
        self.assertEqual(requested['operation_id'], operation)
        self.assertEqual(requested['recovery_evidence']['known_tracking_ids'], sorted([TARGET, CONTROL]))
        self.finish_unknown(browser)
        self.restart_and_verify()


if __name__ == '__main__':
    unittest.main()
