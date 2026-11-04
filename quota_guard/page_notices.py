"""Dismiss only the observed, optional DeepSeek email-marketing reminder.

No form values leave the page, and no login, binding, verification-code or
confirmation action is selected. Unknown dialogs are deliberately left alone.
"""
import re

from .core import GuardError


_STATE = '_quota_guard_optional_notice'
_DESCRIPTION = '我们会通过该邮箱为你推送 DeepSeek API 的功能动态、商业信息和产品通知。'
_MODALS = ('.ds-modal:visible, [aria-modal="true"]:visible, '
           '[role="dialog"]:visible, [role="alertdialog"]:visible, dialog[open]:visible')


def _known_notices(page):
    content = page.locator('.ds-modal-content[role="dialog"]:visible')
    content = content.filter(has=page.locator('.ds-modal-content__title:visible').filter(
        has_text=re.compile(r'^\s*绑定邮箱\s*$')))
    content = content.filter(has=page.locator('p:visible').filter(
        has_text=re.compile(r'^\s*' + re.escape(_DESCRIPTION) + r'\s*$')))
    for placeholder in ('请输入邮箱', '请输入验证码'):
        content = content.filter(has=page.locator(f'input[placeholder="{placeholder}"]:visible'))
    for name in ('发送验证码', '绑定'):
        content = content.filter(has=page.get_by_role('button', name=name, exact=True))
    return page.locator('.ds-modal[aria-modal="true"]:visible').filter(has=content)


def _dismiss(page, *, timeout_ms):
    notices = _known_notices(page)
    if not notices.count():
        return False
    # The verified popup has exactly two modal layers: its outer ds-modal and
    # inner role=dialog. Any additional visible modal makes dismissal ambiguous.
    if notices.count() != 1 or page.locator(_MODALS).count() != 2:
        raise GuardError('绑定邮箱提醒与其他弹窗同时出现或数量不唯一，请手动处理弹窗后重试。')
    modal = notices
    dialog = modal.locator('.ds-modal-content[role="dialog"]:visible')
    if dialog.count() != 1:
        raise GuardError('绑定邮箱提醒结构不唯一，未自动关闭。')
    for placeholder in ('请输入邮箱', '请输入验证码'):
        if dialog.locator(f'input[placeholder="{placeholder}"]').count() != 1:
            raise GuardError('绑定邮箱提醒输入框结构变化，未自动关闭。')
    # Return only a boolean, never the email/code or other entered text.
    if dialog.locator('input, textarea, select, [contenteditable="true"]').evaluate_all('''nodes =>
        nodes.some(node => node.isContentEditable ? !!node.textContent :
            (node.type === 'checkbox' || node.type === 'radio') ? node.checked : !!node.value)'''):
        raise GuardError('绑定邮箱提醒中已有输入内容，请手动处理后重试；未自动丢弃输入。')

    later = dialog.get_by_role('button', name='稍后再填', exact=True, include_hidden=True)
    if later.count():
        button = later
    else:
        # Only the verified title's sibling close control is an allowed fallback.
        title = dialog.locator('.ds-modal-content__title').filter(
            has_text=re.compile(r'^\s*绑定邮箱\s*$'))
        if title.count() != 1:
            raise GuardError('绑定邮箱提醒标题不唯一，未自动关闭。')
        button = title.locator('..').locator(':scope > .ds-modal-content__close[role="button"]')
    if button.count() != 1:
        raise GuardError('绑定邮箱提醒缺少唯一的“稍后再填”或已知关闭按钮，请手动关闭后重试。')
    disabled = button.evaluate('''node => [node, ...node.querySelectorAll('*')].some(item =>
        item.disabled || item.getAttribute('aria-disabled') === 'true' ||
        Array.from(item.classList).some(name => /(?:^|--)disabled$/.test(name)))''')
    if disabled:
        raise GuardError('绑定邮箱提醒的关闭按钮不可用，请手动关闭后重试。')
    try:
        button.click(timeout=timeout_ms)
        # Use the structural container rather than the signature: changing the
        # title or removing form fields does not prove the overlay disappeared.
        page.locator('.ds-modal[aria-modal="true"]:visible').wait_for(state='hidden', timeout=timeout_ms)
    except Exception:
        raise GuardError('绑定邮箱提醒未能安全关闭，请手动关闭后重试。') from None
    return True


def raise_optional_notice_error(page):
    """Consume a handler failure without reading or changing the browser DOM."""
    state = getattr(page, _STATE, None)
    if state and state['error'] is not None:
        message, state['error'] = state['error'], None
        raise GuardError(message)


def install_optional_notice_handler(page):
    """Install once per Page; the locator continues working across navigation.

    Handlers run during Playwright actionability checks, not during evaluate().
    Read-only polling loops must also call dismiss_optional_notice explicitly.
    """
    if getattr(page, _STATE, None) is not None:
        return False
    state = {'trigger': _known_notices(page).first, 'error': None}

    def handler():
        try:
            _dismiss(page, timeout_ms=3000)
        except Exception as exc:
            # Playwright runs callbacks in a separate task. Keep the readable
            # error for the caller's error boundary, while its default wait for
            # the still-visible overlay prevents the original action proceeding.
            state['error'] = (str(exc) if isinstance(exc, GuardError) else
                              '无法核验绑定邮箱提醒，请手动处理弹窗后重试。')

    state['handler'] = handler
    page.add_locator_handler(state['trigger'], handler)
    setattr(page, _STATE, state)
    return True


def dismiss_optional_notice(page, *, timeout_ms=3000):
    """Close a recognized empty optional reminder, or leave unknown UI alone.

    Returns whether a notice was closed. A recognized but ambiguous, filled or
    uncloseable reminder raises GuardError without attempting another action.
    """
    raise_optional_notice_error(page)
    if not _known_notices(page).count():
        return False
    state = getattr(page, _STATE, None)
    if state is not None:
        # An explicit click must not trigger this same handler and dismiss its
        # own target before the original click can complete.
        page.remove_locator_handler(state['trigger'])
    try:
        return _dismiss(page, timeout_ms=timeout_ms)
    except GuardError:
        raise
    except Exception:
        raise GuardError('无法核验绑定邮箱提醒，请手动处理弹窗后重试。') from None
    finally:
        if state is not None and not page.is_closed():
            page.add_locator_handler(state['trigger'], state['handler'])
