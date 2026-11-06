"""Opt-in portable acceptance using real local code and a synthetic transport.

Never called during normal startup. All fixtures and the injected responsibility
registry live in one newly created directory. No existing ledger is accepted.
This checks packaging/integration, not the live platform contract.
"""
from concurrent.futures import Future
from contextlib import ExitStack
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import socket
import time
import tkinter as tk
from unittest.mock import patch
from uuid import uuid4

from . import __version__
from .core import DataStore, GuardError, Person, TZ


def _require(condition):
    if not condition:
        raise AssertionError('OFFLINE_ACCEPTANCE_CHECK_FAILED')


def _new_directory(destination):
    path = Path(destination).absolute()
    if not path.name.startswith('offline-acceptance-'):
        raise ValueError('OFFLINE_ACCEPTANCE_NEW_DIRECTORY_REQUIRED')
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink() or getattr(ancestor, 'is_junction', lambda: False)():
            raise ValueError('OFFLINE_ACCEPTANCE_LINK_REJECTED')
    path.parent.resolve(strict=True)
    # Atomic exclusive creation: never reuse, clean or modify an existing path.
    path.mkdir(exist_ok=False)
    (path / 'OFFLINE-FIXTURE.txt').write_text(
        'Synthetic acceptance data only. No real account or API request.\n', encoding='utf-8')
    return path.resolve()


class _Transport:
    """Exercise actual DeletePermit and receipts, without any HTTP client."""
    def __init__(self, store, inventory, account, registry, *, cost):
        self.store, self.inventory, self.account = store, list(inventory), account
        self.registry = registry
        self.cost = cost
        self.calls = []
        self.future = self.permit = self.target = None
        self.sends = self.consumptions = 0
        self.boundary_checks = 0

    def submit(self, action, **kwargs):
        self.calls.append(action)
        future = Future()
        if action == 'collect':
            ids = [k['tracking_id'] for k in self.inventory if k['name'] in kwargs['managed_names']]
            today = self.store.load().start_date
            future.set_result({'keys': list(self.inventory), 'account_id': self.account,
                'totals': {key: self.cost for key in ids}, 'requested_tracking_ids': ids,
                'source_id': 'offline-acceptance-fixture', 'start_date': today, 'end_date': today,
                'collection_started_wall': time.time(), 'collection_started_monotonic': time.monotonic()})
        elif action == 'read_keys':
            future.set_result({'keys': list(self.inventory), 'account_id': self.account})
        elif action == 'revoke':
            _require(self.future is None)
            self.future, self.permit, self.target = future, kwargs['permit'], kwargs['tracking_id']
            self.permit.set_prepared_inventory(self.target, [k['tracking_id'] for k in self.inventory])
            self.permit.ready.set()
        else:
            raise AssertionError('OFFLINE_UNEXPECTED_TRANSPORT_ACTION')
        return future

    def tick(self):
        if self.future is None or self.future.done() or not self.permit.decided.is_set():
            return
        _require(self.permit.allowed)
        record = self.store.latest_action_record(self.target)
        _require(record['status'] == 'requested' and record['responsibility']['operation_id'] == record['operation_id'])
        _require(record['recovery_evidence']['target_tracking_id'] == self.target)
        from .declared_recovery import SEND_PREFIX
        from .responsibility_registry import ResponsibilityRegistry
        obligations = ResponsibilityRegistry(self.registry).open_obligations()
        _require(len(obligations) == 1 and obligations[0]['receipt'] == record['responsibility']
                 and obligations[0]['phase'] == 'possible_send')
        with self.store._connect() as db:
            row = db.execute('SELECT value FROM meta WHERE key=?',
                             (SEND_PREFIX + record['operation_id'],)).fetchone()
        _require(row is not None and json.loads(row[0]) == {'schema': 1, 'receipt': record['responsibility']})
        self.boundary_checks += 1
        self.permit.consume_for_click()
        self.consumptions += 1
        self.sends += 1
        self.inventory = [key for key in self.inventory if key['tracking_id'] != self.target]
        self.future.set_result({'absent': True})

    def interrupt(self):
        future = Future()
        future.set_result(None)
        return future

    def close(self):
        pass


