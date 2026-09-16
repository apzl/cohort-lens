"""Read-only checks for Flower App Bundle contents."""

from __future__ import annotations

from pathlib import Path
from zipfile import BadZipFile, ZipFile


class FabInspectionError(ValueError):
    """A FAB violates the CohortLens allowlist or sentinel policy."""


def inspect_fab(path: Path, forbidden_text: tuple[str, ...] = ()) -> list[str]:
    """Return sorted members after checking paths, extensions, and sentinels."""
    allowed_roots = {
        ".info",
        "cohortlens_agent",
        "cohortlens_analytics",
        "cohortlens_shared",
    }
    allowed_top_files = {"LICENSE", "pyproject.toml"}
    try:
        with ZipFile(path) as archive:
            names = sorted(archive.namelist())
            for name in names:
                parts = Path(name).parts
                if not parts or ".." in parts or name.startswith("/"):
                    raise FabInspectionError(f"unsafe FAB path: {name}")
                if len(parts) == 1:
                    if name not in allowed_top_files:
                        raise FabInspectionError(f"unexpected FAB member: {name}")
                elif parts[0] not in allowed_roots:
                    raise FabInspectionError(f"unexpected FAB root: {parts[0]}")
                if not (name.endswith((".py", ".toml")) or name == "LICENSE" or parts[0] == ".info"):
                    raise FabInspectionError(f"unexpected FAB file type: {name}")
                payload = archive.read(name)
                for sentinel in forbidden_text:
                    if sentinel.encode("utf-8") in payload:
                        raise FabInspectionError(
                            f"forbidden sentinel {sentinel!r} found in {name}"
                        )
    except BadZipFile as error:
        raise FabInspectionError(f"not a valid FAB zip archive: {path}") from error
    return names
