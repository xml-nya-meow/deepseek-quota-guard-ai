"""Bounded, non-secret identity for source runs and portable build diagnostics."""
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess
import sys

from . import __version__

DIAGNOSTIC_SCHEMA_VERSION = 3


def source_build_identity(root):
    root = Path(root)
    digest = sha256()
    paths = [root / 'main.py', root / 'build.py', root / 'requirements.lock',
             root / 'templates' / '人员额度模板.xlsx', *sorted((root / 'quota_guard').glob('*.py'))]
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode('utf-8'))
        digest.update(b'\0')
        digest.update(path.read_bytes())
        digest.update(b'\0')
    commit = 'unknown'
    try:
        result = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=root, capture_output=True,
                                text=True, timeout=3, check=True,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        candidate = result.stdout.strip()
        if re.fullmatch('[0-9a-f]{40}', candidate):
            commit = candidate
    except (OSError, subprocess.SubprocessError):
        pass
    return {'app_version': __version__, 'build_commit': commit,
            # Includes uncommitted runtime edits; a commit alone is insufficient.
            'build_id': 'code-sha256-' + digest.hexdigest(),
            'diagnostic_schema_version': DIAGNOSTIC_SCHEMA_VERSION}


def diagnostic_build_identity():
    fallback = {'app_version': __version__, 'build_commit': 'unknown',
                'build_id': 'unverified', 'diagnostic_schema_version': DIAGNOSTIC_SCHEMA_VERSION}
    try:
        if not getattr(sys, 'frozen', False):
            return source_build_identity(Path(__file__).resolve().parent.parent)
        path = Path(sys.executable).resolve().parent / 'BUILD-INFO.json'
        with path.open('rb') as stream:
            data = stream.read(65537)
        if len(data) > 65536:
            return fallback
        value = json.loads(data)
        if not isinstance(value, dict) or value.get('app_version') != __version__:
            return fallback
        commit, build_id = value.get('build_commit'), value.get('build_id')
        if not isinstance(commit, str) or not re.fullmatch('[0-9a-f]{40}|unknown', commit):
            return fallback
        if not isinstance(build_id, str) or not re.fullmatch('code-sha256-[0-9a-f]{64}', build_id):
            return fallback
        return {**fallback, 'build_commit': commit, 'build_id': build_id}
    except (OSError, ValueError, TypeError, RecursionError):
        return fallback