class _Driver:
    def __init__(self, store, registry, inventory, account, *, cost=Decimal(0)):
        from .ui import App
        self.root = tk.Tk()
        self.root.withdraw()
        self.errors = []
        self.root.report_callback_exception = lambda *unused: self.errors.append('TK_CALLBACK_FAILED')
        self.app = App(self.root, store.folder, responsibility_directory=registry)
        self.transport = None
        self.until(lambda: self.app.config is not None)
        self.transport = _Transport(self.app.store, inventory, account, registry, cost=cost)
        self.app.browser = self.transport

    def until(self, predicate, timeout=12):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.root.update()
            _require(not self.errors)
            if predicate():
                return
            if self.transport is not None:
                self.transport.tick()
            time.sleep(.005)
        raise AssertionError('OFFLINE_ACCEPTANCE_TIMEOUT')

    def close(self):
        self.app.close()
        deadline = time.monotonic() + 12
        while not self.app.window_destroyed and time.monotonic() < deadline:
            self.root.update()
            time.sleep(.005)
        _require(self.app.window_destroyed and not self.errors)
        _require(self.app.workbook_timer is None and self.app.theme_timer is None)


def _exercise(base, report):
    from openpyxl import load_workbook
    from . import membership_recovery
    from .browser_service import key_fingerprint
    from .responsibility_gate import ResponsibilityGate, action_history
    from .responsibility_registry import ResponsibilityRegistry
    from .preferences import load_table_defaults

    def passed(name):
        report['cases'].append(name)

    identity = uuid4().hex
    keys = [{'name': name, 'tracking_id': f'offline-{identity}-{index}'}
            for index, name in enumerate(('Synthetic X', 'Synthetic Y', 'Synthetic Control'))]
    account = key_fingerprint(keys)
    registry = base / 'registry'
    stores = []
    for label in ('X', 'Y'):
        store = DataStore(base / label)
        store.initialize(14, '1500', datetime.now(TZ).date().isoformat())
        stores.append(store)
    first, second = stores
    driver = _Driver(first, registry, keys, account)
    try:
        app = driver.app
        app._set_table_defaults('0.05', 'kill_line')
        template = base / 'personnel-template.xlsx'
        with patch('quota_guard.ui.filedialog.asksaveasfilename', return_value=str(template)):
            app.save_template()
        _require(template.is_file())
        workbook = load_workbook(template)
        try:
            sheet = workbook.active
            _require(Decimal(str(sheet.cell(2, 2).value)) == Decimal('0.05'))
            # Imported active member after skipped and unnamed rows; no row index identity.
            for row in range(2, sheet.max_row + 1):
                sheet.cell(row, 1).value = None
                sheet.cell(row, 2).value = -1
            sheet.cell(14, 1).value = keys[0]['name']
            sheet.cell(14, 2).value = 1
            workbook.save(template)
        finally:
            workbook.close()
        with patch('quota_guard.ui.filedialog.askopenfilename', return_value=str(template)):
            app.import_people()
        _require(app.action_states.get('import_people') == '已完成')
        _require([(p.name, p.amount) for p in first.read_people() if p.name] == [(keys[0]['name'], Decimal(1))])
        _require(not app.automatic and not app.observing and driver.transport.sends == 0)
        passed('gui_template_import_sparse_rows')
        app.poll_interval_var.set('3')
        app.apply_poll_interval()
        _require(app.poll_interval_seconds == 3)
    finally:
        driver.close()
    passed('cooperative_close_cancels_timers')
    driver = _Driver(first, registry, keys, account)
    try:
        _require(driver.app.poll_interval_seconds == 3)
        _require(load_table_defaults(first.folder)[0].amount == Decimal('0.05'))
        _require([(p.name, p.amount) for p in driver.app.store.read_people() if p.name] == [(keys[0]['name'], Decimal(1))])
        _require(driver.transport.calls == [] and not driver.app.automatic)
        passed('reopen_keeps_import_and_preferences')
    finally:
        driver.close()

    second.save_people([Person(keys[1]['name'], Decimal(100))])
    for store in stores:
        store.bind_keys(keys, account)
    gate = ResponsibilityGate(registry)
    applied = []
    original_apply = membership_recovery._apply_member

    def interrupted(plan, member):
        if applied:
            raise GuardError('OFFLINE_SYNTHETIC_CUT')
        original_apply(plan, member)
        applied.append(member['ledger_id'])

    with patch.object(membership_recovery, '_apply_member', side_effect=interrupted):
        try:
            gate.initialize(stores)
        except GuardError:
            pass
        else:
            raise AssertionError('OFFLINE_CUT_NOT_REACHED')
    pending = gate.registry.membership_plan()
    _require(len(applied) == 1 and pending['state'] == 'pending')
    driver = _Driver(first, registry, keys, account, cost=Decimal(2))
    try:
        completed = ResponsibilityRegistry(registry).membership_plan()
        _require(completed['plan_id'] == pending['plan_id'] and completed['state'] == 'complete')
        _require(driver.transport.calls == [] and driver.transport.sends == 0)
        for store in stores:
            audits = [e for e in store.events(10000) if e['kind'] == 'membership_recovery']
            _require(len(audits) == 1)
        passed('reopen_resumes_original_membership_plan_without_send')
        driver.app.collect()
        driver.until(lambda: not driver.app.busy)
        driver.app.start()
        driver.until(lambda: driver.transport.sends == 1 and not driver.app.busy)
        _require(driver.transport.consumptions == 1)
        report['send_boundary_checks'] += driver.transport.boundary_checks
        history = action_history(first)
        _require([e['status'] for e in history] == ['pending', 'requested', 'absent_verified'])
        _require(len({e['operation_id'] for e in history}) == 1)
        _require(gate.registry.open_obligations() == [])
        keys = list(driver.transport.inventory)
        passed('positive_quota_automatic_synthetic_send_once')
    finally:
        driver.close()
    driver = _Driver(second, registry, keys, account)
    try:
        driver.app.tree.selection_set('0')
        driver.app.revoke_selected_once()
        driver.until(lambda: driver.transport.sends == 1 and not driver.app.busy)
        _require(driver.transport.consumptions == 1)
        report['send_boundary_checks'] += driver.transport.boundary_checks
        _require(action_history(second)[-1]['status'] == 'absent_verified')
        _require(len(driver.transport.inventory) == 1 and driver.transport.inventory[0]['name'] == 'Synthetic Control')
        _require(not driver.app.automatic and gate.registry.open_obligations() == [])
        passed('under_quota_force_synthetic_send_once')
    finally:
        driver.close()


