"""Deterministic, in-memory ordering of already eligible management candidates.

The caller owns quota, identity, freshness, account exclusion and operation
checks. This queue neither grants permission nor consumes a deletion attempt.
Refreshing the same authorization scope keeps continuous candidates' age. A
new scope or explicit reset starts a new waiting epoch; no monotonic clock value
is persisted across processes. Production may leave every rate unknown.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
import math
import re
import time


_TRACKING_ID = re.compile(r"[A-Za-z0-9_-]{8,200}")


class SchedulerError(ValueError):
    """A caller/context inconsistency; the owner must stop or revalidate."""


@dataclass(frozen=True)
class _Candidate:
    row: dict
    first_confirmed: float
    rate: Decimal | None


class CandidateQueue:
    """A small candidate set, selected by fair age, optional rate, then exact ID.

    ``sync`` receives only rows already accepted by the existing business rules.
    A scope is an immutable, hashable identity for the current ledger/period,
    binding context and automatic-management generation. Changing observations
    within that same scope must not change the scope token.

    Rates are optional *validated current-target* Decimal amounts per common
    unit of time. Unknown is None and is never imputed as a zero rate. Known
    positive rates precede unknown/zero before aging. Aged candidates precede
    newer candidates, then sort by first-confirmed batch, positive rate within
    that batch, and stable Tracking ID. The age threshold is not a deadline.
    """

    def __init__(self, age_seconds=30):
        if (isinstance(age_seconds, bool) or not isinstance(age_seconds, (int, float))
                or not math.isfinite(age_seconds) or age_seconds <= 0):
            raise SchedulerError("候选等待阈值必须是有限正数。")
        self.age_seconds = float(age_seconds)
        self.reset()

    def reset(self):
        """End this waiting epoch, without touching any persisted operation."""
        self._scope = None
        self._entries: dict[str, _Candidate] = {}
        self._last_now = None

    def _clock(self, now):
        stamp = time.monotonic() if now is None else now
        if (isinstance(stamp, bool) or not isinstance(stamp, (int, float))
                or not math.isfinite(stamp) or stamp < 0):
            raise SchedulerError("候选调度时间无效，需重新核验管理上下文。")
        stamp = float(stamp)
        if self._last_now is not None and stamp < self._last_now:
            raise SchedulerError("候选调度时钟回退，需停止并重新核验。")
        return stamp

    @staticmethod
    def _row(row):
        if not isinstance(row, dict):
            raise SchedulerError("候选记录格式无效。")
        identity, name = row.get("tracking_id"), row.get("name")
        if (not isinstance(identity, str) or not _TRACKING_ID.fullmatch(identity)
                or identity.lower().startswith("sk-")):
            raise SchedulerError("候选缺少完整的 Tracking ID。")
        if (not isinstance(name, str) or not name.strip() or name != name.strip()
                or len(name) > 512 or any(ord(char) < 32 for char in name)):
            raise SchedulerError("候选缺少明确的人员绑定。")
        return identity, deepcopy(row)

    @staticmethod
    def _rate(rate):
        if rate is not None and (not isinstance(rate, Decimal) or not rate.is_finite() or rate < 0):
            raise SchedulerError("候选速率必须是已核验的非负 Decimal，或明确未知。")
        return rate

    def sync(self, rows, *, scope, now=None, rates=None):
        """Atomically replace current candidates, preserving continuous age.

        One clock read defines the entire batch. Identical duplicate IDs merge;
        conflicting rows reject the entire update. Removing a candidate ends
        its eligibility episode. A changed bound name also starts a new episode
        defensively, even if the caller forgot to change its binding scope.
        Absent rate observations become unknown rather than retaining old rates.
        """
        stamp = self._clock(now)
        if scope is None:
            raise SchedulerError("候选需要明确的管理上下文。")
        try:
            hash(scope)
        except TypeError:
            raise SchedulerError("候选上下文必须是不可变标识。") from None
        if not isinstance(rows, (list, tuple)):
            raise SchedulerError("候选集合必须是一次完整评估得到的列表。")
        if rates is not None and not isinstance(rates, dict):
            raise SchedulerError("候选速率映射格式无效。")
        observations = {}
        for row in rows:
            identity, copied = self._row(row)
            if identity in observations and observations[identity] != copied:
                raise SchedulerError("同一 Tracking ID 的候选记录冲突，未更新队列。")
            observations[identity] = copied
        rates = {} if rates is None else rates
        if set(rates) - observations.keys():
            raise SchedulerError("速率包含本批候选之外的目标，未更新队列。")
        normalized_rates = {identity: self._rate(rates.get(identity)) for identity in observations}
        previous = self._entries if scope == self._scope else {}
        updated = {}
        for identity, row in observations.items():
            old = previous.get(identity)
            first = (old.first_confirmed if old is not None and old.row["name"] == row["name"]
                     else stamp)
            updated[identity] = _Candidate(row, first, normalized_rates[identity])
        self._scope, self._entries, self._last_now = scope, updated, stamp

    def _priority(self, identity, candidate, now):
        positive = candidate.rate is not None and candidate.rate > 0
        # copy_negate preserves all supplied digits regardless of Decimal context.
        rate_key = (0 if positive else 1, candidate.rate.copy_negate() if positive else Decimal(0))
        if now - candidate.first_confirmed >= self.age_seconds:
            return (0, candidate.first_confirmed, *rate_key, identity)
        return (1, *rate_key, candidate.first_confirmed, identity)

    def select(self, *, now=None):
        """Return a copy of the current next row; selection does not dequeue it."""
        stamp = self._clock(now)
        self._last_now = stamp
        if not self._entries:
            return None
        identity = min(self._entries, key=lambda key: self._priority(key, self._entries[key], stamp))
        return deepcopy(self._entries[identity].row)

    def summary(self, *, now=None):
        """Bounded, identity-free queue metrics for the UI and diagnostics."""
        stamp = self._clock(now)
        self._last_now = stamp
        ages = [stamp - entry.first_confirmed for entry in self._entries.values()]
        return {
            "count": len(self._entries),
            "oldest_wait_seconds": max(ages, default=0.0),
            "aged_count": sum(age >= self.age_seconds for age in ages),
            "unknown_rate_count": sum(entry.rate is None for entry in self._entries.values()),
            "zero_rate_count": sum(entry.rate == Decimal(0) for entry in self._entries.values()),
        }

    def snapshot(self, *, now=None):
        """Read-only ordering/age observations, never permissions or operations."""
        stamp = self._clock(now)
        self._last_now = stamp
        ordered = sorted(self._entries, key=lambda key: self._priority(key, self._entries[key], stamp))
        return [{
            "tracking_id": identity,
            "first_confirmed_monotonic": self._entries[identity].first_confirmed,
            "wait_seconds": stamp - self._entries[identity].first_confirmed,
            "rate_status": ("unknown" if self._entries[identity].rate is None else
                            "zero" if self._entries[identity].rate == 0 else "positive"),
        } for identity in ordered]
