"""Versioned, strict contracts shared by CohortLens components."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Annotated, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

SCHEMA_VERSION = "1.0"
PHASE0_SCHEMA_VERSION = SCHEMA_VERSION
Identifier = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$"),
]
ContractHash = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
ShortText = Annotated[str, Field(min_length=1, max_length=1000)]


class StrictModel(BaseModel):
    """Base contract rejecting unknown fields and non-finite numbers."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class VariantSpec(StrictModel):
    """Normalized identity and coding of the single demo variant."""

    reference_build: Identifier
    key: Identifier
    effect_allele: Annotated[str, Field(min_length=1, max_length=64)]
    dosage: Literal["effect_allele_count", "binary_presence"] = (
        "effect_allele_count"
    )


class AnalysisContract(StrictModel):
    """Exact, non-executable Phase 1 scientific contract."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    study_id: Identifier
    analysis_id: Identifier
    parent_result_hash: ContractHash | None = None
    operation: Literal["ASSOCIATE"] = "ASSOCIATE"
    template_id: Identifier
    variant: VariantSpec
    phenotype_id: Identifier
    phenotype_unit: Identifier
    covariate_ids: tuple[Identifier, ...]
    cohort_rule_id: Identifier
    missingness_policy: Literal["COMPLETE_CASE_FIXED"] = "COMPLETE_CASE_FIXED"
    strata: tuple[Identifier, ...] = ()
    site_roster_id: Identifier
    privacy_policy_id: Identifier
    statistical_policy_id: Identifier
    expected_sites: Annotated[int, Field(ge=1, le=256)]
    max_association_rounds: Annotated[int, Field(ge=1, le=2)] = 2

    @field_validator("covariate_ids")
    @classmethod
    def validate_covariates(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(value) > 32 or len(set(value)) != len(value):
            raise ValueError("covariate_ids must be a non-empty unique list")
        return value


class Approval(StrictModel):
    """Phase 1 operator attestation bound to one contract digest."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    approval_id: Identifier
    study_id: Identifier
    analysis_id: Identifier
    contract_hash: ContractHash
    approved_by: Identifier
    scope: Literal["SYNTHETIC_DEMO", "PUBLIC_GDSC_EMULATED_FEDERATION"] = (
        "SYNTHETIC_DEMO"
    )
    approved_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def validate_times(self) -> Approval:
        if self.approved_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("approval timestamps must include a timezone")
        if self.expires_at <= self.approved_at:
            raise ValueError("approval must expire after it is granted")
        return self


class PreflightRequest(StrictModel):
    """Legacy Phase 0 request retained as a regression contract."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    study_id: Identifier
    query_id: Identifier
    contract_hash: ContractHash
    operation: Literal["PREFLIGHT"] = "PREFLIGHT"
    synthetic_only: Literal[True] = True


class StudyPreflightRequest(StrictModel):
    """Approved contract sent for site-local compatibility checks."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    query_id: Identifier
    operation: Literal["PREFLIGHT"] = "PREFLIGHT"
    contract: AnalysisContract
    approval: Approval


class AssociationRequest(StrictModel):
    """Approved request sent only to sites that passed preflight."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    query_id: Identifier
    operation: Literal["ASSOCIATE"] = "ASSOCIATE"
    contract: AnalysisContract
    approval: Approval
    stratum_id: Identifier = "all"


PreflightStatus: TypeAlias = Literal[
    "READY",
    "NOT_AUTHORIZED",
    "INCOMPATIBLE_VARIANT",
    "INCOMPATIBLE_PHENOTYPE",
    "MISSING_COVARIATE",
    "INSUFFICIENT_DATA",
    "POLICY_REJECTED",
    "ERROR",
]


class PreflightReply(StrictModel):
    """Site readiness response containing no row-level values."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    study_id: Identifier
    query_id: Identifier
    contract_hash: ContractHash
    site_alias: Identifier
    snapshot_alias: Identifier
    status: PreflightStatus
    reason_code: Identifier | None = None
    synthetic: bool = True


