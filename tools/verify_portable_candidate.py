"""Run a new Windows candidate against fresh synthetic data, never its live data.

The bundle is read-only. Evidence goes to a NEW directory outside the bundle;
the child processes are bounded and never given real credentials or ledgers.
"""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from build import validate_bundle

EXPECTED_CASES = {
    'gui_template_import_sparse_rows', 'cooperative_close_cancels_timers',
    'reopen_keeps_import_and_preferences',
    'reopen_resumes_original_membership_plan_without_send',
    'positive_quota_automatic_synthetic_send_once', 'under_quota_force_synthetic_send_once',
}


def check_result(mode, folder, version):
    if mode == 'smoke':
        if not (folder / 'OFFLINE-FIXTURE.txt').is_file():
            raise ValueError('SMOKE_ISOLATION_MARKER_MISSING')
        result = json.loads((folder / 'ui-smoke.json').read_text(encoding='utf-8'))
        if result.get('passed') is not True or result.get('callback_errors') != 0:
            raise ValueError('SMOKE_UI_LIFECYCLE_FAILED')
        return result
    name = 'acceptance.json' if mode == 'offline-acceptance' else mode + '.json'
    result = json.loads((folder / name).read_text(encoding='utf-8'))
    if mode == 'offline-acceptance':
        if (result.get('passed') is not True or result.get('version') != version
                or result.get('transport') != 'synthetic' or result.get('real_requests') != 0
                or result.get('send_boundary_checks') != 2
                or len(result.get('cases', [])) != len(EXPECTED_CASES)
                or set(result.get('cases', [])) != EXPECTED_CASES):
            raise ValueError('PORTABLE_WORKFLOW_EVIDENCE_INCOMPLETE')
    elif mode == 'browser-smoke' and result.get('browser_started') is not True:
        raise ValueError('PORTABLE_BROWSER_FAILED')
    elif mode == 'ai-smoke' and result.get('model_ran') is not True:
        raise ValueError('PORTABLE_MODEL_FAILED')
    return result


def verify(bundle, evidence):
    bundle = Path(bundle).resolve(strict=True)
    evidence = Path(evidence).absolute()
    if evidence.resolve().is_relative_to(bundle) or bundle.is_relative_to(evidence.resolve()):
        raise ValueError('EVIDENCE_MUST_BE_OUTSIDE_BUNDLE')
    for path in (evidence, *evidence.parents):
        if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
            raise ValueError('EVIDENCE_LINK_REJECTED')
    validate_bundle(bundle)
    identity = json.loads((bundle / 'BUILD-INFO.json').read_text(encoding='utf-8'))
    executable = bundle / 'DeepSeekQuotaGuardAI.exe'
    executable_digest = sha256(executable.read_bytes()).hexdigest()
    evidence.mkdir(exist_ok=False)
    report = {'passed': False, 'version': identity['application_version'],
              'build_commit': identity['build_commit'], 'build_id': identity['build_id'],
              'executable_sha256': executable_digest, 'modes': {}}
    try:
        for mode in ('smoke', 'browser-smoke', 'ai-smoke', 'offline-acceptance'):
            folder = evidence / ('offline-acceptance-' + mode)
            start = time.monotonic()
            completed = subprocess.run([str(executable), '--' + mode, '--data-dir', str(folder)],
                cwd=evidence, timeout=180, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
                check=False)
            if completed.returncode != 0:
                raise RuntimeError('PORTABLE_MODE_FAILED')
            result = check_result(mode, folder, identity['application_version'])
            report['modes'][mode] = {'seconds': round(time.monotonic() - start, 3), 'evidence': result}
        validate_bundle(bundle)
        if sha256(executable.read_bytes()).hexdigest() != executable_digest:
            raise ValueError('PORTABLE_EXECUTABLE_CHANGED')
        report['passed'] = True
    except Exception as exc:
        report['reason_code'] = 'PORTABLE_ACCEPTANCE_FAILED'
        report['error_type'] = type(exc).__name__
    (evidence / 'portable-acceptance.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--evidence', type=Path, required=True)
    args = parser.parse_args()
    try:
        report = verify(args.bundle, args.evidence)
    except Exception:
        print('Portable acceptance could not start; existing paths were not reused.', file=sys.stderr)
        return 1
    print(json.dumps(report))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
