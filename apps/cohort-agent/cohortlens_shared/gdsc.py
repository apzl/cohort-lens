"""Minimal GDSC2-to-CohortLens snapshot adapter."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np

from .schemas import canonical_json, default_gdsc_contract

GDSC2_RELEASE = "8.5-27Oct23"
COMPOUND_ID = 1036
COMPOUND_NAME = "PLX-4720"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_table(path: Path, *, usecols: list[str]):
    import pandas as pd

    if path.suffix == ".xlsx":
        return pd.read_excel(path, usecols=usecols)
    return pd.read_csv(path, usecols=usecols, low_memory=False)


def _covered(value: object) -> bool:
    return str(value).strip().lower() in {"true", "1", "1.0"}


def _partition(model_id: str) -> int:
    """Assign a model without inspecting its response or mutation status."""
    return int(hashlib.sha256(model_id.encode("utf-8")).hexdigest(), 16) % 3


def prepare_gdsc_snapshots(
    *,
    responses_path: Path,
    models_path: Path,
    mutations_path: Path,
    output_dir: Path,
    overwrite: bool = False,
) -> dict[str, object]:
    """Create three row-free local matrices for PLX-4720/BRAF V600E."""
    for path in (responses_path, models_path, mutations_path):
        if not path.is_file():
            raise ValueError(f"source file does not exist: {path}")

    responses = _read_table(
        responses_path,
        usecols=[
            "SANGER_MODEL_ID",
            "TCGA_DESC",
            "DRUG_ID",
            "DRUG_NAME",
            "LN_IC50",
        ],
    )
    models = _read_table(models_path, usecols=["model_id", "mutation_data"])
    mutations = _read_table(
        mutations_path,
        usecols=["model_id", "gene_symbol", "protein_mutation"],
    )

    responses = responses.loc[
        (responses["DRUG_ID"] == COMPOUND_ID)
        & (responses["DRUG_NAME"] == COMPOUND_NAME)
    ].copy()
    if responses.empty:
        raise ValueError("the pinned PLX-4720 compound was not found")
    if responses["SANGER_MODEL_ID"].isna().any():
        raise ValueError("PLX-4720 responses contain missing model identifiers")
    if responses["SANGER_MODEL_ID"].duplicated().any():
        raise ValueError("PLX-4720 contains duplicate model responses")

    braf = mutations.loc[mutations["gene_symbol"] == "BRAF"]
    v600e_ids = set(
        braf.loc[braf["protein_mutation"] == "p.V600E", "model_id"].dropna()
    )
    other_braf_ids = set(
        braf.loc[braf["protein_mutation"] != "p.V600E", "model_id"].dropna()
    )
    covered_ids = set(
        models.loc[models["mutation_data"].map(_covered), "model_id"].dropna()
    )

    responses["mutation_covered"] = responses["SANGER_MODEL_ID"].isin(covered_ids)
    responses["braf_v600e"] = responses["SANGER_MODEL_ID"].isin(v600e_ids)
    responses["other_braf_driver"] = responses["SANGER_MODEL_ID"].isin(
        other_braf_ids
    )
    eligible = responses.loc[
        responses["mutation_covered"] & ~responses["other_braf_driver"]
    ].copy()
    eligible["skcm"] = eligible["TCGA_DESC"] == "SKCM"
    eligible["partition"] = eligible["SANGER_MODEL_ID"].map(_partition)

    output_dir.mkdir(parents=True, exist_ok=True)
    paths = [output_dir / f"site-{index}.npz" for index in range(1, 4)]
    existing = [str(path) for path in paths if path.exists()]
    if existing and not overwrite:
        raise ValueError("refusing to overwrite existing snapshots: " + ", ".join(existing))

    site_entries: list[dict[str, object]] = []
    for partition, path in enumerate(paths):
        local = eligible.loc[eligible["partition"] == partition]
        # Reuse the existing coefficient-at-column-1 OLS seam. The final column
        # is also the two-level lineage marker used by the bounded follow-up.
        design = np.column_stack(
            [
                np.ones(len(local), dtype=float),
                local["braf_v600e"].to_numpy(dtype=float),
                local["skcm"].to_numpy(dtype=float),
            ]
        )
        outcome = local["LN_IC50"].to_numpy(dtype=float)
        finite = np.isfinite(outcome)
        design, outcome = design[finite], outcome[finite]
        mutant = int(design[:, 1].sum())
        comparator = int(len(design) - mutant)
        if len(design) < 10 or min(mutant, comparator) < 3:
            raise ValueError(f"site-{partition + 1} lacks primary model support")
        if np.linalg.matrix_rank(design) != design.shape[1]:
            raise ValueError(f"site-{partition + 1} has a rank-deficient design")
        np.savez(path, design=design, outcome=outcome)
        path.chmod(0o600)
        site_entries.append(
            {
                "site_alias": f"site-{partition + 1}",
                "snapshot_alias": f"gdsc2-plx4720-site-{partition + 1}-v1",
                "dataset_path": str(path.resolve()),
                "sha256": _sha256(path),
                "rows": int(len(design)),
                "braf_v600e": mutant,
                "no_recorded_braf_driver": comparator,
                "skcm": int(design[:, 2].sum()),
            }
        )

    manifest: dict[str, object] = {
        "schema_version": "1.0",
        "synthetic": False,
        "data_mode": "PUBLIC_GDSC_EMULATED_FEDERATION",
        "format": "COHORTLENS_GDSC_NPZ_V1",
        "gdsc_release": GDSC2_RELEASE,
        "compound": {"id": COMPOUND_ID, "name": COMPOUND_NAME},
        "biomarker": "BRAF p.V600E present",
        "comparator": "no recorded BRAF driver mutation in mutation-covered models",
        "excluded_other_braf_driver_models": int(
            responses["other_braf_driver"].sum()
        ),
        "excluded_without_mutation_coverage": int(
            (~responses["mutation_covered"]).sum()
        ),
        "partition_rule": "sha256(SANGER_MODEL_ID) modulo 3",
        "contract": json.loads(canonical_json(default_gdsc_contract())),
        "sources": {
            "responses": {"filename": responses_path.name, "sha256": _sha256(responses_path)},
            "models": {"filename": models_path.name, "sha256": _sha256(models_path)},
            "mutations": {"filename": mutations_path.name, "sha256": _sha256(mutations_path)},
        },
        "sites": site_entries,
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    manifest_path.chmod(0o600)
    return manifest
