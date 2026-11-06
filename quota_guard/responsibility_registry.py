"""One persistent responsibility gate for this Windows user's cooperating apps.

This registry stores neither official account IDs nor credentials. Runtime locks
remain separate. A receipt is evidence of an obligation, never a send permit.
The external anchor deliberately fails closed across interrupted two-file writes;
it is not a defence against an attacker replacing both files consistently.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
from uuid import UUID, uuid4

from .core import GuardError


SCHEMA = 3
DATABASE = 'responsibility.sqlite3'
_HEX32 = re.compile(r'[0-9a-f]{32}')
_HEX64 = re.compile(r'[0-9a-f]{64}')
_OPERATION = re.compile(r'[A-Za-z0-9_-]{1,200}')
_ERRORS = {
    'REGISTRY_MISSING': '共享责任登记尚未建立或文件缺失；保持只读，请恢复原登记或完成一次性范围建立。',
    'REGISTRY_INVALID': '共享责任登记损坏、格式不符或与外部锚点不一致；已暂停新写入。',
    'REGISTRY_BUSY': '共享责任登记正被其他操作占用；本次未取得新写入权。',
    'REGISTRY_INITIALIZING': '共享责任范围尚未完成建立或迁移；已暂停新写入。',
    'REGISTRY_OCCUPIED': '本机当前 Windows 用户范围内仍有未结责任；请先核实原操作。',
    'REGISTRY_LEDGER_MISMATCH': '账本身份、原登记路径或检查点不一致；不能接管或开启新写入。',
    'REGISTRY_RECEIPT_MISMATCH': '责任回执与原操作或代次不一致；未修改共享责任。',
    'REGISTRY_TRANSITION_REJECTED': '当前责任阶段不允许此变更；原责任保留。',
    'REGISTRY_UPGRADE_REQUIRED': '共享责任登记需要显式升级恢复协议；升级前保持只读，不会重建登记。',
    'REGISTRY_PLAN_REQUIRED': '成员迁移缺少完整持久计划；保持阻断，不能凭成员编号或旧步骤完成迁移。',
    'REGISTRY_PLAN_STALE': '成员迁移计划已过期或与当前登记不一致；未写入任何成员见证。',
    'REGISTRY_MEMBER_PRECHECK_FAILED': '建立成员计划前的本地预检失败；本次计划尚未保存，范围建立可能尚未完成。请修复本地账本问题并保留现有登记文件，保持停止新写入。',
    'REGISTRY_MEMBER_READ_FAILED': '成员恢复的本地预检失败；原迁移计划保留，尚未完成恢复。请修复本地账本读取问题后重试原计划，当前不允许新写入。',
    'REGISTRY_MEMBER_APPLY_FAILED': '成员恢复的本地写入失败；原迁移计划保留，部分成员可能已写入见证。请修复本地账本问题后重试原计划，当前不允许新写入。',
    'REGISTRY_MEMBER_VERIFY_FAILED': '成员恢复的本地复核失败；原迁移计划保留，尚未完成恢复。请修复本地账本问题后重试原计划，当前不允许新写入。',
    'REGISTRY_MEMBER_EXPORT_FAILED': '成员恢复见证已保存，但本地日志导出失败；原迁移计划保留，尚未完成恢复。请恢复日志写入条件后重试原计划，当前不允许新写入。',
}


class RegistryError(GuardError):
    def __init__(self, reason_code='REGISTRY_INVALID'):
        self.reason_code = reason_code if reason_code in _ERRORS else 'REGISTRY_INVALID'
        super().__init__(_ERRORS[self.reason_code])


def _membership_callback(callback, plan, reason_code):
    """Classify only local callback failures, never registry validation/commit.

    A callback gets a detached, validated plan. Known evidence/identity errors
    retain their precise code; incidental local failures get a fixed message
    without including exception text, paths, account data or credentials.
    """
    detached = validate_membership_plan(plan)
    try:
        return callback(detached)
    except RegistryError:
        raise
    except (OSError, sqlite3.Error, ValueError, KeyError, IndexError, TypeError, OverflowError):
        raise RegistryError(reason_code) from None


@dataclass(frozen=True)
class Receipt:
    epoch: str
    ledger_id: str
    operation_id: str
    target_hash: str
    generation: int

    def to_dict(self):
        return asdict(self)

    @property
    def checkpoint(self):
        return self.generation


def _matches(pattern, value):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def validate_receipt(value):
    if isinstance(value, Receipt):
        value = value.to_dict()
    if (not isinstance(value, dict)
            or set(value) != {'epoch', 'ledger_id', 'operation_id', 'target_hash', 'generation'}
            or not _matches(_HEX32, value['epoch']) or not _matches(_HEX32, value['ledger_id'])
            or not _matches(_OPERATION, value['operation_id'])
            or value['operation_id'].lower().startswith('sk-')
            or not _matches(_HEX64, value['target_hash'])
            or type(value['generation']) is not int or not 0 < value['generation'] < 2**63):
        raise RegistryError('REGISTRY_RECEIPT_MISMATCH')
    return Receipt(**value)


def validate_membership_plan(value):
    """Validate bounded structural facts without opening any member ledger.

    Local callbacks separately verify account/history digests and exact anchors.
    This function returns a detached JSON value so callers cannot mutate the
    persisted plan or the deterministic migration generations through aliases.
    """
    def invalid():
        raise RegistryError('REGISTRY_PLAN_STALE')
    def number(item):
        return type(item) is int and 0 <= item < 2**63
    def path(item):
        return isinstance(item, str) and 0 < len(item) <= 32768 and '\x00' not in item and Path(item).is_absolute()
    def anchor(item, epoch):
        return (isinstance(item, dict) and set(item) == {'epoch', 'checkpoint', 'roster_hash'}
                and item['epoch'] == epoch and number(item['checkpoint'])
                and _matches(_HEX64, item['roster_hash']))
    fields = {'schema', 'plan_id', 'epoch', 'mode', 'base_generation', 'base_revision',
              'before_members', 'members', 'operations', 'target_roster_hash'}
    if (not isinstance(value, dict) or set(value) != fields or type(value['schema']) is not int
            or value['schema'] != 1 or not _matches(_HEX32, value['plan_id'])
            or not _matches(_HEX32, value['epoch']) or value['mode'] not in ('initialize', 'enroll')
            or not number(value['base_generation']) or not number(value['base_revision'])
            or not isinstance(value['before_members'], list) or len(value['before_members']) > 1000
            or not isinstance(value['members'], list) or not 1 <= len(value['members']) <= 1000
            or not isinstance(value['operations'], list) or len(value['operations']) > 10000
            or not _matches(_HEX64, value['target_roster_hash'])):
        invalid()
    before = {}
    for member in value['before_members']:
        if (not isinstance(member, dict) or set(member) != {'ledger_id', 'path', 'checkpoint'}
                or not _matches(_HEX32, member['ledger_id']) or member['ledger_id'] in before
                or not path(member['path']) or not number(member['checkpoint'])
                or member['checkpoint'] > value['base_generation']):
            invalid()
        before[member['ledger_id']] = member
    members = {}
    paths = set()
    before_roster = sha256(json.dumps(sorted(before), separators=(',', ':')).encode('ascii')).hexdigest()
    for member in value['members']:
        if (not isinstance(member, dict) or set(member) != {'ledger_id', 'path', 'account_hash',
                'history_digest', 'before_anchor', 'target_anchor'}
                or not _matches(_HEX32, member['ledger_id']) or member['ledger_id'] in members
                or not path(member['path']) or not _matches(_HEX64, member['account_hash'])
                or not _matches(_HEX64, member['history_digest'])
                or not anchor(member['target_anchor'], value['epoch'])
                or member['target_anchor']['roster_hash'] != value['target_roster_hash']):
            invalid()
        canonical = os.path.normcase(os.path.normpath(member['path']))
        if canonical in paths:
            invalid()
        paths.add(canonical)
        original = before.get(member['ledger_id'])
        if original is None:
            if member['before_anchor'] is not None:
                invalid()
        elif (not anchor(member['before_anchor'], value['epoch'])
              or os.path.normcase(member['path']) != os.path.normcase(original['path'])
              or member['before_anchor'] != {'epoch': value['epoch'],
                    'checkpoint': original['checkpoint'], 'roster_hash': before_roster}):
            invalid()
        members[member['ledger_id']] = member
    if (list(before) != sorted(before) or list(members) != sorted(members)
            or not set(before).issubset(members)
            or value['target_roster_hash'] != sha256(json.dumps(sorted(members),
                separators=(',', ':')).encode('ascii')).hexdigest()):
        invalid()
    if value['mode'] == 'initialize':
        if before or value['base_generation'] != 0 or value['base_revision'] != 0:
            invalid()
    elif not before or set(before) == set(members) or value['operations']:
        invalid()
    checkpoints = {identity: before.get(identity, {}).get('checkpoint', 0) for identity in members}
    operation_ids, targets, order = set(), set(), []
    for index, operation in enumerate(value['operations'], start=1):
        if (not isinstance(operation, dict) or set(operation) != {'ledger_id', 'operation_id',
                'target_hash', 'generation', 'prior_checkpoint'}
                or not isinstance(operation['ledger_id'], str) or operation['ledger_id'] not in members):
            invalid()
        try:
            validate_receipt({'epoch': value['epoch'], **{key: operation[key] for key in
                ('ledger_id', 'operation_id', 'target_hash', 'generation')}})
        except RegistryError:
            invalid()
        identity, opid = operation['ledger_id'], operation['operation_id']
        target = (identity, operation['target_hash'])
        if (opid in operation_ids or target in targets or not number(operation['prior_checkpoint'])
                or operation['generation'] != value['base_generation'] + index
                or operation['prior_checkpoint'] != checkpoints[identity]):
            invalid()
        operation_ids.add(opid)
        targets.add(target)
        order.append((identity, opid))
        checkpoints[identity] = operation['generation']
    if order != sorted(order) or any(member['target_anchor']['checkpoint'] != checkpoints[identity]
                                    for identity, member in members.items()):
        invalid()
    try:
        serialized = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(',', ':'))
        if len(serialized) > 8 * 1024 * 1024:
            invalid()
        return json.loads(serialized)
    except (TypeError, ValueError, OverflowError):
        invalid()


def default_registry_directory():
    """Use the Windows Known Folder, not a ledger/profile or editable scope."""
    if os.name != 'nt':
        raise RegistryError('REGISTRY_MISSING')
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [('Data1', wintypes.DWORD), ('Data2', wintypes.WORD),
                    ('Data3', wintypes.WORD), ('Data4', ctypes.c_ubyte * 8)]

    guid = GUID.from_buffer_copy(UUID('F1B32785-6FBA-4FCF-9D55-7B8E7F157091').bytes_le)
    shell, ole = ctypes.WinDLL('shell32'), ctypes.WinDLL('ole32')
    shell.SHGetKnownFolderPath.argtypes = [ctypes.POINTER(GUID), wintypes.DWORD,
                                          wintypes.HANDLE, ctypes.POINTER(ctypes.c_wchar_p)]
    shell.SHGetKnownFolderPath.restype = ctypes.c_long
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    result = ctypes.c_wchar_p()
    if shell.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(result)) != 0:
        raise RegistryError('REGISTRY_MISSING')
    try:
        return Path(result.value) / 'DeepSeekQuotaGuardAI' / 'responsibility'
    finally:
        ole.CoTaskMemFree(ctypes.cast(result, ctypes.c_void_p))


def _plain(path, *, directory=False):
    try:
        info = Path(path).lstat()
    except OSError:
        raise RegistryError('REGISTRY_MISSING') from None
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (not expected(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or getattr(info, 'st_file_attributes', 0) & 0x400
            or not directory and info.st_nlink != 1):
        raise RegistryError()
    return info


def _path(value):
    try:
        path = Path(value).absolute()
        _plain(path, directory=True)
        return str(path.resolve(strict=True))
    except (TypeError, ValueError, OSError):
        raise RegistryError('REGISTRY_LEDGER_MISMATCH') from None


class ResponsibilityRegistry:
    """No constructor side effects. ``directory`` is an offline-test injection.

    Production callers use the single default path and never expose directory or
    scope selection. All methods open existing files; only explicit initialize
    can create them. A missing database cannot silently become an empty scope.
    """
    def __init__(self, directory=None, *, timeout_seconds=.2):
        if (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float))
                or not math.isfinite(timeout_seconds) or not 0 <= timeout_seconds <= 2):
            raise ValueError('Registry busy timeout must be between zero and two seconds.')
        self.directory = Path(default_registry_directory() if directory is None else directory).absolute()
        self.db_path = self.directory / DATABASE
        self.anchor_path = self.directory.parent / (self.directory.name + '.anchor.json')
        self.timeout_seconds = float(timeout_seconds)

    def _read_anchor(self):
        info = _plain(self.anchor_path)
        if info.st_size > 2048:
            raise RegistryError()
        try:
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError('duplicate field')
                    result[key] = value
                return result
            value = json.loads(self.anchor_path.read_text(encoding='utf-8'), object_pairs_hook=unique)
        except (OSError, ValueError, UnicodeError):
            raise RegistryError() from None
        if (not isinstance(value, dict) or set(value) != {'schema', 'epoch', 'revision'}
                or type(value['schema']) is not int or value['schema'] not in (1, 2, SCHEMA)
                or not _matches(_HEX32, value['epoch'])
                or type(value['revision']) is not int or not 0 <= value['revision'] < 2**63):
            raise RegistryError()
        return value

    def _publish_anchor(self, value, *, previous):
        if self._read_anchor() != previous:
            raise RegistryError()
        staging = self.anchor_path.with_name(self.anchor_path.name + '.' + uuid4().hex + '.new')
        try:
            with staging.open('x', encoding='utf-8') as stream:
                json.dump(value, stream, ensure_ascii=True, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            if self._read_anchor() != previous:
                raise RegistryError()
            os.replace(staging, self.anchor_path)
        finally:
            if staging.exists():
                staging.unlink()

    def initialize(self, *, expected_ledger_ids=None, membership_plan=None, before_plan=None):
        """Begin an explicit one-time declared-ledger migration, never unlock it."""
        plan = None if membership_plan is None else validate_membership_plan(membership_plan)
        if plan is not None:
            if plan['mode'] != 'initialize':
                raise RegistryError('REGISTRY_PLAN_STALE')
            planned_ids = [member['ledger_id'] for member in plan['members']]
            if expected_ledger_ids is None:
                expected_ledger_ids = planned_ids
            elif set(expected_ledger_ids) != set(planned_ids):
                raise RegistryError('REGISTRY_PLAN_STALE')
        if (not isinstance(expected_ledger_ids, (list, tuple, set, frozenset))
                or not expected_ledger_ids or len(expected_ledger_ids) > 1000
                or any(not _matches(_HEX32, item) for item in expected_ledger_ids)
                or len(set(expected_ledger_ids)) != len(expected_ledger_ids)):
            raise RegistryError('REGISTRY_LEDGER_MISMATCH')
        if self.anchor_path.exists() or self.anchor_path.is_symlink():
            raise RegistryError()
        if self.directory.exists() and any(self.directory.iterdir()):
            raise RegistryError()
        if plan is not None:
            for member in plan['members']:
                if os.path.normcase(_path(member['path'])) != os.path.normcase(member['path']):
                    raise RegistryError('REGISTRY_LEDGER_MISMATCH')
        epoch = uuid4().hex if plan is None else plan['epoch']
        anchor = {'schema': SCHEMA, 'epoch': epoch, 'revision': 0}
        try:
            self.directory.parent.mkdir(parents=True, exist_ok=True)
            _plain(self.directory.parent, directory=True)
            # Create the external anchor first. Any interrupted creation leaves
            # a detectable incomplete scope; initialize never resets it.
            with self.anchor_path.open('x', encoding='utf-8') as stream:
                json.dump(anchor, stream)
                stream.flush()
                os.fsync(stream.fileno())
            self.directory.mkdir(exist_ok=True)
            _plain(self.directory, directory=True)
            with self.db_path.open('xb'):
                pass
            db = sqlite3.connect(self.db_path, timeout=self.timeout_seconds, isolation_level=None)
            try:
                db.execute('PRAGMA journal_mode=DELETE')
                db.execute('PRAGMA synchronous=FULL')
                db.execute('BEGIN EXCLUSIVE')
                db.execute('CREATE TABLE metadata (singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema INTEGER NOT NULL, epoch TEXT NOT NULL, generation INTEGER NOT NULL CHECK(generation>=0), revision INTEGER NOT NULL CHECK(revision>=0), phase TEXT NOT NULL CHECK(phase IN (\'initializing\',\'ready\')), expected_ledgers TEXT NOT NULL)')
                db.execute('CREATE TABLE ledgers (ledger_id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, checkpoint INTEGER NOT NULL CHECK(checkpoint>=0))')
                db.execute('CREATE TABLE obligations (generation INTEGER PRIMARY KEY, ledger_id TEXT NOT NULL REFERENCES ledgers(ledger_id), operation_id TEXT NOT NULL UNIQUE, target_hash TEXT NOT NULL, phase TEXT NOT NULL CHECK(phase IN (\'declared\',\'possible_send\',\'settled\')), origin TEXT NOT NULL CHECK(origin IN (\'claim\',\'migration\')), resolution TEXT, UNIQUE(ledger_id,operation_id))')
                db.execute('CREATE TABLE active_slot (singleton INTEGER PRIMARY KEY CHECK(singleton=1), generation INTEGER NOT NULL UNIQUE REFERENCES obligations(generation))')
                self._create_recovery_table(db)
                self._create_membership_table(db)
                db.execute('INSERT INTO metadata VALUES(1,?,?,?,?,?,?)',
                           (SCHEMA, epoch, 0, 0, 'initializing', json.dumps(sorted(expected_ledger_ids))))
                if plan is not None:
                    if before_plan is not None:
                        _membership_callback(before_plan, plan, 'REGISTRY_MEMBER_PRECHECK_FAILED')
                    db.execute("INSERT INTO membership_plan VALUES(1,?,'pending',?)",
                               (plan['plan_id'], json.dumps(plan)))
                db.execute(f'PRAGMA user_version={SCHEMA}')
                db.execute('COMMIT')
            finally:
                db.close()
        except (OSError, sqlite3.Error):
            raise RegistryError() from None
        return self.snapshot()

    @staticmethod
    def _create_recovery_table(db):
        db.execute('CREATE TABLE obligation_recovery (generation INTEGER PRIMARY KEY REFERENCES obligations(generation), prior_checkpoint INTEGER, recovery_id TEXT UNIQUE)')

    @staticmethod
    def _create_membership_table(db):
        db.execute("CREATE TABLE membership_plan (singleton INTEGER PRIMARY KEY CHECK(singleton=1), plan_id TEXT NOT NULL UNIQUE, state TEXT NOT NULL CHECK(state IN ('pending','complete')), payload TEXT NOT NULL)")

    @staticmethod
    def _membership(db):
        rows = db.execute('SELECT * FROM membership_plan').fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise RegistryError()
        row = rows[0]
        if (row['singleton'] != 1 or row['state'] not in ('pending', 'complete')
                or not isinstance(row['payload'], str) or len(row['payload']) > 8 * 1024 * 1024):
            raise RegistryError()
        payload = validate_membership_plan(json.loads(row['payload']))
        if row['plan_id'] != payload['plan_id']:
            raise RegistryError()
        return {'plan_id': row['plan_id'], 'state': row['state'], 'payload': payload}

    def _validate_membership_state(self, db, meta):
        fields = [row['name'] for row in db.execute('PRAGMA table_info(membership_plan)')]
        if fields != ['singleton', 'plan_id', 'state', 'payload']:
            raise RegistryError()
        current = self._membership(db)
        if current is None:
            return
        plan = current['payload']
        actual = [dict(row) for row in db.execute('SELECT * FROM ledgers ORDER BY ledger_id')]
        if (plan['epoch'] != meta['epoch']
                or json.loads(meta['expected_ledgers']) != [m['ledger_id'] for m in plan['members']]):
            raise RegistryError()
        if current['state'] == 'pending':
            if (meta['phase'] != 'initializing' or meta['generation'] != plan['base_generation']
                    or meta['revision'] != plan['base_revision'] + (plan['mode'] == 'enroll')
                    or actual != plan['before_members']):
                raise RegistryError()
        else:
            target = {m['ledger_id']: m for m in plan['members']}
            if (meta['phase'] != 'ready' or len(actual) != len(target)
                    or meta['generation'] < plan['base_generation'] + len(plan['operations'])
                    or any(row['ledger_id'] not in target
                        or row['path'] != target[row['ledger_id']]['path']
                        or row['checkpoint'] < target[row['ledger_id']]['target_anchor']['checkpoint']
                        for row in actual)):
                raise RegistryError()
            for operation in plan['operations']:
                saved = db.execute('SELECT * FROM obligations WHERE generation=?',
                                   (operation['generation'],)).fetchone()
                if (saved is None or saved['origin'] != 'migration'
                        or any(saved[key] != operation[key] for key in
                               ('ledger_id', 'operation_id', 'target_hash'))):
                    raise RegistryError()

    def _validated(self, db, *, allow_v1=False):
        anchor = self._read_anchor()
        schema = db.execute('PRAGMA user_version').fetchone()[0]
        if schema not in (1, 2, SCHEMA) or anchor['schema'] != schema:
            raise RegistryError()
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        expected_tables = {'metadata', 'ledgers', 'obligations', 'active_slot'}
        if schema >= 2:
            expected_tables.add('obligation_recovery')
        if schema >= 3:
            expected_tables.add('membership_plan')
        if tables != expected_tables:
            raise RegistryError()
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RegistryError()
        if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RegistryError()
        rows = db.execute('SELECT * FROM metadata').fetchall()
        if len(rows) != 1:
            raise RegistryError()
        meta = dict(rows[0])
        if (meta['singleton'] != 1 or meta['schema'] != schema
                or meta['epoch'] != anchor['epoch'] or meta['revision'] != anchor['revision']
                or type(meta['generation']) is not int or not 0 <= meta['generation'] < 2**63
                or meta['phase'] not in ('initializing', 'ready')):
            raise RegistryError()
        expected = json.loads(meta['expected_ledgers'])
        if (not isinstance(expected, list) or not expected
                or any(not _matches(_HEX32, item) for item in expected)
                or len(set(expected)) != len(expected)):
            raise RegistryError()
        slots = db.execute('SELECT a.singleton,o.phase,o.origin FROM active_slot a JOIN obligations o USING(generation)').fetchall()
        if any(row['singleton'] != 1 or row['phase'] == 'settled' or row['origin'] != 'claim' for row in slots):
            raise RegistryError()
        open_claims = db.execute("SELECT generation FROM obligations WHERE origin='claim' AND phase!='settled'").fetchall()
        if len(open_claims) != len(slots) or len(slots) > 1:
            raise RegistryError()
        if db.execute('SELECT coalesce(max(generation),0) FROM obligations').fetchone()[0] != meta['generation']:
            raise RegistryError()
        for row in db.execute('SELECT * FROM ledgers'):
            if (not _matches(_HEX32, row['ledger_id']) or not isinstance(row['path'], str)
                    or not Path(row['path']).is_absolute() or type(row['checkpoint']) is not int
                    or not 0 <= row['checkpoint'] <= meta['generation']):
                raise RegistryError()
        registered_ids = {row[0] for row in db.execute('SELECT ledger_id FROM ledgers')}
        if (not registered_ids.issubset(expected)
                or meta['phase'] == 'ready' and registered_ids != set(expected)):
            raise RegistryError()
        for row in db.execute('SELECT * FROM obligations'):
            validate_receipt({'epoch': meta['epoch'], **{key: row[key] for key in
                ('ledger_id', 'operation_id', 'target_hash', 'generation')}})
            if (row['phase'] not in ('declared', 'possible_send', 'settled')
                    or row['origin'] not in ('claim', 'migration')
                    or (row['phase'] == 'settled' and row['resolution'] not in ('absent_verified', 'not_submitted'))
                    or (row['phase'] != 'settled' and row['resolution'] is not None)):
                raise RegistryError()
        if schema >= 2:
            fields = [row['name'] for row in db.execute('PRAGMA table_info(obligation_recovery)')]
            if fields != ['generation', 'prior_checkpoint', 'recovery_id']:
                raise RegistryError()
            contexts = db.execute('SELECT r.*,o.ledger_id,o.phase,o.origin,o.resolution FROM obligation_recovery r JOIN obligations o USING(generation)').fetchall()
            count = db.execute('SELECT count(*) FROM obligations').fetchone()[0]
            if len(contexts) != count or db.execute('SELECT count(*) FROM obligation_recovery').fetchone()[0] != count:
                raise RegistryError()
            for row in contexts:
                prior = row['prior_checkpoint']
                if prior is not None:
                    expected_prior = db.execute('SELECT coalesce(max(generation),0) FROM obligations WHERE ledger_id=? AND generation<?',
                                                (row['ledger_id'], row['generation'])).fetchone()[0]
                    if type(prior) is not int or prior != expected_prior:
                        raise RegistryError()
                if row['recovery_id'] is not None and (
                        not _matches(_HEX32, row['recovery_id']) or row['origin'] != 'claim'
                        or row['phase'] not in ('declared', 'settled')
                        or row['phase'] == 'settled' and row['resolution'] != 'not_submitted'):
                    raise RegistryError()
        if schema >= 3:
            self._validate_membership_state(db, meta)
        if schema < SCHEMA and not allow_v1:
            raise RegistryError('REGISTRY_UPGRADE_REQUIRED')
        return meta, anchor

    @contextmanager
    def _transaction(self, *, write=False, allow_v1=False):
        _plain(self.directory, directory=True)
        _plain(self.db_path)
        db = None
        try:
            db = sqlite3.connect(self.db_path.as_uri() + '?mode=rw', uri=True,
                                 timeout=self.timeout_seconds, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA synchronous=FULL')
            # The SQLite database and external anchor form one checked pair.
            # Serialize even the short read methods before reading either one:
            # (1) a SHARED reader must block us before anchor publication, not
            # at COMMIT after publication; (2) an anchor read must not straddle
            # another writer's commit. No browser/network work occurs inside.
            db.execute('BEGIN EXCLUSIVE')
            meta, anchor = self._validated(db, allow_v1=allow_v1)
            yield db, meta
            if write and db.total_changes:
                revision = meta['revision'] + 1
                db.execute('UPDATE metadata SET revision=? WHERE singleton=1', (revision,))
                # Anchor first means commit failure leaves mismatch, never a
                # falsely idle old database. No automatic rollback/repair.
                self._publish_anchor({'schema': SCHEMA, 'epoch': meta['epoch'], 'revision': revision},
                                     previous=anchor)
            db.execute('COMMIT')
        except RegistryError:
            raise
        except sqlite3.OperationalError as exc:
            code = getattr(exc, 'sqlite_errorcode', None)
            raise RegistryError('REGISTRY_BUSY' if code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
                                else 'REGISTRY_INVALID') from None
        except (sqlite3.Error, OSError, ValueError, KeyError, IndexError, TypeError, OverflowError):
            raise RegistryError() from None
        finally:
            if db is not None:
                if db.in_transaction:
                    db.rollback()
                db.close()

    def upgrade_schema(self):
        """Explicitly upgrade a healthy pair without inventing old evidence.

        Existing receipts and epochs remain unchanged. Old obligations have no
        recorded claim-time checkpoint, so their prior_checkpoint stays NULL.
        A torn anchor/database upgrade remains blocked, just like any write.
        """
        with self._transaction(write=True, allow_v1=True) as (db, meta):
            if meta['schema'] == SCHEMA:
                return False
            if meta['schema'] == 1:
                self._create_recovery_table(db)
                db.execute('INSERT INTO obligation_recovery SELECT generation,NULL,NULL FROM obligations')
            self._create_membership_table(db)
            db.execute('UPDATE metadata SET schema=? WHERE singleton=1', (SCHEMA,))
            db.execute(f'PRAGMA user_version={SCHEMA}')
            return True

    def upgrade_v1(self):
        """Compatibility name for explicit upgrade of schema 1 or 2."""
        return self.upgrade_schema()

    def _ledger(self, db, ledger_id, checkpoint=None):
        if not _matches(_HEX32, ledger_id):
            raise RegistryError('REGISTRY_LEDGER_MISMATCH')
        row = db.execute('SELECT * FROM ledgers WHERE ledger_id=?', (ledger_id,)).fetchone()
        if (row is None or checkpoint is not None and
                (type(checkpoint) is not int or row['checkpoint'] != checkpoint)):
            raise RegistryError('REGISTRY_LEDGER_MISMATCH')
        return dict(row)

    def register_ledger(self, ledger_id, path, *, checkpoint=0):
        raise RegistryError('REGISTRY_PLAN_REQUIRED')

    def begin_enrollment(self, ledger_id):
        raise RegistryError('REGISTRY_PLAN_REQUIRED')

    def membership_plan(self, plan_id=None):
        with self._transaction() as (db, _):
            current = self._membership(db)
            if plan_id is not None and (current is None or current['plan_id'] != plan_id):
                raise RegistryError('REGISTRY_PLAN_STALE')
            return current

    def begin_membership_plan(self, plan, *, before_plan=None):
        plan = validate_membership_plan(plan)
        if plan['mode'] != 'enroll':
            raise RegistryError('REGISTRY_PLAN_STALE')
        with self._transaction(write=True) as (db, meta):
            if meta['phase'] != 'ready':
                raise RegistryError('REGISTRY_INITIALIZING')
            if db.execute("SELECT 1 FROM obligations WHERE phase!='settled'").fetchone():
                raise RegistryError('REGISTRY_OCCUPIED')
            current = self._membership(db)
            actual = [dict(row) for row in db.execute('SELECT * FROM ledgers ORDER BY ledger_id')]
            if (plan['epoch'] != meta['epoch'] or plan['base_generation'] != meta['generation']
                    or plan['base_revision'] != meta['revision'] or actual != plan['before_members']
                    or current is not None and (current['state'] != 'complete'
                                               or current['plan_id'] == plan['plan_id'])):
                raise RegistryError('REGISTRY_PLAN_STALE')
            for member in plan['members']:
                if os.path.normcase(_path(member['path'])) != os.path.normcase(member['path']):
                    raise RegistryError('REGISTRY_LEDGER_MISMATCH')
            if before_plan is not None:
                _membership_callback(before_plan, plan, 'REGISTRY_MEMBER_PRECHECK_FAILED')
            db.execute("INSERT OR REPLACE INTO membership_plan VALUES(1,?,'pending',?)",
                       (plan['plan_id'], json.dumps(plan)))
            db.execute("UPDATE metadata SET phase='initializing',expected_ledgers=? WHERE singleton=1",
                       (json.dumps([m['ledger_id'] for m in plan['members']]),))
            return {'plan_id': plan['plan_id'], 'state': 'pending', 'payload': plan}

    def execute_membership_plan(self, plan_id, *, preflight, apply, verify):
        if not _matches(_HEX32, plan_id) or not all(callable(c) for c in (preflight, apply, verify)):
            raise RegistryError('REGISTRY_PLAN_STALE')
        with self._transaction(write=True) as (db, meta):
            current = self._membership(db)
            if current is None:
                raise RegistryError('REGISTRY_PLAN_REQUIRED')
            if current['plan_id'] != plan_id:
                raise RegistryError('REGISTRY_PLAN_STALE')
            if current['state'] == 'complete':
                return False
            plan = current['payload']
            # The plan was committed before this transaction. A failure rolls
            # back shared rows but not local target witnesses: a retry uses
            # this same plan and the same deterministic receipt generations.
            _membership_callback(preflight, plan, 'REGISTRY_MEMBER_READ_FAILED')
            before_ids = {m['ledger_id'] for m in plan['before_members']}
            for member in plan['members']:
                if member['ledger_id'] not in before_ids:
                    db.execute('INSERT INTO ledgers VALUES(?,?,0)',
                               (member['ledger_id'], member['path']))
            for operation in plan['operations']:
                db.execute("INSERT INTO obligations VALUES(?,?,?,?,'possible_send','migration',NULL)",
                           (operation['generation'], operation['ledger_id'],
                            operation['operation_id'], operation['target_hash']))
                db.execute('INSERT INTO obligation_recovery VALUES(?,?,NULL)',
                           (operation['generation'], operation['prior_checkpoint']))
            for member in plan['members']:
                db.execute('UPDATE ledgers SET checkpoint=? WHERE ledger_id=?',
                           (member['target_anchor']['checkpoint'], member['ledger_id']))
            db.execute('UPDATE metadata SET generation=? WHERE singleton=1',
                       (plan['base_generation'] + len(plan['operations']),))
            _membership_callback(apply, plan, 'REGISTRY_MEMBER_APPLY_FAILED')
            _membership_callback(verify, plan, 'REGISTRY_MEMBER_VERIFY_FAILED')
            db.execute("UPDATE membership_plan SET state='complete' WHERE singleton=1")
            db.execute("UPDATE metadata SET phase='ready' WHERE singleton=1")
            return True

    def check_ledger(self, ledger_id, path, epoch, checkpoint):
        if type(checkpoint) is not int or checkpoint < 0:
            raise RegistryError('REGISTRY_LEDGER_MISMATCH')
        path = _path(path)
        with self._transaction() as (db, meta):
            row = self._ledger(db, ledger_id, checkpoint)
            if epoch != meta['epoch'] or os.path.normcase(row['path']) != os.path.normcase(path):
                raise RegistryError('REGISTRY_LEDGER_MISMATCH')
            return {**row, 'epoch': meta['epoch']}

    def finish_initialization(self):
        raise RegistryError('REGISTRY_PLAN_REQUIRED')

    def _new_obligation(self, ledger_id, operation_id, target_hash, expected_checkpoint, *, migration,
                        allow_declared_recovery=True):
        if type(expected_checkpoint) is not int or expected_checkpoint < 0:
            raise RegistryError('REGISTRY_LEDGER_MISMATCH')
        with self._transaction(write=True) as (db, meta):
            if not migration and meta['phase'] != 'ready':
                raise RegistryError('REGISTRY_INITIALIZING')
            validate_receipt({'epoch': meta['epoch'], 'ledger_id': ledger_id,
                              'operation_id': operation_id, 'target_hash': target_hash, 'generation': 1})
            self._ledger(db, ledger_id, expected_checkpoint)
            if migration:
                if meta['phase'] != 'initializing':
                    raise RegistryError('REGISTRY_TRANSITION_REJECTED')
            elif meta['phase'] != 'ready':
                raise RegistryError('REGISTRY_INITIALIZING')
            previous = db.execute('SELECT * FROM obligations WHERE operation_id=?', (operation_id,)).fetchone()
            if previous is not None:
                # Claims are never retried as new grants. Migration alone may
                # return its identical original receipt after interrupted UI.
                if (migration and previous['origin'] == 'migration' and previous['phase'] != 'settled'
                        and previous['ledger_id'] == ledger_id and previous['target_hash'] == target_hash):
                    return Receipt(meta['epoch'], ledger_id, operation_id, target_hash, previous['generation'])
                raise RegistryError('REGISTRY_RECEIPT_MISMATCH')
            if not migration and db.execute("SELECT 1 FROM obligations WHERE phase!='settled' LIMIT 1").fetchone():
                raise RegistryError('REGISTRY_OCCUPIED')
            generation = meta['generation'] + 1
            phase, origin = ('possible_send', 'migration') if migration else ('declared', 'claim')
            db.execute('INSERT INTO obligations VALUES(?,?,?,?,?,?,NULL)',
                       (generation, ledger_id, operation_id, target_hash, phase, origin))
            db.execute('INSERT INTO obligation_recovery VALUES(?,?,NULL)',
                       (generation, expected_checkpoint if allow_declared_recovery else None))
            if not migration:
                db.execute('INSERT INTO active_slot VALUES(1,?)', (generation,))
            db.execute('UPDATE metadata SET generation=? WHERE singleton=1', (generation,))
            db.execute('UPDATE ledgers SET checkpoint=? WHERE ledger_id=?', (generation, ledger_id))
            return Receipt(meta['epoch'], ledger_id, operation_id, target_hash, generation)

    def claim(self, ledger_id, operation_id, target_hash, *, expected_checkpoint,
              allow_declared_recovery=True):
        if type(allow_declared_recovery) is not bool:
            raise RegistryError('REGISTRY_TRANSITION_REJECTED')
        # A legacy manual-page handoff may allow the user to delete outside our
        # permit path. Its unknown predecessor context deliberately disables
        # the never-authorized recovery shortcut from the moment of claim.
        return self._new_obligation(ledger_id, operation_id, target_hash, expected_checkpoint,
                                    migration=False, allow_declared_recovery=allow_declared_recovery)

    def register_unresolved(self, ledger_id, operation_id, target_hash, *, expected_checkpoint):
        raise RegistryError('REGISTRY_PLAN_REQUIRED')

    def _matching(self, db, meta, receipt):
        receipt = validate_receipt(receipt)
        row = db.execute('SELECT * FROM obligations WHERE generation=?', (receipt.generation,)).fetchone()
        if (receipt.epoch != meta['epoch'] or row is None
                or any(row[key] != getattr(receipt, key) for key in ('ledger_id', 'operation_id', 'target_hash'))):
            raise RegistryError('REGISTRY_RECEIPT_MISMATCH')
        return receipt, row

    def mark_possible_send(self, receipt, *, before_mark=None):
        with self._transaction(write=True) as (db, meta):
            receipt, row = self._matching(db, meta, receipt)
            recovery = self._recovery(db, receipt)
            if recovery['recovery_id'] is not None or row['phase'] == 'settled' or row['origin'] != 'claim':
                raise RegistryError('REGISTRY_TRANSITION_REJECTED')
            if row['phase'] == 'declared':
                # The production gate durably witnesses the send boundary in
                # the original ledger here. Holding this same registry lock
                # prevents a recovery precheck from racing that local write.
                if before_mark is not None:
                    before_mark()
                db.execute("UPDATE obligations SET phase='possible_send' WHERE generation=?", (receipt.generation,))
            return receipt

    def release(self, receipt, *, resolution='absent_verified'):
        if resolution not in ('absent_verified', 'not_submitted'):
            raise RegistryError('REGISTRY_TRANSITION_REJECTED')
        with self._transaction(write=True) as (db, meta):
            receipt, row = self._matching(db, meta, receipt)
            if self._recovery(db, receipt)['recovery_id'] is not None:
                raise RegistryError('REGISTRY_TRANSITION_REJECTED')
            if row['phase'] == 'settled':
                return False
            if resolution == 'not_submitted' and row['phase'] != 'declared':
                raise RegistryError('REGISTRY_TRANSITION_REJECTED')
            db.execute("UPDATE obligations SET phase='settled',resolution=? WHERE generation=?",
                       (resolution, receipt.generation))
            db.execute('DELETE FROM active_slot WHERE generation=?', (receipt.generation,))
            return True

    @staticmethod
    def _recovery(db, receipt):
        row = db.execute('SELECT prior_checkpoint,recovery_id FROM obligation_recovery WHERE generation=?',
                         (receipt.generation,)).fetchone()
        if row is None:
            raise RegistryError()
        return {'receipt': receipt.to_dict(), 'prior_checkpoint': row['prior_checkpoint'],
                'recovery_id': row['recovery_id']}

    def recovery_for(self, receipt):
        """Read original recovery context; this never freezes or authorizes."""
        with self._transaction() as (db, meta):
            receipt, _ = self._matching(db, meta, receipt)
            return self._recovery(db, receipt)

    def freeze_declared(self, receipt, *, before_freeze=None):
        """Durably deny promotion while retaining the original occupied slot."""
        with self._transaction(write=True) as (db, meta):
            receipt, row = self._matching(db, meta, receipt)
            slot = db.execute('SELECT generation FROM active_slot WHERE singleton=1').fetchone()
            if (row['origin'] != 'claim' or row['phase'] != 'declared'
                    or slot is None or slot['generation'] != receipt.generation):
                raise RegistryError('REGISTRY_TRANSITION_REJECTED')
            recovery = self._recovery(db, receipt)
            recovery_id = recovery['recovery_id'] or uuid4().hex
            if before_freeze is not None:
                # Give the callback a detached context. It may inspect/write
                # local evidence, but cannot substitute our original fence.
                before_freeze({'receipt': receipt.to_dict(),
                               'prior_checkpoint': recovery['prior_checkpoint'],
                               'recovery_id': recovery_id})
            if recovery['recovery_id'] is None:
                db.execute('UPDATE obligation_recovery SET recovery_id=? WHERE generation=?',
                           (recovery_id, receipt.generation))
            return self._recovery(db, receipt)

    def finish_declared_recovery(self, receipt, recovery_id):
        """Release only this fence after the caller persists/exports local audit.

        This method is not a generic release: a possible_send obligation never
        receives a recovery fence, and ordinary release cannot bypass a fence.
        """
        if not _matches(_HEX32, recovery_id):
            raise RegistryError('REGISTRY_RECEIPT_MISMATCH')
        with self._transaction(write=True) as (db, meta):
            receipt, row = self._matching(db, meta, receipt)
            recovery = self._recovery(db, receipt)
            if recovery['recovery_id'] != recovery_id or row['origin'] != 'claim':
                raise RegistryError('REGISTRY_RECEIPT_MISMATCH')
            if row['phase'] == 'settled' and row['resolution'] == 'not_submitted':
                return False
            slot = db.execute('SELECT generation FROM active_slot WHERE singleton=1').fetchone()
            if row['phase'] != 'declared' or slot is None or slot['generation'] != receipt.generation:
                raise RegistryError('REGISTRY_TRANSITION_REJECTED')
            db.execute("UPDATE obligations SET phase='settled',resolution='not_submitted' WHERE generation=?",
                       (receipt.generation,))
            db.execute('DELETE FROM active_slot WHERE generation=?', (receipt.generation,))
            return True

    def receipt_for(self, ledger_id, operation_id):
        with self._transaction() as (db, meta):
            self._ledger(db, ledger_id)
            row = db.execute('SELECT * FROM obligations WHERE ledger_id=? AND operation_id=?',
                             (ledger_id, operation_id)).fetchone()
            return None if row is None else Receipt(meta['epoch'], ledger_id, operation_id,
                                                   row['target_hash'], row['generation'])

    def _obligations(self, db, meta):
        return [{'receipt': Receipt(meta['epoch'], row['ledger_id'], row['operation_id'], row['target_hash'],
                                    row['generation']).to_dict(),
                 'phase': row['phase'], 'origin': row['origin'], 'ledger_path': row['path'],
                 'recovery': self._recovery(db, Receipt(meta['epoch'], row['ledger_id'], row['operation_id'],
                                                       row['target_hash'], row['generation']))}
                for row in db.execute("SELECT o.*,l.path FROM obligations o JOIN ledgers l USING(ledger_id) WHERE o.phase!='settled' ORDER BY generation")]

    def open_obligations(self):
        with self._transaction() as (db, meta):
            return self._obligations(db, meta)

    def registered_ledgers(self):
        with self._transaction() as (db, meta):
            return [{**dict(row), 'epoch': meta['epoch']} for row in db.execute('SELECT * FROM ledgers ORDER BY ledger_id')]

    def snapshot(self):
        with self._transaction() as (db, meta):
            return {'schema': SCHEMA, 'epoch': meta['epoch'], 'phase': meta['phase'],
                    'generation': meta['generation'], 'revision': meta['revision'],
                    'ledgers': [{**dict(row), 'epoch': meta['epoch']} for row in db.execute('SELECT * FROM ledgers ORDER BY ledger_id')],
                    'open_obligations': self._obligations(db, meta),
                    'membership_plan': self._membership(db)}
