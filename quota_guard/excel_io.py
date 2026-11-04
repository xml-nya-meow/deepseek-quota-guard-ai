"""Read quota spreadsheets without depending on worksheet names or styling."""
from __future__ import annotations

from contextlib import ExitStack, closing
from io import BytesIO
from itertools import zip_longest
from pathlib import Path
from zipfile import ZipFile

from openpyxl import load_workbook

from .core import GuardError, HEADERS, Person, clean_name, money

MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 1000
MAX_ROWS = 20000
MAX_SCANNED_CELLS = 1000000


def _blank(value) -> bool:
    return value is None or isinstance(value, str) and not value.strip()


def _read_package(path: Path) -> bytes:
    # Validate and parse one immutable snapshot, even if Excel saves during import.
    with path.open("rb") as source:
        data = source.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise GuardError("表格超过 10 MB，请只保留要导入的人员与额度。")
    with ZipFile(BytesIO(data)) as archive:
        entries = archive.infolist()
        if (len(entries) > MAX_ARCHIVE_ENTRIES
                or sum(entry.file_size for entry in entries) > MAX_EXPANDED_BYTES):
            raise GuardError("表格解压内容过大，请只保留要导入的人员与额度。")
        if len({entry.filename for entry in entries}) != len(entries):
            raise GuardError("表格内部存在重复文件，请用 Excel/WPS 另存为 .xlsx 后重试。")
    return data


def _select_sheet(workbook):
    candidates = []
    malformed_header = False
    for sheet in workbook.worksheets:
        # Editors can retain huge used-range hints after clearing cells.
        sheet.reset_dimensions()
        with closing(sheet.iter_rows(max_row=1)) as rows:
            header = next(rows, ())
        labels = [cell.value.strip() if isinstance(cell.value, str) else cell.value
                  for cell in header]
        counts = [labels.count(label) for label in HEADERS]
        if counts == [1, 1]:
            candidates.append((sheet, tuple(labels.index(label) for label in HEADERS)))
        elif any(counts):
            malformed_header = True
    if len(candidates) > 1:
        raise GuardError("多个工作表都含有人员额度表头，请只保留一份待导入表格。")
    if not candidates:
        prefix = "第 1 行表头重复或缺列。" if malformed_header else "未找到人员额度表头。"
        raise GuardError(prefix + "首行必须包含“API名/人员名”和“个人金额（元）”两列。")
    return candidates[0]


def _cell_value(row, index):
    return row[index].value if index < len(row) else None


def _read_rows(sheet, cached_sheet, columns, *, allow_unassigned=False) -> list[Person]:
    cached_sheet.reset_dimensions()
    people, name_rows = [], {}
    scanned_cells = 0
    with closing(sheet.iter_rows()) as rows, closing(cached_sheet.iter_rows()) as cached_rows:
        for number, (row, cached_row) in enumerate(zip_longest(rows, cached_rows, fillvalue=()), 1):
            scanned_cells += max(len(row), len(cached_row))
            if number > MAX_ROWS or scanned_cells > MAX_SCANNED_CELLS:
                raise GuardError("表格范围过大，最多 20000 行。请清除多余的空白格式后重试。")
            if any(not _blank(cell.value) for i, cell in enumerate(row) if i not in columns):
                raise GuardError(f"第 {number} 行：规定的两列之外还有内容，请移到其他工作表。")
            if number == 1 or all(_blank(cell.value) for cell in row):
                continue
            name_value, amount_value = (_cell_value(row, i) for i in columns)
            try:
                for index in columns:
                    cell = row[index] if index < len(row) else None
                    if cell is not None and cell.data_type == "e":
                        raise GuardError("包含 Excel 错误值，请修正后重新导入。")
                    if cell is not None and cell.data_type == "f":
                        if index == columns[0]:
                            raise GuardError("API 名称请粘贴为文本，不要使用公式。")
                        cached = cached_row[index] if index < len(cached_row) else None
                        if (cached is None or cached.data_type != "n"
                                or cached.value is None or isinstance(cached.value, bool)):
                            raise GuardError("金额公式没有可读取的数值结果，请在 Excel/WPS 中重新计算并保存，或粘贴为数值。")
                        amount_value = cached.value
                name = clean_name(name_value)
                amount = money(amount_value, allow_skip=True)
                if not name and amount != -1 and not allow_unassigned:
                    raise GuardError("金额为 0 或正数时必须填写 API 名称。")
                if name and name in name_rows:
                    raise GuardError(f"API 名称与第 {name_rows[name]} 行重复，请合并或更正。")
                if name:
                    name_rows[name] = number
                people.append(Person(name, amount))
            except GuardError as exc:
                raise GuardError(f"第 {number} 行：{exc}") from exc
    return people


def read_people_workbook(path: str | Path, *, allow_unassigned=False) -> list[Person]:
    """Read the uniquely headed quota sheet without changing the input file.

    Names, column order, styles and numeric display formats do not matter. Blank
    rows and extra columns with formatting only are ignored. Formulas are never
    executed; only saved numeric caches may supply amounts. Missing results are
    errors, never zero. The caller separately enforces its configured person count.
    External imports remain strict by default. The internal canonical table may
    explicitly allow unassigned rows whose validated amounts persist as defaults.
    """
    try:
        if type(allow_unassigned) is not bool:
            raise GuardError("待填写行选项必须是明确的开关。")
        source = Path(path)
        if source.suffix.lower() != ".xlsx":
            raise GuardError("请选择 .xlsx 文件。旧版 .xls 请先用 Excel/WPS 另存为 .xlsx。")
        data = _read_package(source)
        with ExitStack() as stack:
            workbook = load_workbook(BytesIO(data), read_only=True, data_only=False, keep_links=False)
            stack.callback(workbook.close)
            sheet, columns = _select_sheet(workbook)
            cached = load_workbook(BytesIO(data), read_only=True, data_only=True, keep_links=False)
            stack.callback(cached.close)
            return _read_rows(sheet, cached[sheet.title], columns, allow_unassigned=allow_unassigned)
    except GuardError:
        raise
    except PermissionError as exc:
        raise GuardError("表格暂时无法读取，请先在 Excel/WPS 中保存并关闭，再重试。") from exc
    except Exception as exc:
        raise GuardError("表格无法读取，请确认是完整保存的 .xlsx 文件。") from exc
