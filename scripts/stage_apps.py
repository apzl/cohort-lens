"""Stage authoritative shared source into both self-contained Flower Apps."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "shared" / "cohortlens_shared"
TARGETS = (
    ROOT / "apps" / "cohort-agent" / "cohortlens_shared",
    ROOT / "apps" / "cohort-analytics" / "cohortlens_shared",
)


def tree_digest(path: Path) -> str:
    """Hash relative paths and contents of all staged Python files."""
    digest = hashlib.sha256()
    for file_path in sorted(path.rglob("*.py")):
        digest.update(file_path.relative_to(path).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def stage() -> str:
    """Replace generated copies and prove that both match the source."""
    source_digest = tree_digest(SOURCE)
    for target in TARGETS:
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(
            SOURCE,
            target,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
        if tree_digest(target) != source_digest:
            raise RuntimeError(f"staged shared source differs at {target}")
    return source_digest


if __name__ == "__main__":
    print(f"staged cohortlens_shared sha256:{stage()}")

