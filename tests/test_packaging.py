"""Bundle and shared-source privacy checks."""

from pathlib import Path
from zipfile import ZipFile

import pytest

from cohortlens_operator.fab import FabInspectionError, inspect_fab

ROOT = Path(__file__).resolve().parents[1]


def test_staged_shared_sources_match_authoritative_tree() -> None:
    source = ROOT / "shared" / "cohortlens_shared"
    source_files = {
        path.relative_to(source): path.read_bytes() for path in source.rglob("*.py")
    }
    for app_name in ("cohort-agent", "cohort-analytics"):
        target = ROOT / "apps" / app_name / "cohortlens_shared"
        target_files = {
            path.relative_to(target): path.read_bytes() for path in target.rglob("*.py")
        }
        assert target_files == source_files


def test_fab_inspection_rejects_private_sentinel(tmp_path: Path) -> None:
    fab = tmp_path / "bad.fab"
    with ZipFile(fab, "w") as archive:
        archive.writestr("pyproject.toml", "PRIVATE_PATIENT_SENTINEL")
    with pytest.raises(FabInspectionError, match="forbidden sentinel"):
        inspect_fab(fab, ("PRIVATE_PATIENT_SENTINEL",))


def test_fab_inspection_rejects_unexpected_file(tmp_path: Path) -> None:
    fab = tmp_path / "bad.fab"
    with ZipFile(fab, "w") as archive:
        archive.writestr("data/patients.csv", "id,value")
    with pytest.raises(FabInspectionError, match="unexpected FAB root"):
        inspect_fab(fab)

