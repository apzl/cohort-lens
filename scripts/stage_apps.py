"""Stage authoritative shared source into both self-contained Flower Apps.

Each bundle receives only the shared modules it imports: the analytics bundle
gets no investigator loop or tools, and the agent bundle gets no NumPy-based
statistics or data preparation. ``evidence.py`` is used by the operator only.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "shared" / "cohortlens_shared"
COMMON = ("__init__.py", "schemas.py", "catalog.py")
TARGETS: dict[Path, tuple[str, ...]] = {
    ROOT / "apps" / "cohort-agent" / "cohortlens_shared": COMMON
    + ("tools.py", "investigator.py"),
    ROOT / "apps" / "cohort-analytics" / "cohortlens_shared": COMMON
    + ("statistics.py", "gdsc.py"),
}


def files_digest(directory: Path, names: tuple[str, ...]) -> str:
    """Hash the names and contents of the selected staged Python files."""
    digest = hashlib.sha256()
    for name in sorted(names):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update((directory / name).read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def stage() -> dict[str, str]:
    """Replace generated copies and prove that each matches its source subset."""
    digests: dict[str, str] = {}
    for target, names in TARGETS.items():
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        for name in names:
            shutil.copy2(SOURCE / name, target / name)
        staged = sorted(path.name for path in target.glob("*.py"))
        if staged != sorted(names):
            raise RuntimeError(f"unexpected staged files at {target}")
        digest = files_digest(target, names)
        if digest != files_digest(SOURCE, names):
            raise RuntimeError(f"staged shared source differs at {target}")
        digests[target.parent.name] = digest
    return digests


if __name__ == "__main__":
    for app_name, digest in stage().items():
        print(f"staged cohortlens_shared into {app_name} sha256:{digest}")
