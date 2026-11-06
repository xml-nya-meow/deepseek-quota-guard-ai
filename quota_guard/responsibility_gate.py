"""Join ledger evidence to the shared registry; no browser or remote actions.

The period identity is immutable and copied with a ledger. Paths only locate it.
An explicit initial inventory establishes the supported scope, not a claim to
discover every ledger on this computer. Credentials never enter the registry.
"""
from hashlib import sha256
import json

from .core import DataStore, GuardError
from .responsibility_registry import ResponsibilityRegistry, RegistryError, validate_receipt

ANCHOR_KEY = 'responsibility_anchor_v1'


def ledger_id(store):
    return sha256(store.load(require_workbook=False).period_id.encode('utf-8')).hexdigest()[:32]


def target_hash(tracking_id):
    return sha256(tracking_id.encode('utf-8')).hexdigest()


def ledger_anchor(store):
    with store._connect() as db:
        row = db.execute('SELECT value FROM meta WHERE key=?', (ANCHOR_KEY,)).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row[0])
        if (set(value) != {'epoch', 'checkpoint', 'roster_hash'} or not isinstance(value['epoch'], str)
                or not isinstance(value['roster_hash'], str) or len(value['roster_hash']) != 64
                or type(value['checkpoint']) is not int or value['checkpoint'] < 0):
            raise ValueError()
        return value
    except (TypeError, ValueError):
        raise RegistryError('REGISTRY_LEDGER_MISMATCH') from None


def _save_anchor(store, epoch, checkpoint, roster_hash=None):
    if roster_hash is None:
        old = ledger_anchor(store)
        if old is None:
            raise RegistryError('REGISTRY_LEDGER_MISMATCH')
        roster_hash = old['roster_hash']
    with store._connect() as db:
        db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',
                   (ANCHOR_KEY, json.dumps({'epoch': epoch, 'checkpoint': checkpoint,
                                            'roster_hash': roster_hash})))


def _roster_hash(members):
    # Witness membership in every declared ledger, so restoring an old registry
    # cannot silently erase a later-added member while these ledgers survive.
    identities = sorted(item['ledger_id'] for item in members)
    return sha256(json.dumps(identities, separators=(',', ':')).encode('ascii')).hexdigest()


def action_history(store):
    with store._connect() as db:
        return store._action_history(db)


