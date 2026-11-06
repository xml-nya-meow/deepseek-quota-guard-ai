"""One persistent responsibility gate for this Windows user's cooperating apps.

This registry stores neither official account IDs nor credentials. Runtime locks
remain separate. A receipt is evidence of an obligation, never a send permit.
The external anchor deliberately fails closed across interrupted two-file writes;
it is not a defence against an attacker replacing both files consistently.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
from uuid import UUID, uuid4

from .core import GuardError


SCHEMA = 1
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
}


class RegistryError(GuardError):
    def __init__(self, reason_code='REGISTRY_INVALID'):
        self.reason_code = reason_code if reason_code in _ERRORS else 'REGISTRY_INVALID'
        super().__init__(_ERRORS[self.reason_code])


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
                or type(value['schema']) is not int or value['schema'] != SCHEMA
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

    def initialize(self, *, expected_ledger_ids):
        """Begin an explicit one-time declared-ledger migration, never unlock it."""
        if (not isinstance(expected_ledger_ids, (list, tuple, set, frozenset))
                or not expected_ledger_ids or len(expected_ledger_ids) > 1000
                or any(not _matches(_HEX32, item) for item in expected_ledger_ids)
                or len(set(expected_ledger_ids)) != len(expected_ledger_ids)):
            raise RegistryError('REGISTRY_LEDGER_MISMATCH')
        if self.anchor_path.exists() or self.anchor_path.is_symlink():
            raise RegistryError()
        if self.directory.exists() and any(self.directory.iterdir()):
            raise RegistryError()
        epoch = uuid4().hex
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
                db.execute('INSERT INTO metadata VALUES(1,?,?,?,?,?,?)',
                           (SCHEMA, epoch, 0, 0, 'initializing', json.dumps(sorted(expected_ledger_ids))))
                db.execute(f'PRAGMA user_version={SCHEMA}')
                db.execute('COMMIT')
            finally:
                db.close()
        except (OSError, sqlite3.Error):
            raise RegistryError() from None
        return self.snapshot()

    def _validated(self, db):
        anchor = self._read_anchor()
        if db.execute('PRAGMA user_version').fetchone()[0] != SCHEMA:
            raise RegistryError()
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables != {'metadata', 'ledgers', 'obligations', 'active_slot'}:
            raise RegistryError()
        if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise RegistryError()
        if db.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise RegistryError()
        rows = db.execute('SELECT * FROM metadata').fetchall()
        if len(rows) != 1:
            raise RegistryError()
        meta = dict(rows[0])
        if (meta['singleton'] != 1 or meta['schema'] != SCHEMA
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
        return meta, anchor

    @contextmanager
    def _transaction(self, *, write=False):
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
            meta, anchor = self._validated(db)
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

    def _ledger(self, db, ledger_id, checkpoint=None):
        if not _matches(_HEX32, ledger_id):
            raise RegistryError('REGISTRY_LEDGER_MISMATCH')
        row = db.execute('SELECT * FROM ledgers WHERE ledger_id=?', (ledger_id,)).fetchone()
        if (row is None or checkpoint is not None and
                (type(checkpoint) is not int or row['checkpoint'] != checkpoint)):
            raise RegistryError('REGISTRY_LEDGER_MISMATCH')
        return dict(row)

    def register_ledger(self, ledger_id, path, *, checkpoint=0):
        path = _path(path)
        if not _matches(_HEX32, ledger_id) or type(checkpoint) is not int or checkpoint < 0:
            raise RegistryError('REGISTRY_LEDGER_MISMATCH')
        with self._transaction(write=True) as (db, meta):
            previous = db.execute('SELECT * FROM ledgers WHERE ledger_id=?', (ledger_id,)).fetchone()
            if previous is not None:
                if os.path.normcase(previous['path']) != os.path.normcase(path) or previous['checkpoint'] != checkpoint:
                    raise RegistryError('REGISTRY_LEDGER_MISMATCH')
            else:
                if (checkpoint != 0 or meta['phase'] != 'initializing'
                        or ledger_id not in json.loads(meta['expected_ledgers'])):
                    raise RegistryError('REGISTRY_LEDGER_MISMATCH')
                if db.execute('SELECT 1 FROM ledgers WHERE path=?', (path,)).fetchone():
                    raise RegistryError('REGISTRY_LEDGER_MISMATCH')
                db.execute('INSERT INTO ledgers VALUES(?,?,0)', (ledger_id, path))
            return {'ledger_id': ledger_id, 'path': path, 'epoch': meta['epoch'], 'checkpoint': checkpoint}

    def begin_enrollment(self, ledger_id):
        """Close the write gate before changing the roster in multiple ledgers.

        No existing checkpoint is modified here. The caller registers only the
        declared new ledger, persists the new roster witness in every member,
        and explicitly finishes initialization. Interrupted enrollment stays
        initializing; neither another enrollment nor a claim may bypass it.
        """
        if not _matches(_HEX32, ledger_id):
            raise RegistryError('REGISTRY_LEDGER_MISMATCH')
        with self._transaction(write=True) as (db, meta):
            if meta['phase'] != 'ready':
                raise RegistryError('REGISTRY_INITIALIZING')
            if db.execute("SELECT 1 FROM obligations WHERE phase!='settled' LIMIT 1").fetchone():
                raise RegistryError('REGISTRY_OCCUPIED')
            identities = {row[0] for row in db.execute('SELECT ledger_id FROM ledgers')}
            if ledger_id in identities or len(identities) >= 1000:
                raise RegistryError('REGISTRY_LEDGER_MISMATCH')
            identities.add(ledger_id)
            db.execute("UPDATE metadata SET phase='initializing',expected_ledgers=? WHERE singleton=1",
                       (json.dumps(sorted(identities)),))
            return {'epoch': meta['epoch'], 'phase': 'initializing',
                    'expected_ledger_ids': sorted(identities)}

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
        with self._transaction(write=True) as (db, meta):
            if meta['phase'] != 'initializing':
                raise RegistryError('REGISTRY_TRANSITION_REJECTED')
            actual = {row[0] for row in db.execute('SELECT ledger_id FROM ledgers')}
            if actual != set(json.loads(meta['expected_ledgers'])):
                raise RegistryError('REGISTRY_INITIALIZING')
            db.execute("UPDATE metadata SET phase='ready' WHERE singleton=1")

    def _new_obligation(self, ledger_id, operation_id, target_hash, expected_checkpoint, *, migration):
        if type(expected_checkpoint) is not int or expected_checkpoint < 0:
            raise RegistryError('REGISTRY_LEDGER_MISMATCH')
        with self._transaction(write=True) as (db, meta):
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
            if not migration:
                db.execute('INSERT INTO active_slot VALUES(1,?)', (generation,))
            db.execute('UPDATE metadata SET generation=? WHERE singleton=1', (generation,))
            db.execute('UPDATE ledgers SET checkpoint=? WHERE ledger_id=?', (generation, ledger_id))
            return Receipt(meta['epoch'], ledger_id, operation_id, target_hash, generation)

    def claim(self, ledger_id, operation_id, target_hash, *, expected_checkpoint):
        return self._new_obligation(ledger_id, operation_id, target_hash, expected_checkpoint, migration=False)

    def register_unresolved(self, ledger_id, operation_id, target_hash, *, expected_checkpoint):
        return self._new_obligation(ledger_id, operation_id, target_hash, expected_checkpoint, migration=True)

    def _matching(self, db, meta, receipt):
        receipt = validate_receipt(receipt)
        row = db.execute('SELECT * FROM obligations WHERE generation=?', (receipt.generation,)).fetchone()
        if (receipt.epoch != meta['epoch'] or row is None
                or any(row[key] != getattr(receipt, key) for key in ('ledger_id', 'operation_id', 'target_hash'))):
            raise RegistryError('REGISTRY_RECEIPT_MISMATCH')
        return receipt, row

    def mark_possible_send(self, receipt):
        with self._transaction(write=True) as (db, meta):
            receipt, row = self._matching(db, meta, receipt)
            if row['phase'] == 'settled' or row['origin'] != 'claim':
                raise RegistryError('REGISTRY_TRANSITION_REJECTED')
            if row['phase'] == 'declared':
                db.execute("UPDATE obligations SET phase='possible_send' WHERE generation=?", (receipt.generation,))
            return receipt

    def release(self, receipt, *, resolution='absent_verified'):
        if resolution not in ('absent_verified', 'not_submitted'):
            raise RegistryError('REGISTRY_TRANSITION_REJECTED')
        with self._transaction(write=True) as (db, meta):
            receipt, row = self._matching(db, meta, receipt)
            if row['phase'] == 'settled':
                return False
            if resolution == 'not_submitted' and row['phase'] != 'declared':
                raise RegistryError('REGISTRY_TRANSITION_REJECTED')
            db.execute("UPDATE obligations SET phase='settled',resolution=? WHERE generation=?",
                       (resolution, receipt.generation))
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
                 'phase': row['phase'], 'origin': row['origin'], 'ledger_path': row['path']}
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
                    'open_obligations': self._obligations(db, meta)}
