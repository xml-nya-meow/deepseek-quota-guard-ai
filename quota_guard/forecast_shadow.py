"""Bounded, optional forecast observations; never a source of action authority.

The current console adapter supplies a requested date range and cumulative costs,
not a proven data watermark or historical revision contract. Production callers
therefore leave data_as_of unset and obtain data_time_unknown, without an ETA.
Explicit comparable coverage/revision evidence is supported for offline testing
and a future separately verified adapter. No model, browser or requests run here.
"""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import sqlite3
from statistics import median


DATABASE = "forecast-shadow.sqlite3"
MAX_ROWS = 2048
MAX_TARGET_HISTORY = 12
MAX_COLLECTION_ROWS = 200
_APPLICATION_ID = 1146308179
_SCHEMA = 1


class ShadowRecordError(ValueError):
    """A controlled optional-record failure, never a reason to grant an action."""


@dataclass(frozen=True)
class Observation:
    context: str
    evidence: str
    cumulative: str | None
    complete: bool
    first_observed_at: float
    data_as_of: float | None = None
    interval_seconds: int | None = None
    revision_marker: str | None = None


def _amount(value):
    if value is None or isinstance(value, bool):
        raise ValueError("amount")
    result = Decimal(str(value))
    if (not result.is_finite() or result < 0 or result > Decimal("1000000000000")
            or result.as_tuple().exponent < -18):
        raise ValueError("amount")
    return result


def _stamp(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 <= value <= 253402300799):
        raise ValueError("time")
    return float(value)


def _ref(value):
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise ValueError("reference")
    return sha256(value.encode("utf-8")).hexdigest()


def _canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _result(status, **fields):
    return {"shadow_only": True, "status": status, "eta_at": None, **fields}


def forecast_observations(context, limit, observations, now):
    """Compute a transparent trend only from supplied comparable evidence.

    revision_marker identifies the version of previously covered history, not a
    changing full-response hash. A change or same-time correction resets the
    sample window. Four subsequent comparable endpoints can restore a trend.
    Caller order is first-observed order, never hindsight-sorted data time.
    Returned values cannot authorize deletion or alter collection frequency.
    """
    try:
        ceiling, now = _amount(limit), _stamp(now)
        if ceiling == 0:
            return _result("zero_limit")
        if not observations:
            return _result("insufficient_history")
        if len(observations) > MAX_TARGET_HISTORY:
            return _result("invalid_input")
        seen, accepted = {}, []
        reset_reason = None
        previous_observed = -1.0
        for point in observations:
            if not isinstance(point, Observation) or point.context != context:
                return _result("scope_changed")
            first = _stamp(point.first_observed_at)
            if first > now or first < previous_observed:
                return _result("invalid_time")
            previous_observed = first
            if not isinstance(point.evidence, str) or not point.evidence or len(point.evidence) > 256:
                return _result("invalid_input")
            signature = (point.cumulative, point.complete, point.data_as_of,
                         point.interval_seconds, point.revision_marker)
            if point.evidence in seen:
                if signature != seen[point.evidence]:
                    return _result("evidence_conflict")
                continue
            seen[point.evidence] = signature
            if point.complete is not True or point.cumulative is None:
                accepted = []
                reset_reason = "coverage_incomplete"
                continue
            amount = _amount(point.cumulative)
            if point.data_as_of is None:
                accepted = []
                reset_reason = "data_time_unknown"
                continue
            stamp = _stamp(point.data_as_of)
            if stamp > first:
                return _result("invalid_time")
            if type(point.interval_seconds) is not int or point.interval_seconds not in (3600, 86400):
                accepted = []
                reset_reason = "granularity_unknown"
                continue
            if not isinstance(point.revision_marker, str) or not point.revision_marker:
                accepted = []
                reset_reason = "revision_unknown"
                continue
            if first - stamp > point.interval_seconds:
                accepted = []
                reset_reason = "late_data"
                continue
            current = (stamp, amount, point.interval_seconds, point.revision_marker)
            if accepted:
                before = accepted[-1]
                if stamp < before[0]:
                    accepted = []
                    reset_reason = "data_time_reversed"
                elif (point.revision_marker != before[3] or amount < before[1]
                      or (stamp == before[0] and amount != before[1])):
                    accepted = []
                    reset_reason = "revision_detected"
                elif stamp == before[0]:
                    # Another source response at identical target coverage is
                    # not a new interval, even if another Key changed upstream.
                    continue
                elif (point.interval_seconds != before[2]
                      or stamp - before[0] != point.interval_seconds):
                    accepted = []
                    reset_reason = "irregular_interval"
            accepted.append(current)
        if not accepted:
            return _result(reset_reason or "insufficient_history")
        stamp, last, interval, _revision = accepted[-1]
        if now - stamp > interval:
            return _result("stale_data", data_as_of=stamp)
        if len(accepted) < 4:
            return _result(reset_reason or "insufficient_history", data_as_of=stamp,
                           usable_endpoints=len(accepted))
        window = accepted[-4:]
        with localcontext() as precision:
            precision.prec = 50
            remaining = ceiling - last
            if remaining <= 0:
                return _result("observed_limit", data_as_of=stamp)
            rates = [(right[1] - left[1]) / Decimal(str(right[0] - left[0]))
                     for left, right in zip(window, window[1:])]
            rate = median(rates)
            if max(rates) == 0:
                return _result("no_observed_growth", data_as_of=stamp)
            if rate == 0:
                return _result("intermittent_growth", data_as_of=stamp)
            eta = Decimal(str(stamp)) + remaining / rate
            # No infinity/overflow is sent to JSON for a tiny positive rate.
            if eta > Decimal("253402300799"):
                return _result("eta_out_of_range", data_as_of=stamp)
            return _result("estimated", data_as_of=stamp, eta_at=format(eta, "f"),
                           trend_yuan_per_hour=format(rate * 3600, "f"),
                           interval_seconds=interval,
                           estimate_kind="recent_observed_trend_not_probability",
                           low_resolution=interval == 86400)
    except (ValueError, TypeError, InvalidOperation, OverflowError):
        return _result("invalid_input")


