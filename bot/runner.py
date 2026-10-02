from __future__ import annotations

import asyncio
import os
import re
import signal
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

    def __init__(
        self,
        message: str,
        *,
        log_path: Path | None = None,
        return_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.log_path = log_path
        self.return_code = return_code


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


def _save_engine_log(
    stderr: bytes,
    stdout: bytes,
    *,
    return_code: int | None,
    reason: str | None = None,
) -> Path:
    stderr_text = stderr.decode("utf-8", errors="replace")
    stdout_text = stdout.decode("utf-8", errors="replace")
    sections = [
        "Luau deobfuscator process log",
        f"Process exit code: {return_code if return_code is not None else 'unknown'}",
    ]
    if reason:
        sections.append(f"Reason: {reason}")
    sections.extend(
        [
            "--- STDERR ---",
            stderr_text or "(empty)",
            "--- STDOUT ---",
            stdout_text or "(empty)",
        ]
    )
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        prefix="lph-engine-error-",
        suffix=".txt",
        delete=False,
    ) as log_file:
        log_file.write("\n\n".join(sections))
        return Path(log_file.name)


def _return_code_description(return_code: int) -> str:
    if return_code >= 0:
        return str(return_code)
    try:
        signal_name = signal.Signals(-return_code).name
    except ValueError:
        signal_name = f"signal {-return_code}"
    return f"{return_code} (terminated by {signal_name})"


def _log_excerpt(stderr: bytes, stdout: bytes) -> str:
    diagnostics = (stderr + b"\n" + stdout).decode("utf-8", errors="replace").strip()
    if not diagnostics:
        return "No stdout/stderr was captured; see the attached log for the process exit code."
    return diagnostics[-1200:].replace("```", "`\u200b``")


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
            communication = asyncio.create_task(process.communicate())
            stdout, stderr = await asyncio.wait_for(
                asyncio.shield(communication),
                timeout=settings.process_timeout_seconds + 30,
            )
        except asyncio.TimeoutError as exc:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            stdout, stderr = await communication
            log_path = _save_engine_log(
                stderr,
                stdout,
                return_code=process.returncode,
                reason="The process exceeded the configured timeout and was terminated.",
            )
            excerpt = _log_excerpt(stderr, stdout)
            return_code = (
                _return_code_description(process.returncode)
                if process.returncode is not None
                else "unknown"
            )
            raise DeobfuscationError(
                "El análisis agotó el tiempo permitido "
                f"(código de salida: {return_code}).\n"
                f"Última salida:\n```text\n{excerpt}\n```\n"
                "Se adjunta el log completo en un archivo .txt.",
                log_path=log_path,
                return_code=process.returncode,
            ) from exc
        except OSError as exc:
            raise DeobfuscationError("No se pudo iniciar el motor de Luau.") from exc
        elapsed_seconds = time.perf_counter() - started

        diagnostics = (stderr + b"\n" + stdout).decode("utf-8", errors="replace").strip()
        if process.returncode != 0:
            log_path = _save_engine_log(
                stderr,
                stdout,
                return_code=process.returncode,
            )
            return_code = _return_code_description(process.returncode)
            excerpt = _log_excerpt(stderr, stdout)
            raise DeobfuscationError(
                f"El motor terminó con error (código de salida: {return_code}).\n"
                f"Última salida:\n```text\n{excerpt}\n```\n"
                "Se adjunta el log completo en un archivo .txt.",
                log_path=log_path,
                return_code=process.returncode,
            )
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