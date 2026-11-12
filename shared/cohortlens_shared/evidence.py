"""Aggregate-only reductions from released results to investigator evidence.

Everything returned here is network-level: site aliases, site estimates,
sample counts per site and snapshot identifiers are dropped by construction,
because the output models have no field that could hold them.
"""

from __future__ import annotations

from collections.abc import Sequence

from .schemas import (
    HETEROGENEITY_THRESHOLD,
    AnalysisContract,
    AssociationResult,
    InvestigationEvidence,
    LeaveOneSiteOutResult,
    LosoAggregate,
    NetworkAggregate,
    ReleasedResult,
    RoundSummary,
    StratifiedAssociationResult,
    StratumAggregate,
    contract_digest,
    data_mode_for_contract,
    result_digest,
)

_AGGREGATE_FIELDS = (
    "status",
    "expected_sites",
    "estimated_sites",
    "beta_meta",
    "standard_error_meta",
    "ci95_low",
    "ci95_high",
    "q",
    "q_degrees_freedom",
    "i2",
    "heterogeneity_flag",
)


def network_aggregate(result: AssociationResult) -> NetworkAggregate:
    """Copy only the pooled fields of one association result."""
    return NetworkAggregate(**{name: getattr(result, name) for name in _AGGREGATE_FIELDS})


def _stratum_id(result: AssociationResult, fallback: str) -> str:
    ids = {reply.stratum_id for reply in result.site_results}
    if len(ids) > 1:
        raise ValueError("a stratum result mixes stratum bindings")
    return ids.pop() if ids else fallback


def stratum_aggregates(
    result: StratifiedAssociationResult, strata: Sequence[str]
) -> tuple[StratumAggregate, ...]:
    """Reduce each stratum to its pooled summary, checking its declared id."""
    reduced = []
    for declared, item in zip(strata, result.strata_results, strict=True):
        if _stratum_id(item, declared) != declared:
            raise ValueError("stratified result order does not match the contract strata")
        reduced.append(StratumAggregate(stratum_id=declared, **network_aggregate(item).model_dump()))
    return tuple(reduced)


def loso_aggregate(result: LeaveOneSiteOutResult) -> LosoAggregate:
    """Reduce omissions to identity-free ranges; no per-omission values survive."""
    omissions = result.omissions
    if not omissions:
        return LosoAggregate(omissions_evaluated=0)
    betas = [item.beta_meta for item in omissions]
    i2s = [item.i2 for item in omissions]
    clearing = sum(not item.heterogeneity_flag for item in omissions)
    return LosoAggregate(
        omissions_evaluated=len(omissions),
        pooled_beta_min=min(betas),
        pooled_beta_max=max(betas),
        i2_min=min(i2s),
        i2_max=max(i2s),
        omissions_clearing_flag=clearing,
        any_omission_clears_flag=clearing > 0,
    )


def round_summary(
    result: ReleasedResult, contract: AnalysisContract
) -> RoundSummary:
    """Summarise one released round against the contract that produced it."""
    if result.contract_hash != contract_digest(contract):
        raise ValueError("released result does not belong to this contract")
    common = {
        "round_index": contract.round_index,
        "analysis_id": contract.analysis_id,
        "contract_hash": result.contract_hash,
        "result_hash": result_digest(result),
        "status": result.status,
    }
    if isinstance(result, AssociationResult):
        if contract.round_index != 0:
            raise ValueError("a plain association result must be the primary round")
        return RoundSummary(action="PRIMARY_ASSOCIATION", network=network_aggregate(result), **common)
    if isinstance(result, StratifiedAssociationResult):
        mode = data_mode_for_contract(contract)
        action = "STRATIFY_GDSC_LINEAGE" if mode == "PUBLIC_GDSC" else "STRATIFY_SYNTHETIC_CONTEXT"
        return RoundSummary(action=action, strata=stratum_aggregates(result, contract.strata), **common)
    if isinstance(result, LeaveOneSiteOutResult):
        return RoundSummary(
            action="LEAVE_ONE_SITE_OUT",
            network=network_aggregate(result.full_result),
            loso=loso_aggregate(result),
            **common,
        )
    raise ValueError("unsupported released result for investigation evidence")


def investigation_evidence(
    rounds: Sequence[tuple[ReleasedResult, AnalysisContract]],
) -> InvestigationEvidence:
    """Build the investigator packet from the ordered chain of released rounds."""
    if not rounds:
        raise ValueError("at least the primary round is required")
    history = tuple(round_summary(result, contract) for result, contract in rounds)
    primary_result, primary_contract = rounds[0]
    if not isinstance(primary_result, AssociationResult):
        raise ValueError("the first round must be the primary association result")
    budgets = {contract.max_association_rounds for _, contract in rounds}
    if len(budgets) != 1:
        raise ValueError("every contract in a chain must declare the same budget")
    for (_, contract), previous in zip(rounds[1:], history, strict=False):
        if contract.parent_result_hash != previous.result_hash:
            raise ValueError("follow-up contract is not bound to the previous released result")
    latest = history[-1]
    return InvestigationEvidence(
        study_id=primary_contract.study_id,
        data_mode=data_mode_for_contract(primary_contract),
        phenotype_unit=primary_result.phenotype_unit,
        data_provenance=primary_result.data_provenance,
        synthetic=primary_result.synthetic,
        primary_analysis_id=primary_contract.analysis_id,
        parent_analysis_id=latest.analysis_id,
        parent_result_hash=latest.result_hash,
        next_round_index=len(history),
        max_association_rounds=primary_contract.max_association_rounds,
        heterogeneity_threshold=HETEROGENEITY_THRESHOLD,
        history=history,
    )
