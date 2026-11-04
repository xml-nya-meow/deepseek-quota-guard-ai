"""Clock-controlled permit cases; no browser, process or account is involved."""
from contextlib import contextmanager
import unittest
from unittest.mock import patch

from quota_guard.core import GuardError
from quota_guard.revocation_permit import DeletePermit


@contextmanager
def clocks(monotonic, wall):
    with patch('quota_guard.revocation_permit.time.monotonic', return_value=monotonic), \
            patch('quota_guard.revocation_permit.time.time', return_value=wall):
        yield


class RevocationPermitTests(unittest.TestCase):
    def grant(self):
        parent = DeletePermit()
        with clocks(100, 1000):
            self.assertTrue(parent.decide(True))
            grant = parent.export_grant()
        return parent, grant

    def test_first_decision_is_final_and_export_is_single_use(self):
        parent, grant = self.grant()
        self.assertFalse(parent.decide(False))
        self.assertTrue(parent.allowed)
        with clocks(101, 1001), self.assertRaises(GuardError):
            parent.export_grant()
        denied = DeletePermit()
        self.assertTrue(denied.decide(False))
        self.assertFalse(denied.decide(True))
        self.assertFalse(denied.allowed)
        with self.assertRaises(GuardError):
            denied.consume_for_click()

    def test_ipc_preserves_original_deadlines_and_final_attempt_is_single_use(self):
        _, grant = self.grant()
        child = DeletePermit()
        with clocks(104, 1004):
            self.assertTrue(child.accept_grant(grant))
            lease = child.consume_for_click()
            self.assertEqual(lease['issued_wall_ms'], 1000000)
            self.assertEqual(lease['expires_wall_ms'], 1005000)
            self.assertEqual(lease['remaining_ms'], 1000)
            with self.assertRaises(GuardError):
                child.consume_for_click()

    def test_expired_parent_cannot_export_or_renew_approval(self):
        parent = DeletePermit()
        with clocks(100, 1000):
            parent.decide(True)
        with clocks(105, 1005), self.assertRaises(GuardError):
            parent.export_grant()
        self.assertFalse(parent.decide(True))
        self.assertTrue(parent.allowed, 'Expiry must preserve the historical approval fact.')

    def test_child_receiving_expired_grant_cannot_report_preapproval_cancellation(self):
        _, grant = self.grant()
        child = DeletePermit()
        with clocks(100.1, 2000):
            self.assertFalse(child.accept_grant(grant))
            self.assertTrue(child.allowed)
            self.assertTrue(child.decided.is_set())
            with self.assertRaisesRegex(GuardError, '待核实'):
                child.consume_for_click()

    def test_either_clock_expiry_or_backwards_change_blocks_final_click(self):
        for monotonic, wall in ((105, 1001), (101, 1005), (99, 1001), (101, 999)):
            with self.subTest(monotonic=monotonic, wall=wall):
                _, grant = self.grant()
                child = DeletePermit()
                with clocks(101, 1001):
                    self.assertTrue(child.accept_grant(grant))
                with clocks(monotonic, wall), self.assertRaises(GuardError):
                    child.consume_for_click()

    def test_replayed_or_newer_grant_cannot_replace_a_received_decision(self):
        _, original = self.grant()
        child = DeletePermit()
        with clocks(101, 1001):
            child.accept_grant(original)
        replacement = DeletePermit()
        with clocks(103, 1003):
            replacement.decide(True)
            self.assertFalse(child.accept_grant(replacement.export_grant()))
        with clocks(105, 1005), self.assertRaises(GuardError):
            child.consume_for_click()

    def test_invalid_or_unbounded_ipc_grant_is_rejected(self):
        _, grant = self.grant()
        for changes in ({'expires_wall': float('nan')}, {'expires_monotonic': 106},
                        {'token': 'wrong'}, {'issued_monotonic': True}):
            child = DeletePermit()
            with self.subTest(changes=changes), self.assertRaises(GuardError):
                child.accept_grant({**grant, **changes})
            self.assertFalse(child.decided.is_set())


if __name__ == '__main__':
    unittest.main()