class AssociationReply(StrictModel):
    """Allowlisted site-level statistical release."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    study_id: Identifier
    analysis_id: Identifier
    query_id: Identifier
    contract_hash: ContractHash
    site_alias: Identifier
    snapshot_alias: Identifier
    stratum_id: Identifier = "all"
    status: Literal[
        "ESTIMATED", "SUPPRESSED", "UNESTIMABLE", "NOT_AUTHORIZED", "ERROR"
    ]
    beta: float | None = None
    standard_error: Annotated[float, Field(gt=0)] | None = None
    n_used: Annotated[int, Field(ge=1)] | None = None
    estimator: Literal["OLS_HC3"] | None = None
    policy_version: Identifier = "synthetic_release_v1"
    reason_code: Identifier | None = None
    synthetic: bool = True

    @model_validator(mode="after")
    def validate_estimate_fields(self) -> AssociationReply:
        values = (self.beta, self.standard_error, self.n_used, self.estimator)
        if self.status == "ESTIMATED":
            if any(value is None for value in values):
                raise ValueError("ESTIMATED replies require all estimate fields")
        elif any(value is not None for value in values):
            raise ValueError("non-estimates must not contain numerical estimate fields")
        return self


class SiteState(StrictModel):
    """Coordinator-visible participation state for one selected node/site."""

    site_alias: Identifier
    stage: Literal["PREFLIGHT", "ASSOCIATION"]
    status: Literal[
        "READY",
        "ESTIMATED",
        "NOT_AUTHORIZED",
        "INCOMPATIBLE_VARIANT",
        "INCOMPATIBLE_PHENOTYPE",
        "MISSING_COVARIATE",
        "INSUFFICIENT_DATA",
        "POLICY_REJECTED",
        "SUPPRESSED",
        "UNESTIMABLE",
        "ERROR",
        "NO_RESPONSE",
        "INVALID_REPLY",
        "DUPLICATE_REPLY",
    ]
    reason_code: Identifier | None = None


class ResultEnvelope(StrictModel):
    """Phase 0 released result retained for regression tests."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    result_type: Literal["PHASE0_PREFLIGHT"] = "PHASE0_PREFLIGHT"
    study_id: Identifier
    query_id: Identifier
    contract_hash: ContractHash
    expected_sites: Annotated[int, Field(ge=1, le=256)]
    responding_sites: Annotated[int, Field(ge=0, le=256)]
    ready_sites: Annotated[list[Identifier], Field(max_length=256)]
    status: Literal["COMPLETE", "PARTIAL"]
    synthetic: Literal[True] = True

    @model_validator(mode="after")
    def check_counts(self) -> ResultEnvelope:
        if self.ready_sites != sorted(set(self.ready_sites)):
            raise ValueError("ready_sites must be sorted and unique")
        if self.responding_sites != len(self.ready_sites):
            raise ValueError("responding_sites must equal the number of ready_sites")
        if self.responding_sites > self.expected_sites:
            raise ValueError("responding_sites cannot exceed expected_sites")
        complete = self.responding_sites == self.expected_sites
        if complete != (self.status == "COMPLETE"):
            raise ValueError("status does not match site counts")
        return self


