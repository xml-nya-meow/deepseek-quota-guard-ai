"""Build a Windows portable candidate in a fresh staging directory."""
from __future__ import annotations

import argparse
from importlib.metadata import PackageNotFoundError, distribution
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parent
APP_NAME = "DeepSeekQuotaGuardAI"
PYTHON_VERSIONS = {"3.12.14", "3.13.17"}
BROWSER_COMPONENTS = ("chromium", "ffmpeg", "winldd")


def locked_dependencies() -> dict[str, str]:
    versions = {}
    for line in (ROOT / "requirements.lock").read_text(encoding="utf-8").splitlines():
        requirement = line.split("#", 1)[0].strip()
        if not requirement:
            continue
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+!-]+)", requirement)
        if not match:
            raise RuntimeError("requirements.lock must contain exact package==version pins.")
        name, expected = match.groups()
        try:
            actual = distribution(name).version
        except PackageNotFoundError:
            raise RuntimeError(f"Missing dependency: {name}. Install requirements.lock first.") from None
        if actual != expected:
            raise RuntimeError(f"Dependency mismatch: {name}; expected {expected}, found {actual}.")
        versions[name] = actual
    if not versions:
        raise RuntimeError("requirements.lock is empty.")
    return versions


def browser_components(runtime: Path) -> tuple[list[Path], dict[str, str]]:
    import playwright

    package = Path(playwright.__file__).resolve().parent / "driver" / "package"
    manifest = json.loads((package / "browsers.json").read_text(encoding="utf-8"))
    entries = {item["name"]: item for item in manifest["browsers"]}
    folders, revisions = [], {}
    for name in BROWSER_COMPONENTS:
        entry = entries[name]
        revision = str(entry.get("revisionOverrides", {}).get("win64", entry["revision"]))
        folder = runtime / f"{name.replace('-', '_')}-{revision}"
        if not folder.is_dir() or not (folder / "INSTALLATION_COMPLETE").is_file():
            raise RuntimeError(f"Browser component missing or incomplete: {folder.name}. "
                               "Use --install-browser, or supply a matching --runtime directory.")
        folders.append(folder)
        revisions[name] = revision

    # Resolve through the installed JS API without starting Python's async loop.
    # On Windows that loop needs a local TCP socketpair even for metadata reads.
    try:
        resolved = subprocess.run([
            str(package.parent / "node.exe"), "-e",
            "process.stdout.write(require(process.argv[1]).chromium.executablePath())",
            str(package),
        ], check=True, capture_output=True, text=True, encoding="utf-8", timeout=20)
        if not resolved.stdout.strip():
            raise RuntimeError("The installed driver returned no Chromium executable path.")
        executable = Path(resolved.stdout.strip()).resolve()
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("The installed driver could not resolve its Chromium executable path.") from exc
    if not executable.is_relative_to(folders[0].resolve()) or not executable.is_file():
        raise RuntimeError("Chromium executable does not match the installed Playwright version.")
    return folders, revisions


def copy_notices(output: Path, versions: dict[str, str]) -> dict[str, str]:
    notices = output / "第三方许可"
    notices.mkdir()
    for name in versions:
        package = distribution(name)
        for file in package.files or ():
            parts = file.parts
            metadata_index = next((i for i, part in enumerate(parts) if part.endswith(".dist-info")), None)
            if metadata_index is None or not any(
                part.lower().startswith(("license", "licence", "copying", "notice")) for part in parts
            ):
                continue
            source = Path(package.locate_file(file))
            if source.is_file():
                target = notices / name / Path(*parts[metadata_index + 1:])
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)

    for name in ("LICENSE.txt", "LICENSE"):
        source = Path(sys.base_prefix) / name
        if source.is_file():
            shutil.copy2(source, notices / f"Python-{name}")
    for source in (Path(sys.base_prefix) / "tcl").glob("*/license.terms"):
        target = notices / source.parent.name / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    import tkinter
    interpreter = tkinter.Tcl()
    tcl_version = interpreter.eval('info patchlevel')
    tcl_library = Path(interpreter.eval('info library'))
    tcl_license = tcl_library / 'license.terms'
    tcl_source = 'installed Tcl'
    if not tcl_license.is_file():
        combined = Path(sys.base_prefix) / 'Doc' / 'license.tcltk'
        if combined.is_file():
            tcl_license = combined
            tcl_source = 'Python Tcl/Tk notice'
        elif tcl_version in {'8.6.12', '8.6.15'}:
            tcl_license = ROOT / 'third_party' / f'tcl-{tcl_version}-license.terms'
            tcl_source = 'tcltk/tcl core-' + tcl_version.replace('.', '-')
        else:
            raise RuntimeError(f'Tcl {tcl_version} license missing; supply its matching upstream notice before publishing.')
    tcl_target = notices / 'Tcl' / 'license.terms'
    tcl_target.parent.mkdir(exist_ok=True)
    shutil.copy2(tcl_license, tcl_target)

    import playwright
    driver = Path(playwright.__file__).resolve().parent / "driver"
    for relative in ("LICENSE", "LICENSE.txt", "package/LICENSE", "package/NOTICE"):
        source = driver / relative
        if source.is_file():
            target = notices / "Playwright-driver" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    return {'tcl_version': tcl_version, 'tcl_license_source': tcl_source}


