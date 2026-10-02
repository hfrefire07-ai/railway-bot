from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .engine_bundle import ensure_engine_root

ENGINE_ROOT = ensure_engine_root()
DEOB_SCRIPT = ENGINE_ROOT / "deobf" / "deob.py"

# deob.py prints this line to stderr once it knows which plugin is handling
# the script (see deob.py:main -> "[*] obfuscator: %s%s"); we surface it in
# the Discord embed instead of asking the user to read raw logs.
_OBFUSCATOR_LINE = re.compile(r"^\[\*\]\s*obfuscator:\s*(.+)$", re.MULTILINE)


class DeobfuscationError(RuntimeError):
    """A controlled error from the deobfuscator process."""


@dataclass(frozen=True)
class DeobfuscationResult:
    output_path: Path
    detected_output: str
    diagnostics: str
    detected_obfuscator: str
    elapsed_seconds: float


def _safe_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(name).name).strip("._")
    return (cleaned or "script.luau")[:120]


def _validate_text_file(path: Path) -> None:
    with path.open("rb") as source:
        sample = source.read(64 * 1024)
    if b"\x00" in sample:
        raise DeobfuscationError("El archivo parece binario; se esperan scripts de texto Luau.")


async def run_deobfuscator(
    input_path: Path,
    original_name: str,
    settings: Settings,
    *,
    no_devirt: bool = False,
    fast: bool = False,
    forced_obfuscator: str | None = None,
) -> DeobfuscationResult:
    if not DEOB_SCRIPT.exists():
        raise DeobfuscationError("El motor de Luau no está instalado todavía.")
    size = input_path.stat().st_size
    if size == 0:
        raise DeobfuscationError("El archivo está vacío.")
    if size > settings.max_input_bytes:
        raise DeobfuscationError("El archivo supera el máximo configurado.")
    _validate_text_file(input_path)

    safe_name = _safe_name(original_name)
    if not safe_name.lower().endswith((".lua", ".luau", ".txt", ".lph")):
        safe_name += ".luau"

    with tempfile.TemporaryDirectory(prefix="lph-job-") as workdir:
        output_dir = Path(workdir)
        output_path = output_dir / f"{safe_name}.deobfuscated.luau"
        command = [
            sys.executable,
            str(DEOB_SCRIPT),
            str(input_path),
            "--output",
            str(output_path),
            "--timeout",
            str(settings.process_timeout_seconds),
            "--budget",
            str(settings.trace_budget_seconds),
        ]
        if no_devirt or fast:
            command.append("--no-devirt")
        if fast:
            # Keep the readability/name pass, but skip the expensive loop/helper
            # folding pass that dominates large behaviour traces.
            command.append("--no-fold")
        if forced_obfuscator:
            command.extend(["--obfuscator", forced_obfuscator])

        started = time.perf_counter()
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(ENGINE_ROOT),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=settings.process_timeout_seconds + 30,
            )
        except asyncio.TimeoutError as exc:
            if "process" in locals():
                process.kill()
                await process.wait()
            raise DeobfuscationError("El análisis agotó el tiempo permitido.") from exc
        except OSError as exc:
            raise DeobfuscationError("No se pudo iniciar el motor de Luau.") from exc
        elapsed_seconds = time.perf_counter() - started

        diagnostics = (stderr + b"\n" + stdout).decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            detail = diagnostics[-1800:] if diagnostics else "sin diagnóstico"
            raise DeobfuscationError(f"El motor terminó con error:\n```text\n{detail}\n```")
        if not output_path.exists():
            raise DeobfuscationError("El motor terminó sin generar un archivo de salida.")
        if output_path.stat().st_size > settings.max_output_bytes:
            raise DeobfuscationError("La salida supera el máximo configurado.")

        match = _OBFUSCATOR_LINE.search(diagnostics)
        detected_obfuscator = match.group(1).strip() if match else "desconocido"

        # The temporary directory is copied before it is cleaned up.
        fd, persisted_name = tempfile.mkstemp(prefix="lph-result-", suffix=".luau")
        os.close(fd)
        persisted = Path(persisted_name)
        output_path.replace(persisted)
        return DeobfuscationResult(
            output_path=persisted,
            detected_output=diagnostics,
            diagnostics=diagnostics,
            detected_obfuscator=detected_obfuscator,
            elapsed_seconds=elapsed_seconds,
        )