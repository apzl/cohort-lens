"""Leave-one-site-out statistics, catalog compilation, and budget/chain enforcement."""

import json

import numpy as np
import pytest
from flwr.app import ConfigRecord, Message, RecordDict

from cohortlens_analytics.client_app import _compatibility_status, handle_preflight
from cohortlens_analytics.server_app import Phase1Config, execute_contract
from cohortlens_operator.local_grid import InMemoryGrid, SiteSnapshot, synthetic_sites
from cohortlens_shared.catalog import compile_followup
from cohortlens_shared.evidence import investigation_evidence
from cohortlens_shared.schemas import (
    AnalysisContract,
    LeaveOneSiteOutResult,
    PreflightReply,
    StudyPreflightRequest,
    canonical_json,
    default_gdsc_contract,
    default_phase1_approval,
    proposal_digest,
)
from cohortlens_shared.statistics import (
    SiteEstimate,
    fit_genotype_ols_hc3,
    fixed_effect_meta,
    generate_synthetic_site,
    leave_one_site_out,
)
from support import followup, primary_round, proposal_for, run_round


def _direct_pool(betas, ses):
    weights = 1 / np.square(ses)
    beta = float(np.sum(weights * betas) / np.sum(weights))
    q = float(np.sum(weights * np.square(betas - beta)))
    df = len(betas) - 1
    return beta, float(np.sqrt(1 / np.sum(weights))), q, (0.0 if q == 0 else max(0.0, (q - df) / q))


def test_loso_matches_direct_recomputation() -> None:
    estimates = [
        fit_genotype_ols_hc3(f"site-{i + 1}", *generate_synthetic_site(i)) for i in range(3)
    ]
    for alias, meta in leave_one_site_out(estimates):
        kept = [item for item in estimates if item.site_alias != alias]
        beta, se, q, i2 = _direct_pool(
            np.array([k.beta for k in kept]), np.array([k.standard_error for k in kept])
        )
        assert (meta.beta, meta.standard_error, meta.q, meta.i2) == pytest.approx((beta, se, q, i2), rel=1e-12)
        assert meta.sites == 2
    with pytest.raises(ValueError, match="three"):
        leave_one_site_out(estimates[:2])


def test_loso_round_releases_omissions_but_evidence_stays_identity_free() -> None:
    rounds = [primary_round()]
    result, contract, proposal = followup(rounds, "LEAVE_ONE_SITE_OUT")
    assert isinstance(result, LeaveOneSiteOutResult)
    assert result.full_result.beta_meta == pytest.approx(rounds[0][0].beta_meta)
    assert [item.omitted_site_alias for item in result.omissions] == ["site-1", "site-2", "site-3"]
    replies = [r for r in result.full_result.site_results]
    for omission in result.omissions:
        kept = [SiteEstimate(site_alias=r.site_alias, beta=r.beta, standard_error=r.standard_error, n_used=r.n_used)
                for r in replies if r.site_alias != omission.omitted_site_alias]
        assert omission.beta_meta == pytest.approx(fixed_effect_meta(kept).beta, rel=1e-12)
    summary = investigation_evidence(rounds + [(result, contract)]).history[1]
    assert summary.loso.omissions_evaluated == 3
    assert summary.loso.pooled_beta_min == pytest.approx(min(o.beta_meta for o in result.omissions))
    assert summary.loso.i2_max == pytest.approx(max(o.i2 for o in result.omissions))
    rendered = canonical_json(summary)
    assert "site-" not in rendered and "omitted" not in rendered


@pytest.mark.parametrize("action", ["LEAVE_ONE_SITE_OUT", "STRATIFY_SYNTHETIC_CONTEXT"])
def test_every_compiled_followup_is_accepted_by_sites(action) -> None:
    rounds = [primary_round()]
    evidence = investigation_evidence(rounds)
    contract = compile_followup(evidence, rounds[0][1], proposal_for(evidence, action))
    design, outcome = generate_synthetic_site(0)
    assert _compatibility_status(contract, default_phase1_approval(contract), design, outcome) == ("READY", None)


def test_gdsc_catalog_compiles_to_site_allowlisted_contracts() -> None:
    from cohortlens_analytics.client_app import SITE_POLICIES

    primary = default_gdsc_contract()
    allowed = {item.template_id: item for item in SITE_POLICIES["CMP_DRIVER_SUMMARY_PINNED"].analyses}
    from cohortlens_shared.catalog import LOSO_TEMPLATE, PRIMARY_COVARIATES, STRATA, STRATIFIED_COVARIATES, STRATIFIED_TEMPLATES

    assert allowed[primary.template_id].covariate_ids == primary.covariate_ids
    assert allowed[LOSO_TEMPLATE].covariate_ids == PRIMARY_COVARIATES["PUBLIC_GDSC"]
    stratified = allowed[STRATIFIED_TEMPLATES["PUBLIC_GDSC"]]
    assert (stratified.covariate_ids, stratified.strata) == (STRATIFIED_COVARIATES["PUBLIC_GDSC"], STRATA["PUBLIC_GDSC"])


