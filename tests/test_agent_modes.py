"""Reporter mode and proposal-schema strictness.

Replaces test_phase1_agent.py: the planner mode, whose prompt contained the
only permissible answer, was removed; the reporter now takes the full
aggregate-only investigation evidence and its prompt carries a schema, not values.
"""

import pytest
from pydantic import ValidationError

from cohortlens_agent.agent_app import build_reporter_prompt, parse_reporter_output
from cohortlens_shared.evidence import investigation_evidence
from cohortlens_shared.schemas import FollowUpProposal, ReporterOutput
from support import primary_round, proposal_for


def _evidence():
    return investigation_evidence([primary_round()])


def test_reporter_prompt_contains_only_reduced_evidence_and_no_answer() -> None:
    evidence = _evidence()
    prompt = build_reporter_prompt(evidence)
    assert evidence.parent_result_hash in prompt
    for marker in ("site_results", "site-1", "n_used", "patient_id"):
        assert marker not in prompt
    assert "collude" in prompt
    assert '"summary":' not in prompt.replace('"summary": {', "")


def test_reporter_output_is_strict_and_bound() -> None:
    evidence = _evidence()
    output = (
        '{"schema_version":"1.0","role":"reporter","study_id":"phase1-demo",'
        '"analysis_id":"primary-001","summary":"Pooled effect is positive.",'
        '"site_disagreement":"Heterogeneity is high.","privacy_boundary":"Summaries only.",'
        '"limitations":["Synthetic data."]}'
    )
    assert isinstance(parse_reporter_output(output, evidence), ReporterOutput)
    with pytest.raises(ValueError, match="identifiers"):
        parse_reporter_output(output.replace("primary-001", "other-001"), evidence)
    with pytest.raises(ValidationError):
        parse_reporter_output(output.replace('"limitations"', '"extra":1,"limitations"'), evidence)


def test_proposal_rejects_invented_actions_and_wrong_declared_changes() -> None:
    evidence = _evidence()
    with pytest.raises(ValidationError):
        proposal_for(evidence, "RECHECK_GDSC_METADATA")
    with pytest.raises(ValidationError, match="changed_contract_fields"):
        proposal_for(evidence, "LEAVE_ONE_SITE_OUT", changed_contract_fields=["analysis_id"])
    with pytest.raises(ValidationError, match="changed_contract_fields"):
        proposal_for(evidence, "STOP_NO_FOLLOWUP", changed_contract_fields=["strata"])
    payload = proposal_for(evidence, "LEAVE_ONE_SITE_OUT").model_dump()
    payload["patient_rows"] = ["x"]
    with pytest.raises(ValidationError, match="Extra inputs"):
        FollowUpProposal.model_validate(payload)
