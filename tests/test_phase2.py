"""Phase 2 bounded follow-up contracts and local stratified calculations."""

import pytest
from flwr.app import ConfigRecord, Message, RecordDict

from cohortlens_agent.agent_app import parse_agent_output
from cohortlens_agent.agent_app import build_prompt
from cohortlens_analytics.client_app import handle_association
from cohortlens_analytics.client_app import handle_preflight
from cohortlens_operator.agent_frames import (
    AGENT_RESULT_FRAME_PREFIX,
    AgentFrameError,
    extract_followup_proposal,
)
from cohortlens_shared.schemas import (
    AssociationRequest,
    FollowUpProposal,
    canonical_json,
    compile_stratified_followup,
    default_phase1_approval,
    default_phase1_contract,
    evidence_from_result,
    followup_evidence_from_result,
    investigation_decision_from_result,
    proposal_digest,
)
from cohortlens_shared.statistics import (
    context_stratum,
    fit_genotype_ols_hc3,
    generate_synthetic_site,
)


def _primary_result():
    """A released-like primary result is supplied by the existing server tests."""
    from cohortlens_analytics.server_app import Phase1Config, run_phase1

    contract = default_phase1_contract()
    return run_phase1(_Grid(), Phase1Config(contract, default_phase1_approval(contract), "primary-001"), emit=False)


class _Grid:
    """Minimal real-handler transport fixture; it never sees a local row."""

    node_ids = [101, 102, 103]

    def get_node_ids(self):
        return self.node_ids

    def send_and_receive(self, messages, *, timeout=None):
        replies = []
        for message in messages:
            alias = f"site-{self.node_ids.index(message.metadata.dst_node_id) + 1}"
            replies.append(handle_preflight(message, alias) if message.metadata.message_type == "query.preflight" else handle_association(message, alias))
        return replies


def _proposal(parent):
    return FollowUpProposal(
        study_id=parent.study_id,
        parent_analysis_id=parent.analysis_id,
        parent_result_hash=evidence_from_result(parent).parent_result_hash,
        action="STRATIFY_SYNTHETIC_CONTEXT",
        evidence_fields=("heterogeneity_flag", "i2", "estimated_sites"),
        changed_contract_fields=("analysis_id", "parent_result_hash", "strata", "covariate_ids"),
        expected_interpretive_value="Compare the released effects within predeclared contexts.",
        disclosure_cost="Two bounded site summaries per site; no rows are released.",
        stop_condition="Stop after this approved exploratory follow-up.",
        rationale="The released heterogeneity flag and I2 support checking context mixture.",
    )


def test_stratified_contract_is_parent_bound_and_changes_digest() -> None:
    parent = _primary_result()
    contract = compile_stratified_followup(parent, _proposal(parent))
    assert contract.parent_result_hash == evidence_from_result(parent).parent_result_hash
    assert contract.strata == ("context_0", "context_1")
    assert contract.covariate_ids == ("age_z_v1", "sex_code_v1")
    assert contract.analysis_id != parent.analysis_id


def test_stratified_local_ols_drops_constant_context_column() -> None:
    design, outcome = generate_synthetic_site(1)
    stratum_design, stratum_outcome = context_stratum(design, outcome, "context_1")
    assert stratum_design.shape[1] == 4
    assert fit_genotype_ols_hc3("site-2", stratum_design, stratum_outcome).n_used == len(stratum_outcome)


def test_client_reply_is_limited_to_requested_stratum_summary() -> None:
    parent = _primary_result()
    contract = compile_stratified_followup(parent, _proposal(parent))
    approval = default_phase1_approval(contract)
    request = AssociationRequest(query_id=contract.analysis_id, contract=contract, approval=approval, stratum_id="context_0")
    message = Message(RecordDict({"association_request": ConfigRecord({"json": canonical_json(request)})}), dst_node_id=1, group_id="test", message_type="query.associate")
    reply = handle_association(message, "site-1")
    payload = reply.content["association_reply"]["json"]
    assert '"stratum_id":"context_0"' in payload
    assert "design" not in payload and "outcome" not in payload


