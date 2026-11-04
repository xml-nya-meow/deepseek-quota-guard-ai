"""Inspect official CSV exports without guessing amount/price cost semantics."""
import csv
from datetime import date
from hashlib import sha256
from io import StringIO
from pathlib import Path, PurePosixPath
import re
from zipfile import ZipFile
from .core import GuardError

HEADERS = {
    'amount': 'user_id,start_time_iso,end_time_iso,model,name,api_key_name,api_key,type,price,amount'.split(','),
    'cost': 'user_id,start_time_iso,end_time_iso,model,name,wallet_type,cost,currency'.split(','),
}
LIMIT = 20_000_000
PATTERN = re.compile(r'(amount|cost)-(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})\.csv$')


def inspect_export(path, start_date):
    path = Path(path)
    blobs = {}
    if path.suffix.lower() == '.zip':
        with ZipFile(path) as archive:
            infos = archive.infolist()
            if len(infos) > 20 or sum(x.file_size for x in infos) > LIMIT:
                raise GuardError('导出包过大，未读取。')
            for info in infos:
                name = PurePosixPath(info.filename)
                if name.is_absolute() or '..' in name.parts or '\\' in info.filename:
                    raise GuardError('导出包包含不安全路径。')
                if PATTERN.fullmatch(name.name):
                    if name.name in blobs:
                        raise GuardError('导出包有重复文件名。')
                    blobs[name.name] = archive.read(info)
    else:
        folder = path if path.is_dir() else path.parent
        for item in folder.iterdir():
            if PATTERN.fullmatch(item.name) and item.is_file():
                if item.stat().st_size > LIMIT:
                    raise GuardError('账单文件过大。')
                blobs[item.name] = item.read_bytes()
    if len(blobs) != 2 or sum(map(len, blobs.values())) > LIMIT:
        raise GuardError('请选择同一日期范围的一份 amount 和一份 cost 导出文件，或包含它们的 ZIP。')
    windows, kinds, counts = set(), set(), {}
    digest = sha256()
    for name, content in sorted(blobs.items()):
        kind, begin, end = PATTERN.fullmatch(name).groups()
        date.fromisoformat(begin); date.fromisoformat(end)
        if begin > end:
            raise GuardError('账单日期范围无效。')
        windows.add((begin, end)); kinds.add(kind)
        digest.update(name.encode()); digest.update(content)
        try:
            rows = csv.reader(StringIO(content.decode('utf-8-sig')))
            if next(rows) != HEADERS[kind]:
                raise GuardError('账单表头与已核查格式不符，请核对新版导出。')
            count = 0
            for row in rows:
                if not row:
                    continue
                if len(row) != len(HEADERS[kind]):
                    raise GuardError('账单行字段数不匹配。')
                count += 1
            counts[kind] = count
        except (UnicodeError, StopIteration, csv.Error):
            raise GuardError('账单编码或 CSV 格式不正确。') from None
    if len(windows) != 1 or kinds != {'amount', 'cost'}:
        raise GuardError('amount 与 cost 日期范围不一致。')
    begin, end = next(iter(windows))
    if begin > date.fromisoformat(start_date).isoformat() or end < start_date:
        raise GuardError('导出未覆盖预算统计起点。')
    empty = not any(counts.values())
    return {'status': 'empty' if empty else 'needs_calibration',
            'message': ('导出格式正确，但只有表头，没有消费记录。不把空文件当作每人已核实的零消费。' if empty else
                        '已读取官方文件。amount 是用量维度，cost 缺少 Key 字段；本版不猜测人员扣费，待与平台逐 Key 费用核对。'),
            'start_date': begin, 'end_date': end, 'amount_rows': counts['amount'],
            'cost_rows': counts['cost'], 'source_id': digest.hexdigest()}
