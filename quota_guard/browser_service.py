"""Visible, isolated platform session with a read-only console HTTP adapter.

The optional cost decoder is based on community-observed platform responses,
not a public API contract. Unknown shapes fail closed. A deletion requires a
durable UI-side intent and a fresh, single-use authorization before its click.
"""
from concurrent.futures import Future
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from queue import Queue, Empty
from threading import Thread
from urllib.parse import urlparse, parse_qs
import json
import os
import re
import sys
import time
import uuid

from .core import AccountMismatch, GuardError, _identifier, clean_name
from .browser_startup import (BrowserStartupError, BrowserTempPathError,
                              check_browser_loopback, is_temp_path_too_long)
from .cost_parser import NoUsageData, parse_cost_response
from .page_notices import (dismiss_optional_notice, install_optional_notice_handler,
                           raise_optional_notice_error)
from .platform_http import ConsoleCostReader, CostReadError, cost_metadata, is_cost_endpoint
from .revocation_permit import DeletePermit, KeyInspectionError, RevocationNotSubmitted
from .operator_runtime import OperatorRuntime, OperatorError
from .web_operator import AIConsoleCostReader, WebOperator, WebOperatorError

TZ = timezone(timedelta(hours=8))
BASE = 'https://platform.deepseek.com'
COST_PATH = '/api/v0/usage/by_api_key/cost'

# Observed platform trash icon. Never fall back to an unnamed button index.
TRASH_PATH = 'M6.5 7v4a.5.5 0 0 0 1 0V7a.5.5 0 0 0-1 0zM9 6.5a.5.5 0 0 1 .5.5v4a.5.5 0 0 1-1 0V7a.5.5 0 0 1 .5-.5zM10 4h3a.5.5 0 0 1 0 1h-.553l-.752 6.776A2.5 2.5 0 0 1 9.21 14H6.79a2.5 2.5 0 0 1-2.485-2.224L3.552 5H3a.5.5 0 0 1 0-1h3a2 2 0 1 1 4 0zM8 3a1 1 0 0 0-1 1h2a1 1 0 0 0-1-1zM4.559 5l.74 6.666A1.5 1.5 0 0 0 6.79 13h2.42a1.5 1.5 0 0 0 1.49-1.334L11.442 5H4.56z'
# Observed read-only on 2026-10-31: a DIV role=button with a single path.
CURRENT_TRASH_PATH = 'M14.4782 4.84067L14.2138 10.1152C14.1102 12.1872 14.067 13.0115 13.3866 13.9607C13.1044 14.3546 12.7498 14.6912 12.3424 14.9535C11.8239 15.2872 11.2415 15.4316 10.5585 15.4998C9.88727 15.5668 9.04946 15.5656 7.99998 15.5656C6.95051 15.5656 6.1127 15.5668 5.44142 15.4998C4.75851 15.4316 4.17602 15.2872 3.65753 14.9535C3.25012 14.6912 2.89559 14.3546 2.61332 13.9607C1.93296 13.0115 1.88979 12.1872 1.78619 10.1152L1.52179 4.84067L2.89006 4.77277L3.15343 10.0463C3.26221 12.2218 3.32452 12.6015 3.72646 13.1624C3.90825 13.4161 4.13686 13.6334 4.39927 13.8023C4.66204 13.9714 5.00263 14.0792 5.57825 14.1367C6.16562 14.1953 6.92298 14.1963 7.99998 14.1963C9.07699 14.1963 9.83434 14.1953 10.4217 14.1367C10.9973 14.0792 11.3379 13.9714 11.6007 13.8023C11.8631 13.6334 12.0917 13.4161 12.2735 13.1624C12.6755 12.6015 12.7378 12.2218 12.8465 10.0463L13.1099 4.77277L14.4782 4.84067ZM5.43011 6.22849H6.7994V11.3909H5.43011V6.22849ZM9.20056 6.22849H10.5699V11.3909H9.20056V6.22849ZM8.53597 0.434431C9.17976 0.434431 9.6522 0.426926 10.0966 0.571258C10.2357 0.616451 10.3717 0.672554 10.502 0.738948C10.9182 0.951107 11.2464 1.29099 11.7015 1.74612L12.4978 2.54136H15.3742V3.91169H0.625732V2.54136H3.50218L4.29845 1.74612C4.75358 1.29099 5.08174 0.951107 5.49801 0.738948C5.62831 0.672554 5.76425 0.616451 5.90334 0.571258C6.34776 0.426926 6.82021 0.434431 7.46399 0.434431H8.53597ZM7.46399 1.80476C6.73208 1.80476 6.51641 1.81187 6.32617 1.87369C6.25545 1.89667 6.18668 1.92533 6.12041 1.95907C5.96398 2.03878 5.82348 2.16253 5.44142 2.54136H10.5585C10.1765 2.16253 10.036 2.03878 9.87955 1.95907C9.81329 1.92533 9.74452 1.89667 9.6738 1.87369C9.48356 1.81187 9.26789 1.80476 8.53597 1.80476H7.46399Z'
TRASH_SIGNATURES = [
    {'path': TRASH_PATH, 'viewboxes': [None, '0 0 16 16']},
    {'path': CURRENT_TRASH_PATH, 'viewboxes': ['0 0 16 16']},
]

# Reuse this exact check at discovery and in each atomic check/click task.
# Unknown icons, extra shapes, nested controls and disabled/hidden controls fail
# closed; never infer deletion from control order, CSS class or icon similarity.
_TRASH_BUTTON_JS = r"""(node, signatures) => {
    if (!node.isConnected || !node.matches('button, [role="button"]') ||
        node.matches(':disabled') || node.closest('[inert], [aria-disabled="true"]') ||
        !node.getClientRects().length || getComputedStyle(node).visibility !== 'visible' ||
        node.querySelector('button, [role="button"]')) return false;
    const svgs = node.querySelectorAll('svg');
    if (svgs.length !== 1) return false;
    const svg = svgs[0];
    const paths = svg.querySelectorAll('path');
    if (paths.length !== 1 || svg.querySelector('use, rect, circle, ellipse, polygon, polyline, line, text, image'))
        return false;
    return signatures.some(signature => signature.path === paths[0].getAttribute('d') &&
        signature.viewboxes.includes(svg.getAttribute('viewBox')));
}"""
DELETE_WARNING = '该 API key 将立即被禁用。使用此密钥发出的 API 请求将被拒绝，这可能会导致仍然依赖它的任何系统崩溃。 一旦删除，你将无法再查看或修改此 API key。'

UUID_PATTERN = re.compile(r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}')


def identity_read_args(value):
    """Copy the narrow read-only identity request at both process boundaries."""
    if not isinstance(value, dict) or set(value) - {'account_id', 'known_keys'}:
        raise GuardError('只读 Key 核验参数无效。')
    account = value.get('account_id', '')
    known = value.get('known_keys', ())
    if (not isinstance(account, str) or (account and _identifier(account) != account)
            or not isinstance(known, (list, tuple, set, frozenset))
            or any(not isinstance(key, str) or not UUID_PATTERN.fullmatch(key) for key in known)):
        raise GuardError('只读 Key 核验身份参数无效。')
    if len({key.lower() for key in known}) != len(known):
        raise GuardError('只读 Key 核验包含重复身份。')
    return {'account_id': account, 'known_keys': tuple(sorted(known))}


