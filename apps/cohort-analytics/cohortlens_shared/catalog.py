"""Closed follow-up action catalog, preconditions and contract compilation.

Every action the investigator may propose is listed here with what it costs
and what it reveals. Each non-terminal action compiles deterministically to
exactly one contract shape that sites allowlist independently.
"""

from __future__ import annotations

from typing import Literal

from .schemas import (
    ACTION_CONTRACT_CHANGES,
    AnalysisContract,
    DataMode,
    FollowUpAction,
    FollowUpProposal,
    InvestigationEvidence,
    StrictModel,
    contract_digest,
    data_mode_for_contract,
    proposal_digest,
)

PRIMARY_TEMPLATE = "ols_quantitative_v1"
LOSO_TEMPLATE = "ols_quantitative_loso_v1"
STRATIFIED_TEMPLATES: dict[DataMode, str] = {
    "SYNTHETIC": "ols_quantitative_context_stratified_v1",
    "PUBLIC_GDSC": "ols_quantitative_lineage_stratified_v1",
}
STRATIFY_ACTIONS: dict[DataMode, str] = {
    "SYNTHETIC": "STRATIFY_SYNTHETIC_CONTEXT",
    "PUBLIC_GDSC": "STRATIFY_GDSC_LINEAGE",
}
PRIMARY_COVARIATES: dict[DataMode, tuple[str, ...]] = {
    "SYNTHETIC": ("age_z_v1", "sex_code_v1", "context_v1"),
    "PUBLIC_GDSC": ("TCGA_SKCM_VS_OTHER_V1",),
}
STRATIFIED_COVARIATES: dict[DataMode, tuple[str, ...]] = {
    "SYNTHETIC": ("age_z_v1", "sex_code_v1"),
    "PUBLIC_GDSC": ("INTERCEPT_ONLY_AFTER_STRATIFICATION",),
}
STRATA: dict[DataMode, tuple[str, str]] = {
    "SYNTHETIC": ("context_0", "context_1"),
    "PUBLIC_GDSC": ("non_skcm", "skcm"),
}
ANALYSIS_SLUGS = {
    "STRATIFY_SYNTHETIC_CONTEXT": "context",
    "STRATIFY_GDSC_LINEAGE": "lineage",
    "LEAVE_ONE_SITE_OUT": "loso",
}
LOSO_MIN_ESTIMATES = 3
# Highest round count the coordinator accepts, independent of any contract.
COORDINATOR_MAX_ASSOCIATION_ROUNDS = 3


class ActionSpec(StrictModel):
    """What one catalog action does, costs and reveals."""

    action: FollowUpAction
    terminal: bool
    template_id: str | None
    query_rounds_consumed: Literal[0, 1]
    what_it_tests: str
    released_to_coordinator: str
    released_to_investigator: str
    disclosure_cost: str
    changed_contract_fields: tuple[str, ...]


class PreconditionCheck(StrictModel):
    """Deterministic precondition report for one candidate action."""

    action: str
    allowed: bool
    passed: tuple[str, ...]
    failed: tuple[str, ...]
    would_be_round_index: int | None
    budget_remaining_after: int


def _stratify_spec(data_mode: DataMode) -> ActionSpec:
    action = STRATIFY_ACTIONS[data_mode]
    label = (
        "the two predeclared synthetic contexts"
        if data_mode == "SYNTHETIC"
        else "SKCM and non-SKCM lineages"
    )
    return ActionSpec(
        action=action,  # type: ignore[arg-type]
        terminal=False,
        template_id=STRATIFIED_TEMPLATES[data_mode],
        query_rounds_consumed=1,
        what_it_tests=(
            f"Whether the effect differs between {label}, i.e. whether a "
            "predeclared effect modifier explains the cross-site disagreement."
        ),
        released_to_coordinator="Per site and stratum: beta, robust SE, N (two summaries per site).",
        released_to_investigator="Per stratum: pooled beta, SE, 95% CI, Q, df, I2, status.",
        disclosure_cost=(
            "New site-level information: each site releases two stratum summaries "
            "it has not released before. Smaller strata are more identifying."
        ),
        changed_contract_fields=tuple(sorted(ACTION_CONTRACT_CHANGES[action])),
    )


