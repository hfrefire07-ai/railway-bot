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
            "[luau, hpath], timeout, STALL if cfg.get(\"heartbeat\") else None)",
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


def ensure_engine_root() -> Path:
    """Extract and return the bundled engine; never use an external checkout."""
    if not BUNDLE_ZIP.is_file():
        raise RuntimeError(f"Bundled deobfuscator ZIP is missing: {BUNDLE_ZIP}")

    expected = RUNTIME_ROOT / "deobf" / "deob.py"
    if expected.is_file():
        _patch_child_exit_diagnostics(RUNTIME_ROOT)
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

        if RUNTIME_ROOT.exists():
            shutil.rmtree(RUNTIME_ROOT)
        os.replace(extracted, RUNTIME_ROOT)
        return RUNTIME_ROOT
    finally:
        shutil.rmtree(temporary, ignore_errors=True)