# Read only the name and explicitly identified Tracking ID field. In particular,
# never collect the contents or copy label of the separate secret Key column.
_KEY_ROW_JS = r"""(row, columns) => {
    const cells = Array.from(row.cells);
    const field = cells[columns.tracking];
    if (cells.length !== columns.count || cells.some(cell => cell.colSpan !== 1 || cell.rowSpan !== 1) || !field)
        return {invalid: true};
    const buttons = Array.from(field.querySelectorAll('button[aria-label], [role="button"][aria-label]'));
    return {name: cells[columns.name].textContent.trim(), field: {
        text: field.textContent.trim(),
        titles: [field, ...field.querySelectorAll('[title]')].map(node => node.getAttribute('title')).filter(Boolean),
        copy_labels: buttons.map(node => node.getAttribute('aria-label')).filter(label => /^点击复制\s*[:：]/.test(label))
    }};
}"""

_KEY_TABLE_JS = r"""() => {
    const readRow = """ + _KEY_ROW_JS + r""";
    const visible = node => node.getClientRects().length && getComputedStyle(node).visibility !== 'hidden';
    const norm = value => value.replace(/\s+/g, ' ').trim().toLowerCase();
    const loading = Array.from(document.querySelectorAll(
        '[aria-busy="true"], [role="progressbar"], [data-loading="true"], .ds-loading, .ds-spinner, .ds-skeleton'
    )).some(visible);
    const paginated = Array.from(document.querySelectorAll(
        '.ds-pagination, [aria-label*="pagination" i], [aria-label*="分页"], [aria-label*="next page" i], [data-virtualized], [data-virtual-list]'
    )).some(visible);
    if (paginated) return {error: '分页或虚拟列表尚不支持，不能确认完整 Key 列表。', loading};
    if (Array.from(document.querySelectorAll('input[type="search"]')).some(node => visible(node) && node.value.trim()))
        return {error: '请清除 Key 列表筛选后再读取。', loading};
    const tables = Array.from(document.querySelectorAll('table'));
    const candidates = [];
    tables.forEach((table, index) => {
        if (!visible(table)) return;
        const heads = Array.from(table.querySelectorAll('thead th')).map(node => norm(node.textContent));
        if (heads.some(text => text === '名称' || text === 'name') && heads.includes('tracking id'))
            candidates.push({table, index, heads});
    });
    if (candidates.length !== 1) return {error: '无法唯一识别名称与 Tracking ID 表头，未读取 Key 列。', loading};
    const {table, index, heads} = candidates[0];
    const names = heads.map((text, i) => text === '名称' || text === 'name' ? i : -1).filter(i => i >= 0);
    const ids = heads.map((text, i) => text === 'tracking id' ? i : -1).filter(i => i >= 0);
    if (names.length !== 1 || ids.length !== 1 || heads.filter(text => text === 'key').length !== 1 ||
        table.querySelectorAll('thead tr').length !== 1 || table.tBodies.length !== 1 ||
        Array.from(table.querySelectorAll('thead th')).some(cell => cell.colSpan !== 1 || cell.rowSpan !== 1))
        return {error: 'Key 列表表头重复或结构变化，已停止读取。', loading};
    if (table.hasAttribute('aria-rowcount') || table.querySelector('[aria-rowindex], [data-index]'))
        return {error: '无法确认分页或虚拟行的完整范围。', loading};
    const columns = {name: names[0], tracking: ids[0], count: heads.length};
    const body = table.tBodies[0];
    let rows = Array.from(body.rows);
    const placeholder = rows.length === 1 && rows[0].cells.length === 1 && rows[0].cells[0].colSpan === heads.length;
    const emptyText = placeholder ? norm(rows[0].cells[0].textContent).replace(/\s+/g, '') : '';
    const empty = ['暂无数据', '暂无apikeys', '暂无apikey', 'nodata', 'noapikeys'].includes(emptyText);
    if (empty && placeholder)
        rows = [];
    if (rows.length > 200) return {error: 'Key 行数超出当前已支持的完整列表范围。', loading};
    return {table_index: index, headers: heads, columns, loading, empty,
        rows: rows.map(row => readRow(row, columns))};
}"""


def parse_tracking_field(field):
    """Decode one confirmed Tracking ID cell without guessing an abbreviated ID."""
    values = []
    displays = [str(field.get('text', '')).strip()]
    if not displays[0]:
        raise GuardError('Tracking ID 尚未显示完整字段，不能使用加载中的空白行。')
    for label in field.get('copy_labels', []):
        match = re.fullmatch(r'点击复制\s*[:：]\s*(.+)', label.strip())
        if not match:
            raise GuardError('Tracking ID 复制标签格式无法核验。')
        values.append(match.group(1).strip())
    for title in field.get('titles', []):
        values.append(title.strip())
    for display in displays:
        if UUID_PATTERN.fullmatch(display):
            values.append(display)
    if not values or any(not UUID_PATTERN.fullmatch(value) for value in values):
        raise GuardError('Tracking ID 缺少完整 UUID；不会使用掩码 Key 或猜补省略值。')
    canonical = {value.lower() for value in values}
    if len(canonical) != 1:
        raise GuardError('Tracking ID 的完整值来源互相冲突，已停止。')
    # Preserve the platform value used by existing exact ledger bindings; this
    # parser must not silently migrate identifiers already stored in a ledger.
    tracking = values[0]
    for display in displays:
        if not display or UUID_PATTERN.fullmatch(display):
            continue
        shortened = re.fullmatch(r'([0-9a-fA-F-]+)\s*(?:…|\.{3})\s*([0-9a-fA-F-]+)', display)
        if not shortened or len(shortened[1]) + len(shortened[2]) >= len(tracking) or not (
            tracking.lower().startswith(shortened[1].lower()) and tracking.lower().endswith(shortened[2].lower())
        ):
            raise GuardError('Tracking ID 显示值与完整 UUID 不一致，已停止。')
    return tracking


def parse_key_table(snapshot):
    if snapshot.get('error'):
        raise GuardError(snapshot['error'])
    if snapshot.get('loading'):
        raise GuardError('Key 列表仍在加载，不能应用不完整列表。')
    rows = snapshot.get('rows', [])
    if not rows and not snapshot.get('empty'):
        raise GuardError('Key 列表尚未出现完整行或明确空状态。')
    keys = []
    for number, row in enumerate(rows, 1):
        if row.get('invalid'):
            raise GuardError(f'Key 列表第 {number} 行尚不完整或结构变化。')
        name = clean_name(row.get('name'))
        if not name:
            raise GuardError(f'Key 列表第 {number} 行缺少名称。')
        keys.append({'name': name, 'tracking_id': parse_tracking_field(row['field'])})
    if len({key['tracking_id'].lower() for key in keys}) != len(keys):
        raise GuardError('Key 列表存在重复 Tracking ID，已停止。')
    return keys


