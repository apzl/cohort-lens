"""Shared offline fixtures: real handlers over in-memory sites, evidence chains, fake models."""

from __future__ import annotations

import json
from typing import Any

from cohortlens_operator.local_grid import InMemoryGrid, SiteSnapshot, synthetic_sites
from cohortlens_shared.catalog import compile_followup
from cohortlens_shared.evidence import investigation_evidence
from cohortlens_shared.investigator import FunctionCall, ModelTurn
from cohortlens_shared.schemas import (
    AnalysisContract,
    FollowUpProposal,
    InvestigationEvidence,
    ReleasedResult,
    default_phase1_approval,
    default_phase1_contract,
)

SITE_LEVEL_MARKERS = (
    "site-1", "site-2", "site-3", "site_alias", "site_results", "snapshot",
    "n_used", "omitted_site_alias", "standard_error\"", "design", "outcome",
)


def run_round(
    contract: AnalysisContract,
    proposal: FollowUpProposal | None = None,
    sites: list[SiteSnapshot] | None = None,
) -> ReleasedResult:
    """Run one approved round through the real server and client handlers."""
    from cohortlens_analytics.server_app import Phase1Config, execute_contract

    config = Phase1Config(contract, default_phase1_approval(contract), contract.analysis_id, 0.0, 5.0)
    return execute_contract(InMemoryGrid(sites or synthetic_sites()), config, proposal)


def primary_round(budget: int = 2) -> tuple[ReleasedResult, AnalysisContract]:
    """The synthetic primary round (I2 about 0.97, so the gate fires)."""
    contract = default_phase1_contract(followup_budget=budget)
    return run_round(contract), contract


def proposal_for(evidence: InvestigationEvidence, action: str, **changes: Any) -> FollowUpProposal:
    """A valid proposal for ``action`` bound to ``evidence``."""
    from cohortlens_shared.schemas import ACTION_CONTRACT_CHANGES

    fields = {
        "study_id": evidence.study_id,
        "parent_analysis_id": evidence.parent_analysis_id,
        "parent_result_hash": evidence.parent_result_hash,
        "round_index": evidence.next_round_index,
        "action": action,
        "evidence_fields": ["i2", "heterogeneity_flag"],
        "changed_contract_fields": sorted(ACTION_CONTRACT_CHANGES.get(action, ())),
        "expected_interpretive_value": "Explains whether the disagreement has a predeclared source.",
        "disclosure_cost": "Bounded aggregate summaries only.",
        "stop_condition": "Re-plan after the released aggregates.",
        "rationale": "The released heterogeneity flag fired.",
    }
    return FollowUpProposal.model_validate(fields | changes)


def followup(
    rounds: list[tuple[ReleasedResult, AnalysisContract]], action: str
) -> tuple[ReleasedResult, AnalysisContract, FollowUpProposal]:
    """Compile and run one follow-up after ``rounds``."""
    evidence = investigation_evidence(rounds)
    proposal = proposal_for(evidence, action)
    contract = compile_followup(evidence, rounds[-1][1], proposal)
    return run_round(contract, proposal), contract, proposal


class RecordingTransport:
    """Fake model: replays scripted turns and records every request it receives."""

    def __init__(self, turns: list[ModelTurn | Exception]) -> None:
        self.turns = list(turns)
        self.requests: list[dict[str, Any]] = []

    def create(self, request: dict[str, Any]) -> ModelTurn:
        self.requests.append(json.loads(json.dumps(request)))
        if not self.turns:
            return ModelTurn(text="I have nothing more to add.")
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn


def json_turn(tool: str, arguments: dict[str, Any] | None = None) -> ModelTurn:
    """A json-protocol turn calling ``tool``."""
    return ModelTurn(text=json.dumps({"tool": tool, "arguments": arguments or {}}))


def native_turn(tool: str, arguments: dict[str, Any] | None = None, call_id: str = "c1") -> ModelTurn:
    """A native-protocol turn calling ``tool``."""
    return ModelTurn(function_calls=(FunctionCall(call_id, tool, json.dumps(arguments or {})),))


def assert_aggregate_only(requests: list[dict[str, Any]]) -> None:
    """Fail if any request sent to a model contains a site-level marker."""
    assert requests, "no request reached the model"
    for request in requests:
        rendered = json.dumps(request)
        for marker in SITE_LEVEL_MARKERS:
            assert marker not in rendered, f"site-level marker {marker!r} reached the model"
