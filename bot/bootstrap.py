from __future__ import annotations

import subprocess
import sys

from .engine_bundle import ensure_engine_root

ENGINE_ROOT = ensure_engine_root()
BIN_DIR = ENGINE_ROOT / "deobf" / "bin"
BUILD_SCRIPT = ENGINE_ROOT / "deobf" / "build_luau.py"


def ensure_luau_runtime() -> None:
    required = (BIN_DIR / "luau", BIN_DIR / "luau-ast")
    if all(path.exists() for path in required):
        return
    print("Runtime Luau no encontrado; compilándolo una vez…", flush=True)
    subprocess.run(
        [sys.executable, str(BUILD_SCRIPT), "--portable"],
        cwd=str(ENGINE_ROOT),
        check=True,
    )