class ResponsibilityGate:
    def __init__(self, directory=None):
        self.registry = ResponsibilityRegistry(directory)

    def initialize(self, stores):
        """Explicitly declare all supplied ledgers, including old uncertainty.

        Validate before creating anything. A copied/ambiguous origin or legacy
        operation without an ID must be reconciled before scope establishment.
        Interrupted migration remains blocked, never silently starts afresh.
        """
        stores = list(stores)
        ids = [ledger_id(store) for store in stores]
        if not ids or len(set(ids)) != len(ids) or any(ledger_anchor(s) is not None for s in stores):
            raise RegistryError('REGISTRY_LEDGER_MISMATCH')
        pending = []
        operations = set()
        for store, identity in zip(stores, ids):
            for item in store.unresolved_actions():
                if item.get('read_only'):
                    continue
                op = item.get('operation_id')
                if not op or op in operations or item.get('inherited_from_period'):
                    raise GuardError('旧操作缺少唯一原始编号或来自继承副本；请先在原账本核实，不会另造责任。')
                operations.add(op)
                pending.append((store, identity, item))
        state = self.registry.initialize(expected_ledger_ids=ids)
        epoch = state['epoch']
        for store, identity in zip(stores, ids):
            self.registry.register_ledger(identity, store.folder)
        roster = _roster_hash(self.registry.registered_ledgers())
        for store in stores:
            _save_anchor(store, epoch, 0, roster)
        for store, identity, item in pending:
            receipt = self.registry.register_unresolved(identity, item['operation_id'],
                target_hash(item['tracking_id']), expected_checkpoint=ledger_anchor(store)['checkpoint'])
            _save_anchor(store, epoch, receipt.checkpoint)
        self.registry.finish_initialization()

    def _enroll(self, store):
        identity = ledger_id(store)
        anchor = ledger_anchor(store)
        registered = self.registry.registered_ledgers()
        known = next((item for item in registered if item['ledger_id'] == identity), None)
        if known is not None:
            if anchor is None:
                raise RegistryError('REGISTRY_LEDGER_MISMATCH')
            return self.registry.check_ledger(identity, store.folder,
                                               epoch=anchor['epoch'], checkpoint=anchor['checkpoint'])
        if anchor is not None:
            raise RegistryError('REGISTRY_LEDGER_MISMATCH')
        # An ordinary new list can join an already established scope. Existing
        # action history needs an explicit, audited migration, not auto-enrolment.
        if action_history(store) and not self._trusted_successor(store, registered):
            raise GuardError('此历史账本尚未纳入共享责任登记；保持只读，请先在原账本核实或完成迁移。')
        state = self.registry.snapshot()
        if state['phase'] != 'ready':
            raise RegistryError('REGISTRY_INITIALIZING')
        self.registry.begin_enrollment(identity)
        registered = self.registry.register_ledger(identity, store.folder)
        members = self.registry.registered_ledgers()
        roster = _roster_hash(members)
        for member in members:
            current = DataStore(member['path'])
            if ledger_id(current) != member['ledger_id']:
                raise RegistryError('REGISTRY_LEDGER_MISMATCH')
            _save_anchor(current, member['epoch'], member['checkpoint'], roster)
        self.registry.finish_initialization()
        return registered

    def _trusted_successor(self, store, members):
        """An inherited operation remains owned and resolved by its origin.

        Only exact history references to already registered sources can join.
        Refresh inherited outcomes after the source has durably settled, never
        copy responsibility into a second registry operation.
        """
        by_identity = {m['ledger_id']: m for m in members}
        history = action_history(store)
        groups = {}
        for event in history:
            groups.setdefault(event.get('operation_id'), []).append(event)
        resolutions = []
        for operation, records in groups.items():
            first = records[0]
            parent_period = first.get('inherited_from_period')
            if not operation or not parent_period:
                return False
            parent_id = sha256(parent_period.encode('utf-8')).hexdigest()[:32]
            parent = by_identity.get(parent_id)
            if parent is None:
                return False
            original = DataStore(parent['path'])
            if ledger_id(original) != parent_id or original.load(require_workbook=False).account_id != store.load(require_workbook=False).account_id:
                return False
            source = [event for event in action_history(original) if event.get('operation_id') == operation]
            matching = next((event for event in source if event['id'] == first.get('original_event_id')), None)
            if matching is None:
                return False
            for field in ('tracking_id', 'name', 'status', 'evidence', 'recovery_evidence', 'responsibility'):
                if matching.get(field) != first.get(field):
                    return False
            if source[-1]['status'] == 'absent_verified' and records[-1]['status'] != 'absent_verified':
                resolutions.append((records[-1], source[-1]))
        for current, resolved in resolutions:
            store.record_action(current['name'], current['tracking_id'], 'absent_verified',
                '已核对登记原账本同一 operation 的持久缺席结论；继承引用更新，不产生新撤销。',
                operation_id=current['operation_id'], continuation=True)
        return True

    def _check_members(self):
        """Do not trust an idle registry after losing/replacing a member ledger."""
        open_receipts = {a['receipt']['operation_id']: a['receipt']
                         for a in self.registry.open_obligations()}
        members = self.registry.registered_ledgers()
        roster = _roster_hash(members)
        for member in members:
            store = DataStore(member['path'])
            if ledger_id(store) != member['ledger_id']:
                raise RegistryError('REGISTRY_LEDGER_MISMATCH')
            anchor = ledger_anchor(store)
            if anchor is None or anchor['roster_hash'] != roster:
                raise RegistryError('REGISTRY_LEDGER_MISMATCH')
            self.registry.check_ledger(member['ledger_id'], store.folder,
                                       epoch=anchor['epoch'], checkpoint=anchor['checkpoint'])
            inherited = any(a.get('responsibility', {}).get('ledger_id') not in {None, member['ledger_id']}
                            for a in store.unresolved_actions())
            if inherited and not self._trusted_successor(store, members):
                raise RegistryError('REGISTRY_LEDGER_MISMATCH')
            for action in store.unresolved_actions():
                if action.get('read_only'):
                    continue
                # A successor must not turn an inherited operation into its own.
                owner = action.get('responsibility', {}).get('ledger_id', member['ledger_id'])
                receipt = self.registry.receipt_for(owner, action.get('operation_id'))
                if (receipt is None or receipt.target_hash != target_hash(action['tracking_id'])
                        or open_receipts.get(action.get('operation_id')) != receipt.to_dict()):
                    raise GuardError('发现尚未登记的旧操作责任；共享范围暂停新写，请保留原账本核查。')

    def check(self, store):
        state = self.registry.snapshot()
        if state['phase'] != 'ready':
            raise RegistryError('REGISTRY_INITIALIZING')
        self._check_members()
        self._enroll(store)
        obligations = self.registry.open_obligations()
        if obligations:
            origin = obligations[0]
            raise GuardError('共享范围仍有待核实操作，自动和强制删除均已暂停。请打开原账本并核实：'
                             + origin['ledger_path'] + '；操作 ' + origin['receipt']['operation_id'])

    def claim(self, store, operation_id, tracking_id):
        self.check(store)
        return self.registry.claim(ledger_id(store), operation_id, target_hash(tracking_id),
                                   expected_checkpoint=ledger_anchor(store)['checkpoint'])

    def possible_send(self, store, receipt, *, write_requested, context_valid=lambda: True):
        receipt = validate_receipt(receipt)
        anchor = ledger_anchor(store)
        if anchor is None or ledger_id(store) != receipt.ledger_id:
            raise RegistryError('REGISTRY_LEDGER_MISMATCH')
        self.registry.check_ledger(receipt.ledger_id, store.folder,
                                   epoch=anchor['epoch'], checkpoint=anchor['checkpoint'])
        records = [a for a in action_history(store) if a.get('operation_id') == receipt.operation_id]
        if (not records or records[-1]['status'] != 'pending'
                or records[-1].get('responsibility') != receipt.to_dict()):
            raise RegistryError('REGISTRY_TRANSITION_REJECTED')
        # Only this synchronous transition may cross the export boundary.
        # A saved requested event from a failed or interrupted attempt cannot
        # later be used to resume a send, even if somebody re-exports the logs.
        write_requested()
        if not context_valid():
            return False
        records = [a for a in action_history(store) if a.get('operation_id') == receipt.operation_id]
        if (not records or records[-1]['status'] != 'requested'
                or records[-1].get('responsibility') != receipt.to_dict()
                or target_hash(records[-1]['tracking_id']) != receipt.target_hash):
            raise RegistryError('REGISTRY_RECEIPT_MISMATCH')
        self.registry.mark_possible_send(receipt)
        return True

    def settle(self, store, receipt):
        """A terminal local fact must precede release of this exact old slot."""
        receipt = validate_receipt(receipt)
        if ledger_id(store) != receipt.ledger_id:
            raise RegistryError('REGISTRY_LEDGER_MISMATCH')
        anchor = ledger_anchor(store)
        if anchor is None:
            raise RegistryError('REGISTRY_LEDGER_MISMATCH')
        self.registry.check_ledger(receipt.ledger_id, store.folder,
                                   epoch=anchor['epoch'], checkpoint=anchor['checkpoint'])
        records = [a for a in action_history(store) if a.get('operation_id') == receipt.operation_id]
        if not records or any(target_hash(a['tracking_id']) != receipt.target_hash for a in records):
            raise RegistryError('REGISTRY_RECEIPT_MISMATCH')
        terminal = records[-1]['status']
        if terminal == 'absent_verified':
            resolution = terminal
        elif terminal in {'cancelled', 'not_submitted'}:
            # Registry independently rejects this release if possible_send was
            # ever recorded, including a stop after permit handoff.
            resolution = 'not_submitted'
        else:
            return False
        store.export_logs()
        return self.registry.release(receipt, resolution=resolution)

    def reconcile(self, store):
        """Finish an interrupted terminal->release, never replay a request."""
        identity = ledger_id(store)
        for obligation in self.registry.open_obligations():
            receipt = validate_receipt(obligation['receipt'])
            if receipt.ledger_id == identity:
                self.settle(store, receipt)
        # A newly created successor may contain the final historical record of
        # a registered source. Adopt that reference before fees/bindings reload.
        if ledger_anchor(store) is None and action_history(store):
            self._check_members()
            if self._trusted_successor(store, self.registry.registered_ledgers()):
                self._enroll(store)

    def settle_operation(self, store, operation_id):
        receipt = self.registry.receipt_for(ledger_id(store), operation_id)
        return False if receipt is None else self.settle(store, receipt)
