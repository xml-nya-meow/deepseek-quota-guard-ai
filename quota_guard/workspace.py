"""Portable selection of one complete ledger; never fall back after corruption."""
from datetime import datetime
import json
from pathlib import Path
from uuid import uuid4

from .core import DataStore, GuardError, TZ

POINTER = "当前账本.json"


def has_saved_data(folder: Path) -> bool:
    """A partial ledger or an interrupted initialization still belongs to its owner."""
    if not folder.exists():
        return False
    names = {DataStore.CONFIG, DataStore.WORKBOOK, DataStore.DATABASE,
             DataStore.DATABASE + "-wal", DataStore.DATABASE + "-shm"}
    return any(item.name in names or item.name.startswith(
        ("运行日志-", "运行摘要-", ".initial", ".初始化", "初始化")) for item in folder.iterdir())


def portable_home(program_dir: Path) -> Path:
    """Keep existing root-based installations on their original ledger and preferences."""
    from .appearance import APPEARANCE_FILENAME
    from .preferences import PREFERENCES_FILENAME
    legacy = (POINTER, APPEARANCE_FILENAME, PREFERENCES_FILENAME, "账本", ".运行锁")
    if has_saved_data(program_dir) or any((program_dir / name).exists() or
                                          (program_dir / name).is_symlink() for name in legacy):
        return program_dir
    return program_dir / "用户数据"


def current_ledger(home: Path) -> Path:
    home = Path(home).resolve()
    pointer = home / POINTER
    if not pointer.exists() and not pointer.is_symlink():
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
