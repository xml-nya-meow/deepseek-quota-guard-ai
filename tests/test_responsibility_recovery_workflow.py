"""Real App checks for fenced recovery of never-authorized responsibilities.

Every ledger and registry below is synthetic and isolated.  Threads synchronize
at explicit commit/authorization boundaries; no browser, HTTP, paid request or
real account is used.  Reopening Tk is not an OS-crash or power-loss claim.
"""
from pathlib import Path
import tempfile
from threading import Event, Thread
import unittest
from unittest.mock import patch

from quota_guard.core import DataStore, GuardError
from quota_guard.responsibility_gate import (
    ResponsibilityGate, action_history, ledger_anchor,
)
from quota_guard.responsibility_registry import ResponsibilityRegistry, validate_receipt
from quota_guard.revocation_permit import DeletePermit
from tests.test_responsibility_workflow import OfflineResponsibilityApp, make_ledgers


TEMP_ROOT = Path(__file__).resolve().parents[2] / 'TEMP'


class ResponsibilityRecoveryWorkflowTests(unittest.TestCase):
    def setUp(self):
        TEMP_ROOT.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='recovery-app-', dir=TEMP_ROOT)
        self.base = Path(self.temporary.name).resolve()
        self.stores, self.directory, self.keys, self.targets, self.account = make_ledgers(self.base)
        self.drivers = []
        self.decisions = []
        real_decide = DeletePermit.decide

        def counted_decide(permit, allowed):
            accepted = real_decide(permit, allowed)
            self.decisions.append({'target': permit.target_tracking_id,
                                   'allowed': allowed is True, 'accepted': accepted})
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

    @property
    def grants(self):
        return sum(event['allowed'] and event['accepted'] for event in self.decisions)

    def open(self, which, *, outcome='unknown', prepare=True):
        driver = OfflineResponsibilityApp(self.base / which, self.directory, self.keys,
            self.targets[0 if which == 'X' else 1], self.account,
            outcome=outcome, prepare=prepare)
        self.drivers.append(driver)
        return driver

    def registry(self):
        return ResponsibilityRegistry(self.directory)

    def receipt(self):
        obligations = self.registry().open_obligations()
        self.assertEqual(len(obligations), 1)
        return validate_receipt(obligations[0]['receipt'])

    def audits(self):
        return [event for event in self.stores[0].events(10000)
                if event['kind'] == 'responsibility_recovery']

    def assert_no_send(self, driver):
        self.assertEqual(driver.browser.sends, 0)
        self.assertEqual(driver.browser.consumptions, 0)
        self.assertEqual(driver.browser.observed_grants, 0)

    def assert_recovered(self, receipt):
        self.assertEqual(self.registry().open_obligations(), [])
        self.assertEqual(len(self.audits()), 1)
        self.assertEqual(self.audits()[0]['receipt'], receipt.to_dict())
        self.assertEqual(self.audits()[0]['recovery_id'],
                         self.registry().recovery_for(receipt)['recovery_id'])
        # The immutable receipt remains queryable after settlement; no new
        # operation or replacement epoch is used to escape the original duty.
        self.assertEqual(self.registry().receipt_for(receipt.ledger_id, receipt.operation_id), receipt)
        self.assertEqual(ledger_anchor(self.stores[0])['checkpoint'], receipt.generation)
        with self.assertRaises(GuardError):
            self.registry().mark_possible_send(receipt)

    def assert_y_success(self, mode, previous_operation):
        before = self.grants
        other = self.open('Y', outcome='success')
        other.launch(mode)
        other.finish_or_block()
        self.assertEqual(self.grants - before, 1)
        self.assertEqual(other.browser.sends, 1)
        self.assertEqual(other.browser.consumptions, 1)
        final = other.app.store.latest_action_record(self.targets[1])
        self.assertEqual(final['status'], 'absent_verified')
        self.assertNotEqual(final['operation_id'], previous_operation)
        self.assertEqual({key['tracking_id'] for key in other.browser.inventory},
                         {self.targets[0], self.targets[2]})
        self.assertEqual(self.registry().open_obligations(), [])
        other.close()
        return final

    def fail_before_pending(self):
        owner = self.open('X')
        actual_event = owner.app.store._event

        def fail_event(db, kind, payload, *args, **kwargs):
            if kind == 'action' and payload.get('status') == 'pending':
                raise GuardError('synthetic pending transaction cut')
            return actual_event(db, kind, payload, *args, **kwargs)

        with patch.object(owner.app.store, '_event', side_effect=fail_event):
            owner.launch('automatic')
            owner.finish_or_block()
        self.assert_no_send(owner)
        self.assertEqual(self.grants, 0)
        self.assertEqual(action_history(owner.app.store), [])
        receipt = self.receipt()
        owner.close(fixture_cleanup=True)
        return receipt

    def fail_requested_export(self):
        owner = self.open('X', prepare=False)
        real_export = owner.app.store.export_logs

        def fail_export():
            latest = owner.app.store.latest_action_record(self.targets[0])
            if latest is not None and latest['status'] == 'requested':
                raise GuardError('synthetic requested export cut')
            return real_export()

        with patch.object(owner.app.store, 'export_logs', side_effect=fail_export):
            owner.launch('automatic')
            owner.until(lambda: owner.browser.future is not None)
            owner.browser.prepared()
            # Do not manufacture a worker result before the reopen boundary.
            owner.until(owner.browser.permit.decided.is_set, tick=False)
        self.assert_no_send(owner)
        self.assertEqual(self.grants, 0)
        original = action_history(owner.app.store)
        self.assertEqual([event['status'] for event in original], ['pending', 'requested'])
        receipt = self.receipt()
        owner.close(fixture_cleanup=True)
        return receipt, original

    def test_claim_before_pending_reopens_without_fabricating_intent_then_automatic_resumes(self):
        receipt = self.fail_before_pending()
        reopened = self.open('X')
        self.assert_recovered(receipt)
        self.assertEqual(reopened.browser.calls, [])
        self.assert_no_send(reopened)
        self.assertEqual(self.grants, 0)
        self.assertEqual(action_history(reopened.app.store), [])
        before = self.audits()
        # A delayed owner cannot insert its original pending after recovery.
        with self.assertRaises(GuardError):
            reopened.app.store.record_action('Synthetic X', self.targets[0], 'pending',
                'synthetic late pending', operation_id=receipt.operation_id,
                responsibility=receipt.to_dict())
        self.assertEqual(self.audits(), before)
        reopened.close()
        self.assert_y_success('automatic', receipt.operation_id)

    def test_requested_export_cut_reopens_preserving_evidence_then_force_resumes(self):
        receipt, original = self.fail_requested_export()
        reopened = self.open('X')
        self.assert_recovered(receipt)
        current = action_history(reopened.app.store)
        self.assertEqual(current[:2], original)
        self.assertEqual([event['status'] for event in current], ['pending', 'requested', 'cancelled'])
        self.assertEqual(current[-1]['operation_id'], receipt.operation_id)
        self.assertEqual(current[-1]['recovery_evidence'], original[-1]['recovery_evidence'])
        self.assert_no_send(reopened)
        self.assertEqual(reopened.browser.calls, [])
        self.assertEqual(self.grants, 0)
        for status in ('requested', 'unknown_result'):
            with self.subTest(late_status=status), self.assertRaises(GuardError):
                reopened.app.store.record_action('Synthetic X', self.targets[0], status,
                    'synthetic late callback', operation_id=receipt.operation_id,
                    continuation=status != 'requested')
        self.assertEqual(action_history(reopened.app.store), current)
        reopened.close()
        self.assert_y_success('force', receipt.operation_id)

    def late_authorize_race(self, *, barrier):
        owner = self.open('X', prepare=False)
        owner.launch('automatic')
        owner.until(lambda: owner.browser.future is not None)
        receipt = self.receipt()
        reached, recovered = Event(), Event()
        result = []

        def recover_after_barrier():
            if not reached.wait(8):
                result.append(AssertionError('authorization barrier was not reached'))
                recovered.set()
                return
            try:
                result.append(ResponsibilityGate(self.directory).recover_declared(
                    DataStore(self.base / 'X'), receipt))
            except Exception as exc:
                result.append(exc)
            finally:
                recovered.set()

        worker = Thread(target=recover_after_barrier, daemon=True)
        worker.start()
        actual_send = owner.app.responsibility_gate.possible_send
        actual_mark = owner.app.responsibility_gate.registry.mark_possible_send

        def wait_for_recovery():
            reached.set()
            if not recovered.wait(8):
                raise AssertionError('fenced recovery did not finish at the barrier')

        def paused_send(store, original_receipt, *, write_requested, context_valid=lambda: True):
            def paused_requested():
                wait_for_recovery()
                write_requested()
            return actual_send(store, original_receipt,
                write_requested=paused_requested, context_valid=context_valid)

        def paused_mark(original_receipt, **kwargs):
            wait_for_recovery()
            return actual_mark(original_receipt, **kwargs)

        target = owner.app.responsibility_gate if barrier == 'before_requested' else owner.app.responsibility_gate.registry
        method = 'possible_send' if barrier == 'before_requested' else 'mark_possible_send'
        replacement = paused_send if barrier == 'before_requested' else paused_mark
        try:
            with patch.object(target, method, side_effect=replacement):
                owner.browser.prepared()
                owner.until(owner.browser.permit.decided.is_set, tick=False)
        finally:
            reached.set()
            worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(result, [1])
        self.assertFalse(owner.browser.permit.allowed)
        self.assertEqual(self.grants, 0)
        self.assert_no_send(owner)
        self.assert_recovered(receipt)
        history = action_history(owner.app.store)
        expected = ['pending', 'cancelled'] if barrier == 'before_requested' else ['pending', 'requested', 'cancelled']
        self.assertEqual([event['status'] for event in history], expected)
        # Deliver the real queued callback only after recovery.  It must not
        # revive unknown, rewrite the receipt, or leave an endless audit retry.
        owner.browser.tick()
        owner.until(lambda: not owner.app.busy, tick=False)
        self.assertEqual(action_history(owner.app.store), history)
        self.assertEqual(len(self.audits()), 1)
        self.assertFalse(owner.app.audit_failed)
        self.assert_no_send(owner)
        owner.close()
        self.assert_y_success('automatic', receipt.operation_id)

    def test_recovery_fence_wins_before_late_requested_write(self):
        self.late_authorize_race(barrier='before_requested')

    def test_recovery_fence_wins_after_requested_before_late_mark(self):
        self.late_authorize_race(barrier='before_mark')

    def test_possible_send_wins_recovery_is_refused_even_after_owner_exits(self):
        owner = self.open('X', prepare=False)
        owner.launch('automatic')
        owner.until(lambda: owner.browser.future is not None)
        receipt = self.receipt()
        reached, attempted = Event(), Event()
        outcomes = []

        def attempt_recovery():
            if not reached.wait(8):
                outcomes.append('barrier-timeout')
                attempted.set()
                return
            try:
                outcomes.append(ResponsibilityGate(self.directory).recover_declared(
                    DataStore(self.base / 'X'), receipt))
            except GuardError:
                outcomes.append('rejected')
            finally:
                attempted.set()

        worker = Thread(target=attempt_recovery, daemon=True)
        worker.start()
        real_mark = owner.app.responsibility_gate.registry.mark_possible_send

        def mark_before_recover(original, **kwargs):
            result = real_mark(original, **kwargs)
            reached.set()
            if not attempted.wait(8):
                raise AssertionError('reverse race recovery did not finish')
            return result

        try:
            with patch.object(owner.app.responsibility_gate.registry, 'mark_possible_send',
                              side_effect=mark_before_recover):
                owner.browser.prepared()
                owner.finish_or_block()
        finally:
            reached.set()
            worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(outcomes, ['rejected'])
        self.assertEqual(self.grants, 1)
        self.assertEqual(owner.browser.sends, 1)
        self.assertEqual(owner.browser.consumptions, 1)
        original = action_history(owner.app.store)
        self.assertEqual(original[-1]['status'], 'unknown_result')
        self.assertEqual(self.audits(), [])
        self.assertEqual(self.registry().open_obligations()[0]['phase'], 'possible_send')
        owner.close()
        reopened = self.open('X')
        self.assertEqual(action_history(reopened.app.store), original)
        self.assertEqual(reopened.browser.calls, [])
        self.assertEqual(self.audits(), [])
        self.assertEqual(self.receipt(), receipt)
        self.assert_no_send(reopened)
        reopened.close()

    def recovery_cut(self, *, phase):
        receipt = self.fail_before_pending()
        if phase == 'audit':
            cut = patch('quota_guard.declared_recovery.recover_local_declared',
                        side_effect=GuardError('synthetic recovery transaction cut'))
        elif phase == 'finish':
            cut = patch.object(ResponsibilityRegistry, 'finish_declared_recovery',
                               side_effect=GuardError('synthetic finish recovery cut'))
        else:
            real_export = DataStore.export_logs

            def fail_recovery_export(store):
                if store.folder.resolve() == (self.base / 'X').resolve() and any(
                        event['kind'] == 'responsibility_recovery' for event in store.events(10000)):
                    raise GuardError('synthetic recovery audit export cut')
                return real_export(store)

            cut = patch.object(DataStore, 'export_logs', fail_recovery_export)
        with cut:
            interrupted = self.open('X')
        self.assert_no_send(interrupted)
        self.assertEqual(self.grants, 0)
        self.assertEqual(self.receipt(), receipt)
        fence = self.registry().recovery_for(receipt)
        self.assertIsNotNone(fence['recovery_id'])
        self.assertEqual(len(self.audits()), 0 if phase == 'audit' else 1)
        saved_audits = self.audits()
        with self.assertRaises(GuardError):
            self.registry().mark_possible_send(receipt)
        interrupted.close(fixture_cleanup=True)
        # Recovery is an actual load_folder path.  A second reopen resumes the
        # same durable fence; it does not issue a fresh recovery or action.
        resumed = self.open('X')
        self.assert_recovered(receipt)
        self.assertEqual(self.registry().recovery_for(receipt), fence)
        if saved_audits:
            self.assertEqual(self.audits(), saved_audits)
        self.assertEqual(action_history(resumed.app.store), [])
        self.assert_no_send(resumed)
        self.assertEqual(resumed.browser.calls, [])
        self.assertEqual(self.grants, 0)
        resumed.close()
        self.assert_y_success('automatic', receipt.operation_id)

    def test_fence_before_local_audit_failure_resumes_on_original_reopen(self):
        self.recovery_cut(phase='audit')

    def test_local_recovery_audit_before_export_failure_resumes_without_duplicate(self):
        self.recovery_cut(phase='export')

    def test_export_before_finish_failure_resumes_without_duplicate(self):
        self.recovery_cut(phase='finish')


if __name__ == '__main__':
    unittest.main()