def _loso_spec() -> ActionSpec:
    return ActionSpec(
        action="LEAVE_ONE_SITE_OUT",
        terminal=False,
        template_id=LOSO_TEMPLATE,
        query_rounds_consumed=1,
        what_it_tests=(
            "Whether the pooled estimate and the heterogeneity flag depend on any "
            "single site (sensitivity analysis)."
        ),
        released_to_coordinator=(
            "Per site: the same beta, robust SE, N as the primary model; the "
            "coordinator also releases the pooled result with each site omitted."
        ),
        released_to_investigator=(
            "Identity-free range of pooled beta and I2 across omissions, and how "
            "many single omissions clear the heterogeneity flag."
        ),
        disclosure_cost=(
            "No new site-level quantity beyond the primary model; consumes one "
            "approved round so the omission summaries are a separately authorised release."
        ),
        changed_contract_fields=tuple(sorted(ACTION_CONTRACT_CHANGES["LEAVE_ONE_SITE_OUT"])),
    )


def _stop_spec() -> ActionSpec:
    return ActionSpec(
        action="STOP_NO_FOLLOWUP",
        terminal=True,
        template_id=None,
        query_rounds_consumed=0,
        what_it_tests="Nothing; ends the investigation with the evidence already released.",
        released_to_coordinator="Nothing further.",
        released_to_investigator="Nothing further.",
        disclosure_cost="None.",
        changed_contract_fields=(),
    )


def catalog_for(data_mode: DataMode) -> tuple[ActionSpec, ...]:
    """Return the closed catalog of follow-up actions for one data mode."""
    return (_stratify_spec(data_mode), _loso_spec(), _stop_spec())


def catalog_actions(data_mode: DataMode) -> tuple[str, ...]:
    """Return the action names that exist for one data mode."""
    return tuple(item.action for item in catalog_for(data_mode))


def _already_run(evidence: InvestigationEvidence, action: str) -> bool:
    return any(item.action == action for item in evidence.history)


def _primary_flagged(evidence: InvestigationEvidence) -> bool:
    primary = evidence.history[0].network
    return bool(primary and primary.heterogeneity_flag)


def _primary_estimates(evidence: InvestigationEvidence) -> int:
    primary = evidence.history[0].network
    return 0 if primary is None else primary.estimated_sites


def _query_conditions(evidence: InvestigationEvidence, action: str) -> list[tuple[str, bool]]:
    """Return named conditions for an action that would consume a round."""
    conditions = [
        ("BUDGET_REMAINING", evidence.budget_remaining > 0),
        ("NOT_ALREADY_RUN", not _already_run(evidence, action)),
        ("PRIMARY_HETEROGENEITY_FLAG_SET", _primary_flagged(evidence)),
        ("LATEST_ROUND_RELEASED_AGGREGATE", evidence.history[-1].status != "INSUFFICIENT"),
    ]
    if action == "LEAVE_ONE_SITE_OUT":
        conditions.append(
            ("AT_LEAST_THREE_PRIMARY_ESTIMATES", _primary_estimates(evidence) >= LOSO_MIN_ESTIMATES)
        )
    else:
        conditions.append(("AT_LEAST_TWO_PRIMARY_ESTIMATES", _primary_estimates(evidence) >= 2))
    return conditions


def check_preconditions(evidence: InvestigationEvidence, action: str) -> PreconditionCheck:
    """Evaluate whether an action may be proposed now; pure and deterministic."""
    if action not in catalog_actions(evidence.data_mode):
        return PreconditionCheck(
            action=action,
            allowed=False,
            passed=(),
            failed=("ACTION_IN_CATALOG_FOR_DATA_MODE",),
            would_be_round_index=None,
            budget_remaining_after=evidence.budget_remaining,
        )
    if action == "STOP_NO_FOLLOWUP":
        return PreconditionCheck(
            action=action,
            allowed=True,
            passed=("ALWAYS_PERMITTED",),
            failed=(),
            would_be_round_index=None,
            budget_remaining_after=evidence.budget_remaining,
        )
    conditions = _query_conditions(evidence, action)
    failed = tuple(name for name, ok in conditions if not ok)
    return PreconditionCheck(
        action=action,
        allowed=not failed,
        passed=tuple(name for name, ok in conditions if ok),
        failed=failed,
        would_be_round_index=evidence.next_round_index,
        budget_remaining_after=max(0, evidence.budget_remaining - 1),
    )


