"""Materialize the fixed Windows CPU offline assistant. Run only on the build machine."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
from urllib.request import urlopen
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from quota_guard.local_ai_runtime import LOCK_HASH

LOCK = ROOT / "local_ai" / "asset-lock.json"
MODEL = "qwen2.5-0.5b-instruct-q4_k_m.gguf"
ZIP = "llama-b11960-bin-win-cpu-x64.zip"
QWEN = "https://huggingface.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF"
QWEN_REV = "9217f5db79a29953eb74d5343926648285ec7e67"
LLAMA_REV = "8642541b255bfb5dc39cd351a178be09013a55d6"
LLAMA_RAW = f"https://raw.githubusercontent.com/ggml-org/llama.cpp/{LLAMA_REV}"
SOURCES = {
    ZIP: ("https://github.com/ggml-org/llama.cpp/releases/download/b11960/" + ZIP,
          "73f962f82850d244db47eaacb9d913637027b82399124324298576d58c5f4287", 24407745),
    MODEL: (f"{QWEN}/resolve/{QWEN_REV}/{MODEL}",
            "74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db", 491400032),
    "LICENSE-Qwen": (f"{QWEN}/raw/{QWEN_REV}/LICENSE",
                     "832dd9e00a68dd83b3c3fb9f5588dad7dcf337a0db50f7d9483f310cd292e92e", 11343),
    "LICENSE-llama.cpp": (LLAMA_RAW + "/LICENSE",
                          "94f29bbed6a22c35b992c5c6ebf0e7c92f13b836b90f36f461c9cf2f0f1d010d", None),
    "LICENSE-jsonhpp": (LLAMA_RAW + "/licenses/LICENSE-jsonhpp",
                        "c0d068392ea65358b798b8c165103560f06e9e3b38c4ab4e2d8810a7b931af86", None),
}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def obtain(cache: Path, name: str, *, offline: bool) -> Path:
    url, sha, size = SOURCES[name]
    dest = cache / name
    if not dest.exists():
        if offline:
            raise ValueError(f"Missing cached input: {name}")
        with tempfile.NamedTemporaryFile(dir=cache, delete=False) as partial:
            tmp = Path(partial.name)
        try:
            with tmp.open("wb") as partial:
                with urlopen(url, timeout=120) as upstream:
                    shutil.copyfileobj(upstream, partial, 1024 * 1024)
            if digest(tmp) != sha:
                raise ValueError(f"Upstream file failed SHA-256: {name}")
            tmp.replace(dest)
        finally:
            tmp.unlink(missing_ok=True)
    if (size is not None and dest.stat().st_size != size) or digest(dest) != sha:
        raise ValueError(f"Cached file failed integrity check: {name}")
    return dest


def prepare(cache: Path, destination: Path, *, offline: bool) -> None:
    if destination.exists():
        raise FileExistsError(f"Choose a new output directory: {destination}")
    raw = LOCK.read_bytes().replace(b"\r\n", b"\n")
    if hashlib.sha256(raw).hexdigest() != LOCK_HASH:
        raise ValueError("Source asset manifest failed SHA-256")
    lock = json.loads(raw)
    cache.mkdir(parents=True, exist_ok=True)
    inputs = {name: obtain(cache, name, offline=offline) for name in SOURCES}
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix=".ai-package-") as temp:
        payload = Path(temp) / "local-ai"
        with ZipFile(inputs[ZIP]) as archive:
            for relative, meta in lock["files"].items():
                target = payload / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                name = target.name
                if name in inputs:
                    shutil.copyfile(inputs[name], target)
                else:
                    # Only named members from the release manifest enter the portable bundle.
                    with archive.open(name) as src, target.open("xb") as dst:
                        shutil.copyfileobj(src, dst)
                if target.stat().st_size != meta["size"] or digest(target) != meta["sha256"]:
                    raise ValueError(f"Packaged file failed SHA-256: {relative}")
        shutil.copyfile(LOCK, payload / "asset-lock.json")
        payload.rename(destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--offline", action="store_true", help="Use only pre-fetched, verified local files")
    args = parser.parse_args()
    prepare(args.cache.resolve(), args.output.resolve(), offline=args.offline)
