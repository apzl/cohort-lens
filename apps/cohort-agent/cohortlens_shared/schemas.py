"""Versioned, strict contracts shared by CohortLens components."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Annotated, Literal, TypeAlias, get_args

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    JsonValue,
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
DEFAULT_FOLLOWUP_BUDGET = 2
MAX_ROUNDS_SCHEMA = 4
HETEROGENEITY_THRESHOLD = 0.50


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
    """Exact, non-executable scientific contract for one federated round.

    ``round_index`` 0 is the primary analysis. A follow-up round binds to the
    released parent result and to the exact proposal an operator reviewed.
    ``max_association_rounds`` is the declared query budget of the chain
    (primary included); sites and the coordinator enforce it independently.
    """

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
    max_association_rounds: Annotated[int, Field(ge=1, le=MAX_ROUNDS_SCHEMA)] = (
        DEFAULT_FOLLOWUP_BUDGET + 1
    )
    round_index: Annotated[int, Field(ge=0, le=MAX_ROUNDS_SCHEMA - 1)] = 0
    proposal_hash: ContractHash | None = None

    @field_validator("covariate_ids")
    @classmethod
    def validate_covariates(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or len(value) > 32 or len(set(value)) != len(value):
            raise ValueError("covariate_ids must be a non-empty unique list")
        return value

    @model_validator(mode="after")
    def validate_chain_binding(self) -> AnalysisContract:
        bound = (self.parent_result_hash, self.proposal_hash)
        if self.round_index == 0 and any(value is not None for value in bound):
            raise ValueError("a primary contract cannot bind a parent or proposal")
        if self.round_index > 0 and any(value is None for value in bound):
            raise ValueError("a follow-up contract must bind parent and proposal hashes")
        return self


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


DataMode: TypeAlias = Literal["SYNTHETIC", "PUBLIC_GDSC"]
StratifyAction: TypeAlias = Literal[
    "STRATIFY_SYNTHETIC_CONTEXT", "STRATIFY_GDSC_LINEAGE"
]
FollowUpAction: TypeAlias = Literal[
    "STRATIFY_SYNTHETIC_CONTEXT",
    "STRATIFY_GDSC_LINEAGE",
    "LEAVE_ONE_SITE_OUT",
    "STOP_NO_FOLLOWUP",
]
RoundAction: TypeAlias = Literal[
    "PRIMARY_ASSOCIATION",
    "STRATIFY_SYNTHETIC_CONTEXT",
    "STRATIFY_GDSC_LINEAGE",
    "LEAVE_ONE_SITE_OUT",
]
ContractField: TypeAlias = Literal[
    "analysis_id",
    "parent_result_hash",
    "proposal_hash",
    "round_index",
    "template_id",
    "strata",
    "covariate_ids",
]
EvidenceField: TypeAlias = Literal[
    "status",
    "estimated_sites",
    "beta_meta",
    "ci95",
    "q",
    "q_p_value",
    "i2",
    "heterogeneity_flag",
    "stratum_i2",
    "loso_beta_range",
    "loso_i2_range",
    "loso_flag_cleared",
    "budget_remaining",
]
FOLLOWUP_ACTIONS: tuple[str, ...] = get_args(FollowUpAction)
EVIDENCE_FIELDS: tuple[str, ...] = get_args(EvidenceField)
CONTRACT_FIELDS: tuple[str, ...] = get_args(ContractField)
_BINDING_CHANGES = ("analysis_id", "parent_result_hash", "proposal_hash", "round_index", "template_id")
ACTION_CONTRACT_CHANGES: dict[str, frozenset[str]] = {
    "STRATIFY_SYNTHETIC_CONTEXT": frozenset(_BINDING_CHANGES + ("strata", "covariate_ids")),
    "STRATIFY_GDSC_LINEAGE": frozenset(_BINDING_CHANGES + ("strata", "covariate_ids")),
    "LEAVE_ONE_SITE_OUT": frozenset(_BINDING_CHANGES),
    "STOP_NO_FOLLOWUP": frozenset(),
}


class StratifiedAssociationResult(StrictModel):
    """Exploratory follow-up release: one meta-analysis per predeclared stratum."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    # The wire name predates multi-round chains and is kept for compatibility.
    result_type: Literal["PHASE2_STRATIFIED_ASSOCIATION"] = "PHASE2_STRATIFIED_ASSOCIATION"
    study_id: Identifier
    analysis_id: Identifier
    query_id: Identifier
    contract_hash: ContractHash
    parent_result_hash: ContractHash
    proposal_hash: ContractHash
    round_index: Annotated[int, Field(ge=1, le=MAX_ROUNDS_SCHEMA - 1)] = 1
    strata_results: tuple[AssociationResult, ...]
    status: Literal["COMPLETE", "PARTIAL", "INSUFFICIENT"]
    exploratory: Literal[True] = True

    @model_validator(mode="after")
    def validate_strata(self) -> StratifiedAssociationResult:
        if len(self.strata_results) != 2:
            raise ValueError("a stratified follow-up requires exactly two predeclared strata")
        if any(item.contract_hash != self.contract_hash for item in self.strata_results):
            raise ValueError("stratum result contract hash mismatch")
        return self