def validate_bundle(output: Path) -> None:
    forbidden_names = {"管理设置.json", "当前账本.json", "界面偏好.json", "外观偏好.json", "账本", "人员额度.xlsx", "状态.db", ".运行锁",
                       ".初始化中", "browser-smoke.json", "ai-smoke.json", "acceptance.json",
                       "OFFLINE-FIXTURE.txt", "ui-smoke.json", "responsibility.sqlite3", "responsibility.anchor.json"}
    forbidden_prefixes = ("运行日志-", "运行摘要-", "状态.db-", "responsibility.sqlite3-")
    for item in output.rglob("*"):
        if item.name in forbidden_names or item.name.startswith(forbidden_prefixes):
            raise RuntimeError("Build contains application data; the output was not published.")


def build(args: argparse.Namespace) -> Path:
    if sys.platform != "win32" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise RuntimeError("The portable build requires Windows x64.")
    python_version = platform.python_version()
    if python_version not in PYTHON_VERSIONS:
        raise RuntimeError(f"Use one of the supported Python versions: {', '.join(sorted(PYTHON_VERSIONS))}.")
    versions = locked_dependencies()
    from hashlib import sha256
    from quota_guard.local_ai_runtime import verify_assets
    ai_source = args.ai_assets.expanduser().resolve()
    ai_lock = (ROOT / 'local_ai' / 'asset-lock.json').read_bytes()
    verify_assets(ai_source)
    if (ai_source / 'asset-lock.json').read_bytes() != ai_lock:
        raise RuntimeError("AI assets must match the pinned release manifest.")
    from quota_guard.build_identity import source_build_identity
    build_identity = source_build_identity(ROOT)
    output = args.output.expanduser().resolve()
    runtime = args.runtime.expanduser().resolve()
    if output.exists():
        raise RuntimeError("The output path already exists. Choose a new --output; nothing was overwritten.")
    if output == runtime or output.is_relative_to(runtime) or runtime.is_relative_to(output):
        raise RuntimeError("Output and browser runtime must be separate directories.")

    environment = os.environ.copy()
    environment["PLAYWRIGHT_BROWSERS_PATH"] = str(runtime)
    environment["PYTHONHASHSEED"] = "0"
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(runtime)
    if args.install_browser:
        subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"],
                       cwd=ROOT, env=environment, check=True)
    components, revisions = browser_components(runtime)

    output.parent.mkdir(parents=True, exist_ok=True)
    # Only this unique child of the output parent is cleaned up afterwards.
    with tempfile.TemporaryDirectory(prefix=".quota-build-", dir=output.parent) as temporary:
        staging = Path(temporary).resolve()
        if not staging.is_relative_to(output.parent) or staging == output.parent:
            raise RuntimeError("Invalid staging directory.")
        template = ROOT / "templates" / "人员额度模板.xlsx"
        if not template.is_file():
            raise RuntimeError("The personnel workbook template is missing.")
        data_args = ["--add-data", f"{template};templates"]
        for component in components:
            # runtime/.links contains development paths and is never copied.
            data_args.extend(["--add-data", f"{component};browser-runtime/{component.name}"])
        subprocess.run([
            sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
            "--name", APP_NAME, "--distpath", str(staging / "dist"),
            "--workpath", str(staging / "work"), "--specpath", str(staging / "spec"),
            *data_args, str(ROOT / "main.py"),
        ], cwd=ROOT, env=environment, check=True)
        bundle = staging / "dist" / APP_NAME
        ai_target = bundle / 'local-ai'
        ai_target.mkdir()
        (ai_target / 'asset-lock.json').write_bytes(ai_lock)
        for relative in json.loads(ai_lock)['files']:
            target = ai_target / relative
            if not target.resolve().is_relative_to(ai_target):
                raise RuntimeError("Invalid AI asset path.")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ai_source / relative, target)
        verify_assets(ai_target)
        license_info = copy_notices(bundle, versions)
        (bundle / "templates").mkdir()
        shutil.copy2(template, bundle / "templates" / template.name)
        for name in ("README.md", "使用说明.md", "验证计划.md", "THIRD-PARTY-NOTICES.md", "LICENSE", "NOTICE"):
            source = ROOT / name
            if source.is_file():
                shutil.copy2(source, bundle / name)
        for source in sorted((ROOT / "docs").glob("*.md")):
            if source.is_file():
                (bundle / "docs").mkdir(exist_ok=True)
                shutil.copy2(source, bundle / "docs" / source.name)
        (bundle / "BUILD-INFO.json").write_text(json.dumps({
            **build_identity,
            "application_version": __import__('quota_guard').__version__,
            "python": python_version, "platform": "Windows x64",
            "dependencies": versions, "browser_revisions": revisions,
            "local_ai_manifest_sha256": sha256(ai_lock).hexdigest(),
            **license_info,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        validate_bundle(bundle)
        if output.exists():
            raise RuntimeError("The output path appeared during the build; nothing was overwritten.")
        # Same-filesystem Windows rename fails if a racing destination exists.
        bundle.rename(output)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / APP_NAME,
                        help="New portable directory; an existing path is never overwritten")
    parser.add_argument("--runtime", type=Path, default=ROOT / "browser-runtime",
                        help="Playwright browser cache containing matching runtime components")
    parser.add_argument("--ai-assets", type=Path, default=ROOT / "local-ai",
                        help="Pinned Qwen and llama.cpp tree produced by tools/prepare_local_ai.py")
    parser.add_argument("--install-browser", action="store_true",
                        help="Download Chromium matching the installed Playwright version")
    args = parser.parse_args()
    try:
        output = build(args)
    except (RuntimeError, OSError, subprocess.CalledProcessError) as exc:
        print(f"Build stopped: {exc}", file=sys.stderr)
        return 1
    print(f"Portable candidate created: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