def cost_query_matches(url, start_date, end_date):
    """Only accept the page request for the explicitly selected complete range."""
    parsed = urlparse(url)
    if not is_cost_endpoint(url):
        return False
    query = parse_qs(parsed.query)
    begin = int(datetime.fromisoformat(start_date).replace(tzinfo=TZ).timestamp())
    finish = int((datetime.fromisoformat(end_date).replace(tzinfo=TZ) + timedelta(days=1)).timestamp())
    return (query.get('start') == [str(begin)] and query.get('end') == [str(finish)]
            and query.get('tz') == ['28800'])


def cost_identity_coverage(keys, totals):
    """Read-only set diagnostics, never normalize identifiers used for billing.

    Case-folding is only a comparison of complete UUID spellings. Opaque IDs
    still require exact equality, and these counts cannot authorize an action.
    """
    platform_ids = {key['tracking_id'] for key in keys}
    fee_ids = set(totals)
    matched = fee_ids.intersection(platform_ids)
    platform_uuids = {key.lower() for key in platform_ids if UUID_PATTERN.fullmatch(key)}
    case_only = sum(key not in platform_ids and bool(UUID_PATTERN.fullmatch(key))
                    and key.lower() in platform_uuids for key in fee_ids)
    return {
        'platform_key_count': len(platform_ids),
        'fee_tracking_count': len(fee_ids),
        'matched_platform_count': len(matched),
        'unmatched_fee_count': len(fee_ids - platform_ids),
        'missing_platform_count': len(platform_ids - fee_ids),
        'uuid_casefold_match_count': len(matched) + case_only,
        'case_only_match_count': case_only,
    }


