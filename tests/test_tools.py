"""Deterministic investigator tools over aggregate-only evidence."""

import math

import pytest

from cohortlens_shared.catalog import catalog_for, check_preconditions
from cohortlens_shared.evidence import investigation_evidence
from cohortlens_shared.tools import InvestigationToolbox, chi_square_sf, tool_specs
from support import followup, primary_round, proposal_for


@pytest.mark.parametrize("statistic,df", [(0.0, 1), (0.5, 1), (3.84, 1), (5.23, 2), (8.5, 2), (40.0, 3), (1e-6, 4), (150.0, 2)])
def test_chi_square_sf_matches_scipy(statistic: float, df: int) -> None:
    stats = pytest.importorskip("scipy.stats")
    assert chi_square_sf(statistic, df) == pytest.approx(float(stats.chi2.sf(statistic, df)), rel=1e-9, abs=1e-14)


def test_chi_square_sf_closed_form_for_two_df() -> None:
    assert chi_square_sf(5.0, 2) == pytest.approx(math.exp(-2.5), rel=1e-12)
    with pytest.raises(ValueError):
        chi_square_sf(-1.0, 2)


def test_inspection_tools_return_network_aggregates_only() -> None:
    evidence = investigation_evidence([primary_round()])
    toolbox = InvestigationToolbox(evidence)
    current = toolbox.call("get_current_evidence", {})
    assert current.ok and current.output["budget_remaining"] == 2
    assert current.output["parent_result_hash"] == evidence.parent_result_hash
    history = toolbox.call("get_history", {})
    assert [item["action"] for item in history.output["rounds"]] == ["PRIMARY_ASSOCIATION"]
    for name in ("get_current_evidence", "get_history", "heterogeneity_diagnostics", "list_actions"):
        rendered = str(toolbox.call(name, {}).output)
        assert "site-1" not in rendered and "n_used" not in rendered


def test_heterogeneity_diagnostics_are_derived_from_released_q() -> None:
    evidence = investigation_evidence([primary_round()])
    network = evidence.history[0].network
    output = InvestigationToolbox(evidence).call("heterogeneity_diagnostics", {}).output
    estimate = output["estimates"][0]
    assert estimate["q_p_value"] == pytest.approx(chi_square_sf(network.q, network.q_degrees_freedom))
    assert estimate["i2_margin_to_threshold"] == pytest.approx(network.i2 - 0.5)
    assert estimate["h2"] == pytest.approx(network.q / network.q_degrees_freedom)
    bad = InvestigationToolbox(evidence).call("heterogeneity_diagnostics", {"round_index": 4})
    assert not bad.ok


def test_catalog_lists_three_distinct_actions_per_mode() -> None:
    for mode in ("SYNTHETIC", "PUBLIC_GDSC"):
        specs = catalog_for(mode)
        assert len({spec.action for spec in specs}) == 3
        assert [spec.terminal for spec in specs] == [False, False, True]
        assert len({spec.template_id for spec in specs}) == 3


def test_preconditions_track_budget_history_and_support() -> None:
    rounds = [primary_round(budget=2)]
    evidence = investigation_evidence(rounds)
    assert check_preconditions(evidence, "LEAVE_ONE_SITE_OUT").allowed
    assert not check_preconditions(evidence, "STRATIFY_GDSC_LINEAGE").allowed
    result, contract, _ = followup(rounds, "LEAVE_ONE_SITE_OUT")
    evidence = investigation_evidence(rounds + [(result, contract)])
    repeat = check_preconditions(evidence, "LEAVE_ONE_SITE_OUT")
    assert not repeat.allowed and "NOT_ALREADY_RUN" in repeat.failed
    unflagged = evidence.model_copy(deep=True)
    primary = unflagged.history[0].network.model_copy(update={"heterogeneity_flag": False})
    history = (unflagged.history[0].model_copy(update={"network": primary}),) + unflagged.history[1:]
    unflagged = unflagged.model_copy(update={"history": history})
    assert "PRIMARY_HETEROGENEITY_FLAG_SET" in check_preconditions(unflagged, "STRATIFY_SYNTHETIC_CONTEXT").failed
    assert check_preconditions(evidence, "STOP_NO_FOLLOWUP").allowed


def test_submit_proposal_returns_errors_instead_of_raising() -> None:
    evidence = investigation_evidence([primary_round()])
    toolbox = InvestigationToolbox(evidence)
    good = proposal_for(evidence, "LEAVE_ONE_SITE_OUT").model_dump(mode="json")
    wrong_round = toolbox.call("submit_proposal", good | {"round_index": 2})
    assert not wrong_round.ok and "round_index must be 1" in wrong_round.output["errors"][0]
    missing = toolbox.call("submit_proposal", {"action": "LEAVE_ONE_SITE_OUT"})
    assert not missing.ok and any("rationale" in item for item in missing.output["errors"])
    accepted = toolbox.call("submit_proposal", good)
    assert accepted.ok and accepted.proposal is not None
    assert not toolbox.call("drop_table", {}).ok


def test_tool_specs_expose_the_mode_specific_action_enum() -> None:
    evidence = investigation_evidence([primary_round()])
    specs = {spec.name: spec for spec in tool_specs(evidence)}
    actions = specs["submit_proposal"].parameters["properties"]["action"]["enum"]
    assert actions == ["STRATIFY_SYNTHETIC_CONTEXT", "LEAVE_ONE_SITE_OUT", "STOP_NO_FOLLOWUP"]
    assert set(specs) == set(InvestigationToolbox(evidence).names)
