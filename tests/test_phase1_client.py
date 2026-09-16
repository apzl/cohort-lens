"""Site-local compatibility and release tests."""

import numpy as np
from flwr.app import ConfigRecord, Context, Message, RecordDict

from cohortlens_analytics.client_app import (
    _compatibility_status,
    dataset_from_context,
    handle_association,
    handle_preflight,
)
from cohortlens_shared.schemas import (
    AnalysisContract,
    Approval,
    AssociationReply,
    AssociationRequest,
    PreflightReply,
    StudyPreflightRequest,
    canonical_json,
    default_phase1_approval,
)
from cohortlens_shared.statistics import generate_synthetic_site, phase1_reference


def _message(record_name: str, payload: str, message_type: str) -> Message:
    return Message(
        content=RecordDict({record_name: ConfigRecord({"json": payload})}),
        dst_node_id=1,
        message_type=message_type,
    )


def _reply_json(message: Message, record_name: str) -> str:
    record = message.content[record_name]
    assert isinstance(record, ConfigRecord)
    payload = record["json"]
    assert isinstance(payload, str)
    return payload


def test_preflight_accepts_exact_supported_contract(
    phase1_contract: AnalysisContract, phase1_approval: Approval
) -> None:
    request = StudyPreflightRequest(
        query_id=phase1_contract.analysis_id,
        contract=phase1_contract,
        approval=phase1_approval,
    )
    reply_message = handle_preflight(
        _message("preflight_request", canonical_json(request), "query.preflight"),
        "site-1",
    )
    reply = PreflightReply.model_validate_json(
        _reply_json(reply_message, "preflight_reply")
    )
    assert reply.status == "READY"


def test_site_rejects_unsupported_covariates_even_with_matching_new_approval(
    phase1_contract: AnalysisContract,
) -> None:
    changed = phase1_contract.model_copy(update={"covariate_ids": ("age_z_v1",)})
    approval = default_phase1_approval(changed)
    design, outcome = generate_synthetic_site(0)
    status, reason = _compatibility_status(changed, approval, design, outcome)
    assert status == "MISSING_COVARIATE"
    assert reason == "COVARIATE_SET_MISMATCH"


def test_site_rejects_undersized_cohort(
    phase1_contract: AnalysisContract, phase1_approval: Approval
) -> None:
    design, outcome = generate_synthetic_site(0, n=50)
    status, reason = _compatibility_status(
        phase1_contract, phase1_approval, design, outcome
    )
    assert status == "INSUFFICIENT_DATA"
    assert reason == "MINIMUM_SAMPLE_SIZE"


def test_site_rejects_incompatible_reference_build(
    phase1_contract: AnalysisContract,
) -> None:
    changed_variant = phase1_contract.variant.model_copy(
        update={"reference_build": "SYNTHETIC_V2"}
    )
    changed = phase1_contract.model_copy(update={"variant": changed_variant})
    approval = default_phase1_approval(changed)
    design, outcome = generate_synthetic_site(0)
    status, reason = _compatibility_status(changed, approval, design, outcome)
    assert status == "INCOMPATIBLE_VARIANT"
    assert reason == "VARIANT_DEFINITION_MISMATCH"


def test_association_releases_only_reference_summary(
    phase1_contract: AnalysisContract, phase1_approval: Approval
) -> None:
    request = AssociationRequest(
        query_id=phase1_contract.analysis_id,
        contract=phase1_contract,
        approval=phase1_approval,
    )
    reply_message = handle_association(
        _message("association_request", canonical_json(request), "query.associate"),
        "site-1",
    )
    payload = _reply_json(reply_message, "association_reply")
    reply = AssociationReply.model_validate_json(payload)
    reference, _ = phase1_reference()
    assert reply.status == "ESTIMATED"
    assert reply.beta == reference[0].beta
    assert reply.standard_error == reference[0].standard_error
    assert reply.n_used == 1500
    assert "genotype" not in payload
    assert "biomarker" not in payload
    assert "patient" not in payload.lower()


def test_deployment_context_reads_its_configured_local_snapshot(tmp_path) -> None:
    design, outcome = generate_synthetic_site(1)
    snapshot = tmp_path / "site-2.npz"
    np.savez(snapshot, design=design, outcome=outcome)
    context = Context(
        run_id=1,
        node_id=2,
        node_config={
            "site-alias": "site-2",
            "dataset-path": str(snapshot),
            "snapshot-alias": "deployment-site-2-v1",
        },
        state=RecordDict(),
        run_config={},
    )
    loaded_design, loaded_outcome, alias = dataset_from_context(context, "site-2")
    np.testing.assert_array_equal(loaded_design, design)
    np.testing.assert_array_equal(loaded_outcome, outcome)
    assert alias == "deployment-site-2-v1"
