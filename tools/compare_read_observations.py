"""Run a paired, READ-only local 4B benchmark using production browser actions.

Call with --assets, --chromium and --output (a new directory). No login, ledger
or deletion permit is used. The synthetic backend and model run on loopback.
"""
import argparse
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import Route, sync_playwright
from quota_guard.core import GuardError, TZ
from quota_guard.cost_parser import parse_cost_response
from quota_guard.operator_fixture import HTML, LoopbackTransport, OfflinePlatform
from quota_guard.operator_runtime import (
    ASSET_LOCK_HASH, CONFIG, MODEL_SHA256, SYSTEM, OperatorRuntime, build_messages,
)
from quota_guard.web_operator import STAGE_SECONDS, WebOperator

SEED = 7719
DAY = "2026-11-24"
# Counterbalance representation order across targets, keeping all settings fixed.
CASES = (("flat", "Cedar"), ("table", "Cedar"), ("table", "Maple"), ("flat", "Maple"))


def digest(value):
    data = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def save(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


class ReadOnlyPlatform(OfflinePlatform):
    def handle(self, method, path, body=b"", source="browser"):
        if method not in {"GET", "HEAD"}:
            self.event("write_blocked", method=method)
            return 405, "text/plain", b"Read-only fixture"
        return super().handle(method, path, body, source)


@contextmanager
def loopback_only():
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def guarded(fn):
        def connect(sock, endpoint):
            if not isinstance(endpoint, tuple) or endpoint[0] not in ("127.0.0.1", "::1"):
                raise OSError("Non-loopback connection denied")
            return fn(sock, endpoint)
        return connect

    with patch.object(socket.socket, "connect", guarded(original_connect)), \
         patch.object(socket.socket, "connect_ex", guarded(original_connect_ex)):
        yield


def fixture():
    platform = ReadOnlyPlatform(seed=SEED)
    platform.today = DAY
    fixed_stamp = int(datetime.fromisoformat(DAY).replace(tzinfo=TZ).timestamp())
    for key in platform.keys:
        if key["tracking_id"] == platform.target_id:
            key["name"] = "Cedar"
        elif key["tracking_id"] == platform.control_id:
            key["name"] = "Maple"
        key["created_at"] = fixed_stamp
    platform.initial = [dict(row) for row in platform.keys]
    return platform


class TracedRuntime(OperatorRuntime):
    def __init__(self, *, trace, peer_id, **options):
        super().__init__(**options)
        self.trace = trace
        self.peer_id = peer_id

    def decide(self, task, observation, history, *, deadline):
        if not self.trace:
            def input_except_target(candidate):
                messages = build_messages(candidate, observation, history,
                                          read_observation=self.read_observation)
                content = json.loads(messages[-1]["content"])
                # Compare every input bit except the task's ID on the exact same
                # captured snapshot, including the complete action schema.
                del content["current_task"]["target_ids"]
                return digest([messages[0], content]), content["observation"]
            fingerprint, public = input_except_target(task)
            peer_hash, _ = input_except_target({**task, "target_ids": [self.peer_id]})
            if fingerprint != peer_hash:
                raise AssertionError("Goal-dependent observation or action schema")
            self.first_observation = public
            self.first_fingerprint = fingerprint

        entry = {"step": len(self.trace) + 1}
        self.trace.append(entry)
        try:
            action = super().decide(task, observation, history, deadline=deadline)
        finally:
            entry["metrics"] = dict(self.last_metrics)
        entry["action"] = action
        ref = action["arguments"].get("ref")
        node = next((n for n in observation["nodes"] if n["ref"] == ref), {})
        entry["selected_control"] = {k: node[k] for k in ("text", "tag", "context") if k in node}
        return action


def run_case(browser, platform, assets, run_dir, mode, name):
    context = browser.new_context(locale="zh-CN", timezone_id="Asia/Shanghai", service_workers="block")
    transport = LoopbackTransport(platform.origin)

    def offline_fetch(route, **options):
        if (options.get("max_redirects") != 0 or options.get("max_retries") != 0 or
                set(options) - {"max_redirects", "max_retries", "timeout"}):
            raise AssertionError("Unexpected production forwarding options")
        request = route.request
        return transport.fetch(request.url, method=request.method, body=request.post_data_buffer,
                               source="browser")

    # Unrouted browser traffic cannot escape to the network. Only a request
    # approved by the production gate reaches offline_fetch and the backend.
    context.route("**/*", lambda route: route.abort("blockedbyclient"))
    context.route_web_socket("**/*", lambda ws: ws.close())
    page = context.new_page()
    keys = {k["name"]: k["tracking_id"] for k in platform.keys}
    target = keys[name]
    peer_id = next(keys[n] for n in ("Cedar", "Maple") if n != name)
    steps = []
    events = []
    runtime = TracedRuntime(assets=assets, state_dir=run_dir / "state", read_observation=mode,
                            trace=steps, peer_id=peer_id)
    begin = time.monotonic()
    initial_events = len(platform.snapshot()["events"])
    result = {"mode": mode, "target": name, "tracking_id": target, "steps": steps}
    try:
        runtime.start(deadline=begin + CONFIG["startup_timeout_seconds"])
        result["startup_seconds"] = round(time.monotonic() - begin, 3)
        start_read = time.monotonic()
        with patch.object(Route, "fetch", offline_fetch):
            operator = WebOperator(page, runtime, emit=lambda event, **fields: events.append(
                {"event": event, **fields}))
            try:
                url, payload = operator.read_cost(DAY, DAY, [target])
                totals = parse_cost_response(payload, url, DAY, DAY, tracking_ids=[target])
                result["parsed_amount"] = str(totals.get(target))
            finally:
                result["read_seconds"] = round(time.monotonic() - start_read, 3)
    except GuardError as exc:
        result["reason"] = getattr(exc, "reason_code", type(exc).__name__)
    finally:
        runtime.close()
        context.close()
    result["elapsed_seconds"] = round(time.monotonic() - begin, 3)
    result["gate_events"] = events
    if hasattr(runtime, "first_observation"):
        result["first_input_without_target_sha256"] = runtime.first_fingerprint
        save(run_dir / f"{mode}-{name}-first-observation.json", runtime.first_observation)
    backend = platform.snapshot()["events"][initial_events:]
    result["cost_requests"] = [e for e in backend if e["kind"] == "cost_read"]
    result["write_count"] = sum(e["kind"] in {"post_arrival", "write_blocked"} for e in backend)
    stamp = int(datetime.fromisoformat(DAY).replace(tzinfo=TZ).timestamp())
    matching = [e for e in result["cost_requests"] if e["ids"] == [target]
                and e["start"] == [str(stamp)] and e["end"] == [str(stamp + 86400)]
                and e["source"] == "browser"]
    result["correct_target_gets"] = len(matching)
    result["wrong_scope_gets"] = len(result["cost_requests"]) - len(matching)
    result["read_succeeded"] = (len(matching) == 1 and not result["wrong_scope_gets"]
                                and result.get("parsed_amount") is not None
                                and Decimal(result["parsed_amount"]) == Decimal(platform.cost)
                                and result["write_count"] == 0)
    if result["wrong_scope_gets"] or result["write_count"] or platform.keys != platform.initial:
        raise AssertionError("Unexpected change or request scope at synthetic backend")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--chromium", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.chromium.is_file() or not args.assets.is_dir():
        parser.error("Local Chromium and model assets must exist")
    args.output.mkdir(parents=True, exist_ok=False)
    platform = fixture()
    cases = []
    report = {"scope": "READ through Chromium, production WebOperator and local 4B; synthetic HTTP only",
              "day": DAY, "page_seed": SEED, "model_sha256": MODEL_SHA256, "asset_lock_sha256": ASSET_LOCK_HASH,
              "config": CONFIG, "system_sha256": hashlib.sha256(SYSTEM.encode()).hexdigest(),
              "stage_seconds": STAGE_SECONDS, "case_order": CASES, "html_sha256": hashlib.sha256(HTML.encode()).hexdigest(),
              "rows": [{"name": k["name"], "id": k["tracking_id"], "kind": k["key_type"]} for k in platform.keys],
              "sources_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in
                                 [Path(__file__), *(ROOT / "quota_guard" / n for n in
                                 ("web_operator.py", "operator_runtime.py", "operator_contract.py", "operator_fixture.py"))]},
              "cases": cases}
    save(args.output / "summary.json", report)
    with tempfile.TemporaryDirectory(dir=args.output) as temp_dir, \
         patch.dict(os.environ, {"TEMP": temp_dir, "TMP": temp_dir, "TMPDIR": temp_dir}), \
         loopback_only(), socket.socket() as dead_proxy:
        dead_proxy.bind(("127.0.0.1", 0))
        dead_proxy.listen(4)
        platform.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(executable_path=str(args.chromium), headless=True,
                    proxy={"server": "http://127.0.0.1:" + str(dead_proxy.getsockname()[1])},
                    args=["--disable-background-networking", "--disable-component-update", "--no-first-run"])
                try:
                    report["chromium_version"] = browser.version
                    for mode, name in CASES:
                        result = run_case(browser, platform, args.assets, args.output, mode, name)
                        cases.append(result)
                        print(f'{mode} {name}: read={result["read_succeeded"]}, '
                              f'correctGET={result["correct_target_gets"]}, seconds={result["elapsed_seconds"]}', flush=True)
                        save(args.output / "summary.json", report)
                finally:
                    browser.close()
        finally:
            platform.close()
    comparisons = {}
    for mode in ("flat", "table"):
        group = [c for c in cases if c["mode"] == mode]
        hashes = [c.get("first_input_without_target_sha256") for c in group]
        comparisons[mode] = len(hashes) == 2 and all(hashes) and hashes[0] == hashes[1]
    report["goal_swaps_equal_except_target"] = comparisons
    save(args.output / "summary.json", report)
    if not all(comparisons.values()):
        raise SystemExit("Initial model inputs did not match across both target swaps")
    # A legitimate negative model outcome is a completed experiment.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
