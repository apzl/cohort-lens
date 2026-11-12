"""Bundle and shared-source privacy checks."""

from pathlib import Path
from zipfile import ZipFile

import pytest

from cohortlens_operator.fab import FabInspectionError, inspect_fab

ROOT = Path(__file__).resolve().parents[1]


def _load_stage_module() -> None:
    import importlib.util
    import sys

    spec = importlib.util.spec_from_file_location("scripts_stage", ROOT / "scripts" / "stage_apps.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["scripts_stage"] = module
    spec.loader.exec_module(module)


_load_stage_module()


def test_staged_shared_sources_match_authoritative_tree() -> None:
    from scripts_stage import TARGETS

    source = ROOT / "shared" / "cohortlens_shared"
    for target, names in TARGETS.items():
        target_files = {
            path.name: path.read_bytes() for path in target.rglob("*.py")
        }
        assert sorted(target_files) == sorted(names)
        for name in names:
            assert target_files[name] == (source / name).read_bytes(), (target, name)


def test_bundles_receive_only_the_modules_they_import() -> None:
    agent = ROOT / "apps" / "cohort-agent" / "cohortlens_shared"
    analytics = ROOT / "apps" / "cohort-analytics" / "cohortlens_shared"
    assert not (agent / "statistics.py").exists()
    assert not (agent / "gdsc.py").exists()
    assert not (analytics / "investigator.py").exists()
    assert not (analytics / "tools.py").exists()
    for path in agent.glob("*.py"):
        assert "import numpy" not in path.read_text(), path


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

