"""Deterministic quota rules and local persistence; never sends platform requests.

Money is stored as decimal strings. A snapshot is a replacement of cumulative
costs for the complete configured period, never a delta to add to previous costs.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, localcontext
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
from typing import Iterable
from uuid import uuid4

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.worksheet.datavalidation import DataValidation

TZ = timezone(timedelta(hours=8))
HEADERS = ("API名/人员名", "个人金额（元）")
SCHEMA = 2
_UNRESOLVED_ACTIONS = {"pending", "prepared", "requested", "unknown_result", "unknown", "failed", "confirmed",
                       "observation_failed"}
_LEGACY_READ_ONLY_INTENT = "用户请求只读核实平台状态；未授权或提交撤销"


class GuardError(ValueError):
    """A readable validation/recovery error which must not trigger key deletion."""


class AccountMismatch(GuardError):
    """The caller may offer a separate ledger, never overwrite this account."""


@dataclass(frozen=True)
class SnapshotApplyResult:
    """Separate observation, durable ledger state, and subsequent export."""
    operation_id: str
    observed_source_id: str
    effective_snapshot_id: str
    apply_outcome: str
    persistence_state: str
    reason_code: str
    coverage_summary: dict[str, int]
    rows: list[dict]
    export_state: str

    @property
    def fees_current(self) -> bool:
        return (self.apply_outcome in {"applied", "current_duplicate"}
                and bool(self.observed_source_id)
                and self.observed_source_id == self.effective_snapshot_id)


class SnapshotApplyError(GuardError):
    """Controlled local failure; no raw payload or foreign exception text."""
    def __init__(self, message, *, reason_code, stage, persistence_state="not_written",
                 application=None, coverage_summary=None, operation_id=""):
        super().__init__(message)
        self.reason_code = reason_code
        self.stage = stage
        self.persistence_state = persistence_state
        self.application = application
        self.coverage_summary = dict(coverage_summary or {})
        self.operation_id = operation_id


def money(value, *, allow_skip=False) -> Decimal:
    if value is None or isinstance(value, bool):
        raise GuardError("金额必须是数字。")
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise GuardError("金额必须是普通数字，不能填写公式或文字。") from None
    if not result.is_finite() or result > Decimal("1000000000000"):
        raise GuardError("金额必须是有限且合理的数值。")
    if result < 0 and not (allow_skip and result == -1):
        raise GuardError("金额不能为负数，只有 -1 表示暂不管理。")
    if result.as_tuple().exponent < -18:
        raise GuardError("金额精度最多保留 18 位小数。")
    return result


def clean_name(value) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise GuardError("API 名称必须是文本。")
    name = value.strip()
    if len(name) > 200 or any(ord(c) < 32 for c in name):
        raise GuardError("API 名称过长或含控制字符。")
    if name.startswith(("=", "+", "@")) or re.search(r"sk-[A-Za-z0-9]{8,}", name):
        raise GuardError("请填写 API 名称，不要填写公式或完整密钥。")
    return name


def _date(value: str) -> str:
    try:
        result = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise GuardError("日期必须使用 YYYY-MM-DD 格式。") from None
    return result.isoformat()


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 200:
        raise GuardError("缺少有效的身份标识。")
    if value.startswith("sk-") or any(ord(c) < 32 for c in value):
        raise GuardError("身份标识不能是密钥或含控制字符。")
    return value.strip()


def _now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def _safe_detail(value: str) -> str:
    text = str(value)
    text = re.sub(r"sk-[A-Za-z0-9_*.-]+", "[密钥已隐藏]", text)
    text = re.sub(r"(?i)(bearer\s+)\S+", r"\1[已隐藏]", text)
    text = re.sub(r"(?i)((?:cookie|authorization|password|token)\s*[:=]\s*)[^\s,;]+", r"\1[已隐藏]", text)
    return text[:2000]


@dataclass(frozen=True)
class Person:
    name: str
    amount: Decimal


@dataclass(frozen=True)
class Config:
    period_id: str
    people_count: int
    total_budget: Decimal
    start_date: str
    account_id: str = ""

    @property
    def share(self) -> Decimal:
        with localcontext() as context:
            context.prec = 50
            return (self.total_budget / self.people_count).to_integral_value(rounding=ROUND_FLOOR)


class DataStore:
    CONFIG = "管理设置.json"
    WORKBOOK = "人员额度.xlsx"
    DATABASE = "状态.db"
    MARKER = ".初始化中"

    def __init__(self, folder: Path):
        self.folder = Path(folder).resolve()
        self.config_path = self.folder / self.CONFIG
        self.workbook_path = self.folder / self.WORKBOOK
        self.db_path = self.folder / self.DATABASE

    def has_history(self) -> bool:
        if not self.folder.exists():
            return False
        return any((self.folder / n).exists() for n in
                   (self.CONFIG, self.WORKBOOK, self.DATABASE, self.DATABASE + "-wal",
                    self.DATABASE + "-shm", self.MARKER)) or any(
                       self.folder.glob("运行日志-*.jsonl")) or any(self.folder.glob("运行摘要-*.txt"))

    def initialize(self, people_count: int, total_budget, start_date: str, *, initial_amount=-1) -> Config:
        if isinstance(people_count, bool) or not isinstance(people_count, int) or not 1 <= people_count <= 10000:
            raise GuardError("人数必须是 1 到 10000 的整数。")
        budget = money(total_budget)
        default_amount = money(initial_amount, allow_skip=True)
        start = _date(start_date)
        if start > datetime.now(TZ).date().isoformat():
            raise GuardError("统计开始日期不能晚于今天。")
        if self.has_history():
            raise GuardError("此目录已有设置或历史，请打开/恢复原数据，不能重新初始化清零。")
        config = Config(str(uuid4()), people_count, budget, start)
        self.folder.mkdir(parents=True, exist_ok=True)
        marker = self.folder / self.MARKER
        try:
            with marker.open("x", encoding="utf-8") as f:
                f.write(config.period_id)
            with closing(sqlite3.connect(self.db_path)) as db, db:
                db.executescript("""
                    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE bindings(tracking_id TEXT PRIMARY KEY, person TEXT NOT NULL,
                        active INTEGER NOT NULL DEFAULT 1);
                    CREATE TABLE costs(tracking_id TEXT PRIMARY KEY, amount TEXT NOT NULL,
                        source_id TEXT NOT NULL);
                    CREATE TABLE snapshots(source_id TEXT PRIMARY KEY, digest TEXT NOT NULL,
                        start_date TEXT NOT NULL, end_date TEXT NOT NULL, created TEXT NOT NULL);
                    CREATE TABLE events(id INTEGER PRIMARY KEY AUTOINCREMENT,
                        dedup TEXT UNIQUE, created TEXT NOT NULL, kind TEXT NOT NULL,
                        payload TEXT NOT NULL);
                """)
                for key, value in self._config_dict(config).items():
                    db.execute("INSERT INTO meta VALUES (?,?)", (key, str(value)))
                self._event(db, "initialized", {"people_count": people_count,
                    "total_budget": str(budget), "share": str(config.share), "start_date": start,
                    "initial_amount": str(default_amount)})
            self._write_json(self.config_path, self._config_dict(config))
            self._write_workbook([Person("", default_amount) for _ in range(people_count)])
            marker.unlink()
            self.export_logs()
        except (OSError, sqlite3.Error) as exc:
            raise GuardError("初始化未完整完成，请保留目录并恢复，不能重建覆盖。" + _safe_detail(exc)) from exc
        return config

    @staticmethod
    def _config_dict(config: Config) -> dict:
        return {"schema": SCHEMA, "period_id": config.period_id,
                "people_count": config.people_count, "total_budget": str(config.total_budget),
                "start_date": config.start_date, "account_id": config.account_id,
                "comparison": ">=", "share_rounding": "floor_yuan", "timezone": "UTC+08:00"}

    def load(self, *, require_workbook: bool = True) -> Config:
        if (self.folder / self.MARKER).exists():
            raise GuardError("检测到上次初始化中断，请恢复数据，不会自动清零。")
        required_files = (self.config_path, self.db_path, self.workbook_path) if require_workbook else (
            self.config_path, self.db_path)
        if not all(p.is_file() for p in required_files):
            raise GuardError("设置、表格或统计文件缺失，请恢复原文件，不会自动新建周期。")
        try:
            raw = json.loads(self.config_path.read_text(encoding="utf-8"))
            if raw.get("schema") != SCHEMA or raw.get("comparison") != ">=" or raw.get("share_rounding") != "floor_yuan":
                raise GuardError("设置版本或规则不匹配，请恢复正确设置。")
            n = raw["people_count"]
            if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 10000:
                raise GuardError("设置中的人数无效。")
            legacy_account = raw.get("account_id", "")
            if legacy_account != "":
                legacy_account = _identifier(legacy_account)
            config = Config(_identifier(raw["period_id"]), n, money(raw["total_budget"]),
                            _date(raw["start_date"]))
            with self._connect() as db:
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise GuardError("统计库损坏，请恢复备份。")
                stored = dict(db.execute("SELECT key,value FROM meta"))
                for key, value in self._config_dict(config).items():
                    if key == "account_id":
                        continue
                    if stored.get(key) != str(value):
                        raise GuardError("设置与统计库不匹配，请恢复同一周期的一套文件。")
                # Account identity lives in SQLite, in the same transaction as
                # binding or correcting an unused setup. Old nonempty JSON copies
                # remain assertions; a blank JSON field is not a second store.
                account = stored["account_id"]
                if account != "":
                    account = _identifier(account)
                if legacy_account and legacy_account != account:
                    raise GuardError("设置与统计库的账户不匹配，请恢复同一周期的一套文件。")
                config = Config(config.period_id, n, config.total_budget, config.start_date, account)
            return config
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
            if isinstance(exc, GuardError):
                raise
            raise GuardError("无法读取已有数据，请恢复文件，不会自动清零。") from exc

    @contextmanager
    def _connect(self):
        # URI read/write prevents SQLite from creating an empty replacement DB.
        try:
            db = sqlite3.connect(self.db_path.as_uri() + "?mode=rw", uri=True, timeout=10)
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        except sqlite3.Error as exc:
            raise GuardError("统计库读取或写入失败，请检查目录权限及文件完整性。") from exc
        finally:
            if "db" in locals():
                db.close()

    @staticmethod
    def _write_json(path: Path, value):
        data = json.dumps(value, ensure_ascii=False, indent=2)
        DataStore._atomic_text(path, data)

    @staticmethod
    def _atomic_text(path: Path, content: str):
        fd, temp = tempfile.mkstemp(prefix=".quota-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, path)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    def workbook_hash(self) -> str:
        try:
            return sha256(self.workbook_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise GuardError("无法读取表格，请先保存并关闭占用文件的软件。") from exc

    def _validate_people(self, people: Iterable[Person], count: int, *, allow_unassigned=False) -> list[Person]:
        if type(allow_unassigned) is not bool:
            raise GuardError("待填写行选项必须是明确的开关。")
        rows, names = [], set()
        for person in people:
            name, amount = clean_name(person.name), money(person.amount, allow_skip=True)
            if not name and amount != -1 and not allow_unassigned:
                raise GuardError("金额为 0 或正数时必须填写 API 名称。")
            if name in names:
                raise GuardError(f"API 名称重复：{name}")
            if name:
                names.add(name)
            rows.append(Person(name, amount))
        if len(names) > count:
            raise GuardError("已填写人员数超过分配人数，请检查表格。")
        return rows

    def read_people(self) -> list[Person]:
        from .excel_io import read_people_workbook
        config = self.load()
        # The app's canonical table persists defaults on unassigned rows. They
        # are ignored by binding/evaluation until a valid name is supplied.
        return self._validate_people(read_people_workbook(self.workbook_path, allow_unassigned=True),
                                     config.people_count, allow_unassigned=True)

    def _write_workbook(self, people: list[Person]):
        wb = Workbook()
        ws = wb.active
        ws.title = "人员额度"
        ws.append(HEADERS)
        for cell in ws[1]:
            cell.font = Font(color="FFFFFF", bold=True)
            cell.fill = PatternFill("solid", fgColor="245B78")
        ws.column_dimensions["A"].width = 30
        ws.column_dimensions["B"].width = 24
        ws.freeze_panes = "A2"
        for person in people:
            # Numeric text preserves decimal precision beyond Excel's 15 digits.
            ws.append([person.name, str(person.amount)])
        validation = DataValidation(type="decimal", operator="greaterThanOrEqual", formula1="-1")
        validation.promptTitle = "金额说明"
        validation.prompt = "-1 暂不管理；0 停用；正数为个人累计额度。"
        validation.showInputMessage = True
        ws.add_data_validation(validation)
        validation.add(f"B2:B{max(len(people)+1, 2)}")
        fd, temp = tempfile.mkstemp(prefix=".quota-", suffix=".xlsx", dir=self.folder)
        os.close(fd)
        try:
            wb.save(temp)
            os.replace(temp, self.workbook_path)
        finally:
            wb.close()
            if os.path.exists(temp):
                os.unlink(temp)

    def save_people(self, people: list[Person], expected_hash: str | None = None, *, allow_unassigned=False):
        config = self.load()
        rows = self._validate_people(people, config.people_count, allow_unassigned=allow_unassigned)
        if expected_hash is not None and expected_hash != self.workbook_hash():
            raise GuardError("表格已被其他软件修改，请重新读取后再保存。")
        try:
            self._write_workbook(rows)
        except OSError as exc:
            raise GuardError("表格无法保存，请关闭占用它的 Excel/WPS 后重试。") from exc
        with self._connect() as db:
            self._event(db, "people_saved", {"workbook_hash": self.workbook_hash(),
                "people": [{"name": p.name, "amount": str(p.amount)} for p in rows if p.name]})
        self.evaluate()

    def recovery_workbook_hash(self) -> str | None:
        """Distinguish a missing table from an unreadable/replaced file."""
        try:
            metadata = self.workbook_path.lstat()
            if self.workbook_path.is_symlink() or getattr(metadata, "st_file_attributes", 0) & 0x400:
                raise GuardError("人员表是链接或重解析文件，请先恢复本账本内的原始文件。")
            return self.workbook_hash()
        except FileNotFoundError:
            return None

    def restore_people(self, people: list[Person], *, expected_hash: str | None):
        """Restore only an unusable workbook; never rebuild the ledger or fees."""
        config = self.load(require_workbook=False)
        rows = self._validate_people(people, config.people_count)
        try:
            self.read_people()
        except GuardError:
            pass
        else:
            raise GuardError("当前人员表已可正常读取，请重新检查，或使用普通导入。")
        if self.recovery_workbook_hash() != expected_hash:
            raise GuardError("人员表在确认期间发生变化，请重新选择并确认恢复。")
        backup = None
        if expected_hash is not None:
            original = self.workbook_path.read_bytes()
            if sha256(original).hexdigest() != expected_hash:
                raise GuardError("人员表已改变，未覆盖文件。")
            backup = self.folder / ("人员额度-恢复前-" + uuid4().hex + ".xlsx.bak")
            with backup.open("xb") as stream:
                stream.write(original)
                stream.flush()
                os.fsync(stream.fileno())
        if self.load(require_workbook=False) != config or self.recovery_workbook_hash() != expected_hash:
            raise GuardError("恢复期间设置或人员表已改变，未覆盖文件。")
        self._write_workbook(rows)
        with self._connect() as db:
            self._event(db, "workbook_restored", {"previous_hash": expected_hash,
                "workbook_hash": self.workbook_hash(), "backup": backup.name if backup else None})
        self.export_logs()

    def active_bindings(self, *, require_workbook: bool = True) -> list[dict]:
        """Persisted identities awaiting verified absence, including after restart.

        This is an expected set for reconciliation, not proof that a Key still
        exists. Callers must verify a surviving identity before retiring one.
        """
        self.load(require_workbook=require_workbook)
        with self._connect() as db:
            return [{"name": name, "tracking_id": key} for key, name in db.execute(
                "SELECT tracking_id,person FROM bindings WHERE active=1 ORDER BY tracking_id")]

    def active_tracking_ids(self, *, require_workbook: bool = True) -> set[str]:
        """Return identities not yet retired, without assuming they still exist."""
        return {row["tracking_id"] for row in self.active_bindings(require_workbook=require_workbook)}

    def latest_action(self, tracking_id: str) -> str | None:
        """Latest persisted action status, across the complete event history."""
        record = self.latest_action_record(tracking_id)
        return record["status"] if record is not None else None

    @staticmethod
    def _action_record(number: int, created: str, payload: str) -> dict:
        record = {"id": number, "created": created, "kind": "action", **json.loads(payload)}
        # Existing ledgers predate operation IDs; never invent an ID or evidence.
        record.setdefault("operation_id", None)
        return record

    def latest_action_record(self, tracking_id: str) -> dict | None:
        """Return the full latest action, including legacy and inherited records."""
        self.load(require_workbook=False)
        key_id = _identifier(tracking_id)
        with self._connect() as db:
            for number, created, payload in db.execute(
                    "SELECT id,created,payload FROM events WHERE kind='action' ORDER BY id DESC"):
                event = self._action_record(number, created, payload)
                if event.get("tracking_id") == key_id:
                    return event
        return None

    @classmethod
    def _action_history(cls, db) -> list[dict]:
        return [cls._action_record(number, created, payload) for number, created, payload in db.execute(
            "SELECT id,created,payload FROM events WHERE kind='action' ORDER BY id")]

    @staticmethod
    def _operation_history(history: list[dict], action: dict) -> list[dict]:
        return [event for event in history if event['tracking_id'] == action['tracking_id']
                and event.get('operation_id') == action.get('operation_id')]

    @staticmethod
    def _read_only_operation(history: list[dict]) -> bool:
        """Only an explicit read-only origin can discharge a presence observation.

        Recognize the exact old UI-authored intent for existing ledgers, without
        treating arbitrary detail text or missing submission events as evidence.
        """
        if not history or any('evidence' in event or event.get('requires_reconciliation') is True
                              or event['status'] in {'prepared', 'requested', 'confirmed', 'not_submitted'}
                              for event in history):
            return False
        first = history[0]
        return (first.get('read_only') is True or
                (first['status'] == 'pending' and first.get('operation_id') is not None
                 and first.get('detail') == _LEGACY_READ_ONLY_INTENT))

    @classmethod
    def _unresolved_records(cls, history: list[dict]) -> list[dict]:
        """Keep prior operations visible even after later presence observations."""
        by_target = {}
        for event in history:
            by_target.setdefault(event['tracking_id'], []).append(event)
        unresolved = []
        for records in by_target.values():
            # A verified absence settles earlier uncertainty about this exact
            # target. A cancelled/read-only operation cannot settle another one.
            cutoff = max((event['id'] for event in records if event['status'] == 'absent_verified'), default=-1)
            operations = {}
            for event in records:
                if event['id'] > cutoff:
                    operations.setdefault(event.get('operation_id'), []).append(event)
            pending = []
            for operation in operations.values():
                latest = operation[-1]
                if (latest['status'] in _UNRESOLVED_ACTIONS or
                        (latest['status'] == 'present_verified' and not cls._read_only_operation(operation))):
                    pending.append(latest)
            if pending:
                # One recovery row per target; preserve the original operation.
                unresolved.append(max(pending, key=lambda event: event['id']))
        return sorted(unresolved, key=lambda event: event['id'])

    def unresolved_actions(self) -> list[dict]:
        """Recover each target's last uncertain action without consulting people.

        Removing or skipping a workbook row cannot remove its recovery duty.
        Read all events rather than the bounded UI history window.
        """
        self.load(require_workbook=False)
        with self._connect() as db:
            return self._unresolved_records(self._action_history(db))

    @staticmethod
    def _can_correct_setup(db) -> bool:
        return not db.execute("""SELECT
            EXISTS(SELECT 1 FROM snapshots) OR EXISTS(SELECT 1 FROM costs) OR
            EXISTS(SELECT 1 FROM events WHERE kind='action') OR
            EXISTS(SELECT 1 FROM bindings WHERE active=0)""").fetchone()[0]

    def can_correct_setup(self) -> bool:
        """A binding is provisional only before any billing or action history."""
        self.load()
        with self._connect() as db:
            return self._can_correct_setup(db)

    def reset_initial_binding(self) -> Config:
        """Correct an unused account binding while retaining the complete audit.

        Callers must stop management and discard the old browser session first.
        Even a zero-cost snapshot or an uncertain action prevents this reset.
        """
        config = self.load()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self._can_correct_setup(db):
                raise GuardError("已有账目或处理记录，不能重置账户；请保留原账本并建立新账本。")
            account = db.execute("SELECT value FROM meta WHERE key='account_id'").fetchone()[0]
            bindings = [{"tracking_id": key, "name": name} for key, name in
                        db.execute("SELECT tracking_id,person FROM bindings ORDER BY tracking_id")]
            if account or bindings:
                # Blank the legacy assertion first. If the transaction later
                # rolls back, the old DB identity remains authoritative/readable.
                raw = json.loads(self.config_path.read_text(encoding="utf-8"))
                if raw.get("account_id", ""):
                    raw["account_id"] = ""
                    self._write_json(self.config_path, raw)
                db.execute("DELETE FROM bindings")
                db.execute("UPDATE meta SET value='' WHERE key='account_id'")
                self._event(db, "initial_binding_reset", {"previous_account_id": account,
                    "previous_bindings": bindings, "period_id": config.period_id})
        self.export_logs()
        return self.load()

    def update_start_date(self, start_date: str, destination: Path) -> DataStore:
        """Use a new ledger for a changed range; the original stays untouched."""
        start = _date(start_date)
        if start > datetime.now(TZ).date().isoformat():
            raise GuardError("统计开始日期不能晚于今天。")
        if start == self.load().start_date:
            return self
        return self.create_successor(destination, start, keep_account=True)

    def create_list(self, destination: Path, people_count: int, total_budget,
                    start_date: str, *, inherit_people: bool = True, initial_amount=-1) -> DataStore:
        """Create an independent list while retaining this account's obligations.

        A blank roster is not a new account: existing identity, retirement and
        uncertain operations must remain available for reconciliation.
        """
        if isinstance(people_count, bool) or not isinstance(people_count, int) or not 1 <= people_count <= 10000:
            raise GuardError("人数必须是 1 到 10000 的整数。")
        return self.create_successor(destination, _date(start_date), keep_account=True,
            people_count=people_count, total_budget=money(total_budget), inherit_people=inherit_people,
            initial_amount=initial_amount)

    def create_successor(self, destination: Path, start_date: str | None = None,
                         *, keep_account: bool = False, people_count: int | None = None,
                         total_budget=None, inherit_people: bool = True, initial_amount=-1) -> DataStore:
        """Publish a complete new period without editing or moving the old one.

        The UI supplies a fresh destination and switches only after this returns.
        Same-account successors inherit identity and last action states, but no
        costs or snapshots. Other-account successors have no inherited bindings.
        """
        config = self.load()
        count = config.people_count if people_count is None else people_count
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 10000:
            raise GuardError("人数必须是 1 到 10000 的整数。")
        budget = config.total_budget if total_budget is None else money(total_budget)
        default_amount = money(initial_amount, allow_skip=True)
        if type(inherit_people) is not bool:
            raise GuardError("是否延续人员内容必须是明确的开关。")
        start = config.start_date if start_date is None else _date(start_date)
        if start > datetime.now(TZ).date().isoformat():
            raise GuardError("统计开始日期不能晚于今天。")
        target = Path(destination).resolve()
        if target.exists():
            raise GuardError("新账本目录必须不存在，不能覆盖已有文件夹。")
        fingerprint = self.workbook_hash()
        original_people = self.read_people()
        people = (self._validate_people(original_people, count, allow_unassigned=True) if inherit_people
                  else [Person("", default_amount)] * count)
        if inherit_people and people_count is not None:
            # New-list defaults apply only to vacant positions. Retained names
            # keep exact amounts, including explicitly skipped named people.
            people = [person for person in people if person.name]
            people.extend([Person("", default_amount)] * (count - len(people)))
        stage = None
        try:
            # Freeze the source's identity/actions while constructing its
            # successor. Workbook edits are checked separately before publish.
            with self._connect() as source_db:
                source_db.execute("BEGIN IMMEDIATE")
                if self.load() != config:
                    raise GuardError("创建期间原账本设置发生变化，请重试。")
                bindings = list(source_db.execute("SELECT tracking_id,person,active FROM bindings")) if keep_account else []
                names = {key: name for key, name, _ in bindings}
                actions = {}
                if keep_account:
                    history = self._action_history(source_db)
                    blocked = {event['tracking_id']: event for event in self._unresolved_records(history)}
                    for event in reversed(history):
                        number, created = event['id'], event['created']
                        action = {key: value for key, value in event.items() if key not in {'id', 'created', 'kind'}}
                        key = action["tracking_id"]
                        if key not in names or action["name"] != names[key]:
                            raise GuardError("历史操作与绑定不一致，不能安全建立新周期。")
                        if key not in actions:
                            if key in blocked:
                                event = blocked[key]
                                number, created = event['id'], event['created']
                                action = {field: value for field, value in event.items()
                                          if field not in {'id', 'created', 'kind'}}
                            operation = self._operation_history(history, action)
                            if self._read_only_operation(operation):
                                action['read_only'] = True
                            elif action['status'] == 'present_verified':
                                action['requires_reconciliation'] = True
                            actions[key] = {**action, "inherited_from_period": config.period_id,
                                            "original_event_id": number, "original_created": created}
                target.parent.mkdir(parents=True, exist_ok=True)
                stage = Path(tempfile.mkdtemp(prefix=".new-ledger-", dir=target.parent)).resolve()
                successor = DataStore(stage)
                successor.initialize(count, budget, start, initial_amount=default_amount)
                with successor._connect() as new_db:
                    new_db.execute("BEGIN IMMEDIATE")
                    if keep_account:
                        new_db.execute("UPDATE meta SET value=? WHERE key='account_id'", (config.account_id,))
                        new_db.executemany("INSERT INTO bindings VALUES (?,?,?)", bindings)
                        for action in sorted(actions.values(), key=lambda value: value["original_event_id"]):
                            self._event(new_db, "action", action)
                    self._event(new_db, "successor_created", {"previous_period_id": config.period_id,
                        "previous_folder": str(self.folder), "previous_start_date": config.start_date,
                        "start_date": start, "keep_account": keep_account,
                        "inherit_people": inherit_people, "initial_amount": str(default_amount),
                        "inherited_binding_count": len(bindings), "inherited_action_count": len(actions)})
                successor.save_people(people, allow_unassigned=True)
                successor.load()
                if self.workbook_hash() != fingerprint or self.load() != config:
                    raise GuardError("创建期间原账本或人员表发生变化，新账本未启用，请重试。")
                if target.exists():
                    raise GuardError("新账本目录已被其他操作创建，未覆盖。")
                # Both paths are explicitly resolved siblings under the chosen
                # parent; only the newly created staging directory is renamed.
                if stage.parent != target.parent or stage == self.folder:
                    raise GuardError("新账本暂存目录无效，未移动任何原数据。")
                stage.rename(target)
            return DataStore(target)
        except Exception as exc:
            if stage is not None and stage.is_dir():
                try:
                    (stage / self.MARKER).write_text("后继账本创建中断；原账本未修改。", encoding="utf-8")
                except OSError:
                    pass
            if isinstance(exc, GuardError):
                raise
            raise GuardError("新账本创建未完成，原账本仍保留且未修改，请检查目录权限后重试。") from exc

    def bind_keys(self, keys: list[dict], account_id: str):
        config = self.load()
        account = _identifier(account_id)
        if config.account_id and account != config.account_id:
            raise AccountMismatch("登录账户与此数据目录不一致；请登录原账户，或保留原账本并建立新账本。")
        people = [p for p in self.read_people() if p.name and p.amount >= 0]
        by_name: dict[str, list[str]] = {}
        seen_ids = set()
        for key in keys:
            name, key_id = clean_name(key["name"]), _identifier(key["tracking_id"])
            if key_id in seen_ids:
                raise GuardError("平台返回重复 Tracking ID，无法安全绑定。")
            seen_ids.add(key_id)
            by_name.setdefault(name, []).append(key_id)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            bindings = {r[0]: r[1] for r in db.execute("SELECT tracking_id, person FROM bindings")}
            active = {r[0]: r[1] for r in db.execute("SELECT person,tracking_id FROM bindings WHERE active=1")}
            additions = []
            for p in people:
                matches = by_name.get(p.name, [])
                historical = [key for key, name in bindings.items() if name == p.name]
                if historical and p.name not in active:
                    if matches:
                        raise GuardError(f"{p.name} 已有退休 Key 的历史，出现同名 Key 必须人工核对换绑，不能自动清零。")
                    continue
                if len(matches) != 1:
                    raise GuardError(f"{p.name}：平台必须存在且仅存在一个同名 Key。")
                key_id = matches[0]
                if p.name in active and active[p.name] != key_id:
                    raise GuardError(f"{p.name} 的 Tracking ID 已变更，必须人工核对历史并换绑，不能自动清零。")
                if key_id in bindings and bindings[key_id] != p.name:
                    raise GuardError(f"{p.name} 的 Key 已绑定其他人员，拒绝按新名字覆盖历史。")
                if key_id not in bindings:
                    additions.append((key_id, p.name))
            for key_id, name in additions:
                db.execute("INSERT INTO bindings(tracking_id,person) VALUES (?,?)", (key_id, name))
            if not config.account_id:
                db.execute("UPDATE meta SET value=? WHERE key='account_id'", (account,))
            if additions or not config.account_id:
                self._event(db, "keys_bound", {"bindings": [{"name": n, "tracking_id": k} for k, n in additions]})
        self.evaluate()

    def bind_forced_target(self, keys: list[dict], account_id: str, name: str, tracking_id: str):
        """Bind only the explicitly selected roster target, regardless of quota.

        The full platform list is still checked for identity ambiguity. Missing
        unrelated roster names do not prevent an otherwise exact manual target.
        This does not change the automatic binding rule that skips amount -1.
        """
        config = self.load()
        account, selected_id = _identifier(account_id), _identifier(tracking_id)
        selected_name = clean_name(name)
        if config.account_id and account != config.account_id:
            raise AccountMismatch("登录账户与此数据目录不一致；请登录原账户，或保留原账本并建立新账本。")
        fingerprint = self.workbook_hash()
        people = self.read_people()
        if not selected_name or sum(person.name == selected_name for person in people) != 1:
            raise GuardError("强制删除目标必须是当前人员表中的一个明确人员。")
        if not isinstance(keys, list) or not keys:
            raise GuardError("平台完整 Key 列表为空或无效，无法安全绑定。")
        by_name: dict[str, list[str]] = {}
        seen_ids = set()
        for key in keys:
            if not isinstance(key, dict) or "name" not in key or "tracking_id" not in key:
                raise GuardError("平台 Key 列表缺少姓名或 Tracking ID，无法安全绑定。")
            platform_name, key_id = clean_name(key["name"]), _identifier(key["tracking_id"])
            if not platform_name or key_id in seen_ids:
                raise GuardError("平台完整 Key 列表存在空名或重复 Tracking ID，无法安全绑定。")
            seen_ids.add(key_id)
            by_name.setdefault(platform_name, []).append(key_id)
        if by_name.get(selected_name) != [selected_id]:
            raise GuardError("选中人员与平台唯一同名 Key 的 Tracking ID 不一致，请重新核对。")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if self.load() != config or self.workbook_hash() != fingerprint:
                raise GuardError("核对期间账本或人员表发生变化，请重新选择目标。")
            bindings = list(db.execute("SELECT tracking_id,person,active FROM bindings"))
            historical = [(key, active) for key, person, active in bindings if person == selected_name]
            active_ids = [key for key, active in historical if active]
            if historical and not active_ids:
                raise GuardError(f"{selected_name} 已有退休 Key 的历史，必须人工核对换绑，不能按同名 Key 覆盖历史。")
            if active_ids and active_ids != [selected_id]:
                raise GuardError(f"{selected_name} 的 Tracking ID 已变更，必须人工核对历史并换绑。")
            existing = next(((person, active) for key, person, active in bindings if key == selected_id), None)
            if existing is not None and (existing[0] != selected_name or not existing[1]):
                raise GuardError("选中 Key 已绑定其他人员或已退休，拒绝覆盖既有身份。")
            additions = []
            if existing is None:
                db.execute("INSERT INTO bindings(tracking_id,person) VALUES (?,?)", (selected_id, selected_name))
                additions.append({"name": selected_name, "tracking_id": selected_id})
            if not config.account_id:
                db.execute("UPDATE meta SET value=? WHERE key='account_id'", (account,))
            if additions or not config.account_id:
                self._event(db, "keys_bound", {"bindings": additions, "source": "manual_force"})
        self.export_logs()

    def apply_snapshot(self, totals: dict[str, Decimal], source_id: str,
                       start_date: str, end_date: str) -> list[dict]:
        """Compatibility entry point. GUI callers also need the source outcome."""
        return self.apply_snapshot_result(totals, source_id, start_date, end_date).rows

    def apply_snapshot_result(self, totals: dict[str, Decimal], source_id: str,
                              start_date: str, end_date: str, *, operation_id="") -> SnapshotApplyResult:
        coverage = {}

        def rejected(message, code, *, stage="snapshot_validation", state="not_written"):
            return SnapshotApplyError(message, reason_code=code, stage=stage,
                                      persistence_state=state, coverage_summary=coverage,
                                      operation_id=operation_id)

        try:
            config, people = self.load(), self.read_people()
        except Exception:
            raise rejected("无法核验本地账本和人员表；未应用本次费用，请恢复原文件。",
                           "LOCAL_CONTEXT_UNAVAILABLE", stage="local_context") from None
        try:
            start, end = _date(start_date), _date(end_date)
        except GuardError:
            raise rejected("账单日期无效，未应用本次费用。", "PERIOD_CONFIG_MISMATCH") from None
        if start != config.start_date or end < start or end > datetime.now(TZ).date().isoformat():
            raise rejected("账单时间范围错误，必须从本期起点开始且不晚于今天。", "PERIOD_CONFIG_MISMATCH")
        if not config.account_id:
            raise rejected("请先绑定已核实的平台账户及人员 Key。", "ACCOUNT_NOT_BOUND")
        try:
            source = _identifier(source_id)
            normalized = {_identifier(k): money(v) for k, v in totals.items()}
        except (GuardError, AttributeError, TypeError):
            raise rejected("费用标识或金额无效，未应用本次费用。", "SNAPSHOT_DATA_INVALID") from None
        if len(normalized) != len(totals):
            raise rejected("账单存在重复的 Tracking ID，请核对来源。", "SNAPSHOT_DATA_INVALID")
        digest = sha256(json.dumps({"start": start, "end": end,
            "totals": {k: format(v, "f") for k, v in sorted(normalized.items())}}, sort_keys=True).encode()).hexdigest()
        try:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                bindings = list(db.execute("SELECT tracking_id,person,active FROM bindings"))
                required = {key for key, _, _ in bindings}
                managed_names = {p.name for p in people if p.name and p.amount >= 0}
                active_managed = {key for key, name, active in bindings if active and name in managed_names}
                historical = {key for key, _, active in bindings if not active}
                received = required.intersection(normalized)
                coverage.update({
                    "bound_total_count": len(required), "active_managed_count": len(active_managed),
                    "fee_tracking_count": len(normalized), "matched_total_count": len(received),
                    "matched_active_count": len(active_managed.intersection(normalized)),
                    "matched_historical_count": len(historical.intersection(normalized)),
                    "active_missing_count": len(active_managed.difference(normalized)),
                })
                existing = db.execute("SELECT digest FROM snapshots WHERE source_id=?", (source,)).fetchone()
                latest = db.execute("SELECT source_id FROM snapshots ORDER BY rowid DESC LIMIT 1").fetchone()
                effective = latest[0] if latest else ""
                if existing:
                    if existing[0] != digest:
                        raise rejected("相同账单标识对应不同内容，请重新核实来源。", "SNAPSHOT_CONFLICT")
                    outcome = "current_duplicate" if source == effective else "stale_replay"
                else:
                    previous_end = db.execute("SELECT MAX(end_date) FROM snapshots").fetchone()[0]
                    if previous_end and end < previous_end:
                        raise rejected("导入的账单截止日期早于现有统计，拒绝回退。", "END_DATE_REGRESSION")
                    if not required:
                        raise rejected("费用已读取，但当前账本没有已绑定 Key；请先核对受管人员，未应用本次费用。",
                                       "NO_MANAGED_BINDINGS")
                    if not received:
                        raise rejected("费用已读取，但没有匹配此账本绑定的 Key；原累计保留，请核对 Key 绑定。",
                                       "NO_BOUND_COST_MATCH")
                    old = {k: Decimal(v) for k, v in db.execute("SELECT tracking_id,amount FROM costs")}
                    if any(key in old and normalized[key] < old[key] for key in received):
                        raise rejected("累计费用下降，请核对账单修订或日期范围，未覆盖原统计。", "CUMULATIVE_DECREASE")
                    for key_id in received:
                        db.execute("INSERT INTO costs VALUES (?,?,?) ON CONFLICT(tracking_id) DO UPDATE "
                                    "SET amount=excluded.amount, source_id=excluded.source_id",
                                    (key_id, str(normalized[key_id]), source))
                    db.execute("INSERT INTO snapshots VALUES (?,?,?,?,?)", (source, digest, start, end, _now()))
                    self._event(db, "snapshot", {"source_id": source, "start_date": start,
                        "end_date": end, "tracked_key_count": len(received),
                        "missing_key_count": len(required - received),
                        "schema_version": 1, "timezone": "UTC+08:00", "currency": "CNY",
                        "totals": {key: format(normalized[key], "f") for key in sorted(received)},
                        "coverage": {"scope": "bound_keys", "complete": required == received,
                                     "missing_tracking_ids": sorted(required - received),
                                     "missing_cost": "unknown"}})
                    outcome, effective = "applied", source
                results = self._evaluate(db, config, people)
                # An old observation cannot authorize or create new trigger evidence.
                if outcome != "stale_replay":
                    self._record_triggers(db, config, results)
        except SnapshotApplyError:
            raise
        except Exception:
            # Do not infer rollback merely from a missing diagnostic event. Even
            # a commit/connection failure must be reconciled against the database.
            raise rejected("费用保存结果待核实；请恢复本地账本，核实前保持自动管理停止。",
                           "SNAPSHOT_PERSISTENCE_UNKNOWN", stage="snapshot_transaction", state="unknown") from None
        application = SnapshotApplyResult(
            operation_id, source, effective, outcome, "persisted",
            "STALE_SNAPSHOT_REPLAY" if outcome == "stale_replay" else "OK",
            dict(coverage), results, "pending")
        # Only successful exit from the SQLite transaction establishes persistence.
        try:
            self.export_logs()
        except Exception:
            raise SnapshotApplyError("费用和事件已保存在统计库，但日志导出未完成；请恢复目录写入后保存处理记录。",
                                     reason_code="AUDIT_EXPORT_FAILED", stage="audit_export",
                                     persistence_state="persisted", application=application,
                                     coverage_summary=coverage, operation_id=operation_id) from None
        return replace(application, export_state="complete")

    def current_snapshot_id(self) -> str:
        """Query identity only, without evaluation, events, or exports."""
        try:
            with closing(sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True, timeout=10)) as db:
                row = db.execute("SELECT source_id FROM snapshots ORDER BY rowid DESC LIMIT 1").fetchone()
                return row[0] if row else ""
        except sqlite3.Error:
            raise GuardError("无法只读核验当前费用快照，请检查统计库。") from None

    def read_evaluation(self) -> list[dict]:
        """Read current rows for recovery; never reconcile triggers or export."""
        config, people = self.load(), self.read_people()
        try:
            with closing(sqlite3.connect(self.db_path.as_uri() + "?mode=ro", uri=True, timeout=10)) as db:
                db.execute("BEGIN")
                return self._evaluate(db, config, people)
        except sqlite3.Error:
            raise GuardError("无法只读恢复人员状态，请检查统计库。") from None

    def evaluate(self) -> list[dict]:
        """Evaluate current rules and idempotently reconcile their audit events.

        Called on startup and after workbook edits as well as after collection,
        so zero budgets and external edits do not depend on a new cost report.
        """
        config, people = self.load(), self.read_people()
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            results = self._evaluate(db, config, people)
            self._record_triggers(db, config, results)
        self.export_logs()
        return results

    def _record_triggers(self, db, config: Config, results: list[dict]):
        for result in results:
            if result["status"] != "triggered":
                continue
            payload = {k: str(v) if isinstance(v, Decimal) else v for k, v in result.items()}
            dedup = sha256(json.dumps({"period": config.period_id, "name": result["name"],
                "key": result["tracking_id"], "amount": str(result["amount"]),
                "reasons": result["reasons"]}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            self._event(db, "quota_triggered", payload, dedup="trigger:"+dedup)

    @staticmethod
    def _evaluate(db, config: Config, people: list[Person]) -> list[dict]:
        bindings = list(db.execute("SELECT tracking_id,person,active FROM bindings"))
        costs = {k: (Decimal(v), source) for k, v, source in
                 db.execute("SELECT tracking_id,amount,source_id FROM costs")}
        latest = db.execute("SELECT source_id FROM snapshots ORDER BY rowid DESC LIMIT 1").fetchone()
        latest_source = latest[0] if latest else None
        results = []
        for person in people:
            ids = [key for key, name, _ in bindings if person.name and name == person.name]
            active_ids = [key for key, name, active in bindings if person.name and name == person.name and active]
            with localcontext() as context:
                context.prec = 50
                known = [costs[k][0] for k in ids if k in costs]
                spent = sum(known, Decimal(0)) if known else None
            complete = bool(ids) and latest_source is not None and all(
                k in costs and costs[k][1] == latest_source for k in ids)
            result = {"name": person.name, "amount": person.amount, "spent": spent,
                      "share": config.share, "tracking_id": active_ids[0] if len(active_ids) == 1 else "",
                      "status": "ok", "reasons": [], "complete": complete,
                      "spent_is_lower_bound": spent is not None and not complete}
            if not person.name:
                result["status"] = "unassigned"
                result["reasons"].append("尚未填写 API 名称，不参与管理")
            elif person.amount == -1:
                result["status"] = "skipped"
            elif ids and not active_ids:
                result["status"] = "retired"
                result["reasons"].append("已核实平台列表中不存在，保留历史费用；不据此认定删除原因")
            elif person.amount == 0 or config.share == 0:
                result["status"] = "triggered"
                if person.amount == 0:
                    result["reasons"].append("个人额度为零")
                if config.share == 0:
                    result["reasons"].append("公共人均额度为零")
            elif spent is None or not result["tracking_id"]:
                result["status"] = "unknown"
                result["reasons"].append("尚未完成 Key 绑定或缺少完整周期账单")
            else:
                if spent >= person.amount:
                    result["reasons"].append("达到个人额度")
                if spent >= config.share:
                    result["reasons"].append("达到公共人均额度")
                if result["reasons"]:
                    result["status"] = "triggered"
                elif not complete:
                    result["status"] = "unknown"
                    result["reasons"].append("最近费用快照缺少该人员部分记录，历史金额仅为已知下界")
            results.append(result)
        return results

    def action_evidence(self, name: str, tracking_id: str) -> dict:
        """Capture the current rule and cost basis using a fixed non-secret schema."""
        config = self.load()
        name, key_id = clean_name(name), _identifier(tracking_id)
        fingerprint = self.workbook_hash()
        people = self.read_people()
        with self._connect() as db:
            db.execute("BEGIN")
            bound = db.execute("SELECT person FROM bindings WHERE tracking_id=?", (key_id,)).fetchone()
            if not bound or bound[0] != name:
                raise GuardError("操作证据目标与既有绑定不一致。")
            row = next((item for item in self._evaluate(db, config, people) if item["name"] == name), None)
            if row is None or row["tracking_id"] != key_id:
                raise GuardError("当前人员表或活动 Key 不支持此操作目标，请重新核对。")
            latest = db.execute("SELECT source_id FROM snapshots ORDER BY rowid DESC LIMIT 1").fetchone()
            evidence = {"period_id": config.period_id, "account_id": config.account_id,
                "workbook_hash": fingerprint, "amount": format(row["amount"], "f"),
                "share": format(row["share"], "f"),
                "spent": format(row["spent"], "f") if row["spent"] is not None else None,
                "reasons": list(row["reasons"]), "snapshot_id": latest[0] if latest else None,
                "complete": row["complete"], "status": row["status"]}
        if self.workbook_hash() != fingerprint or self.load() != config:
            raise GuardError("读取操作证据期间设置或人员表发生变化，请重新核对。")
        return evidence

    @staticmethod
    def _validated_action_evidence(evidence: dict) -> dict:
        allowed = {"period_id", "account_id", "workbook_hash", "amount", "share", "spent",
                   "reasons", "snapshot_id", "complete", "status"}
        if not isinstance(evidence, dict) or set(evidence) != allowed:
            raise GuardError("操作证据字段不完整或包含不支持的内容。")
        result = {}
        for key in ("period_id", "account_id", "snapshot_id"):
            value = evidence[key]
            if key == "snapshot_id" and value is None:
                result[key] = None
            else:
                value = _identifier(value)
                if _safe_detail(value) != value:
                    raise GuardError("操作证据不能包含认证信息。")
                result[key] = value
        fingerprint = evidence["workbook_hash"]
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise GuardError("操作证据缺少有效的人员表指纹。")
        result["workbook_hash"] = fingerprint
        result["amount"] = format(money(evidence["amount"], allow_skip=True), "f")
        result["share"] = format(money(evidence["share"]), "f")
        result["spent"] = None if evidence["spent"] is None else format(money(evidence["spent"]), "f")
        reasons = evidence["reasons"]
        known_reasons = {"个人额度为零", "公共人均额度为零", "达到个人额度", "达到公共人均额度",
            "尚未完成 Key 绑定或缺少完整周期账单",
            "最近费用快照缺少该人员部分记录，历史金额仅为已知下界",
            "已核实平台列表中不存在，保留历史费用；不据此认定删除原因"}
        if (not isinstance(reasons, list) or len(reasons) > 4
                or any(not isinstance(reason, str) or reason not in known_reasons for reason in reasons)):
            raise GuardError("操作证据包含不支持的判断原因。")
        if (type(evidence["complete"]) is not bool or not isinstance(evidence["status"], str)
                or evidence["status"] not in {"ok", "skipped", "retired", "triggered", "unknown"}):
            raise GuardError("操作证据的数据状态无效。")
        result.update(reasons=list(reasons), complete=evidence["complete"], status=evidence["status"])
        return result

    def record_action(self, name: str, tracking_id: str, status: str, detail: str, *,
                      operation_id: str | None = None, evidence: dict | None = None,
                      continuation: bool = False, read_only: bool = False,
                      intent: str | None = None):
        # Only the outcome of an existing operation can bypass the workbook.
        # New intents/approval and new evidence always require a valid table.
        self.load(require_workbook=not continuation)
        if continuation:
            if status in {"pending", "requested"} or evidence is not None:
                raise GuardError("恢复只允许记录原操作结果，不能建立新撤销意图、许可或费用依据。")
        else:
            self.read_people()
        name, key_id = clean_name(name), _identifier(tracking_id)
        if status not in {"pending", "prepared", "unknown_result", "absent_verified",
                          "requested", "confirmed", "failed", "unknown", "cancelled", "present_verified", "not_submitted",
                          "observation_failed"}:
            raise GuardError("无效的操作状态。")
        if type(read_only) is not bool or (read_only and
                (status != 'pending' or continuation or evidence is not None or operation_id is None)):
            raise GuardError("只读标记仅用于无撤销证据的新核实起点。")
        if intent is not None and (intent != 'manual_force' or status not in {'pending', 'requested'}
                or continuation or read_only or operation_id is None):
            raise GuardError("强制删除意图仅用于有编号的新撤销操作起点，不能追加到恢复或只读记录。")
        if status == 'not_submitted' and (not continuation or operation_id is None):
            raise GuardError("未提交结论只允许延续有编号的新准备操作。")
        if status == 'observation_failed' and not continuation:
            raise GuardError("只读观察失败只能续记已有待核实操作。")
        if operation_id is not None:
            operation_id = _identifier(operation_id)
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", operation_id):
                raise GuardError("操作编号无效。")
        payload = {"name": name, "tracking_id": key_id, "status": status, "detail": _safe_detail(detail)}
        if operation_id is not None:
            payload["operation_id"] = operation_id
        if evidence is not None:
            payload["evidence"] = self._validated_action_evidence(evidence)
        if read_only:
            payload['read_only'] = True
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            bound = db.execute("SELECT person FROM bindings WHERE tracking_id=?", (key_id,)).fetchone()
            if not bound or bound[0] != name:
                raise GuardError("操作目标与既有绑定不一致，拒绝记录成功。")
            history = self._action_history(db)
            operation = self._operation_history(history, payload)
            if intent is not None:
                if operation:
                    raise GuardError("已有操作不能追加或改写为强制删除意图。")
                payload['intent'] = intent
            elif operation:
                original_intent = operation[0].get('intent')
                if original_intent not in {None, 'manual_force'} or any(
                        event.get('intent') != original_intent for event in operation):
                    raise GuardError("原操作的删除意图不一致，不能继续记录。")
                if original_intent is not None:
                    payload['intent'] = original_intent
            if read_only and operation:
                raise GuardError("不能把已有操作重新标记为只读核实。")
            if status == 'pending':
                for blocked in self._unresolved_records(history):
                    if blocked['tracking_id'] == key_id and not (
                            blocked.get('operation_id') == operation_id and blocked['status'] == 'pending'):
                        raise GuardError("原操作仍可能提交或结果未知，不能建立新的处理意图。")
            if self._read_only_operation(operation):
                if status in {'prepared', 'requested', 'confirmed', 'not_submitted'} or evidence is not None:
                    raise GuardError("只读核实不能转为撤销或签发许可。")
                payload['read_only'] = True
            if continuation:
                previous = operation[-1] if operation else None
                if (previous is None or previous.get("name") != name
                        or previous.get("operation_id") != operation_id):
                    raise GuardError("未找到同一目标的原操作，不能通过恢复入口新增或替换操作。")
                presence_continuation = (previous.get('status') == 'present_verified'
                                         and not self._read_only_operation(operation)
                                         and status in {'present_verified', 'absent_verified', 'observation_failed'})
                if previous.get("status") not in _UNRESOLVED_ACTIONS | {status} and not presence_continuation:
                    raise GuardError("原操作已核实结束，不能用迟到结果改写结论。")
                if status == 'not_submitted' and (previous['status'] not in {'pending', 'not_submitted'} or
                        any(event['status'] not in {'pending', 'not_submitted'} for event in operation)):
                    raise GuardError("不能将曾经可能提交或观察过的操作回填为未提交。")
                if "evidence" in previous:
                    payload["evidence"] = self._validated_action_evidence(previous["evidence"])
            if operation_id is not None:
                for (previous,) in db.execute("SELECT payload FROM events WHERE kind='action' ORDER BY id DESC"):
                    action = json.loads(previous)
                    if action.get("operation_id") != operation_id:
                        continue
                    if action.get("name") != name or action.get("tracking_id") != key_id:
                        raise GuardError("操作编号已关联其他目标，拒绝混用处理记录。")
                    if "evidence" not in payload and "evidence" in action:
                        # Late results keep their original approved basis even if
                        # the workbook changed or a successor ledger was opened.
                        payload["evidence"] = self._validated_action_evidence(action["evidence"])
            if status == 'present_verified' and not self._read_only_operation(operation):
                payload['requires_reconciliation'] = True
            replay_not_submitted = False
            if status == 'not_submitted':
                recorded = [event for event in operation if event['status'] == 'not_submitted']
                if recorded:
                    # SQLite may have committed before the text-log export
                    # failed. Replay that exact result by exporting again,
                    # never by inserting a second result or clearing other duty.
                    def original_payload(event):
                        return {key: value for key, value in event.items()
                                if key not in {'id', 'created', 'kind'}}
                    if (operation[-1]['status'] != 'not_submitted'
                            or any(original_payload(event) != payload for event in recorded)):
                        raise GuardError("已有未提交结论与本次重试不一致，未改写原操作。")
                    replay_not_submitted = True
            if status == "absent_verified":
                db.execute("UPDATE bindings SET active=0 WHERE tracking_id=?", (key_id,))
            if not replay_not_submitted:
                self._event(db, "action", payload)
        self.export_logs()

    @staticmethod
    def _event(db, kind: str, payload: dict, dedup: str | None = None):
        db.execute("INSERT OR IGNORE INTO events(dedup,created,kind,payload) VALUES (?,?,?,?)",
                   (dedup, _now(), kind, json.dumps(payload, ensure_ascii=False)))

    def events(self, limit: int = 200) -> list[dict]:
        self.load(require_workbook=False)
        with self._connect() as db:
            rows = db.execute("SELECT id,created,kind,payload FROM events ORDER BY id DESC LIMIT ?",
                              (max(1, min(int(limit), 10000)),)).fetchall()
        return [{"id": i, "created": t, "kind": k, **json.loads(p)} for i, t, k, p in rows]

    def export_logs(self):
        """SQLite is authoritative. Rebuilding daily files makes crash recovery idempotent."""
        with self._connect() as db:
            rows = db.execute("SELECT id,created,kind,payload FROM events ORDER BY id").fetchall()
        daily: dict[str, list[dict]] = {}
        for number, created, kind, payload in rows:
            daily.setdefault(created[:10], []).append({"id": number, "created": created,
                                                       "kind": kind, **json.loads(payload)})
        try:
            for day, events in daily.items():
                jsonl = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
                readable = "".join(f"#{e['id']} {e['created']} | {e['kind']} | " +
                    json.dumps({k: v for k, v in e.items() if k not in {"id", "created", "kind"}}, ensure_ascii=False)
                    + "\n" for e in events)
                self._atomic_text(self.folder / f"运行日志-{day}.jsonl", jsonl)
                self._atomic_text(self.folder / f"运行摘要-{day}.txt", readable)
        except OSError as exc:
            raise GuardError("事件已保存在统计库，但同目录日志写入失败；请停止实际操作并恢复目录权限。") from exc