Phase2AssociationResult = StratifiedAssociationResult


class SiteOmission(StrictModel):
    """Pooled estimate recomputed with one site's released estimate left out."""

    omitted_site_alias: Identifier
    estimated_sites: Annotated[int, Field(ge=2, le=255)]
    beta_meta: float
    standard_error_meta: Annotated[float, Field(gt=0)]
    ci95_low: float
    ci95_high: float
    q: Annotated[float, Field(ge=0)]
    q_degrees_freedom: Annotated[int, Field(ge=1)]
    i2: Annotated[float, Field(ge=0, le=1)]
    heterogeneity_flag: bool


class LeaveOneSiteOutResult(StrictModel):
    """Sensitivity release: the full meta-analysis plus every single-site omission.

    Sites answer exactly the primary model under the LOSO template; the
    coordinator derives the omissions from the replies it already validated.
    """

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    result_type: Literal["LEAVE_ONE_SITE_OUT"] = "LEAVE_ONE_SITE_OUT"
    study_id: Identifier
    analysis_id: Identifier
    query_id: Identifier
    contract_hash: ContractHash
    parent_result_hash: ContractHash
    proposal_hash: ContractHash
    round_index: Annotated[int, Field(ge=1, le=MAX_ROUNDS_SCHEMA - 1)]
    full_result: AssociationResult
    omissions: tuple[SiteOmission, ...]
    status: Literal["COMPLETE", "PARTIAL", "INSUFFICIENT"]
    exploratory: Literal[True] = True

    @model_validator(mode="after")
    def validate_omissions(self) -> LeaveOneSiteOutResult:
        if self.full_result.contract_hash != self.contract_hash:
            raise ValueError("LOSO full result contract hash mismatch")
        estimated = sorted(
            item.site_alias
            for item in self.full_result.site_results
            if item.status == "ESTIMATED"
        )
        omitted = [item.omitted_site_alias for item in self.omissions]
        expected = estimated if len(estimated) >= 3 else []
        if omitted != expected:
            raise ValueError("omissions must cover every estimated site exactly once")
        if any(item.estimated_sites != len(estimated) - 1 for item in self.omissions):
            raise ValueError("each omission must pool all remaining estimates")
        wanted = "INSUFFICIENT" if len(estimated) < 3 else self.full_result.status
        if self.status != wanted:
            raise ValueError("LOSO status does not match the available estimates")
        return self


class NetworkAggregate(StrictModel):
    """One network-level fixed-effect summary; never a site-level value."""

    status: Literal["COMPLETE", "PARTIAL", "INSUFFICIENT"]
    expected_sites: Annotated[int, Field(ge=1, le=256)]
    estimated_sites: Annotated[int, Field(ge=0, le=256)]
    beta_meta: float | None = None
    standard_error_meta: Annotated[float, Field(gt=0)] | None = None
    ci95_low: float | None = None
    ci95_high: float | None = None
    q: Annotated[float, Field(ge=0)] | None = None
    q_degrees_freedom: Annotated[int, Field(ge=1)] | None = None
    i2: Annotated[float, Field(ge=0, le=1)] | None = None
    heterogeneity_flag: bool | None = None


