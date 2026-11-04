"""One local classification per click. Only pre-approved help topic IDs can return."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
from contextlib import ExitStack

MODEL = "qwen2.5-0.5b-instruct-q4_k_m.gguf"
LOCK_HASH = "ec78fc16a93ca5310e2be9e5dae1a80ed98e2fe05ce104ff948efa85dbf7efa4"
FIELDS = frozenset({"stage", "reason", "fee", "submission", "verification", "kind"})


class LocalAIError(Exception):
    """Safe UI fallback. No runner output, paths or request contents in the message."""


def _inputs(case_json: str, allowed: tuple[str, ...]) -> dict:
    from .local_ai_assistant import DiagnosticCase, topics_for

    if not isinstance(case_json, str) or len(case_json) > 1024:
        raise LocalAIError("诊断摘要不符合本地助手输入要求。")
    try:
        def unique(pairs):
            if len(pairs) != len(dict(pairs)):
                raise ValueError
            return dict(pairs)
        data = json.loads(case_json, object_pairs_hook=unique)
        if not isinstance(data, dict) or set(data) != FIELDS:
            raise ValueError
        case = DiagnosticCase(**data)
    except (TypeError, ValueError):
        raise LocalAIError("诊断摘要不符合本地助手输入要求。") from None
    eligible = topics_for(case)
    if (not isinstance(allowed, tuple) or not allowed or
            not all(isinstance(v, str) and v in eligible for v in allowed)
            or len(set(allowed)) != len(allowed)):
        raise LocalAIError("没有可解释的诊断主题。")
    return data


def _assets_dir() -> Path:
    app = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
    return app / "local-ai"


def _regular(path: Path, *, directory: bool = False) -> bool:
    entry = path.lstat()
    cls = stat.S_ISDIR if directory else stat.S_ISREG
    return cls(entry.st_mode) and not getattr(entry, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT


def verify_assets(root: Path) -> None:
    """Authenticate pinned model/engine, including every file in the DLL search directory."""
    try:
        manifest = root / "asset-lock.json"
        if not _regular(root, directory=True) or not _regular(manifest) or manifest.stat().st_size > 16384:
            raise ValueError
        raw = manifest.read_bytes().replace(b"\r\n", b"\n")
        if hashlib.sha256(raw).hexdigest() != LOCK_HASH:
            raise ValueError
        expected = json.loads(raw)["files"]
        for folder in ("bin", "models", "licenses"):
            if not _regular(root / folder, directory=True):
                raise ValueError
            names = {(folder + "/" + f.name) for f in (root / folder).iterdir()}
            if names != {path for path in expected if path.startswith(folder + "/")}:
                raise ValueError
        for relative, info in expected.items():
            item = root / relative
            if not _regular(item) or item.stat().st_size != info["size"]:
                raise ValueError
            with item.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != info["sha256"]:
                raise ValueError
    except (OSError, ValueError):
        raise LocalAIError("本地 AI 文件缺失或校验失败，请重新解压完整发布包。") from None


def _command(root: Path, case: dict, allowed: tuple[str, ...]) -> list[str]:
    choices = ", ".join(allowed)
    prompt = (
        "<|im_start|>system\nPick the help topic which most specifically explains the diagnostic. "
        "DSH means a Harness-managed key. Unknown submission means its outcome is uncertain. "
        "Return exactly one listed identifier.<|im_end|>\n<|im_start|>user\n"
        f"Diagnostic: {json.dumps(case, sort_keys=True)}.\nTopics: {choices}.<|im_end|>\n"
        "<|im_start|>assistant\n"
    )
    grammar = "root ::= " + " | ".join(json.dumps(t) for t in allowed)
    return [str(root / "bin" / "llama-completion.exe"), "--model", str(root / "models" / MODEL),
            "--offline", "--simple-io", "--color", "off", "--no-conversation", "--no-display-prompt", "--no-warmup",
            "--threads", "2", "--ctx-size", "512", "--n-predict", "32", "--temp", "0", "--seed", "1",
            "--grammar", grammar, "--prompt", prompt]


def _run(command: list[str], root: Path, timeout: float) -> bytes:
    if os.name != "nt":
        raise LocalAIError("此发布包的本地 AI 引擎适用于 Windows x64。")
    # Launch the verified, single-process CPU runner with a fresh environment.
    # It receives no account credentials, user proxy settings or launcher PATH.
    import _winapi
    import ctypes
    import msvcrt
    from .browser_process import _WindowsJob
    from .core import GuardError
    system = os.environ.get("SystemRoot", r"C:\Windows")
    env = {"SystemRoot": system, "WINDIR": system, "PATH": str(Path(system) / "System32"),
           "HF_HUB_OFFLINE": "1", "OMP_NUM_THREADS": "2"}
    try:
        with ExitStack() as handles:
            r, w = os.pipe()
            read_pipe = handles.enter_context(os.fdopen(r, "rb"))
            with os.fdopen(w, "wb") as writer, open(os.devnull, "r+b") as null:
                write_handle = msvcrt.get_osfhandle(writer.fileno())
                null_handle = msvcrt.get_osfhandle(null.fileno())
                for handle in (write_handle, null_handle):
                    os.set_handle_inheritable(handle, True)
                startup = subprocess.STARTUPINFO()
                startup.dwFlags = subprocess.STARTF_USESTDHANDLES
                startup.hStdInput = startup.hStdError = null_handle
                startup.hStdOutput = write_handle
                startup.lpAttributeList = {"handle_list": [write_handle, null_handle]}
                # Suspend before any runner/launcher code can start children outside its Job.
                flags = 0x00000004 | subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS
                proc, thread, pid, _ = _winapi.CreateProcess(
                    command[0], subprocess.list2cmdline(command), None, None, True,
                    flags, env, str(root / "bin"), startup)
            handles.callback(_winapi.CloseHandle, proc)
            handles.callback(_winapi.CloseHandle, thread)
            try:
                job = _WindowsJob(pid, name=None)
            except BaseException:
                _winapi.TerminateProcess(proc, 1)
                raise
            handles.callback(job.close)
            resume = ctypes.WinDLL("kernel32", use_last_error=True).ResumeThread
            resume.argtypes, resume.restype = [ctypes.c_void_p], ctypes.c_uint32
            if resume(thread) != 1:
                raise OSError("ResumeThread")
            if _winapi.WaitForSingleObject(proc, max(1, int(timeout * 1000))) == _winapi.WAIT_TIMEOUT:
                raise LocalAIError("本地 AI 运行超时。")
            if _winapi.GetExitCodeProcess(proc):
                raise LocalAIError("本地 AI 引擎未返回可用结果。")
            job.close()  # also closes any still-live descendant stdout writers
            output = read_pipe.read(257)
            if len(output) > 256:
                raise LocalAIError("本地 AI 引擎未返回可用结果。")
            return output
    except (OSError, GuardError):
        raise LocalAIError("未能启动或隔离本地 AI 引擎。") from None


def rank_topics(case_json: str, allowed: tuple[str, ...], *, assets_dir: str | Path | None = None,
                timeout: float = 45) -> tuple[str, ...]:
    """Rank a fixed catalog; caller always renders its own already-approved explanations."""
    started = time.monotonic()
    case = _inputs(case_json, allowed)
    if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or not 0 < timeout <= 120:
        raise LocalAIError("本地 AI 运行时限无效。")
    root = Path(assets_dir or _assets_dir()).absolute()
    verify_assets(root)
    remaining = timeout - (time.monotonic() - started)
    if remaining <= 0:
        raise LocalAIError("本地 AI 校验超时。")
    raw = _run(_command(root, case, allowed), root, remaining)
    try:
        winner = raw.decode("ascii").strip().removesuffix(" [end of text]").strip()
    except UnicodeError:
        winner = ""
    if winner not in allowed:
        raise LocalAIError("本地 AI 返回了无法识别的诊断主题。")
    return (winner, *(topic for topic in allowed if topic != winner))
