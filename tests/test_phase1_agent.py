"""Tests for bounded agent inputs and outputs."""

import pytest
from pydantic import ValidationError

from cohortlens_agent.agent_app import build_prompt, parse_agent_output
from cohortlens_shared.schemas import AgentEvidence, PlannerProposal, ReporterOutput, canonical_json


def test_planner_accepts_only_the_fixed_analysis() -> None:
    output = """{
      "schema_version":"1.0",
      "role":"planner",
      "study_id":"phase1-demo",
      "action":"RUN_PRIMARY_ASSOCIATION",
      "template_id":"ols_quantitative_v1",
      "variant_key":"demo:1:1000:A:G",
      "effect_allele":"G",
      "phenotype_id":"biomarker_y_v1",
      "rationale":"Run the approved synthetic association across three sites.",
      "limitations":["Synthetic data only.","Association is not causation."]
    }"""
    proposal = parse_agent_output("planner", output)
    assert isinstance(proposal, PlannerProposal)
    assert proposal.action == "RUN_PRIMARY_ASSOCIATION"


def test_planner_rejects_an_invented_analysis() -> None:
    output = '{"schema_version":"1.0","role":"planner","study_id":"phase1-demo","action":"EXPORT_PATIENTS","template_id":"anything","variant_key":"secret","effect_allele":"A","phenotype_id":"private","rationale":"x","limitations":["x"]}'
    with pytest.raises(ValidationError):
        parse_agent_output("planner", output)


def test_reporter_prompt_contains_only_reduced_evidence() -> None:
    evidence = AgentEvidence(
        study_id="phase1-demo",
        analysis_id="primary-001",
        contract_hash="sha256:" + "1" * 64,
        expected_sites=3,
        estimated_sites=3,
        beta_meta=0.5,
        standard_error_meta=0.02,
        ci95_low=0.46,
        ci95_high=0.54,
        i2=0.97,
        heterogeneity_flag=True,
        phenotype_unit="demo_unit",
    )
    prompt, validated = build_prompt("reporter", canonical_json(evidence))
    assert validated == evidence
    assert "patient_id" not in prompt
    assert "site_results" not in prompt
    assert "collude" in prompt


def test_reporter_output_is_strict() -> None:
    output = '{"schema_version":"1.0","role":"reporter","study_id":"phase1-demo","analysis_id":"primary-001","summary":"The released aggregate is positive.","site_disagreement":"Heterogeneity is high.","privacy_boundary":"Rows stayed local, but summaries and dishonest sites remain risks.","limitations":["Synthetic data.","No causal claim."]}'
    assert isinstance(parse_agent_output("reporter", output), ReporterOutput)