class AssociationResult(StrictModel):
    """Released Phase 1 meta-analysis and permitted site summaries."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    result_type: Literal["PHASE1_ASSOCIATION"] = "PHASE1_ASSOCIATION"
    study_id: Identifier
    analysis_id: Identifier
    query_id: Identifier
    contract_hash: ContractHash
    expected_sites: Annotated[int, Field(ge=1, le=256)]
    responding_sites: Annotated[int, Field(ge=0, le=256)]
    estimated_sites: Annotated[int, Field(ge=0, le=256)]
    site_states: tuple[SiteState, ...]
    site_results: tuple[AssociationReply, ...]
    beta_meta: float | None = None
    standard_error_meta: Annotated[float, Field(gt=0)] | None = None
    ci95_low: float | None = None
    ci95_high: float | None = None
    q: Annotated[float, Field(ge=0)] | None = None
    q_degrees_freedom: Annotated[int, Field(ge=1)] | None = None
    i2: Annotated[float, Field(ge=0, le=1)] | None = None
    heterogeneity_flag: bool | None = None
    status: Literal["COMPLETE", "PARTIAL", "INSUFFICIENT"]
    estimator: Literal["OLS_HC3_FIXED_EFFECT"] = "OLS_HC3_FIXED_EFFECT"
    phenotype_unit: Identifier
    data_provenance: Identifier = "COHORTLENS_PUBLIC_GENERATOR_V1"
    synthetic_provenance: Literal["COHORTLENS_PUBLIC_GENERATOR_V1"] | None = None
    synthetic: bool = True

    @model_validator(mode="after")
    def validate_result(self) -> AssociationResult:
        aliases = [item.site_alias for item in self.site_states]
        result_aliases = [item.site_alias for item in self.site_results]
        if aliases != sorted(set(aliases)):
            raise ValueError("site_states must be sorted with unique aliases")
        if result_aliases != sorted(set(result_aliases)):
            raise ValueError("site_results must be sorted with unique aliases")
        estimated = sum(item.status == "ESTIMATED" for item in self.site_results)
        if estimated != self.estimated_sites:
            raise ValueError("estimated_sites does not match site_results")
        if self.responding_sites > self.expected_sites:
            raise ValueError("responding_sites cannot exceed expected_sites")
        aggregate = (
            self.beta_meta,
            self.standard_error_meta,
            self.ci95_low,
            self.ci95_high,
            self.q,
            self.q_degrees_freedom,
            self.i2,
            self.heterogeneity_flag,
        )
        if self.estimated_sites >= 2 and any(value is None for value in aggregate):
            raise ValueError("two or more estimates require aggregate fields")
        if self.estimated_sites < 2 and any(value is not None for value in aggregate):
            raise ValueError("insufficient results must not contain an aggregate")
        expected_status = (
            "INSUFFICIENT"
            if self.estimated_sites < 2
            else "COMPLETE"
            if self.estimated_sites == self.expected_sites
            else "PARTIAL"
        )
        if self.status != expected_status:
            raise ValueError("result status does not match estimate counts")
        return self


class AgentEvidence(StrictModel):
    """Reduced aggregate-only packet safe for the reporting AgentApp."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    study_id: Identifier
    analysis_id: Identifier
    contract_hash: ContractHash
    expected_sites: int
    estimated_sites: int
    beta_meta: float
    standard_error_meta: float
    ci95_low: float
    ci95_high: float
    i2: float
    heterogeneity_flag: bool
    phenotype_unit: Identifier
    data_provenance: Identifier = "COHORTLENS_PUBLIC_GENERATOR_V1"
    synthetic: bool = True
    parent_result_hash: ContractHash | None = None
    allowed_actions: tuple[
        Literal[
            "REPORT_PRIMARY_RESULT",
            "STRATIFY_SYNTHETIC_CONTEXT",
            "STRATIFY_GDSC_LINEAGE",
            "RECHECK_PHENOTYPE_METADATA",
            "RECHECK_GDSC_METADATA",
            "STOP_INSUFFICIENT_EVIDENCE",
        ],
        ...,
    ] = ("REPORT_PRIMARY_RESULT",)


class InvestigationDecision(StrictModel):
    """Deterministic gate deciding whether an investigator may be invoked."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    study_id: Identifier
    parent_analysis_id: Identifier
    parent_result_hash: ContractHash
    decision: Literal[
        "FINISH_COMPATIBLE", "INVESTIGATE", "STOP_INSUFFICIENT_EVIDENCE"
    ]
    evidence_fields: tuple[
        Literal["status", "estimated_sites", "heterogeneity_flag", "i2"], ...
    ]
    reason: ShortText


class FollowUpProposal(StrictModel):
    """One bounded investigator recommendation; never an authorization."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    role: Literal["investigator"] = "investigator"
    study_id: Identifier
    parent_analysis_id: Identifier
    parent_result_hash: ContractHash
    action: Literal[
        "STRATIFY_SYNTHETIC_CONTEXT",
        "STRATIFY_GDSC_LINEAGE",
        "RECHECK_PHENOTYPE_METADATA",
        "RECHECK_GDSC_METADATA",
        "STOP_INSUFFICIENT_EVIDENCE",
    ]
    evidence_fields: tuple[Literal["heterogeneity_flag", "i2", "estimated_sites"], ...]
    changed_contract_fields: tuple[Literal["analysis_id", "parent_result_hash", "strata", "covariate_ids"], ...]
    expected_interpretive_value: ShortText
    disclosure_cost: ShortText
    stop_condition: ShortText
    rationale: ShortText

    @model_validator(mode="after")
    def validate_action_shape(self) -> "FollowUpProposal":
        if not self.evidence_fields:
            raise ValueError("proposal must reference released evidence fields")
        if self.action in {"STRATIFY_SYNTHETIC_CONTEXT", "STRATIFY_GDSC_LINEAGE"}:
            if set(self.changed_contract_fields) != {"analysis_id", "parent_result_hash", "strata", "covariate_ids"}:
                raise ValueError("stratification must declare exactly its permitted changes")
        elif self.changed_contract_fields:
            raise ValueError("non-association actions cannot change a contract")
        return self


