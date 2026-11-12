"""Deterministic scripted investigator policy for tests and the offline demo.

THIS IS NOT A LANGUAGE MODEL. It is a fixed rule set that speaks the same
tool protocols as a model, so the real loop, tools, validation and ledger are
exercised end to end without network access. Every record it produces is
labelled ``policy_kind = "SCRIPTED_POLICY"`` and ``policy_label =
"cohortlens-scripted-policy-v1"``.

Rule set, applied only to what the tools returned in this conversation:

1. Inspect current evidence, history, heterogeneity diagnostics and the catalog.
2. If the latest round was a stratification and every stratum's I2 is below the
   threshold, the disagreement is explained: prefer ``STOP_NO_FOLLOWUP``.
3. Otherwise prefer, in order: leave-one-site-out (no new site-level quantity),
   then the predeclared stratification (tests an effect modifier), then stop.
4. Check each preferred action's preconditions and submit the first allowed one.
"""

from __future__ import annotations

import json
from typing import Any

from cohortlens_shared.investigator import (
    TOOL_RESULT_PREFIX,
    FunctionCall,
    ModelTurn,
)

POLICY_KIND = "SCRIPTED_POLICY"
POLICY_LABEL = "cohortlens-scripted-policy-v1"
INSPECTION_ORDER = ("get_current_evidence", "get_history", "heterogeneity_diagnostics", "list_actions")
EVIDENCE_FIELDS = {
    "LEAVE_ONE_SITE_OUT": [
        "heterogeneity_flag", "i2", "q_p_value", "estimated_sites", "budget_remaining",
    ],
    "STRATIFY": [
        "heterogeneity_flag", "i2", "q_p_value", "loso_flag_cleared", "loso_i2_range",
        "budget_remaining",
    ],
    "STOP_NO_FOLLOWUP": ["stratum_i2", "i2", "budget_remaining"],
}


def _tool_results(request: dict[str, Any]) -> list[dict[str, Any]]:
    """Recover tool outputs from either protocol's conversation input."""
    results = []
    for item in request.get("input", []):
        if item.get("type") == "function_call_output":
            results.append(json.loads(item["output"]))
        elif item.get("role") == "user" and str(item.get("content", "")).startswith(TOOL_RESULT_PREFIX):
            results.append(json.loads(item["content"][len(TOOL_RESULT_PREFIX):]))
    return results


def _latest(results: list[dict[str, Any]], tool: str) -> dict[str, Any] | None:
    matches = [item["output"] for item in results if item.get("tool") == tool]
    return matches[-1] if matches else None


def _checks(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        item["output"]["action"]: item["output"]
        for item in results
        if item.get("tool") == "check_action" and item.get("ok")
    }


def _stratify_action(catalog: dict[str, Any]) -> str:
    return next(item["action"] for item in catalog["actions"] if item["action"].startswith("STRATIFY_"))


def _explained_by_strata(current: dict[str, Any]) -> bool:
    latest = current["latest_round"]
    strata = latest.get("strata", [])
    threshold = current["heterogeneity_threshold"]
    return bool(strata) and all(item.get("i2") is not None and item["i2"] < threshold for item in strata)


def preferences(current: dict[str, Any], catalog: dict[str, Any]) -> list[str]:
    """Ordered candidate actions under rules 2 and 3."""
    if _explained_by_strata(current):
        return ["STOP_NO_FOLLOWUP"]
    return ["LEAVE_ONE_SITE_OUT", _stratify_action(catalog), "STOP_NO_FOLLOWUP"]


def _fmt(value: Any) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def _rationale(action: str, current: dict[str, Any], history: dict[str, Any]) -> str:
    primary = history["rounds"][0].get("network", {})
    base = (
        f"Primary I2 {_fmt(primary.get('i2'))} with {primary.get('estimated_sites')} estimates; "
        f"{current['budget_remaining']} follow-up round(s) remain."
    )
    loso = next((r["loso"] for r in history["rounds"] if r.get("loso")), None)
    if loso is not None:
        base += (
            f" LOSO: pooled beta range [{_fmt(loso.get('pooled_beta_min'))}, "
            f"{_fmt(loso.get('pooled_beta_max'))}],"
            f" I2 range [{_fmt(loso.get('i2_min'))}, {_fmt(loso.get('i2_max'))}],"
            f" {loso.get('omissions_clearing_flag')} single omission(s) clear the flag."
        )
    reasons = {
        "LEAVE_ONE_SITE_OUT": (
            " Scripted rule 3: check first whether one site drives the disagreement,"
            " at no new site-level disclosure."
        ),
        "STOP_NO_FOLLOWUP": (
            " Scripted rule 2/3: no further permitted query is expected to add"
            " information worth its cost."
        ),
    }
    default = " Scripted rule 3: test the predeclared effect modifier as the explanation."
    return base + reasons.get(action, default)


def build_proposal(
    action: str, current: dict[str, Any], history: dict[str, Any], catalog: dict[str, Any]
) -> dict[str, Any]:
    """Fill a proposal from tool outputs only."""
    spec = next(item for item in catalog["actions"] if item["action"] == action)
    key = "STRATIFY" if action.startswith("STRATIFY_") else action
    return {
        "study_id": current["study_id"],
        "parent_analysis_id": current["parent_analysis_id"],
        "parent_result_hash": current["parent_result_hash"],
        "round_index": current["next_round_index"],
        "action": action,
        "evidence_fields": EVIDENCE_FIELDS[key],
        "changed_contract_fields": spec["changed_contract_fields"],
        "expected_interpretive_value": spec["what_it_tests"],
        "disclosure_cost": spec["disclosure_cost"],
        "stop_condition": (
            "Re-plan after the released aggregates; stop when the budget is spent"
            " or nothing permitted adds information."
        ),
        "rationale": _rationale(action, current, history),
    }


class ScriptedPolicyTransport:
    """``ModelTransport`` implementation of the rule set above."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self._calls = 0

    def _next_action(self, results: list[dict[str, Any]]) -> tuple[str, dict[str, Any]]:
        for tool in INSPECTION_ORDER:
            if _latest(results, tool) is None:
                return tool, {}
        current = _latest(results, "get_current_evidence")
        history = _latest(results, "get_history")
        catalog = _latest(results, "list_actions")
        assert current is not None and history is not None and catalog is not None
        checks = _checks(results)
        for action in preferences(current, catalog):
            if action not in checks:
                return "check_action", {"action": action}
            if checks[action]["allowed"]:
                return "submit_proposal", build_proposal(action, current, history, catalog)
        return "submit_proposal", build_proposal("STOP_NO_FOLLOWUP", current, history, catalog)

    def create(self, request: dict[str, Any]) -> ModelTurn:
        """Return the next tool call in the protocol the request uses."""
        self.requests.append(request)
        tool, arguments = self._next_action(_tool_results(request))
        self._calls += 1
        if "tools" in request:
            call = FunctionCall(f"scripted-{self._calls}", tool, json.dumps(arguments, sort_keys=True))
            return ModelTurn(function_calls=(call,))
        return ModelTurn(text=json.dumps({"tool": tool, "arguments": arguments}, sort_keys=True))
