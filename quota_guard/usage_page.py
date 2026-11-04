"""Select the observed usage-page date controls without reading account data.

The button omits years, so this UI acknowledgement is not proof that a cost
response covers the range. Callers must separately validate the response's
complete UTC+8 interval and ignore responses for the previous/default range.
"""
from datetime import date
import re
import time

from .core import GuardError
from .page_notices import dismiss_optional_notice, raise_optional_notice_error


PANEL = '.ds-date-picker-panel'
TITLE = PANEL + '__title'
MONTH_TITLE = re.compile(r'\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*')
RANGE_LABEL = re.compile(r'时间维度\s+(\d{1,2})/(\d{1,2})\s*-\s*(\d{1,2})/(\d{1,2})')


def _date(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except (TypeError, ValueError):
        raise GuardError('用量日期必须是完整的 YYYY-MM-DD。') from None


def _remaining(deadline):
    remaining = int((deadline - time.monotonic()) * 1000)
    if remaining <= 0:
        raise GuardError('用量日期选择未在限定时间内完成，费用未核验。')
    return remaining


def _unique_visible(locator, deadline, description):
    # Locator actions wait for rendering; count() alone does not.
    try:
        locator.wait_for(state='visible', timeout=_remaining(deadline))
    except Exception:
        raise GuardError(f'无法唯一识别{description}，页面结构可能已变化。') from None
    if locator.count() != 1:
        raise GuardError(f'无法唯一识别{description}，页面结构可能已变化。')
    return locator


def _click(locator, deadline, description):
    locator = _unique_visible(locator, deadline, description)
    disabled = locator.evaluate('''node => {
        const nodes = [node, ...node.querySelectorAll('*')];
        return nodes.some(item => item.disabled || item.getAttribute('aria-disabled') === 'true' ||
            Array.from(item.classList).some(name => /(?:^|--)disabled$/.test(name)));
    }''')
    if disabled:
        raise GuardError(f'{description}不可用，未继续选择用量日期。')
    try:
        locator.click(timeout=_remaining(deadline))
    except Exception:
        raise GuardError(f'{description}未完成点击，费用未核验。') from None


def _months(page, deadline):
    while True:
        _remaining(deadline)
        titles = page.locator(PANEL + ':visible').evaluate_all('''panels =>
            panels.map(panel => Array.from(panel.querySelectorAll('.ds-date-picker-panel__title'))
                .map(title => title.textContent.trim()))''')
        if not titles:
            page.wait_for_timeout(min(50, _remaining(deadline)))
            continue
        if len(titles) != 2 or any(len(values) != 1 for values in titles):
            raise GuardError('用量日期面板数量或年月标题变化，已停止选择。')
        months = []
        for values in titles:
            match = MONTH_TITLE.fullmatch(values[0])
            if not match or not 1 <= int(match[2]) <= 12:
                raise GuardError('无法核验用量日期面板的完整年月。')
            months.append((int(match[1]), int(match[2])))
        if len(set(months)) != 2:
            raise GuardError('用量日期面板年月重复，不能唯一定位日期。')
        return months


def _panel(page, month, deadline):
    year, number = month
    title = page.locator(TITLE, has_text=re.compile(rf'^\s*{year}\s*年\s*{number}\s*月\s*$'))
    return _unique_visible(page.locator(PANEL + ':visible').filter(has=title), deadline, '用量年月面板')


def _select_day(page, wanted, deadline):
    wanted_month = (wanted.year, wanted.month)
    target_index = wanted.year * 12 + wanted.month
    for _ in range(241):
        months = _months(page, deadline)
        if wanted_month in months:
            panel = _panel(page, wanted_month, deadline)
            inner = page.locator(PANEL + '__date-inner', has_text=re.compile(rf'^\s*{wanted.day}\s*$'))
            day = panel.locator(PANEL + '__date--current-month').filter(has=inner)
            _click(day, deadline, '用量目标日期')
            return
        nearest = min(months, key=lambda month: abs(month[0] * 12 + month[1] - target_index))
        direction = 'prev' if target_index < nearest[0] * 12 + nearest[1] else 'next'
        button = _panel(page, nearest, deadline).locator(PANEL + f'__{direction}-month-button')
        _click(button, deadline, '用量月份切换按钮')
        while _months(page, deadline) == months:
            page.wait_for_timeout(min(50, _remaining(deadline)))
    raise GuardError('用量日期超出支持的月份导航范围，未应用费用。')


def _open_calendar(page, time_button, deadline, stage):
    """Recover one observed menu collapse without replaying calendar clicks."""
    custom = page.get_by_text('自定义', exact=True)

    def state():
        raise_optional_notice_error(page)
        panels = page.locator(PANEL + ':visible').count()
        choices = custom.evaluate_all('''nodes => nodes.filter(node =>
            node.getClientRects().length && getComputedStyle(node).visibility !== 'hidden').length''')
        if panels:
            # Count alone is insufficient: the two full month titles must agree
            # with the established calendar structure before continuing.
            _months(page, deadline)
            stage('用量日期日历已展开', state='calendar_visible', count=panels)
            return 'calendar'
        if choices > 1:
            raise GuardError('自定义日期选项数量不唯一，未继续选择用量日期。')
        return 'menu' if choices == 1 else 'closed'

    reopened = False
    while True:
        stage('正在打开用量日期菜单', state='reopening' if reopened else 'opening', count=int(reopened))
        _click(time_button, min(deadline, time.monotonic() + 1.2), '时间维度按钮')
        # A short render wait leaves time to recover under the original deadline.
        render_deadline = min(deadline, time.monotonic() + .7)
        notice_closed = False
        while True:
            notice_closed = dismiss_optional_notice(page, timeout_ms=min(500, _remaining(deadline))) or notice_closed
            current = state()
            if current != 'closed' or notice_closed or time.monotonic() >= render_deadline:
                break
            page.wait_for_timeout(min(50, _remaining(deadline)))
        if current == 'calendar':
            return
        observed_menu = current == 'menu'
        if observed_menu:
            stage('正在展开自定义日期', state='menu_visible', count=1)
            try:
                _click(custom, min(deadline, time.monotonic() + 1.2), '自定义日期选项')
            except GuardError as exc:
                current = state()
                if current == 'calendar':
                    # Hover may open the calendar and hide the menu item before
                    # Playwright dispatches its click. Do not click it again.
                    return
                if current == 'menu':
                    stage('自定义日期选项未完成点击', state='menu_blocked', count=1)
                    if '不可用' in str(exc):
                        raise
                    raise GuardError('自定义日期选项仍可见但无法点击，请处理遮挡后重试；费用未核验。') from None
            else:
                current = state()
                if current == 'closed':
                    # The option may close its menu before the calendar's next
                    # render. Confirm collapse rather than reopening mid-render.
                    render_deadline = min(deadline, time.monotonic() + .3)
                    while current == 'closed' and time.monotonic() < render_deadline:
                        page.wait_for_timeout(min(50, _remaining(deadline)))
                        current = state()
                if current == 'calendar':
                    return
                if current == 'menu':
                    # Rendering can follow the completed menu action. No second
                    # click is needed while the same menu is still open.
                    _months(page, deadline)
                    stage('用量日期日历已展开', state='calendar_visible', count=2)
                    return
        if current == 'closed' and (observed_menu or notice_closed) and not reopened:
            stage('日期菜单已收起，正在重新打开', state='menu_collapsed', count=1)
            reopened = True
            continue
        stage('用量日期菜单未能保持展开', state='menu_closed', count=int(reopened))
        if reopened:
            raise GuardError('用量日期菜单已收起；重开仍未完成，请关闭页面遮挡后重试。')
        raise GuardError('自定义日期选项未出现，请检查页面遮挡或日期控件后重试；费用未核验。')


def select_usage_date_range(page, start_date, end_date, *, timeout_ms=30000, on_stage=None):
    """Clear filters and select both inclusive dates on the observed real UI.

    The caller supplies today's UTC+8 date as ``end_date``. A same-day range
    intentionally clicks the date twice. No requests are created or replayed;
    the page makes its own requests when the second date applies the selection.
    A successful return still requires independent cost-response validation.
    """
    start, end = _date(start_date), _date(end_date)
    if start > end:
        raise GuardError('用量起始日期不能晚于结束日期。')
    started = time.monotonic()
    deadline = started + timeout_ms / 1000

    def stage(message, *, state, count=0):
        if on_stage is not None:
            on_stage(message, state=state, count=count, elapsed=round(time.monotonic() - started, 3))

    stage('正在核验用量筛选条件', state='checking_filters')
    clear = page.get_by_role('button', name='清除筛选条件', exact=True)
    if clear.count():
        _click(clear, deadline, '清除筛选条件按钮')
    all_keys = page.get_by_role('button', name='API Key 全部', exact=True)
    _unique_visible(all_keys, deadline, 'API Key 全部筛选状态')
    time_button = page.get_by_role('button', name=re.compile(r'^时间维度\s'))
    _open_calendar(page, time_button, deadline, stage)
    stage('正在选择用量起始日期', state='selecting_start')
    _select_day(page, start, deadline)
    stage('正在选择用量结束日期', state='selecting_end')
    _select_day(page, end, deadline)
    while page.locator(PANEL + ':visible').count():
        page.wait_for_timeout(min(50, _remaining(deadline)))
    label = _unique_visible(time_button, deadline, '时间维度按钮').inner_text().strip()
    match = RANGE_LABEL.fullmatch(label)
    if not match or tuple(map(int, match.groups())) != (start.month, start.day, end.month, end.day):
        raise GuardError('页面显示的用量日期与所选范围不一致，未应用费用。')
    _click(page.get_by_role('heading', name='用量信息', exact=True), deadline, '用量信息标题')
    _unique_visible(all_keys, deadline, 'API Key 全部筛选状态')
    stage('用量日期范围已确认', state='range_confirmed', count=2)
    return start.isoformat(), end.isoformat()