class StratumAggregate(NetworkAggregate):
    """A network-level summary restricted to one predeclared stratum."""

    stratum_id: Identifier


class LosoAggregate(StrictModel):
    """Identity-free reduction of the leave-one-site-out omissions."""

    omissions_evaluated: Annotated[int, Field(ge=0, le=256)]
    pooled_beta_min: float | None = None
    pooled_beta_max: float | None = None
    i2_min: Annotated[float, Field(ge=0, le=1)] | None = None
    i2_max: Annotated[float, Field(ge=0, le=1)] | None = None
    omissions_clearing_flag: Annotated[int, Field(ge=0, le=256)] = 0
    any_omission_clears_flag: bool = False


class RoundSummary(StrictModel):
    """Aggregate-only record of one completed federated round."""

    round_index: Annotated[int, Field(ge=0, le=MAX_ROUNDS_SCHEMA - 1)]
    action: RoundAction
    analysis_id: Identifier
    contract_hash: ContractHash
    result_hash: ContractHash
    status: Literal["COMPLETE", "PARTIAL", "INSUFFICIENT"]
    network: NetworkAggregate | None = None
    strata: tuple[StratumAggregate, ...] = ()
    loso: LosoAggregate | None = None


class InvestigationEvidence(StrictModel):
    """Everything the investigator may see: network aggregates and the budget."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    study_id: Identifier
    data_mode: DataMode
    phenotype_unit: Identifier
    data_provenance: Identifier
    synthetic: bool
    primary_analysis_id: Identifier
    parent_analysis_id: Identifier
    parent_result_hash: ContractHash
    next_round_index: Annotated[int, Field(ge=1, le=MAX_ROUNDS_SCHEMA)]
    max_association_rounds: Annotated[int, Field(ge=1, le=MAX_ROUNDS_SCHEMA)]
    heterogeneity_threshold: Annotated[float, Field(gt=0, lt=1)] = HETEROGENEITY_THRESHOLD
    history: tuple[RoundSummary, ...]

    @model_validator(mode="after")
    def validate_history(self) -> InvestigationEvidence:
        if not self.history:
            raise ValueError("investigation evidence requires at least the primary round")
        if [item.round_index for item in self.history] != list(range(len(self.history))):
            raise ValueError("history rounds must be consecutive from the primary")
        if self.history[0].action != "PRIMARY_ASSOCIATION":
            raise ValueError("history must start with the primary association")
        if any(item.action == "PRIMARY_ASSOCIATION" for item in self.history[1:]):
            raise ValueError("only round 0 may be the primary association")
        latest = self.history[-1]
        if (
            latest.analysis_id != self.parent_analysis_id
            or latest.result_hash != self.parent_result_hash
            or self.history[0].analysis_id != self.primary_analysis_id
        ):
            raise ValueError("evidence parent binding does not match its history")
        if self.next_round_index != len(self.history):
            raise ValueError("next_round_index must follow the last released round")
        return self

    @property
    def budget_remaining(self) -> int:
        """Follow-up rounds still permitted by the contract chain."""
        return max(0, self.max_association_rounds - self.next_round_index)


class InvestigationDecision(StrictModel):
    """Deterministic gate deciding whether an investigator may be invoked."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    study_id: Identifier
    parent_analysis_id: Identifier
    parent_result_hash: ContractHash
    round_index: Annotated[int, Field(ge=0, le=MAX_ROUNDS_SCHEMA - 1)] = 0
    decision: Literal[
        "FINISH_COMPATIBLE",
        "INVESTIGATE",
        "STOP_INSUFFICIENT_EVIDENCE",
        "BUDGET_EXHAUSTED",
    ]
    evidence_fields: tuple[
        Literal["status", "estimated_sites", "heterogeneity_flag", "i2", "budget_remaining"],
        ...,
    ]
    reason: ShortText