class BrowserService:
    """All Playwright objects stay on one worker thread; UI receives Futures."""
    def __init__(self, diagnostics_dir=None):
        from .diagnostics import DiagnosticLog
        self.diagnostics = DiagnosticLog(diagnostics_dir) if diagnostics_dir else None
        self.progress = {'stage': '等待操作', 'started_at': time.monotonic()}
        self.queue = Queue()
        self.closed = False
        self.thread = Thread(target=self._worker, name='platform-browser', daemon=True)
        self.thread.start()

    def submit(self, action, **kwargs):
        future = Future()
        if self.closed:
            future.set_exception(GuardError('浏览器服务已关闭。'))
        else:
            self.queue.put((action, kwargs, future))
        return future

    def close(self):
        self.closed = True
        self.queue.put(None)
        self.thread.join(timeout=45)

    def _stage(self, stage, **fields):
        started = getattr(self, 'progress', {}).get('started_at', time.monotonic())
        self.progress = {'stage': stage, 'started_at': started}
        self._log('browser_stage', stage=stage, **fields)

    def _log(self, event, **fields):
        logger = getattr(self, 'diagnostics', None)
        if logger is not None:
            operation = getattr(self, '_diagnostic_operation_id', None)
            if operation:
                fields.setdefault('operation_id', operation)
            logger.emit(event, **fields)

    def _worker(self):
        self.pw = self.context = self.page = self.browser = None
        self.anchor = None
        self.known_keys = set()
        try:
            while True:
                try:
                    task = self.queue.get(timeout=.1)
                except Empty:
                    if self.page is not None:
                        try:
                            self.page.wait_for_timeout(30)
                        except Exception:
                            pass
                    continue
                if task is None:
                    break
                action, args, future = task
                if not future.set_running_or_notify_cancel():
                    continue
                operation = args.get('operation_id') if action == 'collect' else None
                self._diagnostic_operation_id = (operation if isinstance(operation, str)
                                                 and re.fullmatch('[0-9a-f]{32}', operation) else None)
                self.progress = {'stage': '正在开始操作', 'started_at': time.monotonic()}
                self._log('browser_action_started', action=action, pid=os.getpid(), args=args)
                entered_action = False
                try:
                    if action not in ('login', 'collect', 'read_keys', 'prepare_revoke', 'inspect_revoke', 'verify_revoke', 'revoke', 'selftest'):
                        raise GuardError('不支持的浏览器动作。')
                    if action != 'login':
                        self._ensure_browser()
                    entered_action = True
                    result = getattr(self, '_' + action)(**args)
                    if self.page is not None:
                        raise_optional_notice_error(self.page)
                    self._log('browser_action_finished', action=action, success=True, result=result)
                    future.set_result(result)
                except Exception as exc:
                    if action == 'read_keys':
                        self.anchor = None
                        self.known_keys = set()
                        self._last_key_snapshot = None
                    if action == 'revoke' and not entered_action:
                        exc = RevocationNotSubmitted(reason_code='KEY_SESSION_UNAVAILABLE')
                    if action == 'inspect_revoke':
                        exc = KeyInspectionError(reason_code=(
                            'KEY_SESSION_UNAVAILABLE' if not entered_action else
                            exc.reason_code if isinstance(exc, KeyInspectionError) else 'PREPARATION_FAILED'))
                    try:
                        if self.page is not None:
                            raise_optional_notice_error(self.page)
                    except GuardError as notice_error:
                        if action == 'inspect_revoke':
                            exc = KeyInspectionError(reason_code='KEY_NOTICE_REQUIRES_USER')
                        else:
                            exc = (RevocationNotSubmitted(reason_code='KEY_NOTICE_REQUIRES_USER')
                                   if isinstance(exc, RevocationNotSubmitted) else notice_error)
                    # Playwright exception text may include page values. Never log it.
                    self._log('browser_action_finished', action=action, success=False,
                              error_type=type(exc).__name__)
                    future.set_exception(exc if isinstance(exc, GuardError) else GuardError(
                        '平台操作未完成，请检查网络、登录状态或页面是否改版。未确认任何删除成功。'))
                finally:
                    self._diagnostic_operation_id = None
        finally:
            self._dispose_browser()

    def _dispose_browser(self):
        # Run on the Playwright worker thread. Invalidate the browser session
        # immediately, but keep a model/reader whose cleanup was not confirmed.
        # Otherwise a normal re-login could lose the last owned-process handle
        # and launch a replacement while the previous model is still running.
        context, browser, pw = (getattr(self, name, None) for name in ('context', 'browser', 'pw'))
        self.context = self.browser = self.pw = self.page = None
        self.anchor = None
        self.known_keys = set()
        self._last_key_snapshot = None
        self._login_waiting = True
        reader = getattr(self, '_cost_reader', None)
        self._cost_reader_identity = None
        pending = False
        if reader is not None:
            try:
                reader.clear()
            except Exception:
                pending = True
            else:
                self._cost_reader = None
        runtime = getattr(self, '_operator_runtime', None)
        if runtime is not None:
            try:
                runtime.close()
            except Exception:
                pending = True
            else:
                self._operator_runtime = None
        self._operator_cleanup_pending = pending
        for resource, method in ((context, 'close'), (browser, 'close'), (pw, 'stop')):
            if resource is not None:
                try:
                    getattr(resource, method)()
                except Exception:
                    pass
        if pending:
            # All other cleanup steps above have run. No raw close exception
            # can cross the UI boundary, and no replacement is started.
            raise OperatorError('AI_CLEANUP_PENDING') from None

    def _ensure_browser(self):
        if (not getattr(self, '_operator_cleanup_pending', False)
                and self.context and self.browser and self.page):
            try:
                if self.browser.is_connected() and not self.page.is_closed():
                    return
            except Exception:
                pass
        self._dispose_browser()
        root = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1]))
        bundled = root / 'browser-runtime'
        if not bundled.is_dir() and not getattr(sys, 'frozen', False):
            bundled = root.parent / 'browser-runtime'  # Existing local work copy.
        if bundled.is_dir():
            os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(bundled)
        self._stage('正在检查本机浏览器通信', phase='loopback')
        try:
            check_browser_loopback()
        except BrowserStartupError as exc:
            self._log('browser_startup_failed', phase='loopback',
                      reason_code=exc.reason_code, error_code=exc.error_code)
            raise
        from playwright.sync_api import sync_playwright
        self._stage('正在初始化浏览器驱动', phase='driver')
        self.pw = sync_playwright().start()
        self._stage('正在启动独立浏览器', phase='chromium')
        # Ephemeral context: no password, token or login state saved to a file.
        try:
            self.browser = self.pw.chromium.launch(headless=False, timeout=30000)
        except Exception as exc:
            if not is_temp_path_too_long(exc):
                raise
            safe = BrowserTempPathError()
            self._log('browser_startup_failed', phase='chromium',
                      reason_code=safe.reason_code, error_code=safe.error_code)
            raise safe from None
        self.context = self.browser.new_context(locale='zh-CN', timezone_id='Asia/Shanghai',
                                               accept_downloads=True, service_workers='block')
        self.page = self.context.new_page()
        self.page.set_default_timeout(12000)
        install_optional_notice_handler(self.page)

    def _get_operator_runtime(self, *, deadline):
        if getattr(self, '_operator_cleanup_pending', False):
            raise OperatorError('AI_CLEANUP_PENDING')
        runtime = getattr(self, '_operator_runtime', None)
        if runtime is None:
            runtime = self._operator_runtime = OperatorRuntime(emit=self._log)
        self._stage('正在启动本地 4B 操作模型', phase='operator_start')
        runtime.start(deadline=deadline)
        return runtime

    def _create_cost_reader(self, *, deadline):
        return AIConsoleCostReader(self.context, page=self.page,
            runtime=self._get_operator_runtime(deadline=deadline),
            emit=self._log, stage=self._stage, deadline=deadline)

    def _login(self):
        self._stage('正在重建登录会话')
        self._dispose_browser()
        self._ensure_browser()
        self.page.goto(BASE + '/api_keys', wait_until='domcontentloaded')
        self._stage('等待你在浏览器完成登录')
        return {'message': '已重建独立浏览器，请重新登录；原账本保留，需重新核对账户和费用。'}

    def _check_login_ready(self):
        """Never navigate away from an unfinished interactive login."""
        if not getattr(self, '_login_waiting', False):
            return
        self._stage('正在检查登录是否完成')
        dismiss_optional_notice(self.page)
        parsed = urlparse(self.page.url)
        heading = self.page.get_by_role('heading', name='API keys', exact=True)
        if (parsed.scheme != 'https' or parsed.hostname != 'platform.deepseek.com'
                or parsed.path != '/api_keys' or heading.count() != 1 or not heading.is_visible()):
            raise GuardError('请先在已打开的浏览器完成登录，再点“立即读取费用”；当前登录页面已保留。')
        self._login_waiting = False

    def _selftest(self):
        """Packaging check only: start the bundled browser without a network call."""
        self.page.goto('about:blank')
        return {'browser_started': True, 'version': self.browser.version}

    def _keys(self):
        self._stage('正在核验完整 Key 列表')
        page = self.page
        install_optional_notice_handler(page)
        pending = set()
        failed = False

        def started(request):
            if request.resource_type in ('fetch', 'xhr') and urlparse(request.url).hostname == 'platform.deepseek.com':
                pending.add(request)

        def finished(request):
            nonlocal failed
            if request in pending:
                response = request.response()
                failed = failed or response is None or response.status >= 400
                pending.discard(request)

        def rejected(request):
            nonlocal failed
            if request in pending:
                failed = True
                pending.discard(request)

        callbacks = (('request', started), ('requestfinished', finished), ('requestfailed', rejected))
        for event, callback in callbacks:
            page.on(event, callback)
        self._last_key_snapshot = None
        try:
            page.goto(BASE + '/api_keys', wait_until='domcontentloaded')
            page.get_by_role('heading', name='API keys', exact=True).wait_for()
            deadline = time.monotonic() + getattr(self, 'key_table_timeout', 12)
            stable_for = getattr(self, 'key_table_stable_seconds', 1.0)
            previous, stable_since = None, None
            while time.monotonic() < deadline:
                dismiss_optional_notice(page)
                if failed:
                    raise GuardError('Key 列表加载请求失败，不能确认完整列表。')
                snapshot = page.evaluate(_KEY_TABLE_JS)
                if pending or snapshot.get('loading'):
                    previous, stable_since = None, None
                else:
                    # The observed platform renders all (at most 200) keys in a
                    # native, unpaginated table. Stability supplements completed
                    # same-origin requests and explicit non-loading state; it
                    # cannot establish completeness for an unmarked future UI.
                    try:
                        keys = parse_key_table(snapshot)
                    except GuardError:
                        if snapshot.get('error'):
                            raise
                        previous, stable_since = None, None
                    else:
                        signature = json.dumps(snapshot, sort_keys=True, ensure_ascii=False)
                        now = time.monotonic()
                        if signature != previous:
                            previous, stable_since = signature, now
                        elif stable_since is not None and now - stable_since >= stable_for:
                            self._last_key_snapshot = snapshot
                            self._log('key_list_verified', key_count=len(keys))
                            return keys
                page.wait_for_timeout(100)
            raise GuardError('Key 列表未完成稳定加载或含不完整 Tracking ID；本次未应用，也不据此确认删除。')
        finally:
            for event, callback in callbacks:
                page.remove_listener(event, callback)

    def _key_row(self, name, tracking_id):
        snapshot = self.page.evaluate(_KEY_TABLE_JS)
        keys = parse_key_table(snapshot)
        previous = getattr(self, '_last_key_snapshot', None)
        # The table may have been sorted again. Verify the complete identity
        # set, then locate and recheck the actual row in the current snapshot.
        if previous is None or key_fingerprint(keys) != key_fingerprint(parse_key_table(previous)):
            raise GuardError('定位期间 Key 列表发生变化，已停止。')
        matches = [index for index, key in enumerate(keys) if key == {'name': name, 'tracking_id': tracking_id}]
        if len(matches) != 1:
            raise GuardError('不能唯一定位名称与 Tracking ID 完全一致的 Key。')
        row = self.page.locator('table').nth(snapshot['table_index']).locator('tbody > tr').nth(matches[0]).element_handle()
        if row is None or not row.evaluate('(node) => node.isConnected'):
            raise GuardError('待处理 Key 行已变化。')
        current = row.evaluate(_KEY_ROW_JS, snapshot['columns'])
        if current.get('invalid') or clean_name(current.get('name')) != name or parse_tracking_field(current['field']) != tracking_id:
            raise GuardError('待处理 Key 行身份发生变化，已停止。')
        return row, snapshot['columns'], current

    def _read_keys(self, account_id='', known_keys=()):
        """Bind a fresh complete Key list without collecting or approving spend.

        This permits a manual target check when costs are unavailable. The
        separate management preparation still rechecks the account, complete
        HTTP target and surviving control Key before any deletion permit.
        """
        self.anchor = None
        self.known_keys = set()
        self._last_key_snapshot = None
        args = identity_read_args({'account_id': account_id, 'known_keys': known_keys})
        self._check_login_ready()
        keys = self._keys()
        if not keys:
            raise GuardError('平台没有可绑定的 Key；无法确认当前账户。')
        identity = check_identity(keys, **args)
        reader = getattr(self, '_cost_reader', None)
        if reader is not None and getattr(self, '_cost_reader_identity', None) != identity:
            reader.clear()
            self._cost_reader = None
            self._cost_reader_identity = None
        self.anchor = key_fingerprint(keys)
        self.known_keys = {key['tracking_id'] for key in keys}
        return {'keys': keys, 'account_id': identity}

    def _collect(self, start_date, account_id='', known_keys=(), operation_id='', managed_names=None,
                 fee_basis='period'):
        # GUI collection always supplies the validated roster scope. None is
        # retained only for legacy/offline adapter callers, never as a fallback.
        if managed_names is not None:
            if (not isinstance(managed_names, (list, tuple)) or not managed_names
                    or any(not isinstance(name, str) or not name or clean_name(name) != name
                           for name in managed_names)
                    or len(set(managed_names)) != len(managed_names)):
                raise GuardError('费用读取没有明确且唯一的受管名单，未发送费用请求。')
        if fee_basis == 'key_lifetime':
            if managed_names is None:
                raise GuardError('创建以来费用读取必须提供明确受管名单。')
            return self._collect_lifetime(start_date, account_id, known_keys,
                                          operation_id, managed_names)
        if fee_basis != 'period':
            raise GuardError('费用统计口径无效，未发送费用请求。')
        self._check_login_ready()
        started_wall, started_monotonic = time.time(), time.monotonic()
        keys = self._keys()
        if not keys:
            raise GuardError('平台没有可绑定的 Key；无法确认当前账户。')
        # A local binding marker, NOT a DeepSeek account identifier. Existing
        # bindings must have a surviving public Tracking ID to prove continuity.
        identity = check_identity(keys, account_id, known_keys)
        self.anchor = key_fingerprint(keys)
        self.known_keys = {k['tracking_id'] for k in keys}
        end = datetime.now(TZ).date().isoformat()
        selected_ids = None
        if managed_names is not None:
            selected_ids = []
            for name in managed_names:
                matches = [key['tracking_id'] for key in keys if key['name'] == name]
                if len(matches) > 1:
                    raise GuardError('受管姓名在平台对应多个 Key，未发送费用请求。')
                selected_ids.extend(matches)
        reader = getattr(self, '_cost_reader', None)
        if reader is not None and getattr(self, '_cost_reader_identity', None) != identity:
            reader.clear()
            reader = None
        if reader is None:
            reader = self._cost_reader = self._create_cost_reader(deadline=started_monotonic + 540)
            self._cost_reader_identity = identity
        if isinstance(reader, AIConsoleCostReader):
            reader.deadline = started_monotonic + 540
        result = {'keys': keys, 'account_id': identity, 'totals': None,
                  'start_date': start_date, 'end_date': end,
                  'source_id': '', 'reason_code': 'NOT_COLLECTED', 'retryable': False,
                  'collection_started_wall': started_wall,
                  'collection_started_monotonic': started_monotonic,
                  'message': 'Key 列表已读取；尚未取得可核验的完整周期费用。'}
        if selected_ids is not None:
            result['requested_tracking_ids'] = selected_ids
            self._log('cost_scope_selected', count=len(selected_ids),
                      scope_kind='managed_keys', operation_id=operation_id)
            if not selected_ids:
                # The UI still binds/reconciles the complete identity list. It
                # distinguishes absent retired targets from invalid roster names.
                result.update(reason_code='NO_MANAGED_KEYS',
                              message='本次没有可查询的受管 Key，未发送费用请求。')
                return result
        payload = None
        self._log('cost_http_started', start_date=start_date, end_date=end,
                  request_start=int(datetime.fromisoformat(start_date).replace(tzinfo=TZ).timestamp()),
                  request_end=int((datetime.fromisoformat(end).replace(tzinfo=TZ) + timedelta(days=1)).timestamp()),
                  request_tz=28800,
                  request_span_days=(datetime.fromisoformat(end).date()
                                     - datetime.fromisoformat(start_date).date()).days + 1)
        try:
            self._stage('正在准备本次 AI 费用操作')
            if selected_ids is None:
                reader.prepare(self.page)
            else:
                reader.prepare(self.page, tracking_ids=selected_ids)
            self._stage('正在由本地 4B 操作网页读取指定周期费用')
            if selected_ids is None:
                url, payload = reader.read(start_date, end)
                result['totals'] = parse_cost_response(payload, url, start_date, end)
            else:
                url, payload = reader.read(start_date, end, tracking_ids=selected_ids)
                result['totals'] = parse_cost_response(payload, url, start_date, end,
                                                      tracking_ids=selected_ids)
            result['coverage_summary'] = cost_identity_coverage(keys, result['totals'])
            self._log('cost_identity_coverage', fee_id_source='api_key.tracking_id',
                      key_id_source='tracking_id_column', **result['coverage_summary'])
            result['source_id'] = sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
            result['reason_code'] = 'OK'
            result['message'] = '已读取所选周期的有效逐 Key 费用；未提供费用记录的 Key 仍保留未知。'
            self._log('costs_verified', cost_count=len(result['totals']),
                      start_date=start_date, end_date=end, totals_available=True,
                      source_id=result['source_id'], reason_code='OK', **cost_metadata(payload))
        except CostReadError as exc:
            result['message'] = str(exc)
            result['reason_code'] = exc.reason_code
            if selected_ids is not None and exc.reason_code == 'NO_USAGE_DATA':
                result['reason_code'] = 'NO_SELECTED_USAGE_DATA'
                result['message'] = '指定受管 Key 在所选期间暂无费用明细；不代表绑定错误，费用仍未知。'
            result['retryable'] = exc.retryable
            if exc.reason_code == 'PLATFORM_REJECTED':
                reader.clear()
            self._log('cost_collection_rejected', reason_code=result['reason_code'], retryable=exc.retryable,
                      message=str(exc), start_date=start_date, end_date=end, **cost_metadata(payload))
        if key_fingerprint(self._keys()) != self.anchor:
            reader.clear()
            raise GuardError('读取费用期间 Key 列表发生变化，本次结果未应用。')
        return result

    def _collect_lifetime(self, ledger_start, account_id, known_keys, operation_id, managed_names):
        """Creation-origin billing, bound to fresh HTTP and DOM identities.

        ledger_start is only the unchanged local ledger context. Each key's
        actual HTTP period is carried explicitly in key_periods, never inferred
        from that date or silently replaced by the page's default time range.
        """
        from .lifetime_costs import creation_periods, read_lifetime_costs
        from .platform_keys import ConsoleKeyManager, KeyPreparationError, KeyReadError
        self._check_login_ready()
        started_wall, started_monotonic = time.time(), time.monotonic()
        end = datetime.now(TZ).date().isoformat()
        manager = ConsoleKeyManager(self.context, emit=self._log)
        try:
            with manager.capture(self.page):
                keys = self._keys()
                if not keys:
                    raise GuardError('平台没有可绑定的 Key；无法确认当前账户。')
                identity = check_identity(keys, account_id, known_keys)
                self.anchor = key_fingerprint(keys)
                self.known_keys = {key['tracking_id'] for key in keys}
                selected = []
                for name in managed_names:
                    matches = [key['tracking_id'] for key in keys if key['name'] == name]
                    if len(matches) > 1:
                        raise GuardError('受管姓名在平台对应多个 Key，未发送费用请求。')
                    selected.extend(matches)
                result = {'keys': keys, 'account_id': identity, 'totals': None,
                    'start_date': ledger_start, 'end_date': end, 'source_id': '',
                    'fee_basis': 'key_lifetime', 'key_periods': {},
                    'requested_tracking_ids': selected, 'reason_code': 'NOT_COLLECTED',
                    'retryable': False, 'collection_started_wall': started_wall,
                    'collection_started_monotonic': started_monotonic,
                    'message': '正在核验受管 Key 的创建日期及创建以来费用。'}
                self._log('cost_scope_selected', count=len(selected), scope_kind='managed_keys',
                          fee_basis='key_lifetime', operation_id=operation_id)
                if not selected:
                    result.update(reason_code='NO_MANAGED_KEYS', message='本次没有可查询的受管 Key，未发送费用请求。')
                    return result
                reader = getattr(self, '_cost_reader', None)
                if reader is not None and getattr(self, '_cost_reader_identity', None) != identity:
                    reader.clear()
                    reader = None
                if reader is None:
                    reader = self._cost_reader = self._create_cost_reader(deadline=started_monotonic + 540)
                    self._cost_reader_identity = identity
                if isinstance(reader, AIConsoleCostReader):
                    reader.deadline = started_monotonic + 540
                try:
                    self._stage('正在只读核验受管 Key 的创建日期')
                    # This metadata read must never visit account-wide usage.
                    manager.prepare(self.page, allow_cost_fallback=False)
                    origins = manager.read_creation_times(keys, selected, observed_wall=started_wall)
                    periods = creation_periods(origins, end, started_wall)
                    self._stage('正在准备本次 AI 费用操作')
                    reader.prepare(self.page, tracking_ids=selected)
                    self._stage('正在分段读取每枚 Key 创建以来的费用')
                    try:
                        totals, source = read_lifetime_costs(reader, periods, emit=self._log,
                                                           started_monotonic=started_monotonic,
                                                           max_read_seconds=540)
                    except CostReadError as exc:
                        if exc.reason_code != 'LIFETIME_PERIOD_INCOMPLETE':
                            raise
                        # Even an empty report needs final creation/identity
                        # evidence before the GUI may retain identity-only state.
                        totals, source = None, ''
                    self._stage('正在复核费用期间的 Key 身份与创建日期')
                    after_origins = manager.read_creation_times(keys, selected, observed_wall=started_wall)
                    if after_origins != origins or key_fingerprint(self._keys()) != self.anchor:
                        raise CostReadError('读取期间 Key 身份或创建日期发生变化，本次结果未应用。',
                                            reason_code='LIFETIME_IDENTITY_CHANGED', retryable=True)
                    result['key_periods'] = periods
                    if totals is None:
                        raise CostReadError('受管 Key 创建以来费用尚无完整分段证据；未知时段未按零元计算。',
                                            reason_code='LIFETIME_PERIOD_INCOMPLETE', retryable=True)
                    result.update(totals=totals, source_id=source, reason_code='OK',
                        coverage_summary=cost_identity_coverage(keys, totals),
                        message='已核验每枚受管 Key 创建以来的费用；缺少完整分段证据的 Key 仍为未知。')
                    self._log('costs_verified', cost_count=len(totals), fee_basis='key_lifetime',
                              end_date=end, totals_available=True, source_id=source, reason_code='OK')
                except (KeyPreparationError, KeyReadError) as exc:
                    reason = ('LIFETIME_SCOPE_UNSUPPORTED' if getattr(exc, 'reason_code', '')
                              in {'KEY_TARGET_KIND_UNSUPPORTED', 'KEY_TARGET_NOT_DELETABLE'}
                              else 'LIFETIME_METADATA_INVALID')
                    result.update(reason_code=reason,
                                  message='未取得与完整平台列表一致的普通 Key 创建日期，未应用创建以来费用。')
                    reader.clear()
                    self._log('cost_collection_rejected', reason_code=reason,
                              metadata_reason_code=getattr(exc, 'reason_code', ''),
                              fee_basis='key_lifetime', end_date=end)
                except CostReadError as exc:
                    result.update(reason_code=exc.reason_code, retryable=exc.retryable, message=str(exc))
                    if exc.reason_code != 'LIFETIME_PERIOD_INCOMPLETE':
                        result['key_periods'] = {}
                    if exc.reason_code in {'PLATFORM_REJECTED', 'LIFETIME_IDENTITY_CHANGED'}:
                        reader.clear()
                    self._log('cost_collection_rejected', reason_code=exc.reason_code,
                              retryable=exc.retryable, fee_basis='key_lifetime', end_date=end)
                return result
        finally:
            manager.clear()

    def _prepare_revoke(self, name, tracking_id):
        keys = self._keys()
        if {'name': name, 'tracking_id': tracking_id} not in keys:
            raise GuardError('名字或 Tracking ID 不一致，未打开删除操作。')
        identity = key_fingerprint(keys)
        if not self.anchor or identity != self.anchor:
            raise GuardError('账户或 Key 集合变化，未打开删除操作。')
        # No brittle unnamed button indexing: select exact row for the user,
        # let them click the trash icon and final confirmation themselves.
        row, _columns, _identity = self._key_row(name, tracking_id)
        row.scroll_into_view_if_needed()
        self.page.bring_to_front()
        return {'message': '已定位对应 Key 行；请自行核对后点击垃圾桶及确认。'}

    def _verify_revoke(self, name, tracking_id, known_keys=(), *, http_keys=None):
        keys = self._keys()
        if http_keys is not None and (
                {(key['name'], key['tracking_id']) for key in keys}
                != {(key['name'], key['tracking_id']) for key in http_keys}):
            raise GuardError('新鲜 HTTP 列表与重新加载的平台列表不一致，撤销结果待核实。')
        current = {k['tracking_id'] for k in keys}
        expected = set(known_keys) or getattr(self, 'known_keys', set())
        if tracking_id not in expected:
            raise GuardError('待核实目标不属于既有已知身份，不能采用本次观察。')
        # Positive presence is a narrow observation, not a new account anchor
        # or proof that an earlier uncertain request cannot still take effect.
        if tracking_id in expected and {'name': name, 'tracking_id': tracking_id} in keys:
            return {'absent': False, 'present': True}
        # Persisted bindings survive application restarts. A remaining bound Key
        # proves continuity; a completely empty/unrelated account cannot do so.
        if not current.intersection(expected - {tracking_id}):
            raise GuardError('没有其他存活的已知 Key 可核验账户连续性；不能确认停用结果。')
        absent = tracking_id in expected and tracking_id not in current
        if not absent and {'name': name, 'tracking_id': tracking_id} not in keys:
            raise GuardError('当前 Key 的名字或身份发生变化，需人工核实。')
        if absent:
            self.known_keys = current
            self.anchor = key_fingerprint(keys)
        return {'absent': absent, 'present': not absent}

    @contextmanager
    def _key_preparation_step(self, phase, stage):
        started = time.monotonic()
        try:
            self._stage(stage, phase=phase)
            self._log('key_preparation_step', phase=phase, status='started', elapsed_ms=0)
        except Exception:
            raise RevocationNotSubmitted(reason_code='KEY_DIAGNOSTIC_FAILED') from None
        yield
        try:
            self._log('key_preparation_step', phase=phase, status='passed',
                      elapsed_ms=max(0, round((time.monotonic() - started) * 1000)))
        except Exception:
            raise RevocationNotSubmitted(reason_code='KEY_DIAGNOSTIC_FAILED') from None

    @contextmanager
    def _prepared_http_target(self, name, tracking_id):
        """Shared read-only preparation; its yielded state is never cached.

        The caller chooses inspection or permit/submit after all checks pass.
        No write intent, DeletePermit, ready event, or mutation is created here.
        Exceptions from the caller after yield retain their submission meaning.
        """
        from .platform_keys import ConsoleKeyManager
        manager = None
        started = time.monotonic()
        try:
            preparation_phase = 'KEY_SESSION_UNAVAILABLE'
            phase = 'capture'
            try:
                manager = ConsoleKeyManager(self.context, emit=self._log)
                # Observe the first required list navigation instead of missing
                # its request and depending on a second page load to repeat it.
                with manager.capture(self.page):
                    preparation_phase = 'KEY_LIST_READ_FAILED'
                    phase = 'key_list'
                    with self._key_preparation_step(phase, '正在只读核验账户与完整 Key 列表'):
                        keys = self._keys()
                        target = {'name': name, 'tracking_id': tracking_id}
                        if target not in keys or key_fingerprint(keys) != self.anchor:
                            raise RevocationNotSubmitted(reason_code='KEY_ACCOUNT_CHANGED')
                        expected = {key['tracking_id'] for key in keys}
                        if len(expected) < 2:
                            raise RevocationNotSubmitted(reason_code='KEY_LAST_TARGET')
                    preparation_phase = 'KEY_BOOTSTRAP_FAILED'
                    phase = 'authorization'
                    with self._key_preparation_step(phase, '正在取得本次管理授权'):
                        manager.prepare(self.page, allow_cost_fallback=False)
                    preparation_phase = 'KEY_TARGET_READ_FAILED'
                    phase = 'target_http'
                    with self._key_preparation_step(phase, '正在通过 HTTP 核验完整目标信息'):
                        prepared = manager.read_target(name, tracking_id, keys)
                    preparation_phase = 'KEY_ACCOUNT_RECHECK_FAILED'
                    phase = 'account_recheck'
                    with self._key_preparation_step(phase, '正在只读复核账户与目标身份'):
                        if key_fingerprint(self._keys()) != self.anchor:
                            raise RevocationNotSubmitted(reason_code='KEY_ACCOUNT_CHANGED')
                preparation_phase = 'KEY_DIAGNOSTIC_FAILED'
                self._log('key_preparation_completed', channel='console_http', status='passed',
                          key_count=len(expected), elapsed_ms=max(0, round((time.monotonic() - started) * 1000)))
            except Exception as exc:
                # This block contains only read-only preparations. No permit
                # readiness or write adapter entry has occurred on this path.
                failure = RevocationNotSubmitted(reason_code=getattr(exc, 'reason_code', preparation_phase))
                try:
                    self._log('revocation_preparation_failed', channel='console_http', status='failed',
                              phase=phase, reason_code=failure.reason_code,
                              elapsed_ms=max(0, round((time.monotonic() - started) * 1000)))
                except Exception:
                    pass
                raise failure from None
            yield manager, prepared, expected
        finally:
            if manager is not None:
                manager.clear()

    def _inspect_revoke(self, name, tracking_id):
        """Exercise the real read-only preparation path, then discard its state."""
        try:
            with self._prepared_http_target(name, tracking_id) as (_manager, _prepared, expected):
                return {'preflight_passed': True, 'name': name, 'tracking_id': tracking_id,
                        'key_count': len(expected)}
        except Exception as exc:
            reason = exc.reason_code if isinstance(exc, RevocationNotSubmitted) else 'PREPARATION_FAILED'
            raise KeyInspectionError(reason_code=reason) from None

    def _revoke(self, name, tracking_id, permit):
        """Model-selected UI actions; one request-boundary-authorized POST.

        Native HTTP remains read-only evidence. It is never a deletion fallback.
        The original short permit begins only when the final browser POST is
        paused at the route, after all model inference and confirmation steps.
        """
        with self._prepared_http_target(name, tracking_id) as (manager, prepared, expected):
            deadline = time.monotonic() + 180
            try:
                runtime = self._get_operator_runtime(deadline=deadline)
                WebOperator(self.page, runtime, emit=self._log, stage=self._stage).delete(
                    prepared, permit, expected, deadline=deadline)
            except (WebOperatorError, OperatorError) as exc:
                self._log('operator_delete_stopped', reason_code=exc.reason_code,
                          may_submit=bool(getattr(exc, 'may_have_submitted', False)))
                if (not permit.ready.is_set() and not permit.allowed
                        and not getattr(exc, 'may_have_submitted', False)):
                    raise RevocationNotSubmitted(
                        '本地 AI 操作在提交前停止，尚未进入撤销提交步骤。\n' + str(exc),
                        reason_code='PREPARATION_FAILED') from None
                raise
            self._stage('正在独立核验平台撤销结果')
            # A frontend may optimistically remove a row. Require a separate
            # server GET as well as the reloaded DOM and surviving known anchor.
            http_keys = manager.read_keys()
            result = self._verify_revoke(name, tracking_id, expected, http_keys=http_keys)
            if result.get('absent') is not True:
                raise GuardError('平台仍显示该 Key，撤销结果未确认；已暂停，不自动重试。')
            return result

    def _revoke_dom_legacy(self, name, tracking_id, permit):
        """Offline compatibility reference; not an exposed worker action.

        Automatic management uses _revoke (HTTP). This retained draft is never
        used as a fallback and cannot be selected through the worker protocol.
        """
        """One attempt only. The caller must persist unknown outcomes, not retry."""
        keys = self._keys()
        target = {'name': name, 'tracking_id': tracking_id}
        if target not in keys or key_fingerprint(keys) != self.anchor:
            raise GuardError('撤销前账户或 Key 列表发生变化，已停止。')
        expected = {k['tracking_id'] for k in keys}
        if len(expected) < 2:
            raise GuardError('这是账户内最后一个 Key，无法用其他 Key 核验删除结果；请使用手动处理。')
        row, columns, row_identity = self._key_row(name, tracking_id)
        buttons = [button for button in row.query_selector_all('button, [role="button"]')
                   if button.evaluate(_TRASH_BUTTON_JS, TRASH_SIGNATURES)]
        if len(buttons) != 1:
            raise GuardError('平台删除按钮结构发生变化，已停止。')
        button = buttons[0]
        # The platform's dialog does not show a target ID. Lock this document
        # while opening it, and retain exact nodes rather than re-resolving a
        # generic confirmation button. Any human interaction aborts this attempt.
        token = uuid.uuid4().hex
        self.page.evaluate("""token => {
            const state = {token, interrupted: false};
            state.events = ['pointerdown', 'keydown', 'click', 'touchstart'];
            state.block = event => {
                if (event.isTrusted) {
                    state.interrupted = true;
                    event.preventDefault(); event.stopImmediatePropagation();
                }
            };
            for (const type of state.events) document.addEventListener(type, state.block, true);
            window.__quotaGuardInteraction = state;
        }""", token)
        try:
            if not button.evaluate("""(node, args) => {
                const readRow = """ + _KEY_ROW_JS + """;
                const isTrash = """ + _TRASH_BUTTON_JS + """;
                if (!node.isConnected || !args.row.isConnected || !args.row.contains(node) ||
                    !isTrash(node, args.signatures) || node.closest('tr') !== args.row ||
                    location.href !== args.url ||
                    JSON.stringify(readRow(args.row, args.columns)) !== JSON.stringify(args.identity)) return false;
                node.click(); return true;
            }""", {'row': row, 'columns': columns, 'identity': row_identity, 'url': BASE + '/api_keys',
                   'signatures': TRASH_SIGNATURES}):
                raise GuardError('待撤销按钮已离开当前页面，未执行操作。')
            confirm = self.page.get_by_role('button', name='删除', exact=True)
            warning = self.page.get_by_text(DELETE_WARNING, exact=True)
            confirm.wait_for(state='visible')
            if confirm.count() != 1 or not warning.is_visible():
                raise GuardError('撤销确认框不符合已核验结构，未点击确认。')
            confirm_node = confirm.element_handle()
            warning_node = warning.element_handle()
            self._stage('等待账本核验和一次性撤销许可')
            permit.ready.set()
            if not permit.decided.wait(20) or not permit.allowed:
                cancel = self.page.get_by_role('button', name='取消', exact=True)
                if cancel.count() == 1:
                    cancel.evaluate('(node) => node.click()')
                return {'cancelled': True}
            # Check and click in one document task. A replaced modal or changed
            # document cannot redirect this click to a newly matched button.
            self._stage('正在提交撤销，结果需要核验')
            self._log('revocation_click_preparing', submitted=False, tracking_id=tracking_id)
            clock_sample = self.page.evaluate('() => ({monotonic: performance.now(), wall: Date.now()})')
            lease = permit.consume_for_click()
            clicked = confirm_node.evaluate("""(node, args) => {
                const readRow = """ + _KEY_ROW_JS + """;
                const isTrash = """ + _TRASH_BUTTON_JS + """;
                const state = window.__quotaGuardInteraction;
                if (!state || state.token !== args.token || state.interrupted ||
                    location.href !== args.url || !node.isConnected ||
                    !args.warning.isConnected || !node.getClientRects().length ||
                    !args.warning.getClientRects().length || node.textContent.trim() !== '删除' ||
                    !args.row.isConnected || !args.button.isConnected || !args.row.contains(args.button) ||
                    !isTrash(args.button, args.signatures) || args.button.closest('tr') !== args.row ||
                    JSON.stringify(readRow(args.row, args.columns)) !== JSON.stringify(args.identity)) return false;
                const now = Date.now();
                if (now < args.lease.issued_wall_ms || now >= args.lease.expires_wall_ms) return false;
                const mono = performance.now();
                if (mono < args.clock.monotonic || mono - args.clock.monotonic >= args.lease.remaining_ms ||
                    now < args.clock.wall) return false;
                node.click(); return true;
            }""", {'token': token, 'url': BASE + '/api_keys', 'warning': warning_node,
                   'row': row, 'button': button, 'columns': columns, 'identity': row_identity,
                   'lease': lease, 'clock': clock_sample, 'signatures': TRASH_SIGNATURES})
            if not clicked:
                raise GuardError('确认期间页面或操作目标发生变化，未提交撤销。')
            self._log('revocation_click_completed', tracking_id=tracking_id)
            # No retry: a timeout after this click may mean it already ran.
            confirm_node.wait_for_element_state('hidden')
        finally:
            self.page.evaluate("""token => {
                const state = window.__quotaGuardInteraction;
                if (state && state.token === token) {
                    for (const type of state.events) document.removeEventListener(type, state.block, true);
                    delete window.__quotaGuardInteraction;
                }
            }""", token)
        self._stage('正在核验平台撤销结果')
        result = self._verify_revoke(name, tracking_id, expected)
        if not result['absent']:
            raise GuardError('平台仍显示该 Key，撤销结果未确认；已暂停，不自动重试。')
        return result


def key_fingerprint(keys):
    return sha256(json.dumps(sorted((k['tracking_id'], k['name']) for k in keys),
                             ensure_ascii=False).encode()).hexdigest()


def check_identity(keys, account_id, known_keys):
    if account_id:
        current = {k['tracking_id'] for k in keys}
        if not current.intersection(known_keys):
            raise AccountMismatch('没有仍存在的已绑定 Key 可确认账户连续性；请登录原账户或选择其他账本，未重建或清零数据。')
        return account_id
    return key_fingerprint(keys)
