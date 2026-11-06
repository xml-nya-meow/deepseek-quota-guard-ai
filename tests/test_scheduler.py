"""Offline candidate ordering. These tests do not grant or submit deletions."""
from decimal import Decimal, localcontext
import random
import unittest
from unittest.mock import patch

from quota_guard.scheduler import CandidateQueue, SchedulerError


def row(target, name="synthetic-person", **changes):
    return {"tracking_id": target, "name": name, "status": "triggered",
            "amount": Decimal("1"), "spent": Decimal("1"), "complete": True, **changes}


class CandidateQueueTests(unittest.TestCase):
    def test_same_batch_gets_one_clock_read_and_input_order_does_not_win(self):
        items = [row("target-b-002"), row("target-a-001")]
        for seed in range(24):
            random.Random(seed).shuffle(items)
            queue = CandidateQueue()
            with patch("quota_guard.scheduler.time.monotonic", return_value=12) as clock:
                queue.sync(items, scope=("ledger", "period", 1))
                clock.assert_called_once_with()
            self.assertEqual(queue.select(now=12)["tracking_id"], "target-a-001")
            self.assertEqual({item["first_confirmed_monotonic"] for item in queue.snapshot(now=12)}, {12})

    def test_same_name_different_ids_remain_distinct_and_duplicate_ids_merge(self):
        queue = CandidateQueue()
        first, second = row("target-a-001"), row("target-b-002")
        queue.sync([second, first, first.copy()], scope="scope", now=0)
        self.assertEqual(queue.summary(now=0)["count"], 2)
        self.assertEqual([item["tracking_id"] for item in queue.snapshot(now=0)],
                         ["target-a-001", "target-b-002"])

    def test_conflicting_duplicates_reject_atomically_in_both_orders(self):
        queue = CandidateQueue()
        original = row("target-a-001")
        queue.sync([original], scope="scope", now=0)
        conflicting = row("target-a-001", name="different-owner")
        for items in ([original, conflicting], [conflicting, original]):
            with self.assertRaises(SchedulerError):
                queue.sync(items, scope="scope", now=10)
            self.assertEqual(queue.select(now=0), original)

    def test_refresh_updates_evidence_without_resetting_continuous_age(self):
        queue = CandidateQueue()
        first = row("target-a-001", evidence="old")
        queue.sync([first], scope="scope", now=10)
        refreshed = row("target-a-001", evidence="new", spent=Decimal("2"))
        queue.sync([refreshed], scope="scope", now=35)
        self.assertEqual(queue.select(now=40), refreshed)
        self.assertEqual(queue.summary(now=40)["oldest_wait_seconds"], 30)
        self.assertEqual(queue.snapshot(now=40)[0]["first_confirmed_monotonic"], 10)

    def test_scope_change_or_reset_starts_new_epoch(self):
        queue = CandidateQueue()
        candidate = row("target-a-001")
        queue.sync([candidate], scope=("ledger", "period", 1), now=5)
        queue.sync([candidate], scope=("ledger", "period", 2), now=100)
        self.assertEqual(queue.summary(now=100)["oldest_wait_seconds"], 0)
        queue.reset()
        queue.sync([candidate], scope=("ledger", "period", 2), now=1)
        self.assertEqual(queue.summary(now=1)["oldest_wait_seconds"], 0)

    def test_removed_and_later_returned_candidate_starts_new_episode(self):
        queue = CandidateQueue()
        first, second = row("target-a-001"), row("target-b-002")
        queue.sync([first, second], scope="scope", now=0)
        queue.sync([second], scope="scope", now=20)
        queue.sync([first, second], scope="scope", now=40)
        values = {item["tracking_id"]: item for item in queue.snapshot(now=40)}
        self.assertEqual(values["target-a-001"]["first_confirmed_monotonic"], 40)
        self.assertEqual(values["target-b-002"]["first_confirmed_monotonic"], 0)
        self.assertEqual(queue.select(now=40)["tracking_id"], "target-b-002")

    def test_bound_owner_change_defensively_resets_candidate_age(self):
        queue = CandidateQueue()
        queue.sync([row("target-a-001", name="owner-a")], scope="scope", now=0)
        queue.sync([row("target-a-001", name="owner-b")], scope="scope", now=40)
        self.assertEqual(queue.summary(now=40)["oldest_wait_seconds"], 0)

    def test_unknown_and_measured_zero_keep_different_status_but_fair_opportunity(self):
        queue = CandidateQueue()
        unknown, zero, fast = row("target-u-001"), row("target-z-001"), row("target-f-001")
        queue.sync([unknown, zero], scope="scope", now=0,
                   rates={zero["tracking_id"]: Decimal(0)})
        queue.sync([unknown, zero, fast], scope="scope", now=1,
                   rates={zero["tracking_id"]: Decimal(0), fast["tracking_id"]: Decimal("0.1")})
        self.assertEqual(queue.select(now=29.999)["tracking_id"], fast["tracking_id"])
        self.assertEqual(queue.select(now=30)["tracking_id"], unknown["tracking_id"])
        status = {item["tracking_id"]: item["rate_status"] for item in queue.snapshot(now=30)}
        self.assertEqual(status[unknown["tracking_id"]], "unknown")
        self.assertEqual(status[zero["tracking_id"]], "zero")
        self.assertEqual(queue.summary(now=30)["unknown_rate_count"], 1)
        self.assertEqual(queue.summary(now=30)["zero_rate_count"], 1)

    def test_same_batch_aged_candidates_use_positive_rate_then_stable_id(self):
        queue = CandidateQueue()
        rows = [row("target-a-001"), row("target-b-002"), row("target-c-003")]
        queue.sync(rows, scope="scope", now=0,
                   rates={"target-b-002": Decimal("1"), "target-c-003": Decimal("1")})
        self.assertEqual(queue.select(now=100)["tracking_id"], "target-b-002")

    def test_missing_rate_on_refresh_becomes_unknown(self):
        queue = CandidateQueue()
        queue.sync([row("target-a-001")], scope="scope", now=0,
                   rates={"target-a-001": Decimal("12")})
        queue.sync([row("target-a-001")], scope="scope", now=5)
        self.assertEqual(queue.snapshot(now=5)[0]["rate_status"], "unknown")
        self.assertEqual(queue.snapshot(now=5)[0]["first_confirmed_monotonic"], 0)

    def test_rate_comparison_preserves_decimal_precision(self):
        queue = CandidateQueue()
        with localcontext() as context:
            context.prec = 6
            queue.sync([row("target-a-001"), row("target-b-002")], scope="scope", now=0,
                       rates={"target-a-001": Decimal("0.000000100000000001"),
                              "target-b-002": Decimal("0.000000100000000002")})
            self.assertEqual(queue.select(now=0)["tracking_id"], "target-b-002")

    def test_select_does_not_consume_or_mutate_candidate(self):
        queue = CandidateQueue()
        candidate = row("target-a-001", reasons=["synthetic"])
        queue.sync([candidate], scope="scope", now=0)
        candidate["reasons"].append("changed input")
        selected = queue.select(now=0)
        selected["reasons"].append("changed return")
        self.assertEqual(queue.select(now=0)["reasons"], ["synthetic"])
        self.assertEqual(queue.summary(now=0)["count"], 1)

    def test_hundred_targets_refresh_past_300_seconds_preserves_waiting_ages(self):
        queue = CandidateQueue()
        remaining = [row(f"target-{index:03d}") for index in range(100)]
        random.Random(20261106).shuffle(remaining)
        queue.sync(remaining, scope="scope", now=0)
        selected = []
        for number in range(100):
            stamp = number * 4
            queue.sync(list(reversed(remaining)), scope="scope", now=stamp)
            chosen = queue.select(now=stamp)
            selected.append(chosen["tracking_id"])
            self.assertEqual(queue.summary(now=stamp)["oldest_wait_seconds"], stamp)
            remaining = [item for item in remaining if item["tracking_id"] != chosen["tracking_id"]]
        self.assertEqual(selected, [f"target-{index:03d}" for index in range(100)])
        queue.sync([], scope="scope", now=400)
        self.assertIsNone(queue.select(now=400))
        self.assertEqual(queue.summary(now=400)["count"], 0)
        # This tests age through caller-supplied fresh syncs, not live fee freshness.

    def test_clock_regression_and_invalid_clocks_fail_without_new_permissions(self):
        queue = CandidateQueue()
        queue.sync([row("target-a-001")], scope="scope", now=10)
        for bad in (9.99, -1, True, float("nan"), float("inf")):
            with self.assertRaises(SchedulerError):
                queue.select(now=bad)
        self.assertEqual(queue.summary(now=10)["oldest_wait_seconds"], 0)

    def test_invalid_rates_scope_and_id_do_not_replace_valid_queue(self):
        queue = CandidateQueue()
        original = row("target-a-001")
        queue.sync([original], scope="scope", now=10)
        for bad in (Decimal("NaN"), Decimal("Infinity"), Decimal("-1"), 1.0, True, "1"):
            with self.assertRaises(SchedulerError):
                queue.sync([original], scope="scope", now=20, rates={"target-a-001": bad})
        with self.assertRaises(SchedulerError):
            queue.sync([original], scope=["mutable"], now=20)
        with self.assertRaises(SchedulerError):
            queue.sync([original], scope=None, now=20)
        with self.assertRaises(SchedulerError):
            queue.sync([original], scope="scope", now=20, rates={"target-outside": Decimal(1)})
        for bad in ("", "short", "sk-" + "a" * 32):
            with self.assertRaises(SchedulerError):
                queue.sync([row(bad)], scope="scope", now=20)
        self.assertEqual(queue.select(now=10), original)

    def test_zero_quota_payload_is_preserved_without_reinterpreting_business_rule(self):
        queue = CandidateQueue()
        zero = row("target-a-001", amount=Decimal(0), complete=False, spent=None)
        queue.sync([zero], scope="scope", now=0)
        self.assertEqual(queue.select(now=0), zero)


if __name__ == "__main__":
    unittest.main()
