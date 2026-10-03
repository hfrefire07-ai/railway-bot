from __future__ import annotations

import os
import shutil
import tempfile
import zipfile
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BUNDLE_ZIP = PROJECT_ROOT / "vendor" / "Deobfuscator.zip"
RUNTIME_ROOT = PROJECT_ROOT / ".runtime" / "Deobfuscator"


def _safe_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """Reject archive members that could escape the runtime directory."""
    members = archive.infolist()
    for member in members:
        path = Path(member.filename)
        if path.is_absolute() or ".." in path.parts:
            raise RuntimeError(f"Unsafe engine archive member: {member.filename!r}")
    return members


def _patch_child_exit_diagnostics(engine_root: Path) -> None:
    """Keep the Luau child process exit code and output in failed-run diagnostics."""
    harness = engine_root / "deobf" / "harness.py"
    source = harness.read_text(encoding="utf-8")
    replacements = (
        (
            '    return b"".join(parts[1]), b"".join(parts[2])',
            '    return b"".join(parts[1]), b"".join(parts[2]), proc.returncode',
        ),
        (
            "        out, errb = _communicate([luau, hpath], timeout, STALL if cfg.get(\"heartbeat\") else None)",
            "        out, errb, child_return_code = _communicate("
            "[luau, hpath], timeout, "
            "STALL if cfg.get(\"heartbeat\") and timeout > 0 else None)",
        ),
        (
            '        return None, stdout[-3000:] + "\\n" + errb.decode("utf-8", "replace")[-3000:]',
            '''        stderr = errb.decode("utf-8", "replace")
        return None, (
            "Luau child process exit code: %s\\n"
            "Captured stdout bytes: %d; stderr bytes: %d\\n"
            "--- STDOUT ---\\n%s\\n"
            "--- STDERR ---\\n%s"
            % (
                child_return_code,
                len(out),
                len(errb),
                stdout if stdout.strip() else "(empty or only heartbeat padding)",
                stderr or "(empty)",
            )
        )''',
        ),
    )
    for old, new in replacements:
        if new in source:
            continue
        if source.count(old) != 1:
            raise RuntimeError(
                f"Cannot safely patch Luau child diagnostics in {harness}: expected one source marker."
            )
        source = source.replace(old, new, 1)
    harness.write_text(source, encoding="utf-8", newline="\n")


def _patch_unlimited_time_limits(engine_root: Path) -> None:
    """Make the bundled engine use zero to mean that no time limit is set."""
    harness = engine_root / "deobf" / "harness.py"
    source = harness.read_text(encoding="utf-8")
    harness_replacements = (
        (
            "if now - t0 > timeout or (stall and now - last[0] > stall):",
            "if (timeout > 0 and now - t0 > timeout) or (stall and now - last[0] > stall):",
            "harness wall-clock timeout",
        ),
        (
            "_communicate([luau, hpath], timeout, STALL if cfg.get(\"heartbeat\") else None)",
            "_communicate([luau, hpath], timeout, "
            "STALL if cfg.get(\"heartbeat\") and timeout > 0 else None)",
            "harness stall timeout",
        ),
        (
            "        self.deadline = time.time() + timeout",
            "        self.deadline = time.time() + timeout if timeout > 0 else None",
            "harness server timeout",
        ),
    )
    for original, replacement, label in harness_replacements:
        if replacement in source:
            continue
        if source.count(original) != 1:
            raise RuntimeError(
                f"Cannot safely patch {label} in {harness}: expected one source marker."
            )
        source = source.replace(original, replacement, 1)
    close_original = (
        "    finally:\n"
        "        for t in threads:\n"
        "            t.join(5)\n"
    )
    close_replacement = (
        close_original
        + "        for stream in (proc.stdout, proc.stderr):\n"
        + "            if stream is not None:\n"
        + "                stream.close()\n"
    )
    if close_replacement not in source:
        if source.count(close_original) != 1:
            raise RuntimeError(
                f"Cannot safely close Luau process streams in {harness}: "
                "expected one source marker."
            )
        source = source.replace(close_original, close_replacement, 1)
    harness.write_text(source, encoding="utf-8", newline="\n")

    runtime = engine_root / "deobf" / "envlog.luau"
    source = runtime.read_text(encoding="utf-8")
    original = "if now - START > TIME_BUDGET then"
    replacement = "if TIME_BUDGET > 0 and now - START > TIME_BUDGET then"
    if replacement not in source:
        if source.count(original) != 1:
            raise RuntimeError(
                f"Cannot safely patch Luau trace budget in {runtime}: "
                "expected one source marker."
            )
        source = source.replace(original, replacement, 1)
        runtime.write_text(source, encoding="utf-8", newline="\n")


def ensure_engine_root() -> Path:
    """Extract and return the bundled engine; never use an external checkout."""
    if not BUNDLE_ZIP.is_file():
        raise RuntimeError(f"Bundled deobfuscator ZIP is missing: {BUNDLE_ZIP}")

    expected = RUNTIME_ROOT / "deobf" / "deob.py"
    if expected.is_file():
        _patch_child_exit_diagnostics(RUNTIME_ROOT)
        _patch_unlimited_time_limits(RUNTIME_ROOT)
        return RUNTIME_ROOT

    RUNTIME_ROOT.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="deobfuscator-", dir=RUNTIME_ROOT.parent))
    try:
        with zipfile.ZipFile(BUNDLE_ZIP) as archive:
            members = _safe_members(archive)
            archive.extractall(temporary, members=members)

        extracted = temporary / "Deobfuscator"
        if not (extracted / "deobf" / "deob.py").is_file():
            raise RuntimeError("Bundled ZIP does not contain Deobfuscator/deobf/deob.py")

        _patch_child_exit_diagnostics(extracted)
        _patch_unlimited_time_limits(extracted)

        if RUNTIME_ROOT.exists():
            shutil.rmtree(RUNTIME_ROOT)
        os.replace(extracted, RUNTIME_ROOT)
        return RUNTIME_ROOT
    finally:
        shutil.rmtree(temporary, ignore_errors=True)