def validate_proposal_binding(
    evidence: InvestigationEvidence, proposal: FollowUpProposal
) -> None:
    """Reject a proposal not bound to this evidence or not currently permitted."""
    if proposal.study_id != evidence.study_id:
        raise ValueError("proposal study_id does not match the evidence")
    if proposal.parent_analysis_id != evidence.parent_analysis_id:
        raise ValueError("proposal parent_analysis_id does not match the latest round")
    if proposal.parent_result_hash != evidence.parent_result_hash:
        raise ValueError("proposal parent_result_hash does not match the latest released result")
    if proposal.round_index != evidence.next_round_index:
        raise ValueError(
            f"proposal round_index must be {evidence.next_round_index}, the next round"
        )
    check = check_preconditions(evidence, proposal.action)
    if not check.allowed:
        raise ValueError(
            f"{proposal.action} is not permitted now; failed preconditions: "
            + ", ".join(check.failed)
        )


def _followup_analysis_id(evidence: InvestigationEvidence, action: str) -> str:
    return f"{evidence.primary_analysis_id}-r{evidence.next_round_index}-{ANALYSIS_SLUGS[action]}"


def compile_followup(
    evidence: InvestigationEvidence,
    parent_contract: AnalysisContract,
    proposal: FollowUpProposal,
) -> AnalysisContract:
    """Compile the single contract an approved non-terminal proposal permits."""
    if proposal.action == "STOP_NO_FOLLOWUP":
        raise ValueError("STOP_NO_FOLLOWUP is terminal and compiles to no contract")
    validate_proposal_binding(evidence, proposal)
    if contract_digest(parent_contract) != evidence.history[-1].contract_hash:
        raise ValueError("parent contract does not match the latest released round")
    if parent_contract.max_association_rounds != evidence.max_association_rounds:
        raise ValueError("evidence budget does not match the parent contract")
    data_mode = data_mode_for_contract(parent_contract)
    if data_mode != evidence.data_mode:
        raise ValueError("parent contract data mode does not match the evidence")
    stratify = proposal.action != "LEAVE_ONE_SITE_OUT"
    # model_validate (not model_copy) so the chain-binding validators run.
    return AnalysisContract.model_validate(
        {
            **parent_contract.model_dump(),
            "analysis_id": _followup_analysis_id(evidence, proposal.action),
            "parent_result_hash": evidence.parent_result_hash,
            "proposal_hash": proposal_digest(proposal),
            "round_index": evidence.next_round_index,
            "template_id": STRATIFIED_TEMPLATES[data_mode] if stratify else LOSO_TEMPLATE,
            "strata": STRATA[data_mode] if stratify else (),
            "covariate_ids": (
                STRATIFIED_COVARIATES[data_mode] if stratify else PRIMARY_COVARIATES[data_mode]
            ),
        }
    )


def round_budget_violation(contract: AnalysisContract, max_rounds_cap: int) -> str | None:
    """Return a refusal code when a contract exceeds its own or a local budget."""
    if contract.max_association_rounds > max_rounds_cap:
        return "QUERY_BUDGET_ABOVE_LOCAL_CAP"
    if contract.round_index >= contract.max_association_rounds:
        return "QUERY_BUDGET_EXHAUSTED"
    return None


def action_for_template(data_mode: DataMode, template_id: str) -> str:
    """Map a follow-up template back to the only action that compiles to it."""
    if template_id == LOSO_TEMPLATE:
        return "LEAVE_ONE_SITE_OUT"
    if template_id == STRATIFIED_TEMPLATES[data_mode]:
        return STRATIFY_ACTIONS[data_mode]
    raise ValueError(f"template {template_id} is not a follow-up template for {data_mode}")


def verify_followup_binding(
    contract: AnalysisContract,
    proposal: FollowUpProposal,
    *,
    max_rounds_cap: int = COORDINATOR_MAX_ASSOCIATION_ROUNDS,
) -> None:
    """Coordinator-side check that a follow-up contract is the one approved."""
    if contract.round_index < 1:
        raise ValueError("a follow-up requires a contract with round_index >= 1")
    violation = round_budget_violation(contract, max_rounds_cap)
    if violation is not None:
        raise ValueError(f"follow-up refused: {violation}")
    if contract.proposal_hash != proposal_digest(proposal):
        raise ValueError("approved contract is not bound to this exact proposal")
    if contract.parent_result_hash != proposal.parent_result_hash:
        raise ValueError("approved contract is not bound to the proposal parent result")
    if contract.round_index != proposal.round_index or contract.study_id != proposal.study_id:
        raise ValueError("approved contract round or study does not match the proposal")
    expected = action_for_template(data_mode_for_contract(contract), contract.template_id)
    if proposal.action != expected:
        raise ValueError("the proposal action does not compile to this contract template")