class FollowUpProposal(StrictModel):
    """One bounded investigator recommendation; never an authorization."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    role: Literal["investigator"] = "investigator"
    study_id: Identifier
    parent_analysis_id: Identifier
    parent_result_hash: ContractHash
    round_index: Annotated[int, Field(ge=1, le=MAX_ROUNDS_SCHEMA - 1)]
    action: FollowUpAction
    evidence_fields: tuple[EvidenceField, ...]
    changed_contract_fields: tuple[ContractField, ...]
    expected_interpretive_value: ShortText
    disclosure_cost: ShortText
    stop_condition: ShortText
    rationale: ShortText

    @model_validator(mode="after")
    def validate_action_shape(self) -> FollowUpProposal:
        if not self.evidence_fields:
            raise ValueError("proposal must reference released evidence fields")
        if len(set(self.changed_contract_fields)) != len(self.changed_contract_fields):
            raise ValueError("changed_contract_fields must be unique")
        expected = ACTION_CONTRACT_CHANGES[self.action]
        if set(self.changed_contract_fields) != expected:
            raise ValueError(
                f"{self.action} must declare exactly these changed_contract_fields: "
                f"{sorted(expected)}"
            )
        return self


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


class TraceEntry(StrictModel):
    """One auditable step of the investigator tool loop."""

    step: Annotated[int, Field(ge=0, le=1000)]
    kind: Literal[
        "tool_call", "tool_result", "invalid_turn", "protocol_fallback", "model_text"
    ]
    tool: str | None = None
    call_id: str | None = None
    arguments: dict[str, JsonValue] | None = None
    ok: bool | None = None
    output: dict[str, JsonValue] | None = None
    message: str | None = None


class InvestigatorRecord(StrictModel):
    """Framed outcome of one investigator run, including its full tool trace."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    role: Literal["investigator_run"] = "investigator_run"
    study_id: Identifier
    parent_analysis_id: Identifier
    parent_result_hash: ContractHash
    evidence_hash: ContractHash
    policy_kind: Literal["MODEL", "SCRIPTED_POLICY"]
    policy_label: Identifier
    protocol_requested: Literal["native", "json"]
    protocol_used: Literal["native", "json"]
    status: Literal["PROPOSED", "FAILED"]
    failure_reason: ShortText | None = None
    proposal: FollowUpProposal | None = None
    steps: Annotated[int, Field(ge=0)]
    tool_calls: Annotated[int, Field(ge=0)]
    invalid_turns: Annotated[int, Field(ge=0)]
    trace: tuple[TraceEntry, ...]

    @model_validator(mode="after")
    def validate_outcome(self) -> InvestigatorRecord:
        if (self.status == "PROPOSED") != (self.proposal is not None):
            raise ValueError("a PROPOSED record carries exactly one proposal")
        if self.status == "FAILED" and self.failure_reason is None:
            raise ValueError("a FAILED record must state its failure reason")
        if self.proposal is not None and (
            self.proposal.parent_result_hash != self.parent_result_hash
            or self.proposal.study_id != self.study_id
        ):
            raise ValueError("proposal is not bound to the record's parent result")
        return self


ReleasedResult: TypeAlias = (
    ResultEnvelope
    | AssociationResult
    | StratifiedAssociationResult
    | LeaveOneSiteOutResult
)
_RELEASED_RESULT_ADAPTER = TypeAdapter(ReleasedResult)
AgentArtifact: TypeAlias = InvestigatorRecord | ReporterOutput
_AGENT_ARTIFACT_ADAPTER = TypeAdapter(AgentArtifact)


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
    """Reject altered, mismatched, or expired approvals."""
    if approval.contract_hash != contract_digest(contract):
        raise ValueError("approval contract hash does not match contract")
    if approval.study_id != contract.study_id:
        raise ValueError("approval study_id does not match contract")
    if approval.analysis_id != contract.analysis_id:
        raise ValueError("approval analysis_id does not match contract")
    current = now or datetime.now(timezone.utc)
    if current >= approval.expires_at:
        raise ValueError("approval has expired")


def default_phase1_contract(
    *, followup_budget: int = DEFAULT_FOLLOWUP_BUDGET
) -> AnalysisContract:
    """Return the primary synthetic analysis contract with its follow-up budget."""
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
        max_association_rounds=followup_budget + 1,
    )


