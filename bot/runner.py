from __future__ import annotations

import asyncio
import logging
import os
import re
import signal
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .engine_bundle import ensure_engine_root

ENGINE_ROOT = ensure_engine_root()
DEOB_SCRIPT = ENGINE_ROOT / "deobf" / "deob.py"
LOGGER = logging.getLogger("luau-discord-bot.runner")

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
    mode: str
    fallback_used: bool


@dataclass(frozen=True)
class _ProcessRun:
    process: asyncio.subprocess.Process
    stdout: bytes
    stderr: bytes
    elapsed_seconds: float
    timed_out: bool


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


def _prepend_output_note(path: Path, note: str) -> None:
    """Add a Luau comment without loading a potentially large result into RAM."""
    noted_path = path.with_name(path.name + ".noted")
    try:
        with noted_path.open("wb") as destination:
            destination.write(note.encode("utf-8"))
            with path.open("rb") as source:
                shutil.copyfileobj(source, destination, length=1024 * 1024)
        noted_path.replace(path)
    finally:
        noted_path.unlink(missing_ok=True)


def _kill_process_tree(process: asyncio.subprocess.Process) -> None:
    """Kill the deobfuscator and any Luau child it may have left behind."""
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        try:
            process.kill()
        except ProcessLookupError:
            pass


async def _run_process(command: list[str], settings: Settings) -> _ProcessRun:
    started = time.perf_counter()
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(ENGINE_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=(os.name == "posix"),
        )
    except OSError as exc:
        raise DeobfuscationError("No se pudo iniciar el motor de Luau.") from exc

    communication = asyncio.create_task(process.communicate())
    process_wait = asyncio.create_task(process.wait())
    done, _ = await asyncio.wait(
        {communication, process_wait},
        timeout=settings.process_timeout_seconds + 30,
        return_when=asyncio.FIRST_COMPLETED,
    )
    timed_out = not done
    if timed_out or communication not in done:
        # If the parent exits before its pipes close, a Luau child may still
        # hold those descriptors. End the isolated process group before retry.
        _kill_process_tree(process)
    stdout, stderr = await communication
    await process_wait

    return _ProcessRun(
        process=process,
        stdout=stdout,
        stderr=stderr,
        elapsed_seconds=time.perf_counter() - started,
        timed_out=timed_out,
    )


def _raise_process_failure(
    stderr: bytes,
    stdout: bytes,
    *,
    return_code: int | None,
    reason: str,
    timed_out: bool = False,
) -> None:
    log_path = _save_engine_log(
        stderr,
        stdout,
        return_code=return_code,
        reason=reason,
    )
    return_code_text = (
        _return_code_description(return_code) if return_code is not None else "unknown"
    )
    excerpt = _log_excerpt(stderr, stdout)
    if timed_out:
        message = (
            "El análisis agotó el tiempo permitido "
            f"(código de salida: {return_code_text})."
        )
    elif return_code == -signal.SIGKILL:
        message = (
            "El sistema terminó el motor con SIGKILL, normalmente por falta de "
            "memoria disponible. Se adjunta el log completo en un archivo `.txt`."
        )
    else:
        message = f"El motor terminó con error (código de salida: {return_code_text})."
    raise DeobfuscationError(
        f"{message}\nÚltima salida:\n```text\n{excerpt}\n```\n"
        "Se adjunta el log completo en un archivo `.txt`.",
        log_path=log_path,
        return_code=return_code,
    )


async def run_deobfuscator(
    input_path: Path,
    original_name: str,
    settings: Settings,
    *,
    no_devirt: bool = False,
    fast: bool = False,
    allow_fast_fallback: bool = True,
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

        full_requested = not fast and not no_devirt
        first_run = await _run_process(command, settings)
        if first_run.timed_out:
            _raise_process_failure(
                first_run.stderr,
                first_run.stdout,
                return_code=first_run.process.returncode,
                reason="The process exceeded the configured timeout and was terminated.",
                timed_out=True,
            )

        elapsed_seconds = first_run.elapsed_seconds
        stdout, stderr = first_run.stdout, first_run.stderr
        fallback_used = False
        if first_run.process.returncode == -signal.SIGKILL:
            # The main process may have been killed by Railway while a Luau
            # child is still alive. Reap the whole isolated process group
            # before starting a lower-memory retry.
            _kill_process_tree(first_run.process)

        if (
            first_run.process.returncode == -signal.SIGKILL
            and full_requested
            and allow_fast_fallback
        ):
            LOGGER.warning(
                "Full devirtualization for %s was terminated by SIGKILL; retrying in fast mode. "
                "Last full-mode output: %s",
                original_name,
                _log_excerpt(first_run.stderr, first_run.stdout),
            )
            fallback_run = await _run_process(
                [*command, "--no-devirt", "--no-fold"],
                settings,
            )
            elapsed_seconds += fallback_run.elapsed_seconds
            stdout = (
                first_run.stdout
                + b"\n\n[Full attempt terminated by SIGKILL; automatic fast retry follows]\n\n"
                + fallback_run.stdout
            )
            stderr = (
                first_run.stderr
                + b"\n\n[Full attempt terminated by SIGKILL; automatic fast retry follows]\n\n"
                + fallback_run.stderr
            )
            if fallback_run.process.returncode == -signal.SIGKILL:
                _kill_process_tree(fallback_run.process)
            if fallback_run.timed_out:
                _raise_process_failure(
                    stderr,
                    stdout,
                    return_code=fallback_run.process.returncode,
                    reason=(
                        "Full devirtualization was terminated by SIGKILL; "
                        "the automatic fast retry exceeded the configured timeout."
                    ),
                    timed_out=True,
                )
            if fallback_run.process.returncode != 0:
                fallback_reason = (
                    "Full devirtualization was terminated by SIGKILL; "
                    "the automatic fast retry also failed."
                )
                _raise_process_failure(
                    stderr,
                    stdout,
                    return_code=fallback_run.process.returncode,
                    reason=fallback_reason,
                )
            fallback_used = True
            LOGGER.warning(
                "Automatic fast retry completed for %s; returning a partial behavior trace.",
                original_name,
            )
        elif first_run.process.returncode != 0:
            _raise_process_failure(
                stderr,
                stdout,
                return_code=first_run.process.returncode,
                reason=(
                    "The full devirtualization process was terminated by SIGKILL."
                    if first_run.process.returncode == -signal.SIGKILL
                    else "The engine process returned a non-zero exit code."
                ),
            )

        diagnostics = (stderr + b"\n" + stdout).decode("utf-8", errors="replace").strip()
        if not output_path.exists():
            raise DeobfuscationError("El motor terminó sin generar un archivo de salida.")

        if fallback_used or fast or no_devirt:
            if fallback_used:
                note = (
                    "-- NOTE: Full devirtualization was terminated by the host "
                    "(SIGKILL). This automatic fast trace may be incomplete.\n\n"
                )
            else:
                note = (
                    "-- NOTE: Fast mode skips VM devirtualization. "
                    "This behavior trace may be incomplete.\n\n"
                )
            _prepend_output_note(output_path, note)

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
            mode=(
                "Fast fallback (partial)"
                if fallback_used
                else "Fast trace (partial)"
                if fast or no_devirt
                else "Full devirtualization"
            ),
            fallback_used=fallback_used,
        )