"""One target-independent action contract shared by model and executor.

The schema compiler sees only the current public observation. It cannot rank
references by the task target or consume evaluator truth.
"""
from __future__ import annotations

from copy import deepcopy

MAX_STRING = 4000
MODEL_TOOLS = ('act_on_node', 'handle_dialog', 'poll_action', 'report_blocker')


def object_schema(properties=None, required=None):
    return {'type': 'object', 'properties': properties or {},
            'required': required or [], 'additionalProperties': False}


def string_schema():
    return {'type': 'string', 'maxLength': MAX_STRING}


def _args(action, *, text=None, value=False, refs=None):
    props = {'ref': string_schema() if refs is None else {'type': 'string', 'enum': refs},
             'action': {'const': action}}
    required = ['ref', 'action']
    if value:
        props['value'] = string_schema()
        required.append('value')
    if text:
        props['text'] = string_schema()
    return object_schema(props, required)


TOOL_SCHEMAS = {
    'observe_page': object_schema(),
    'navigate': object_schema({'path': string_schema()}, ['path']),
    'act_on_node': {'oneOf': [_args('click'), _args('scroll'),
                            _args('type', value=True), _args('select', value=True)]},
    'handle_dialog': {'oneOf': [_args('accept', text=True), _args('dismiss')]},
    'poll_action': object_schema(),
    'read_evidence': object_schema(),
    'report_blocker': object_schema({'reason': string_schema()}, ['reason']),
}


def _branch(tool, args):
    return object_schema({'tool': {'const': tool}, 'arguments': args}, ['tool', 'arguments'])


def compile_action_schema(observation):
    """Return all structurally available actions; deliberately no target input."""
    branches = []
    dialog = observation.get('dialog')
    pending = observation.get('action_state', {}).get('status') == 'pending'
    if dialog and isinstance(dialog.get('ref'), str):
        refs = [dialog['ref']]
        branches.append(_branch('handle_dialog', _args('accept', refs=refs,
                                                      text=dialog.get('type') == 'prompt')))
        branches.append(_branch('handle_dialog', _args('dismiss', refs=refs)))
    elif not pending:
        nodes = [n for n in observation.get('nodes', [])
                 if isinstance(n.get('ref'), str) and n['ref'] and not n.get('disabled')]
        refs = list(dict.fromkeys(n['ref'] for n in nodes))
        if refs:
            branches.append(_branch('act_on_node', _args('click', refs=refs)))
            branches.append(_branch('act_on_node', _args('scroll', refs=refs)))
        text_refs = [n['ref'] for n in nodes if n.get('tag') == 'input'
                     and n.get('input_type', '').lower() in
                     {'', 'text', 'search', 'email', 'url', 'tel', 'number', 'date', 'datetime-local'}]
        select_refs = [n['ref'] for n in nodes if n.get('tag') == 'select']
        for action, candidates in [('type', text_refs), ('select', select_refs)]:
            if candidates:
                branches.append(_branch('act_on_node', _args(action, refs=candidates, value=True)))
    branches.append(_branch('poll_action', object_schema()))
    branches.append(_branch('report_blocker', deepcopy(TOOL_SCHEMAS['report_blocker'])))
    return {'oneOf': branches}


def validate_action(tool, args, observation=None):
    """Static protocol validation, optionally including public state/ref checks."""
    if tool not in TOOL_SCHEMAS or not isinstance(args, dict):
        return False, 'Unknown tool or non-object arguments'
    if any(not isinstance(v, str) or len(v) > MAX_STRING for v in args.values()):
        return False, 'Arguments must be bounded strings'
    keys = set(args)
    if tool == 'act_on_node':
        action = args.get('action')
        expected = {'ref', 'action'} | ({'value'} if action in {'type', 'select'} else set())
        if action not in {'click', 'scroll', 'type', 'select'} or keys != expected or not args.get('ref'):
            return False, 'Action fields must exactly match click/scroll or type/select contract'
    elif tool == 'handle_dialog':
        if (args.get('action') not in {'accept', 'dismiss'} or not args.get('ref')
                or not {'ref', 'action'} <= keys or keys - {'ref', 'action', 'text'}
                or ('text' in args and args['action'] != 'accept')):
            return False, 'Dialog fields must match accept or dismiss contract'
    else:
        expected = set(TOOL_SCHEMAS[tool]['required'])
        if keys != expected:
            return False, 'Arguments do not match tool contract'
    if observation is not None and tool in MODEL_TOOLS:
        schema = compile_action_schema(observation)
        for branch in schema['oneOf']:
            if branch['properties']['tool']['const'] != tool:
                continue
            contract = branch['properties']['arguments']
            props = contract['properties']
            if not set(contract['required']) <= keys or keys - set(props):
                continue
            if all(('const' not in props[k] or v == props[k]['const']) and
                   ('enum' not in props[k] or v in props[k]['enum']) for k, v in args.items()):
                return True, ''
        return False, 'Action is unavailable in this public observation'
    return True, ''


def public_ready(observation):
    """A loaded empty list is ready; neither row shape nor task ID is consulted."""
    if observation.get('dialog'):
        return True
    state = observation.get('readiness', {})
    return (state.get('document_state') in {'interactive', 'complete'}
            and state.get('main_present') is True and state.get('busy') is False
            and state.get('content_ready') is True)


REASONS = {
    'STALE_REFERENCE': 'The observed reference or its context is no longer current; observe again.',
    'NATIVE_DIALOG_OPEN': 'A native dialog is open; background page actions are unavailable.',
    'ACTION_PENDING': 'The previous action is still pending; poll or handle its observed dialog, do not repeat it.',
    'INVALID_ARGUMENTS': 'The action arguments do not satisfy the tool contract; this call was not started.',
    'ORIGIN_BLOCKED': 'Navigation outside the approved platform origin is disabled.',
    'SUBMISSION_UNKNOWN': 'Submission outcome is unknown; do not resubmit the operation.',
    'ACTION_FAILED': 'The browser action failed; its side effects were not inferred.',
    'TOOL_FAILURE': 'The tool failed; its side effects were not inferred.',
}


def normalize_tool_error(result):
    """Keep stable error meaning without copying stale actionable refs or prose."""
    if not isinstance(result, dict):
        return None
    error = result.get('tool_error')
    if error is None:
        if result.get('status') not in {'failed', 'unknown'}:
            return None
        error = result
    if not isinstance(error, dict):
        error = {}
    data = error.get('structuredContent') or error
    code = data.get('code')
    if code == -32602 or error.get('code') == -32602:
        code = 'INVALID_ARGUMENTS'
    if code not in REASONS:
        code = 'SUBMISSION_UNKNOWN' if data.get('status') == 'unknown' else 'TOOL_FAILURE'
    return {'code': code, 'reason': REASONS[code],
            'phase': data.get('phase', 'tool'),
            'side_effect': data.get('side_effect', 'not_started' if code == 'INVALID_ARGUMENTS' else 'not_inferred'),
            'action_ref': None}  # Never return a historic actionable reference to the next inference.