def _open_database(path):
    if path.is_symlink():
        raise ShadowRecordError("预测影子记录路径无效，未写入。")
    existed = path.exists()
    connection = sqlite3.connect(path, timeout=0.15)
    try:
        if existed:
            if (connection.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID
                    or connection.execute("PRAGMA user_version").fetchone()[0] != _SCHEMA):
                raise ShadowRecordError("预测影子记录版本不符，未写入。")
        else:
            connection.execute("PRAGMA auto_vacuum=FULL")
            connection.execute(f"PRAGMA application_id={_APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version={_SCHEMA}")
            connection.execute("""CREATE TABLE observations(
                id INTEGER PRIMARY KEY, context TEXT NOT NULL, target TEXT NOT NULL,
                evidence TEXT NOT NULL, source_ref TEXT NOT NULL,
                coverage_end REAL, interval_seconds INTEGER, revision_marker TEXT,
                cumulative TEXT, complete INTEGER NOT NULL,
                first_observed REAL NOT NULL, last_read REAL NOT NULL,
                status TEXT NOT NULL, forecast TEXT NOT NULL,
                UNIQUE(context,target,evidence))""")
            connection.commit()
        return connection
    except Exception:
        connection.close()
        raise


def record_collection(directory, *, scope, rows, source_id, collected_at,
                      data_as_of=None, interval_seconds=None, revision_marker=None):
    """Record an optional shadow of a successful collection in a separate file.

    scope must include ledger/account/period/baseline/currency/binding context.
    Per-row binding_scope is optional additional context. No raw name, Tracking
    ID, response, credential or source string is stored. Amounts remain local.
    Current production must omit time/revision arguments it cannot establish.
    The caller isolates ShadowRecordError from the business/action workflow.
    """
    try:
        directory = Path(directory)
        if not directory.is_dir():
            raise ValueError("directory")
        scope_ref, source_ref, now = _ref(scope), _ref(source_id), _stamp(collected_at)
        if not isinstance(rows, (list, tuple)) or len(rows) > MAX_COLLECTION_ROWS:
            raise ValueError("rows")
        if data_as_of is not None:
            data_as_of = _stamp(data_as_of)
        if interval_seconds is not None and (type(interval_seconds) is not int
                                              or interval_seconds not in (3600, 86400)):
            raise ValueError("interval")
        revision_ref = _ref(revision_marker) if revision_marker is not None else None
        prepared, identities = [], set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("row")
            if not row.get("name") or row.get("status") in {
                    "skipped", "retired", "recovery_pending", "present_unresolved"}:
                continue
            if Decimal(str(row.get("amount"))) == -1:
                continue
            key = row.get("tracking_id")
            if not key:
                continue
            if (not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,200}", key)
                    or key.lower().startswith("sk-") or key in identities):
                raise ValueError("identity")
            identities.add(key)
            target = _ref(key)
            limit = min(_amount(row["amount"]), _amount(row["share"]))
            amount = None if row.get("spent") is None else format(_amount(row["spent"]), "f")
            complete = row.get("complete") is True
            binding = row.get("binding_scope", "default")
            context = _ref(_canonical([scope_ref, _ref(binding), target,
                                      format(limit, "f")]))
            # Target content, coverage and revision form evidence. A changing
            # full-payload digest alone cannot create a new target timepoint.
            evidence = _ref(_canonical([amount, complete, data_as_of,
                                       interval_seconds, revision_ref]))
            prepared.append((context, target, evidence, amount, complete, limit))
        if not prepared:
            return {"shadow_only": True, "count": 0, "estimated": 0, "statuses": {}}
        statuses = {}
        with closing(_open_database(directory / DATABASE)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            for context, target, evidence, amount, complete, limit in prepared:
                existing = db.execute("SELECT id,first_observed FROM observations WHERE context=? AND target=? AND evidence=?",
                                      (context, target, evidence)).fetchone()
                if existing:
                    db.execute("UPDATE observations SET last_read=MAX(last_read,?), source_ref=? WHERE id=?",
                               (now, source_ref, existing[0]))
                    current_id = existing[0]
                else:
                    cursor = db.execute("""INSERT INTO observations(context,target,evidence,source_ref,
                        coverage_end,interval_seconds,revision_marker,cumulative,complete,
                        first_observed,last_read,status,forecast) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (context, target, evidence, source_ref, data_as_of, interval_seconds,
                         revision_ref, amount, int(complete), now, now, "pending", "{}"))
                    current_id = cursor.lastrowid
                db.execute("""DELETE FROM observations WHERE context=? AND target=? AND id NOT IN
                    (SELECT id FROM observations WHERE context=? AND target=? ORDER BY id DESC LIMIT ?)""",
                           (context, target, context, target, MAX_TARGET_HISTORY))
                history = db.execute("""SELECT evidence,cumulative,complete,first_observed,
                    coverage_end,interval_seconds,revision_marker FROM observations
                    WHERE context=? AND target=? ORDER BY id""", (context, target)).fetchall()
                observations = [Observation(context, item[0], item[1], bool(item[2]), item[3],
                                            item[4], item[5], item[6]) for item in history]
                output = (forecast_observations(context, limit, observations, now)
                          if evidence == history[-1][0] else _result("stale_replay"))
                db.execute("UPDATE observations SET status=?,forecast=? WHERE id=?",
                           (output["status"], _canonical(output), current_id))
                statuses[output["status"]] = statuses.get(output["status"], 0) + 1
            db.execute("DELETE FROM observations WHERE id NOT IN (SELECT id FROM observations ORDER BY id DESC LIMIT ?)",
                       (MAX_ROWS,))
        return {"shadow_only": True, "count": len(prepared),
                "estimated": statuses.get("estimated", 0), "statuses": statuses}
    except ShadowRecordError:
        raise
    except (ValueError, TypeError, InvalidOperation, OSError, sqlite3.Error, OverflowError, KeyError):
        raise ShadowRecordError("预测影子记录暂不可用，业务费用与处理资格不受影响。") from None
