"""Site-local Phase 1 preflight and association actions."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from flwr.app import ConfigRecord, Context, Message, RecordDict
from flwr.clientapp import ClientApp
from pydantic import ValidationError

from cohortlens_shared.schemas import (
    AnalysisContract,
    AssociationReply,
    AssociationRequest,
    Approval,
    PreflightReply,
    PreflightRequest,
    StudyPreflightRequest,
    canonical_json,
    contract_digest,
    validate_contract_approval,
)
from cohortlens_shared.statistics import (
    binary_group_count,
    complete_cases,
    context_stratum,
    fit_genotype_ols_hc3,
    generate_synthetic_site,
    load_site_dataset,
    minor_allele_count,
)

MAX_PAYLOAD_BYTES = 64 * 1024
MIN_SAMPLE_SIZE = 100
MIN_MINOR_ALLELE_COUNT = 20
MIN_GDSC_SAMPLE_SIZE = 10
MIN_GDSC_GROUP_COUNT = 3
app = ClientApp()


def site_alias_from_context(context: Context) -> str:
    """Resolve a non-sensitive site alias from local runtime configuration."""
    configured = context.node_config.get("site-alias")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    partition_id = context.node_config.get("partition-id")
    if isinstance(partition_id, int) and partition_id >= 0:
        return f"site-{partition_id + 1}"
    return f"site-node-{context.node_id}"


def _payload(message: Message, record_name: str) -> str:
    record = message.content.get(record_name)
    if not isinstance(record, ConfigRecord):
        raise ValueError(f"{record_name} ConfigRecord is required")
    payload = record.get("json")
    if not isinstance(payload, str):
        raise ValueError(f"{record_name}.json must be a string")
    if len(payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"{record_name}.json exceeds the payload limit")
    return payload


def _site_index(site_alias: str) -> int:
    try:
        site_number = int(site_alias.removeprefix("site-"))
    except ValueError as error:
        raise ValueError("Phase 1 synthetic aliases must be site-1 through site-3") from error
    if site_number not in (1, 2, 3):
        raise ValueError("Phase 1 synthetic aliases must be site-1 through site-3")
    return site_number - 1


def dataset_from_context(
    context: Context, site_alias: str
) -> tuple[np.ndarray, np.ndarray, str]:
    """Resolve either a deployment-local snapshot or a simulation fixture."""
    configured_path = context.node_config.get("dataset-path")
    if configured_path is None:
        design, outcome = generate_synthetic_site(_site_index(site_alias))
        return design, outcome, f"synthetic-{site_alias}-v1"
    if not isinstance(configured_path, str) or not configured_path.strip():
        raise ValueError("dataset-path must be a non-empty local path")
    snapshot_alias = context.node_config.get("snapshot-alias")
    if not isinstance(snapshot_alias, str) or not snapshot_alias.strip():
        raise ValueError("deployment nodes require a snapshot-alias")
    design, outcome = load_site_dataset(Path(configured_path))
    return design, outcome, snapshot_alias.strip()


def _compatibility_status(
    contract: AnalysisContract,
    approval: Approval,
    design,
    outcome,
) -> tuple[str, str | None]:
    try:
        validate_contract_approval(contract, approval)
    except ValueError:
        return "NOT_AUTHORIZED", "APPROVAL_INVALID"

    synthetic = contract.variant.reference_build == "SYNTHETIC_V1"
    gdsc = contract.variant.reference_build == "CMP_DRIVER_SUMMARY_PINNED"
    if (synthetic and approval.scope != "SYNTHETIC_DEMO") or (
        gdsc and approval.scope != "PUBLIC_GDSC_EMULATED_FEDERATION"
    ):
        return "NOT_AUTHORIZED", "APPROVAL_SCOPE_MISMATCH"
    supported_variant = (
        contract.variant.reference_build == "SYNTHETIC_V1"
        and contract.variant.key == "demo:1:1000:A:G"
        and contract.variant.effect_allele == "G"
        and contract.variant.dosage == "effect_allele_count"
    ) or (
        gdsc
        and contract.variant.key == "BRAF:p.V600E"
        and contract.variant.effect_allele == "PRESENT"
        and contract.variant.dosage == "binary_presence"
    )
    if not supported_variant:
        return "INCOMPATIBLE_VARIANT", "VARIANT_DEFINITION_MISMATCH"
    supported_phenotype = (
        synthetic
        and contract.phenotype_id == "biomarker_y_v1"
        and contract.phenotype_unit == "demo_unit"
    ) or (
        gdsc
        and contract.phenotype_id == "GDSC2_PLX4720_LN_IC50"
        and contract.phenotype_unit == "ln_micromolar"
    )
    if not supported_phenotype:
        return "INCOMPATIBLE_PHENOTYPE", "PHENOTYPE_DEFINITION_MISMATCH"
    primary = (
        (
            synthetic
            and contract.covariate_ids
            == ("age_z_v1", "sex_code_v1", "context_v1")
        )
        or (gdsc and contract.covariate_ids == ("TCGA_SKCM_VS_OTHER_V1",))
    ) and not contract.strata
    stratified = synthetic and (
        contract.template_id == "ols_quantitative_context_stratified_v1"
        and contract.covariate_ids == ("age_z_v1", "sex_code_v1")
        and contract.strata == ("context_0", "context_1")
        and contract.parent_result_hash is not None
        and contract.max_association_rounds == 2
    )
    gdsc_stratified = gdsc and (
        contract.template_id == "ols_quantitative_lineage_stratified_v1"
        and contract.covariate_ids == ("INTERCEPT_ONLY_AFTER_STRATIFICATION",)
        and contract.strata == ("non_skcm", "skcm")
        and contract.parent_result_hash is not None
        and contract.max_association_rounds == 2
    )
    if not primary and not stratified and not gdsc_stratified:
        return "MISSING_COVARIATE", "COVARIATE_SET_MISMATCH"
    supported_policy = contract.missingness_policy == "COMPLETE_CASE_FIXED" and (
        synthetic
        and contract.template_id
        in {"ols_quantitative_v1", "ols_quantitative_context_stratified_v1"}
        and contract.cohort_rule_id == "all_synthetic_adults_v1"
        and contract.statistical_policy_id == "ols_hc3_fixed_effect_v1"
        and contract.privacy_policy_id == "synthetic_release_v1"
        or gdsc
        and contract.template_id
        in {"ols_quantitative_v1", "ols_quantitative_lineage_stratified_v1"}
        and contract.cohort_rule_id == "GDSC2_PLX4720_MUTATION_COVERED_V1"
        and contract.statistical_policy_id == "OLS_HC3_FIXED_EFFECT_V1"
        and contract.privacy_policy_id == "PUBLIC_GDSC_RELEASE_V1"
    )
    if not supported_policy:
        return "POLICY_REJECTED", "UNSUPPORTED_ANALYSIS_POLICY"

    filtered_design, _ = complete_cases(design, outcome)
    minimum_n = MIN_GDSC_SAMPLE_SIZE if gdsc else MIN_SAMPLE_SIZE
    if len(filtered_design) < minimum_n:
        return "INSUFFICIENT_DATA", "MINIMUM_SAMPLE_SIZE"
    try:
        support_count = (
            binary_group_count(filtered_design)
            if gdsc
            else minor_allele_count(filtered_design)
        )
    except ValueError:
        return "INSUFFICIENT_DATA", "INVALID_DOSAGE"
    minimum_group = MIN_GDSC_GROUP_COUNT if gdsc else MIN_MINOR_ALLELE_COUNT
    if support_count < minimum_group:
        return "INSUFFICIENT_DATA", (
            "MINIMUM_GROUP_COUNT" if gdsc else "MINIMUM_ALLELE_COUNT"
        )
    return "READY", None


def handle_preflight(
    message: Message,
    site_alias: str,
    *,
    design: np.ndarray | None = None,
    outcome: np.ndarray | None = None,
    snapshot_alias: str | None = None,
) -> Message:
    """Validate either the retained Phase 0 or approved Phase 1 preflight."""
    if (design is None) != (outcome is None):
        raise ValueError("design and outcome must be supplied together")
    if design is None or outcome is None:
        design, outcome = generate_synthetic_site(_site_index(site_alias))
    resolved_snapshot = snapshot_alias or f"synthetic-{site_alias}-v1"
    payload = _payload(message, "preflight_request")
    try:
        request = StudyPreflightRequest.model_validate_json(payload)
    except ValidationError:
        legacy = PreflightRequest.model_validate_json(payload)
        reply = PreflightReply(
            study_id=legacy.study_id,
            query_id=legacy.query_id,
            contract_hash=legacy.contract_hash,
            site_alias=site_alias,
            snapshot_alias=resolved_snapshot,
            status="READY",
        )
    else:
        status, reason = _compatibility_status(
            request.contract, request.approval, design, outcome
        )
        reply = PreflightReply(
            study_id=request.contract.study_id,
            query_id=request.query_id,
            contract_hash=contract_digest(request.contract),
            site_alias=site_alias,
            snapshot_alias=resolved_snapshot,
            status=status,
            reason_code=reason,
            synthetic=request.contract.variant.reference_build == "SYNTHETIC_V1",
        )
    content = RecordDict(
        {"preflight_reply": ConfigRecord({"json": canonical_json(reply)})}
    )
    return Message(content, reply_to=message)


def handle_association(
    message: Message,
    site_alias: str,
    *,
    design: np.ndarray | None = None,
    outcome: np.ndarray | None = None,
    snapshot_alias: str | None = None,
) -> Message:
    """Run the fixed regression locally and release only its approved summary."""
    if (design is None) != (outcome is None):
        raise ValueError("design and outcome must be supplied together")
    if design is None or outcome is None:
        design, outcome = generate_synthetic_site(_site_index(site_alias))
    resolved_snapshot = snapshot_alias or f"synthetic-{site_alias}-v1"
    request = AssociationRequest.model_validate_json(
        _payload(message, "association_request")
    )
    compatibility, reason = _compatibility_status(
        request.contract, request.approval, design, outcome
    )
    if request.stratum_id != "all" and request.stratum_id not in request.contract.strata:
        compatibility, reason = "UNESTIMABLE", "UNDECLARED_STRATUM"
    common = {
        "study_id": request.contract.study_id,
        "analysis_id": request.contract.analysis_id,
        "query_id": request.query_id,
        "contract_hash": contract_digest(request.contract),
        "site_alias": site_alias,
        "snapshot_alias": resolved_snapshot,
        "stratum_id": request.stratum_id,
        "policy_version": request.contract.privacy_policy_id,
        "synthetic": request.contract.variant.reference_build == "SYNTHETIC_V1",
    }
    if compatibility != "READY":
        status = "NOT_AUTHORIZED" if compatibility == "NOT_AUTHORIZED" else "UNESTIMABLE"
        reply = AssociationReply(status=status, reason_code=reason, **common)
    else:
        try:
            if request.stratum_id != "all":
                design, outcome = context_stratum(design, outcome, request.stratum_id)
                minimum_n = (
                    MIN_GDSC_SAMPLE_SIZE
                    if request.contract.variant.reference_build
                    == "CMP_DRIVER_SUMMARY_PINNED"
                    else MIN_SAMPLE_SIZE
                )
                if len(design) < minimum_n:
                    raise ValueError("insufficient stratum sample size")
                if (
                    request.contract.variant.reference_build
                    == "CMP_DRIVER_SUMMARY_PINNED"
                    and binary_group_count(design) < MIN_GDSC_GROUP_COUNT
                ):
                    raise ValueError("insufficient stratum group support")
            estimate = fit_genotype_ols_hc3(site_alias, design, outcome)
            reply = AssociationReply(
                status="ESTIMATED",
                beta=estimate.beta,
                standard_error=estimate.standard_error,
                n_used=estimate.n_used,
                estimator="OLS_HC3",
                **common,
            )
        except (ValueError, ArithmeticError):
            reply = AssociationReply(
                status="UNESTIMABLE",
                reason_code="LOCAL_MODEL_UNESTIMABLE",
                **common,
            )
    content = RecordDict(
        {"association_reply": ConfigRecord({"json": canonical_json(reply)})}
    )
    return Message(content, reply_to=message)


@app.query("preflight")
def preflight(message: Message, context: Context) -> Message:
    """Flower action for compatibility checks."""
    site_alias = site_alias_from_context(context)
    design, outcome, snapshot_alias = dataset_from_context(context, site_alias)
    return handle_preflight(
        message,
        site_alias,
        design=design,
        outcome=outcome,
        snapshot_alias=snapshot_alias,
    )


@app.query("associate")
def associate(message: Message, context: Context) -> Message:
    """Flower action for the approved local association calculation."""
    site_alias = site_alias_from_context(context)
    design, outcome, snapshot_alias = dataset_from_context(context, site_alias)
    return handle_association(
        message,
        site_alias,
        design=design,
        outcome=outcome,
        snapshot_alias=snapshot_alias,
    )