def default_gdsc_contract(
    *,
    simulation_split: str | None = None,
    followup_budget: int = DEFAULT_FOLLOWUP_BUDGET,
) -> AnalysisContract:
    """Return the primary GDSC2 PLX-4720 contract with its follow-up budget."""
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
        max_association_rounds=followup_budget + 1,
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
    """Parse any supported result envelope using its discriminating type."""
    return _RELEASED_RESULT_ADAPTER.validate_json(payload)


def parse_agent_artifact_json(payload: str) -> AgentArtifact:
    """Parse an investigator record or a reporter output from an agent frame."""
    return _AGENT_ARTIFACT_ADAPTER.validate_json(payload)


def result_digest(result: BaseModel) -> str:
    """Hash a released result exactly as follow-up contracts bind to it."""
    return sha256_digest(canonical_json(result))


def proposal_digest(proposal: FollowUpProposal) -> str:
    """Hash the exact validated proposal that an operator reviewed."""
    return sha256_digest(canonical_json(proposal))


def data_mode_for_contract(contract: AnalysisContract) -> DataMode:
    """Classify a contract by its pinned variant source."""
    if contract.variant.reference_build == "CMP_DRIVER_SUMMARY_PINNED":
        return "PUBLIC_GDSC"
    if contract.variant.reference_build == "SYNTHETIC_V1":
        return "SYNTHETIC"
    raise ValueError("contract variant source is not a supported data mode")


def investigation_decision_from_result(
    result: AssociationResult,
) -> InvestigationDecision:
    """Apply the frozen primary gate without model discretion."""
    common = {
        "study_id": result.study_id,
        "parent_analysis_id": result.analysis_id,
        "parent_result_hash": result_digest(result),
        "round_index": 0,
    }
    if result.status == "INSUFFICIENT" or result.estimated_sites < 2:
        return InvestigationDecision(
            **common,
            decision="STOP_INSUFFICIENT_EVIDENCE",
            evidence_fields=("status", "estimated_sites"),
            reason="Fewer than two valid site estimates cannot support a network investigation.",
        )
    if (
        result.heterogeneity_flag is True
        and result.i2 is not None
        and result.i2 >= HETEROGENEITY_THRESHOLD
    ):
        return InvestigationDecision(
            **common,
            decision="INVESTIGATE",
            evidence_fields=("heterogeneity_flag", "i2", "estimated_sites"),
            reason="The predeclared heterogeneity warning fired with adequate site support.",
        )
    return InvestigationDecision(
        **common,
        decision="FINISH_COMPATIBLE",
        evidence_fields=("heterogeneity_flag", "i2", "estimated_sites"),
        reason="The predeclared heterogeneity warning did not fire; no second query is authorized.",
    )


def continuation_decision(evidence: InvestigationEvidence) -> InvestigationDecision:
    """Decide, without model discretion, whether the investigator may re-plan.

    The primary gate opens an investigation; after each follow-up this gate
    only checks the declared budget and that the round produced an aggregate.
    Whether another query is *worth* running is the investigator's proposal
    and the operator's approval, never this function's.
    """
    latest = evidence.history[-1]
    common = {
        "study_id": evidence.study_id,
        "parent_analysis_id": evidence.parent_analysis_id,
        "parent_result_hash": evidence.parent_result_hash,
        "round_index": latest.round_index,
    }
    if latest.status == "INSUFFICIENT":
        return InvestigationDecision(
            **common,
            decision="STOP_INSUFFICIENT_EVIDENCE",
            evidence_fields=("status", "estimated_sites"),
            reason="The latest follow-up did not release an adequately supported aggregate.",
        )
    if evidence.budget_remaining == 0:
        return InvestigationDecision(
            **common,
            decision="BUDGET_EXHAUSTED",
            evidence_fields=("budget_remaining",),
            reason="The contract-declared follow-up budget is spent; no further query is authorized.",
        )
    return InvestigationDecision(
        **common,
        decision="INVESTIGATE",
        evidence_fields=("status", "budget_remaining"),
        reason="Budget remains and the latest round released aggregates; the investigator may re-plan.",
    )
