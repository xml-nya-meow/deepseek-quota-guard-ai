"""A bounded edit history for the canonical people table.

Successful edits are written immediately. Closing an editor needs no write;
undo and discard are new, guarded writes rather than restoration of database
snapshots. Financial events and identity bindings are never rolled back.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from .core import DataStore, GuardError, Person
from .excel_io import MAX_ROWS


class PeopleEditorSession:
    """Keep one immutable opening baseline and up to 50 successful edits."""

    MAX_HISTORY = 50

    def __init__(self, store: DataStore):
        self.store = store
        self._config = store.load()
        self._rows, self._expected_hash = self._read_snapshot()
        self._baseline = self._rows
        self._history: deque[tuple[Person, ...]] = deque(maxlen=self.MAX_HISTORY)

    @property
    def rows(self) -> tuple[Person, ...]:
        return self._rows

    @property
    def baseline(self) -> tuple[Person, ...]:
        return self._baseline

    @property
    def expected_hash(self) -> str:
        return self._expected_hash

    @property
    def can_undo(self) -> bool:
        return bool(self._history)

    @property
    def changed(self) -> bool:
        return self._rows != self._baseline

    @property
    def history_depth(self) -> int:
        return len(self._history)

    def _check_config(self):
        if self.store.load() != self._config:
            raise GuardError("账本设置已改变，请关闭填写窗口并重新打开；本次不会覆盖表格。")

    def _read_snapshot(self) -> tuple[tuple[Person, ...], str]:
        self._check_config()
        before = self.store.workbook_hash()
        rows = tuple(self.store.read_people())
        after = self.store.workbook_hash()
        self._check_config()
        if before != after:
            raise GuardError("读取期间表格已改变，请关闭填写窗口并重新打开。")
        return rows, after

    def _assert_unchanged(self):
        self._check_config()
        if self.store.workbook_hash() != self._expected_hash:
            raise GuardError("表格已被其他操作修改，请关闭填写窗口并重新打开；本次不会覆盖表格。")

    def _persist(self, rows: tuple[Person, ...]) -> bool:
        self._assert_unchanged()
        if rows == self._rows:
            return False
        # DataStore can raise after replacing the workbook (for example when
        # appending its audit event fails). Never assume rollback in that case:
        # leave our old hash intact, so another operation refuses to overwrite
        # the changed file until the user reopens it.
        self.store.save_people(list(rows), expected_hash=self._expected_hash,
                               allow_unassigned=True)
        actual_rows, written_hash = self._read_snapshot()
        if actual_rows != rows:
            raise GuardError("保存后表格内容再次改变，请关闭填写窗口并重新打开；不会继续覆盖。")
        self._rows = rows
        self._expected_hash = written_hash
        return True

    def replace(self, people: Iterable[Person]) -> bool:
        rows = tuple(self.store._validate_people(people, self._config.people_count,
                                                 allow_unassigned=True))
        if len(rows) >= MAX_ROWS:
            raise GuardError(f"人员表最多保留 {MAX_ROWS - 1} 行，请先移除多余空行。")
        previous = self._rows
        if not self._persist(rows):
            return False
        self._history.append(previous)
        return True

    def _index(self, index: int) -> int:
        if type(index) is not int or not 0 <= index < len(self._rows):
            raise GuardError("请先选中要修改的有效行。")
        return index

    def update(self, index: int, person: Person) -> bool:
        rows = list(self._rows)
        rows[self._index(index)] = person
        return self.replace(rows)

    def add(self, person: Person) -> bool:
        return self.replace((*self._rows, person))

    def remove(self, index: int) -> bool:
        rows = list(self._rows)
        del rows[self._index(index)]
        return self.replace(rows)

    def undo(self) -> bool:
        self._assert_unchanged()
        if not self._history:
            return False
        changed = self._persist(self._history[-1])
        self._history.pop()
        return changed

    def discard(self) -> bool:
        changed = self._persist(self._baseline)
        self._history.clear()
        return changed
