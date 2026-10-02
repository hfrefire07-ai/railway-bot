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


def ensure_engine_root() -> Path:
    """Extract and return the bundled engine; never use an external checkout."""
    if not BUNDLE_ZIP.is_file():
        raise RuntimeError(f"Bundled deobfuscator ZIP is missing: {BUNDLE_ZIP}")

    expected = RUNTIME_ROOT / "deobf" / "deob.py"
    if expected.is_file():
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

        if RUNTIME_ROOT.exists():
            shutil.rmtree(RUNTIME_ROOT)
        os.replace(extracted, RUNTIME_ROOT)
        return RUNTIME_ROOT
    finally:
        shutil.rmtree(temporary, ignore_errors=True)