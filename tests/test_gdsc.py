"""Minimal real-data adapter and reused federated-analysis tests."""

from __future__ import annotations

import hashlib

import numpy as np
import pandas as pd
from flwr.app import ConfigRecord, Message, RecordDict

from cohortlens_analytics.client_app import handle_association, handle_preflight
from cohortlens_shared.gdsc import prepare_gdsc_snapshots
from cohortlens_shared.schemas import (
    AssociationReply,
    AssociationRequest,
    PreflightReply,
    StudyPreflightRequest,
    canonical_json,
    default_gdsc_contract,
    default_phase1_approval,
)
from cohortlens_shared.statistics import load_site_dataset


def _partition(model_id: str) -> int:
    return int(hashlib.sha256(model_id.encode()).hexdigest(), 16) % 3


def _source_fixture(tmp_path):
    ids: dict[int, list[str]] = {0: [], 1: [], 2: []}
    index = 0
    while any(len(items) < 12 for items in ids.values()):
        model_id = f"SIDM_TEST_{index:04d}"
        part = _partition(model_id)
        if len(ids[part]) < 12:
            ids[part].append(model_id)
        index += 1

    rows = []
    models = []
    mutations = []
    for part, model_ids in ids.items():
        for offset, model_id in enumerate(model_ids):
            mutant = offset < 4
            skcm = offset % 2 == 0
            rows.append(
                {
                    "SANGER_MODEL_ID": model_id,
                    "TCGA_DESC": "SKCM" if skcm else "LUAD",
                    "DRUG_ID": 1036,
                    "DRUG_NAME": "PLX-4720",
                    "LN_IC50": float(2.0 - mutant + 0.25 * skcm + offset / 100),
                }
            )
            models.append({"model_id": model_id, "mutation_data": True})
            if mutant:
                mutations.append(
                    {
                        "model_id": model_id,
                        "gene_symbol": "BRAF",
                        "protein_mutation": "p.V600E",
                    }
                )

    rows.extend(
        [
            {
                "SANGER_MODEL_ID": "SIDM_OTHER_BRAF",
                "TCGA_DESC": "SKCM",
                "DRUG_ID": 1036,
                "DRUG_NAME": "PLX-4720",
                "LN_IC50": -9.0,
            },
            {
                "SANGER_MODEL_ID": "SIDM_UNSEQUENCED",
                "TCGA_DESC": "SKCM",
                "DRUG_ID": 1036,
                "DRUG_NAME": "PLX-4720",
                "LN_IC50": -9.0,
            },
        ]
    )
    models.extend(
        [
            {"model_id": "SIDM_OTHER_BRAF", "mutation_data": True},
            {"model_id": "SIDM_UNSEQUENCED", "mutation_data": False},
        ]
    )
    mutations.append(
        {
            "model_id": "SIDM_OTHER_BRAF",
            "gene_symbol": "BRAF",
            "protein_mutation": "p.G469A",
        }
    )
    responses = tmp_path / "responses.csv"
    model_list = tmp_path / "models.csv"
    mutation_summary = tmp_path / "mutations.csv"
    pd.DataFrame(rows).to_csv(responses, index=False)
    pd.DataFrame(models).to_csv(model_list, index=False)
    pd.DataFrame(mutations).to_csv(mutation_summary, index=False)
    return responses, model_list, mutation_summary


def _message(name: str, payload: str, message_type: str) -> Message:
    return Message(
        RecordDict({name: ConfigRecord({"json": payload})}),
        dst_node_id=1,
        message_type=message_type,
    )


def test_gdsc_adapter_reuses_row_free_snapshots(tmp_path) -> None:
    responses, models, mutations = _source_fixture(tmp_path)
    output = tmp_path / "prepared"
    manifest = prepare_gdsc_snapshots(
        responses_path=responses,
        models_path=models,
        mutations_path=mutations,
        output_dir=output,
    )
    assert manifest["synthetic"] is False
    assert manifest["excluded_other_braf_driver_models"] == 1
    assert manifest["excluded_without_mutation_coverage"] == 1
    assert [site["rows"] for site in manifest["sites"]] == [12, 12, 12]
    for index in range(1, 4):
        with np.load(output / f"site-{index}.npz", allow_pickle=False) as archive:
            assert set(archive.files) == {"design", "outcome"}
            assert archive["design"].shape == (12, 3)


def test_gdsc_contract_runs_through_existing_client_handlers(tmp_path) -> None:
    responses, models, mutations = _source_fixture(tmp_path)
    output = tmp_path / "prepared"
    prepare_gdsc_snapshots(
        responses_path=responses,
        models_path=models,
        mutations_path=mutations,
        output_dir=output,
    )
    design, outcome = load_site_dataset(output / "site-1.npz")
    contract = default_gdsc_contract()
    approval = default_phase1_approval(contract)
    preflight = StudyPreflightRequest(
        query_id=contract.analysis_id, contract=contract, approval=approval
    )
    preflight_reply = handle_preflight(
        _message("preflight_request", canonical_json(preflight), "query.preflight"),
        "site-1",
        design=design,
        outcome=outcome,
        snapshot_alias="gdsc-test-site-1",
    )
    parsed_preflight = PreflightReply.model_validate_json(
        preflight_reply.content["preflight_reply"]["json"]
    )
    assert parsed_preflight.status == "READY"
    assert parsed_preflight.synthetic is False

    request = AssociationRequest(
        query_id=contract.analysis_id, contract=contract, approval=approval
    )
    reply = handle_association(
        _message("association_request", canonical_json(request), "query.associate"),
        "site-1",
        design=design,
        outcome=outcome,
        snapshot_alias="gdsc-test-site-1",
    )
    parsed = AssociationReply.model_validate_json(
        reply.content["association_reply"]["json"]
    )
    assert parsed.status == "ESTIMATED"
    assert parsed.synthetic is False
    assert parsed.policy_version == "PUBLIC_GDSC_RELEASE_V1"
    assert parsed.beta is not None and parsed.beta < 0