class Phase2AssociationResult(StrictModel):
    """Traceable, exploratory context-stratified second-run release."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    result_type: Literal["PHASE2_STRATIFIED_ASSOCIATION"] = "PHASE2_STRATIFIED_ASSOCIATION"
    study_id: Identifier
    analysis_id: Identifier
    query_id: Identifier
    contract_hash: ContractHash
    parent_result_hash: ContractHash
    proposal_hash: ContractHash
    strata_results: tuple[AssociationResult, ...]
    status: Literal["COMPLETE", "PARTIAL", "INSUFFICIENT"]
    exploratory: Literal[True] = True

    @model_validator(mode="after")
    def validate_strata(self) -> "Phase2AssociationResult":
        if len(self.strata_results) != 2:
            raise ValueError("Phase 2 requires exactly two predeclared strata")
        if any(item.contract_hash != self.contract_hash for item in self.strata_results):
            raise ValueError("stratum result contract hash mismatch")
        return self


class StratumAgentEvidence(StrictModel):
    """One aggregate-only stratum result released to the reporting model."""

    stratum_id: Identifier
    beta_meta: float | None = None
    standard_error_meta: Annotated[float, Field(gt=0)] | None = None
    ci95_low: float | None = None
    ci95_high: float | None = None
    i2: Annotated[float, Field(ge=0, le=1)] | None = None
    estimated_sites: Annotated[int, Field(ge=0, le=256)]
    status: Literal["COMPLETE", "PARTIAL", "INSUFFICIENT"]


class FollowUpAgentEvidence(StrictModel):
    """Reduced Phase 2 evidence with no site-level summaries or rows."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    study_id: Identifier
    analysis_id: Identifier
    contract_hash: ContractHash
    parent_result_hash: ContractHash
    proposal_hash: ContractHash
    status: Literal["COMPLETE", "PARTIAL", "INSUFFICIENT"]
    exploratory: Literal[True] = True
    phenotype_unit: Identifier
    data_provenance: Identifier = "COHORTLENS_PUBLIC_GENERATOR_V1"
    synthetic: bool = True
    strata: tuple[StratumAgentEvidence, ...]
    allowed_actions: tuple[Literal["REPORT_FOLLOWUP_RESULT"], ...] = (
        "REPORT_FOLLOWUP_RESULT",
    )

    @model_validator(mode="after")
    def validate_two_strata(self) -> "FollowUpAgentEvidence":
        stratum_ids = tuple(item.stratum_id for item in self.strata)
        if stratum_ids not in {
            ("context_0", "context_1"),
            ("non_skcm", "skcm"),
        }:
            raise ValueError("follow-up evidence requires an approved ordered pair")
        return self


class PlannerProposal(StrictModel):
    """Bounded Phase 1 planning output from the agent."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    role: Literal["planner"] = "planner"
    study_id: Identifier
    action: Literal["RUN_PRIMARY_ASSOCIATION"] = "RUN_PRIMARY_ASSOCIATION"
    template_id: Literal["ols_quantitative_v1"] = "ols_quantitative_v1"
    variant_key: Literal["demo:1:1000:A:G"] = "demo:1:1000:A:G"
    effect_allele: Literal["G"] = "G"
    phenotype_id: Literal["biomarker_y_v1"] = "biomarker_y_v1"
    rationale: ShortText
    limitations: tuple[ShortText, ...]


class ReporterOutput(StrictModel):
    """Bounded explanation produced only from released aggregate evidence."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    role: Literal["reporter"] = "reporter"
    study_id: Identifier
    analysis_id: Identifier
    summary: ShortText
    site_disagreement: ShortText
    privacy_boundary: ShortText
    limitations: tuple[ShortText, ...]