def _contract_beyond_budget(round_index: int, max_rounds: int) -> AnalysisContract:
    rounds = [primary_round()]
    evidence = investigation_evidence(rounds)
    contract = compile_followup(evidence, rounds[0][1], proposal_for(evidence, "LEAVE_ONE_SITE_OUT"))
    return AnalysisContract.model_validate(
        contract.model_dump() | {"round_index": round_index, "max_association_rounds": max_rounds}
    )


def _preflight_status(contract: AnalysisContract) -> tuple[str, str | None]:
    request = StudyPreflightRequest(query_id=contract.analysis_id, contract=contract, approval=default_phase1_approval(contract))
    message = Message(RecordDict({"preflight_request": ConfigRecord({"json": canonical_json(request)})}), dst_node_id=1, message_type="query.preflight")
    reply = PreflightReply.model_validate_json(handle_preflight(message, "site-1").content["preflight_reply"]["json"])
    return reply.status, reply.reason_code


def test_site_refuses_beyond_budget_even_with_a_valid_approval() -> None:
    assert _preflight_status(_contract_beyond_budget(3, 3)) == ("POLICY_REJECTED", "QUERY_BUDGET_EXHAUSTED")
    assert _preflight_status(_contract_beyond_budget(1, 4)) == ("POLICY_REJECTED", "QUERY_BUDGET_ABOVE_LOCAL_CAP")


def test_site_refuses_followup_template_at_round_zero_and_unknown_templates() -> None:
    primary = primary_round()[1]
    loso_at_zero = primary.model_copy(update={"template_id": "ols_quantitative_loso_v1"})
    assert _preflight_status(loso_at_zero) == ("POLICY_REJECTED", "ROUND_TEMPLATE_MISMATCH")
    invented = primary.model_copy(update={"template_id": "export_rows_v1"})
    assert _preflight_status(invented) == ("POLICY_REJECTED", "UNSUPPORTED_ANALYSIS_POLICY")


def test_server_refuses_beyond_budget_and_mismatched_proposals() -> None:
    over = _contract_beyond_budget(3, 3)
    with pytest.raises(ValueError, match="QUERY_BUDGET_EXHAUSTED"):
        run_round(over)
    rounds = [primary_round()]
    evidence = investigation_evidence(rounds)
    proposal = proposal_for(evidence, "LEAVE_ONE_SITE_OUT")
    contract = compile_followup(evidence, rounds[0][1], proposal)
    other = proposal_for(evidence, "LEAVE_ONE_SITE_OUT", rationale="A different reviewed text.")
    with pytest.raises(ValueError, match="exact proposal"):
        run_round(contract, other)
    with pytest.raises(ValueError, match="requires the exact operator-approved"):
        run_round(contract, None)
    with pytest.raises(ValueError, match="primary round"):
        run_round(rounds[0][1], proposal)


def test_contract_schema_requires_chain_binding() -> None:
    primary = primary_round()[1]
    payload = json.loads(canonical_json(primary))
    with pytest.raises(ValueError, match="must bind"):
        AnalysisContract.model_validate(payload | {"round_index": 1})
    with pytest.raises(ValueError, match="cannot bind"):
        AnalysisContract.model_validate(payload | {"proposal_hash": "sha256:" + "1" * 64})


def test_loso_is_insufficient_without_three_estimates() -> None:
    small = generate_synthetic_site(2, n=50)
    sites = synthetic_sites()[:2] + [SiteSnapshot("site-3", "tiny", small[0], small[1])]
    primary = primary_round()[1]
    first = run_round(primary, sites=sites)
    assert first.estimated_sites == 2
    evidence = investigation_evidence([(first, primary)])
    check = proposal_for(evidence, "STRATIFY_SYNTHETIC_CONTEXT")
    assert compile_followup(evidence, primary, check).round_index == 1
    with pytest.raises(ValueError, match="AT_LEAST_THREE_PRIMARY_ESTIMATES"):
        compile_followup(evidence, primary, proposal_for(evidence, "LEAVE_ONE_SITE_OUT"))
    assert proposal_digest(check).startswith("sha256:")


def test_in_memory_grid_runs_real_handlers() -> None:
    contract = primary_round()[1]
    grid = InMemoryGrid(synthetic_sites())
    result = execute_contract(grid, Phase1Config(contract, default_phase1_approval(contract), contract.analysis_id, 0.0, 5.0), None)
    assert result.estimated_sites == 3
    assert grid.messages_sent.count("query.preflight") == 3
