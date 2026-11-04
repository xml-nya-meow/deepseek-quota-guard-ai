"""Generate a blank personnel template; never edit a live personnel workbook."""
from __future__ import annotations

from copy import copy
from decimal import Decimal
import os
from pathlib import Path
import tempfile

from openpyxl import load_workbook

from .core import DataStore, GuardError, HEADERS, money


def write_personnel_template(target: Path, source_template: Path, amount,
                             people_count: int = 14) -> None:
    """Preserve template styling while prefilling new blank rows with exact text.

    A nonnegative template remains a draft until names are filled. Its blank
    rows deliberately do not pass the normal personnel-import validation.
    """
    selected_amount = money(amount, allow_skip=True)
    if selected_amount == 0:
        selected_amount = Decimal(0)
    elif selected_amount == -1:
        selected_amount = Decimal(-1)
    if type(people_count) is not int or not 1 <= people_count <= 10000:
        raise GuardError('模板人数必须是 1 到 10000 的整数。')
    target, source_template = Path(target).resolve(), Path(source_template).resolve()
    if target == source_template:
        raise GuardError('不能覆盖程序自带的空白模板，请另选文件名。')
    if target.suffix.lower() != '.xlsx':
        raise GuardError('模板请保存为 .xlsx 文件。')
    if (target.name.casefold() == DataStore.WORKBOOK.casefold()
            and DataStore(target.parent).has_history()):
        raise GuardError('模板不能覆盖当前使用的人员额度表，请另选文件名。')

    book = load_workbook(source_template, keep_links=False)
    temporary = None
    try:
        sheets = [sheet for sheet in book.worksheets
                  if tuple(sheet.cell(1, col).value for col in (1, 2)) == HEADERS]
        if len(sheets) != 1:
            raise GuardError('程序自带模板的人员表头不完整，请恢复正确模板。')
        sheet = sheets[0]
        styles = [copy(sheet.cell(2, col)._style) for col in (1, 2)]
        row_height = sheet.row_dimensions[2].height
        if sheet.max_row > 1:
            sheet.delete_rows(2, sheet.max_row - 1)
        for row in range(2, people_count + 2):
            sheet.cell(row, 1, None)
            sheet.cell(row, 2, format(selected_amount, 'f'))
            for col in (1, 2):
                sheet.cell(row, col)._style = copy(styles[col - 1])
            sheet.row_dimensions[row].height = row_height
        for validation in sheet.data_validations.dataValidation:
            validation.sqref = f'B2:B{people_count + 1}'

        instructions = book['填写说明'] if '填写说明' in book.sheetnames else book.create_sheet('填写说明')
        instructions.append(['本次预填：' + format(selected_amount, 'f') + ' 元；只影响本模板，不改变已有人员额度。'])
        instructions.append(['金额为 0 或正数的行必须填写名称；不用的行请改为 -1 或删除后再导入。'])
        fd, temporary = tempfile.mkstemp(prefix='.quota-template-', suffix='.xlsx', dir=target.parent)
        os.close(fd)
        book.save(temporary)
        os.replace(temporary, target)
    finally:
        book.close()
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)
