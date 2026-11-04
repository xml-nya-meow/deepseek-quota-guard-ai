"""Portable selection of one complete ledger; never fall back after corruption."""
from datetime import datetime
import json
from pathlib import Path
from uuid import uuid4

from .core import DataStore, GuardError, TZ

POINTER = "当前账本.json"


def current_ledger(home: Path) -> Path:
    home = Path(home).resolve()
    pointer = home / POINTER
    if not pointer.exists():
        return home
    try:
        value = json.loads(pointer.read_text(encoding="utf-8"))
        if value["schema"] != 1 or not isinstance(value["folder"], str) or not value["folder"]:
            raise ValueError("invalid pointer")
        target = Path(value["folder"])
        target = (home / target).resolve() if not target.is_absolute() else target.resolve()
        if not target.is_dir():
            raise ValueError("missing ledger")
        store = DataStore(target)
        period = value.get("period_id")
        if period or store.has_history():
            # A damaged workbook must not hide the ledger's recovery view.
            # Settings, database and period still have to agree.
            config = store.load(require_workbook=False)
            if period and config.period_id != period:
                raise ValueError("different period")
        return target
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise GuardError("当前账本指向的文件缺失、损坏或周期不符；请恢复原账本或选择正确目录，不会自动新建清零。") from exc


def save_current_ledger(home: Path, folder: Path):
    home, folder = Path(home).resolve(), Path(folder).resolve()
    if not folder.is_dir():
        raise GuardError("账本目录不存在，不能保存当前选择。")
    store = DataStore(folder)
    period = store.load(require_workbook=False).period_id if store.has_history() else None
    try:
        location = str(folder.relative_to(home))
    except ValueError:
        location = str(folder)
    DataStore._write_json(home / POINTER, {"schema": 1, "folder": location, "period_id": period})


def new_ledger_path(home: Path) -> Path:
    return Path(home).resolve() / "账本" / (datetime.now(TZ).strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8])
