"""Follow-up rounds: compilation, local strata, the gates and frame collection.

Replaces the former single-follow-up tests (test_phase2.py): the follow-up is
now one of several catalog actions in a hash-chained, budgeted sequence.
"""

import pytest
from flwr.app import ConfigRecord, Message, RecordDict

from cohortlens_analytics.client_app import handle_association
from cohortlens_operator.agent_frames import (
    AGENT_RESULT_FRAME_PREFIX,
    AgentFrameError,
    extract_followup_proposal,
    extract_investigator_record,
)
from cohortlens_shared.catalog import compile_followup
from cohortlens_shared.evidence import investigation_evidence
from cohortlens_shared.schemas import (
    AssociationRequest,
    InvestigatorRecord,
    canonical_json,
    continuation_decision,
    default_phase1_approval,
    investigation_decision_from_result,
    proposal_digest,
    result_digest,
)
from cohortlens_shared.statistics import context_stratum, fit_genotype_ols_hc3, generate_synthetic_site
from support import followup, primary_round, proposal_for


def test_stratified_contract_is_parent_and_proposal_bound() -> None:
    rounds = [primary_round()]
    evidence = investigation_evidence(rounds)
    proposal = proposal_for(evidence, "STRATIFY_SYNTHETIC_CONTEXT")
    contract = compile_followup(evidence, rounds[0][1], proposal)
    assert contract.parent_result_hash == result_digest(rounds[0][0])
    assert contract.proposal_hash == proposal_digest(proposal)
    assert contract.round_index == 1
    assert contract.strata == ("context_0", "context_1")
    assert contract.covariate_ids == ("age_z_v1", "sex_code_v1")
    assert contract.analysis_id == "primary-001-r1-context"


def test_stratified_local_ols_drops_constant_context_column() -> None:
    design, outcome = generate_synthetic_site(1)
    stratum_design, stratum_outcome = context_stratum(design, outcome, "context_1")
    assert stratum_design.shape[1] == 4
    assert fit_genotype_ols_hc3("site-2", stratum_design, stratum_outcome).n_used == len(stratum_outcome)


def test_client_reply_is_limited_to_requested_stratum_summary() -> None:
    rounds = [primary_round()]
    evidence = investigation_evidence(rounds)
    contract = compile_followup(evidence, rounds[0][1], proposal_for(evidence, "STRATIFY_SYNTHETIC_CONTEXT"))
    request = AssociationRequest(
        query_id=contract.analysis_id,
        contract=contract,
        approval=default_phase1_approval(contract),
        stratum_id="context_0",
    )
    message = Message(
        RecordDict({"association_request": ConfigRecord({"json": canonical_json(request)})}),
        dst_node_id=1,
        group_id="test",
        message_type="query.associate",
    )
    payload = handle_association(message, "site-1").content["association_reply"]["json"]
    assert '"stratum_id":"context_0"' in payload
    assert '"status":"ESTIMATED"' in payload
    assert "design" not in payload and "outcome" not in payload


def test_stratified_round_returns_two_traceable_stratum_meta_analyses() -> None:
    rounds = [primary_round()]
    result, contract, proposal = followup(rounds, "STRATIFY_SYNTHETIC_CONTEXT")
    assert result.parent_result_hash == proposal.parent_result_hash
    assert result.round_index == 1
    assert [item.site_results[0].stratum_id for item in result.strata_results] == ["context_0", "context_1"]
    assert all(item.estimated_sites == 3 for item in result.strata_results)
    assert all(item.i2 is not None and item.i2 < 0.5 for item in result.strata_results)
    evidence = investigation_evidence(rounds + [(result, contract)])
    assert [item.stratum_id for item in evidence.history[1].strata] == ["context_0", "context_1"]
    assert "site-1" not in canonical_json(evidence)


def test_invented_followup_and_bad_parent_are_rejected() -> None:
    rounds = [primary_round()]
    evidence = investigation_evidence(rounds)
    bad_parent = proposal_for(evidence, "STRATIFY_SYNTHETIC_CONTEXT", parent_result_hash="sha256:" + "0" * 64)
    with pytest.raises(ValueError, match="parent_result_hash"):
        compile_followup(evidence, rounds[0][1], bad_parent)
    with pytest.raises(ValueError):
        proposal_for(evidence, "EXPORT_PATIENTS")
    with pytest.raises(ValueError, match="STOP_NO_FOLLOWUP is terminal"):
        compile_followup(evidence, rounds[0][1], proposal_for(evidence, "STOP_NO_FOLLOWUP"))


def test_primary_gate_invokes_investigator_only_for_flagged_disagreement() -> None:
    parent, _ = primary_round()
    assert investigation_decision_from_result(parent).decision == "INVESTIGATE"
    compatible = parent.model_copy(update={"heterogeneity_flag": False, "i2": 0.10})
    assert investigation_decision_from_result(compatible).decision == "FINISH_COMPATIBLE"
    insufficient = parent.model_copy(update={"status": "INSUFFICIENT", "estimated_sites": 1})
    assert investigation_decision_from_result(insufficient).decision == "STOP_INSUFFICIENT_EVIDENCE"


def test_continuation_gate_enforces_the_declared_budget() -> None:
    rounds = [primary_round(budget=2)]
    result, contract, _ = followup(rounds, "LEAVE_ONE_SITE_OUT")
    rounds.append((result, contract))
    assert continuation_decision(investigation_evidence(rounds)).decision == "INVESTIGATE"
    result, contract, _ = followup(rounds, "STRATIFY_SYNTHETIC_CONTEXT")
    rounds.append((result, contract))
    decision = continuation_decision(investigation_evidence(rounds))
    assert decision.decision == "BUDGET_EXHAUSTED"
    assert decision.round_index == 2


def _record(evidence, proposal) -> InvestigatorRecord:
    return InvestigatorRecord(
        study_id=evidence.study_id,
        parent_analysis_id=evidence.parent_analysis_id,
        parent_result_hash=evidence.parent_result_hash,
        evidence_hash="sha256:" + "a" * 64,
        policy_kind="MODEL",
        policy_label="flower-endeavor",
        protocol_requested="json",
        protocol_used="json",
        status="PROPOSED",
        proposal=proposal,
        steps=1,
        tool_calls=1,
        invalid_turns=0,
        trace=(),
    )


def test_investigator_frame_collection_is_unique_and_parent_bound() -> None:
    evidence = investigation_evidence([primary_round()])
    proposal = proposal_for(evidence, "LEAVE_ONE_SITE_OUT")
    frame = AGENT_RESULT_FRAME_PREFIX + canonical_json(_record(evidence, proposal))
    parent_hash = evidence.parent_result_hash
    assert extract_followup_proposal([frame, f"INFO {frame}"], expected_parent_result_hash=parent_hash) == proposal
    assert extract_investigator_record([frame]).trace == ()
    with pytest.raises(AgentFrameError, match="expected parent"):
        extract_followup_proposal([frame], expected_parent_result_hash="sha256:" + "0" * 64)
    other = proposal_for(evidence, "STOP_NO_FOLLOWUP")
    conflicting = AGENT_RESULT_FRAME_PREFIX + canonical_json(_record(evidence, other))
    with pytest.raises(AgentFrameError, match="conflicting"):
        extract_investigator_record([frame, conflicting])
