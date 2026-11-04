"""Desktop timing and future-table defaults, kept separate from ledger rules."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import json
from pathlib import Path
import re

from .core import DataStore, GuardError, money


PREFERENCES_FILENAME = '界面偏好.json'
DEFAULT_POLL_INTERVAL_SECONDS = 10
MIN_POLL_INTERVAL_SECONDS = 1
MAX_POLL_INTERVAL_SECONDS = 3600


def validate_kill_line(value) -> Decimal:
    try:
        amount = money(value)
    except GuardError as exc:
        raise ValueError('斩杀线最小为 0，且须为最多 18 位小数的有效金额。') from exc
    # A positive exponent on zero must not expand to a huge persisted string.
    return Decimal(0) if amount == 0 else amount


@dataclass(frozen=True)
class TableDefaults:
    kill_line: Decimal = Decimal('0')
    fill_mode: str = 'skip'

    def __post_init__(self):
        object.__setattr__(self, 'kill_line', validate_kill_line(self.kill_line))
        if not isinstance(self.fill_mode, str) or self.fill_mode not in {'skip', 'kill_line'}:
            raise ValueError('生成表格时请选择 -1（跳过）或斩杀线。')

    @property
    def amount(self) -> Decimal:
        return Decimal(-1) if self.fill_mode == 'skip' else self.kill_line


def validate_poll_interval(value) -> int:
    message = '自动管理间隔必须是 1～3600 之间的整数秒。'
    if type(value) is int:
        interval = value
    elif isinstance(value, str) and re.fullmatch(r'[0-9]{1,4}', value.strip()):
        interval = int(value.strip())
    else:
        raise ValueError(message)
    if not MIN_POLL_INTERVAL_SECONDS <= interval <= MAX_POLL_INTERVAL_SECONDS:
        raise ValueError(message)
    return interval


def _read_preferences(home: Path) -> tuple[int, TableDefaults]:
    path = Path(home) / PREFERENCES_FILENAME
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        return DEFAULT_POLL_INTERVAL_SECONDS, TableDefaults()
    if (not isinstance(data, dict) or type(data.get('schema')) is not int
            or type(data.get('poll_interval_seconds')) is not int):
        raise ValueError('unsupported preferences')
    interval = validate_poll_interval(data['poll_interval_seconds'])
    if data['schema'] == 1 and set(data) == {'schema', 'poll_interval_seconds'}:
        return interval, TableDefaults()
    if (data['schema'] != 2
            or set(data) != {'schema', 'poll_interval_seconds', 'kill_line', 'fill_mode'}
            or not isinstance(data['kill_line'], str)):
        raise ValueError('unsupported preferences')
    return interval, TableDefaults(data['kill_line'], data['fill_mode'])


def load_poll_interval(home: Path) -> tuple[int, str | None]:
    try:
        interval, _defaults = _read_preferences(home)
        return interval, None
    except (OSError, UnicodeError, ValueError, TypeError):
        return DEFAULT_POLL_INTERVAL_SECONDS, '偏好无法读取，间隔暂用 10 秒；原文件保留，请恢复偏好文件后再保存。'


def load_table_defaults(home: Path) -> tuple[TableDefaults, str | None]:
    try:
        _interval, defaults = _read_preferences(home)
        return defaults, None
    except (OSError, UnicodeError, ValueError, TypeError):
        return TableDefaults(), '偏好无法读取，新表暂填 -1；原文件保留，请恢复偏好文件后再保存。'


def _read_for_save(home: Path) -> tuple[int, TableDefaults]:
    try:
        return _read_preferences(home)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise ValueError('现有偏好无法读取，未覆盖原文件；请恢复偏好文件后再保存。') from exc


def _save_preferences(home: Path, interval: int, defaults: TableDefaults) -> None:
    DataStore._write_json(Path(home) / PREFERENCES_FILENAME, {
        'schema': 2, 'poll_interval_seconds': interval,
        'kill_line': format(defaults.kill_line, 'f'), 'fill_mode': defaults.fill_mode,
    })


def save_poll_interval(home: Path, value) -> int:
    interval = validate_poll_interval(value)
    _previous_interval, defaults = _read_for_save(home)
    _save_preferences(home, interval, defaults)
    return interval


def save_table_defaults(home: Path, kill_line, fill_mode: str) -> TableDefaults:
    defaults = TableDefaults(kill_line, fill_mode)
    interval, _previous_defaults = _read_for_save(home)
    _save_preferences(home, interval, defaults)
    return defaults