def test_second_run_returns_two_traceable_stratum_meta_analyses() -> None:
    from cohortlens_analytics.server_app import Phase1Config, run_phase2

    parent = _primary_result()
    proposal = _proposal(parent)
    contract = compile_stratified_followup(parent, proposal)
    result = run_phase2(
        _Grid(), Phase1Config(contract, default_phase1_approval(contract), contract.analysis_id), proposal
    )
    assert result.parent_result_hash == proposal.parent_result_hash
    assert [item.site_results[0].stratum_id for item in result.strata_results] == ["context_0", "context_1"]
    assert all(item.estimated_sites == 3 for item in result.strata_results)
    evidence = followup_evidence_from_result(result)
    assert [item.stratum_id for item in evidence.strata] == [
        "context_0",
        "context_1",
    ]
    assert "site-1" not in canonical_json(evidence)
    prompt, validated = build_prompt(
        "followup_reporter", canonical_json(evidence)
    )
    assert validated == evidence
    assert "not replication" in prompt
    parsed = parse_agent_output(
        "followup_reporter",
        '{"schema_version":"1.0","role":"reporter","study_id":"phase1-demo","analysis_id":"primary-001-context-001","summary":"Exploratory contexts differ.","site_disagreement":"Within-context heterogeneity is low.","privacy_boundary":"Only aggregate evidence was provided.","limitations":["This is not independent replication."]}',
    )
    assert parsed.analysis_id == result.analysis_id


def test_invented_followup_and_bad_parent_are_rejected() -> None:
    parent = _primary_result()
    proposal = _proposal(parent)
    with pytest.raises(ValueError):
        compile_stratified_followup(parent, proposal.model_copy(update={"parent_result_hash": "sha256:" + "0" * 64}))
    with pytest.raises(ValueError):
        parse_agent_output("investigator", '{"schema_version":"1.0","role":"investigator","study_id":"phase1-demo","parent_analysis_id":"primary-001","parent_result_hash":"sha256:' + "1" * 64 + '","action":"EXPORT_PATIENTS","evidence_fields":["i2"],"changed_contract_fields":[],"expected_interpretive_value":"x","disclosure_cost":"x","stop_condition":"x","rationale":"x"}')
    assert proposal_digest(proposal).startswith("sha256:")


def test_deterministic_gate_invokes_investigator_only_for_flagged_disagreement() -> None:
    parent = _primary_result()
    assert investigation_decision_from_result(parent).decision == "INVESTIGATE"
    compatible = parent.model_copy(
        update={"heterogeneity_flag": False, "i2": 0.10}
    )
    assert (
        investigation_decision_from_result(compatible).decision
        == "FINISH_COMPATIBLE"
    )
    insufficient = parent.model_copy(
        update={"status": "INSUFFICIENT", "estimated_sites": 1}
    )
    assert (
        investigation_decision_from_result(insufficient).decision
        == "STOP_INSUFFICIENT_EVIDENCE"
    )


def test_investigator_prompt_contains_exact_strict_shape() -> None:
    evidence = evidence_from_result(_primary_result())
    prompt, validated = build_prompt("investigator", canonical_json(evidence))
    assert validated == evidence
    assert '"parent_analysis_id":"primary-001"' in prompt
    assert '"expected_interpretive_value"' in prompt
    assert "bound_to_parent_analysis" not in prompt


def test_investigator_frame_collection_is_unique_and_parent_bound() -> None:
    parent = _primary_result()
    proposal = _proposal(parent)
    frame = AGENT_RESULT_FRAME_PREFIX + canonical_json(proposal)
    parent_hash = evidence_from_result(parent).parent_result_hash
    assert (
        extract_followup_proposal(
            [frame, f"INFO {frame}"], expected_parent_result_hash=parent_hash
        )
        == proposal
    )
    with pytest.raises(AgentFrameError, match="expected parent"):
        extract_followup_proposal(
            [frame], expected_parent_result_hash="sha256:" + "0" * 64
        )