ReleasedResult: TypeAlias = ResultEnvelope | AssociationResult | Phase2AssociationResult
_RELEASED_RESULT_ADAPTER = TypeAdapter(ReleasedResult)


def canonical_json(model: BaseModel) -> str:
    """Serialize a validated model deterministically for hashing and framing."""
    return json.dumps(
        model.model_dump(mode="json", exclude_none=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def sha256_digest(data: str | bytes) -> str:
    """Return a namespaced SHA-256 digest."""
    encoded = data.encode("utf-8") if isinstance(data, str) else data
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def contract_digest(contract: AnalysisContract) -> str:
    """Hash the canonical contract, excluding approval metadata by construction."""
    return sha256_digest(canonical_json(contract))


def validate_contract_approval(
    contract: AnalysisContract,
    approval: Approval,
    *,
    now: datetime | None = None,
) -> None:
    """Reject altered, mismatched, or expired synthetic approvals."""
    if approval.contract_hash != contract_digest(contract):
        raise ValueError("approval contract hash does not match contract")
    if approval.study_id != contract.study_id:
        raise ValueError("approval study_id does not match contract")
    if approval.analysis_id != contract.analysis_id:
        raise ValueError("approval analysis_id does not match contract")
    current = now or datetime.now(timezone.utc)
    if current >= approval.expires_at:
        raise ValueError("approval has expired")


def default_phase1_contract() -> AnalysisContract:
    """Return the single supported Phase 1 synthetic analysis contract."""
    return AnalysisContract(
        study_id="phase1-demo",
        analysis_id="primary-001",
        template_id="ols_quantitative_v1",
        variant=VariantSpec(
            reference_build="SYNTHETIC_V1",
            key="demo:1:1000:A:G",
            effect_allele="G",
        ),
        phenotype_id="biomarker_y_v1",
        phenotype_unit="demo_unit",
        covariate_ids=("age_z_v1", "sex_code_v1", "context_v1"),
        cohort_rule_id="all_synthetic_adults_v1",
        site_roster_id="demo_three_sites_v1",
        privacy_policy_id="synthetic_release_v1",
        statistical_policy_id="ols_hc3_fixed_effect_v1",
        expected_sites=3,
    )


def default_gdsc_contract(
    *, simulation_split: str | None = None
) -> AnalysisContract:
    """Return the minimal approved GDSC2 PLX-4720 analysis contract."""
    if simulation_split not in {None, "lineage-enriched-v1"}:
        raise ValueError(f"unsupported GDSC simulation split: {simulation_split}")
    lineage_simulation = simulation_split == "lineage-enriched-v1"
    return AnalysisContract(
        study_id="gdsc2-plx4720-braf-v600e",
        analysis_id=(
            "primary-lineage-enriched-sim-001"
            if lineage_simulation
            else "primary-001"
        ),
        template_id="ols_quantitative_v1",
        variant=VariantSpec(
            reference_build="CMP_DRIVER_SUMMARY_PINNED",
            key="BRAF:p.V600E",
            effect_allele="PRESENT",
            dosage="binary_presence",
        ),
        phenotype_id="GDSC2_PLX4720_LN_IC50",
        phenotype_unit="ln_micromolar",
        covariate_ids=("TCGA_SKCM_VS_OTHER_V1",),
        cohort_rule_id="GDSC2_PLX4720_MUTATION_COVERED_V1",
        site_roster_id=(
            "GDSC2_LINEAGE_ENRICHED3_SIM_V1"
            if lineage_simulation
            else "GDSC2_MODEL_HASH3_V1"
        ),
        privacy_policy_id="PUBLIC_GDSC_RELEASE_V1",
        statistical_policy_id="OLS_HC3_FIXED_EFFECT_V1",
        expected_sites=3,
    )


def default_phase1_approval(contract: AnalysisContract | None = None) -> Approval:
    """Return a baked-in approval for a supported public demonstration."""
    approved_contract = contract or default_phase1_contract()
    return Approval(
        approval_id="phase1-demo-approval",
        study_id=approved_contract.study_id,
        analysis_id=approved_contract.analysis_id,
        contract_hash=contract_digest(approved_contract),
        approved_by="apsal",
        scope=(
            "PUBLIC_GDSC_EMULATED_FEDERATION"
            if approved_contract.variant.reference_build
            == "CMP_DRIVER_SUMMARY_PINNED"
            else "SYNTHETIC_DEMO"
        ),
        approved_at=datetime(2026, 9, 16, 0, 0, tzinfo=timezone.utc),
        expires_at=datetime(2099, 1, 1, 0, 0, tzinfo=timezone.utc),
    )


def parse_released_result_json(payload: str) -> ReleasedResult:
    """Parse either supported result envelope using its discriminating type."""
    return _RELEASED_RESULT_ADAPTER.validate_json(payload)


def evidence_from_result(result: AssociationResult) -> AgentEvidence:
    """Remove site-level releases before sending evidence to an LLM."""
    if result.status == "INSUFFICIENT":
        raise ValueError("cannot create agent evidence without an aggregate")
    assert result.beta_meta is not None
    assert result.standard_error_meta is not None
    assert result.ci95_low is not None
    assert result.ci95_high is not None
    assert result.i2 is not None
    assert result.heterogeneity_flag is not None
    return AgentEvidence(
        study_id=result.study_id,
        analysis_id=result.analysis_id,
        contract_hash=result.contract_hash,
        expected_sites=result.expected_sites,
        estimated_sites=result.estimated_sites,
        beta_meta=result.beta_meta,
        standard_error_meta=result.standard_error_meta,
        ci95_low=result.ci95_low,
        ci95_high=result.ci95_high,
        i2=result.i2,
        heterogeneity_flag=result.heterogeneity_flag,
        phenotype_unit=result.phenotype_unit,
        data_provenance=result.data_provenance,
        synthetic=result.synthetic,
        parent_result_hash=sha256_digest(canonical_json(result)),
        allowed_actions=(
            (
                "STRATIFY_SYNTHETIC_CONTEXT",
                "RECHECK_PHENOTYPE_METADATA",
                "STOP_INSUFFICIENT_EVIDENCE",
            )
            if result.synthetic
            else (
                "STRATIFY_GDSC_LINEAGE",
                "RECHECK_GDSC_METADATA",
                "STOP_INSUFFICIENT_EVIDENCE",
            )
        ),
    )


def investigation_decision_from_result(
    result: AssociationResult,
) -> InvestigationDecision:
    """Apply the frozen synthetic follow-up gate without model discretion."""
    parent_hash = sha256_digest(canonical_json(result))
    if result.status == "INSUFFICIENT" or result.estimated_sites < 2:
        return InvestigationDecision(
            study_id=result.study_id,
            parent_analysis_id=result.analysis_id,
            parent_result_hash=parent_hash,
            decision="STOP_INSUFFICIENT_EVIDENCE",
            evidence_fields=("status", "estimated_sites"),
            reason="Fewer than two valid site estimates cannot support a network investigation.",
        )
    if result.heterogeneity_flag is True and result.i2 is not None and result.i2 >= 0.50:
        return InvestigationDecision(
            study_id=result.study_id,
            parent_analysis_id=result.analysis_id,
            parent_result_hash=parent_hash,
            decision="INVESTIGATE",
            evidence_fields=("heterogeneity_flag", "i2", "estimated_sites"),
            reason="The predeclared heterogeneity warning fired with adequate site support.",
        )
    return InvestigationDecision(
        study_id=result.study_id,
        parent_analysis_id=result.analysis_id,
        parent_result_hash=parent_hash,
        decision="FINISH_COMPATIBLE",
        evidence_fields=("heterogeneity_flag", "i2", "estimated_sites"),
        reason="The predeclared heterogeneity warning did not fire; no second query is authorized.",
    )


def followup_evidence_from_result(
    result: Phase2AssociationResult,
) -> FollowUpAgentEvidence:
    """Remove every site-level release before Phase 2 model reporting."""
    strata: list[StratumAgentEvidence] = []
    expected_ids = tuple(
        item.site_results[0].stratum_id
        for item in result.strata_results
        if item.site_results
    )
    if len(expected_ids) != 2:
        raise ValueError("follow-up result requires two bound strata")
    for expected_id, item in zip(expected_ids, result.strata_results, strict=True):
        actual_ids = {reply.stratum_id for reply in item.site_results}
        if actual_ids and actual_ids != {expected_id}:
            raise ValueError("Phase 2 result contains an unexpected stratum binding")
        strata.append(
            StratumAgentEvidence(
                stratum_id=expected_id,
                beta_meta=item.beta_meta,
                standard_error_meta=item.standard_error_meta,
                ci95_low=item.ci95_low,
                ci95_high=item.ci95_high,
                i2=item.i2,
                estimated_sites=item.estimated_sites,
                status=item.status,
            )
        )
    return FollowUpAgentEvidence(
        study_id=result.study_id,
        analysis_id=result.analysis_id,
        contract_hash=result.contract_hash,
        parent_result_hash=result.parent_result_hash,
        proposal_hash=result.proposal_hash,
        status=result.status,
        phenotype_unit=result.strata_results[0].phenotype_unit,
        data_provenance=result.strata_results[0].data_provenance,
        synthetic=result.strata_results[0].synthetic,
        strata=tuple(strata),
    )


def proposal_digest(proposal: FollowUpProposal) -> str:
    """Hash the exact validated proposal that an operator reviewed."""
    return sha256_digest(canonical_json(proposal))


def compile_stratified_followup(
    parent: AssociationResult, proposal: FollowUpProposal
) -> AnalysisContract:
    """Compile the only Phase 2 association follow-up from released evidence."""
    if proposal.action not in {"STRATIFY_SYNTHETIC_CONTEXT", "STRATIFY_GDSC_LINEAGE"}:
        raise ValueError("only stratification produces a Phase 2 association contract")
    if proposal.study_id != parent.study_id or proposal.parent_analysis_id != parent.analysis_id:
        raise ValueError("proposal is not bound to the parent result")
    parent_hash = sha256_digest(canonical_json(parent))
    if proposal.parent_result_hash != parent_hash:
        raise ValueError("proposal parent_result_hash does not match released result")
    if not parent.heterogeneity_flag:
        raise ValueError("stratification requires the released heterogeneity flag")
    gdsc = proposal.action == "STRATIFY_GDSC_LINEAGE"
    return AnalysisContract(
        study_id=parent.study_id,
        analysis_id=f"{parent.analysis_id}-{'lineage' if gdsc else 'context'}-001",
        parent_result_hash=parent_hash,
        template_id=(
            "ols_quantitative_lineage_stratified_v1"
            if gdsc
            else "ols_quantitative_context_stratified_v1"
        ),
        variant=(
            VariantSpec(
                reference_build="CMP_DRIVER_SUMMARY_PINNED",
                key="BRAF:p.V600E",
                effect_allele="PRESENT",
                dosage="binary_presence",
            )
            if gdsc
            else VariantSpec(
                reference_build="SYNTHETIC_V1",
                key="demo:1:1000:A:G",
                effect_allele="G",
            )
        ),
        phenotype_id=("GDSC2_PLX4720_LN_IC50" if gdsc else "biomarker_y_v1"),
        phenotype_unit=parent.phenotype_unit,
        covariate_ids=("INTERCEPT_ONLY_AFTER_STRATIFICATION",) if gdsc else ("age_z_v1", "sex_code_v1"),
        cohort_rule_id=("GDSC2_PLX4720_MUTATION_COVERED_V1" if gdsc else "all_synthetic_adults_v1"),
        strata=("non_skcm", "skcm") if gdsc else ("context_0", "context_1"),
        site_roster_id=(
            (
                "GDSC2_LINEAGE_ENRICHED3_SIM_V1"
                if parent.data_provenance
                == "GDSC2_8.5_LINEAGE_ENRICHED_SIM_V1"
                else "GDSC2_MODEL_HASH3_V1"
            )
            if gdsc
            else "demo_three_sites_v1"
        ),
        privacy_policy_id=("PUBLIC_GDSC_RELEASE_V1" if gdsc else "synthetic_release_v1"),
        statistical_policy_id=("OLS_HC3_FIXED_EFFECT_V1" if gdsc else "ols_hc3_fixed_effect_v1"),
        expected_sites=parent.expected_sites,
        max_association_rounds=2,
    )