def run_offline_acceptance(destination):
    """Return a small nonsecret report; leave all synthetic evidence for review."""
    base = _new_directory(destination)
    report = {'schema': 1, 'version': __version__, 'passed': False,
              'transport': 'synthetic', 'cases': [], 'real_requests': 0, 'send_boundary_checks': 0}
    try:
        with ExitStack() as stack:
            for name in ('connect', 'connect_ex'):
                stack.enter_context(patch.object(socket.socket, name, side_effect=RuntimeError('OFFLINE_NETWORK_BLOCKED')))
            stack.enter_context(patch('quota_guard.browser_process.IsolatedBrowserService',
                                      side_effect=RuntimeError('OFFLINE_BROWSER_BLOCKED')))
            for name in ('showinfo', 'showwarning'):
                stack.enter_context(patch('quota_guard.ui.messagebox.' + name))
            stack.enter_context(patch('quota_guard.ui.messagebox.showerror',
                                      side_effect=AssertionError('OFFLINE_UNEXPECTED_DIALOG')))
            stack.enter_context(patch('quota_guard.ui.messagebox.askyesno', return_value=True))
            _exercise(base, report)
        report['passed'] = True
    except Exception as exc:
        # No raw exceptions, names, identifiers, paths or credential-bearing output.
        report['reason_code'] = 'OFFLINE_ACCEPTANCE_FAILED'
        report['error_type'] = type(exc).__name__
    (base / 'acceptance.json').write_text(json.dumps(report, ensure_ascii=True, indent=2) + '\n', encoding='utf-8')
    return report
