"""Deterministic investigator tools over aggregate-only evidence.

The tools are pure functions of an ``InvestigationEvidence`` packet. They
never touch a site, a file or the network, and they use only the standard
library so the agent bundle needs no numerical dependency.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from .catalog import catalog_actions, catalog_for, check_preconditions, validate_proposal_binding
from .schemas import (
    CONTRACT_FIELDS,
    EVIDENCE_FIELDS,
    FollowUpProposal,
    InvestigationEvidence,
    NetworkAggregate,
    RoundSummary,
)

SUBMIT_TOOL = "submit_proposal"


@dataclass(frozen=True)
class ToolSpec:
    """Name, description and JSON-schema parameters of one tool."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolOutcome:
    """Result of one tool call; ``proposal`` is set only by a valid submission."""

    ok: bool
    output: dict[str, Any]
    proposal: FollowUpProposal | None = None


def _regularized_upper_gamma(shape: float, x: float) -> float:
    """Q(shape, x) by series (x < shape + 1) or continued fraction."""
    if x <= 0:
        return 1.0
    log_prefix = shape * math.log(x) - x - math.lgamma(shape)
    if x < shape + 1:
        term = total = 1.0 / shape
        denominator = shape
        for _ in range(500):
            denominator += 1
            term *= x / denominator
            total += term
            if abs(term) < abs(total) * 1e-15:
                break
        return max(0.0, 1.0 - total * math.exp(log_prefix))
    tiny = 1e-300
    b = x + 1 - shape
    c = 1 / tiny
    d = 1 / b
    h = d
    for index in range(1, 500):
        a = -index * (index - shape)
        b += 2
        d = a * d + b
        d = tiny if abs(d) < tiny else d
        c = b + a / c
        c = tiny if abs(c) < tiny else c
        d = 1 / d
        delta = d * c
        h *= delta
        if abs(delta - 1) < 1e-15:
            break
    return min(1.0, math.exp(log_prefix) * h)


def chi_square_sf(statistic: float, degrees_freedom: int) -> float:
    """Upper-tail probability of a chi-square statistic (Cochran's Q p-value)."""
    if degrees_freedom < 1:
        raise ValueError("degrees of freedom must be positive")
    if statistic < 0 or not math.isfinite(statistic):
        raise ValueError("statistic must be finite and non-negative")
    return _regularized_upper_gamma(degrees_freedom / 2.0, statistic / 2.0)


def _diagnose(label: str, item: NetworkAggregate, threshold: float) -> dict[str, Any]:
    """Heterogeneity diagnostics for one pooled estimate."""
    base: dict[str, Any] = {
        "estimate": label,
        "status": item.status,
        "estimated_sites": item.estimated_sites,
    }
    if item.q is None or item.q_degrees_freedom is None or item.i2 is None:
        return base | {"available": False}
    return base | {
        "available": True,
        "q": item.q,
        "q_degrees_freedom": item.q_degrees_freedom,
        "q_p_value": chi_square_sf(item.q, item.q_degrees_freedom),
        "i2": item.i2,
        "h2": item.q / item.q_degrees_freedom,
        "i2_threshold": threshold,
        "i2_margin_to_threshold": item.i2 - threshold,
        "heterogeneity_flag": item.heterogeneity_flag,
    }


def _round_dump(item: RoundSummary) -> dict[str, Any]:
    return item.model_dump(mode="json", exclude_none=True)


class InvestigationToolbox:
    """Executes investigator tools against one immutable evidence packet."""

    def __init__(self, evidence: InvestigationEvidence) -> None:
        self.evidence = evidence
        self._handlers: dict[str, Callable[[dict[str, Any]], ToolOutcome]] = {
            "get_current_evidence": self._current_evidence,
            "get_history": self._history,
            "heterogeneity_diagnostics": self._diagnostics,
            "list_actions": self._list_actions,
            "check_action": self._check_action,
            SUBMIT_TOOL: self._submit,
        }

    @property
    def names(self) -> tuple[str, ...]:
        """Tool names in catalog order."""
        return tuple(self._handlers)

    def call(self, name: str, arguments: dict[str, Any]) -> ToolOutcome:
        """Run one tool; unknown tools and bad arguments are reported, not raised."""
        handler = self._handlers.get(name)
        if handler is None:
            return ToolOutcome(False, {"error": f"unknown tool {name!r}", "tools": list(self.names)})
        if not isinstance(arguments, dict):
            return ToolOutcome(False, {"error": "arguments must be a JSON object"})
        return handler(arguments)

    def _current_evidence(self, arguments: dict[str, Any]) -> ToolOutcome:
        evidence = self.evidence
        return ToolOutcome(True, {
            "study_id": evidence.study_id,
            "data_mode": evidence.data_mode,
            "phenotype_unit": evidence.phenotype_unit,
            "data_provenance": evidence.data_provenance,
            "parent_analysis_id": evidence.parent_analysis_id,
            "parent_result_hash": evidence.parent_result_hash,
            "next_round_index": evidence.next_round_index,
            "max_association_rounds": evidence.max_association_rounds,
            "budget_remaining": evidence.budget_remaining,
            "heterogeneity_threshold": evidence.heterogeneity_threshold,
            "latest_round": _round_dump(evidence.history[-1]),
        })

    def _history(self, arguments: dict[str, Any]) -> ToolOutcome:
        return ToolOutcome(True, {"rounds": [_round_dump(item) for item in self.evidence.history]})

    def _diagnostics(self, arguments: dict[str, Any]) -> ToolOutcome:
        index = arguments.get("round_index", self.evidence.history[-1].round_index)
        if not isinstance(index, int) or not 0 <= index < len(self.evidence.history):
            return ToolOutcome(False, {"error": "round_index must name a released round"})
        item = self.evidence.history[index]
        threshold = self.evidence.heterogeneity_threshold
        estimates = []
        if item.network is not None:
            estimates.append(_diagnose("network", item.network, threshold))
        estimates.extend(_diagnose(f"stratum:{s.stratum_id}", s, threshold) for s in item.strata)
        output: dict[str, Any] = {"round_index": index, "action": item.action, "estimates": estimates}
        if item.loso is not None:
            output["leave_one_site_out"] = item.loso.model_dump(mode="json", exclude_none=True)
        return ToolOutcome(True, output)

    def _list_actions(self, arguments: dict[str, Any]) -> ToolOutcome:
        specs = catalog_for(self.evidence.data_mode)
        return ToolOutcome(True, {
            "actions": [spec.model_dump(mode="json") for spec in specs],
            "note": "Proposals are recommendations; a human must approve a new contract.",
        })

    def _check_action(self, arguments: dict[str, Any]) -> ToolOutcome:
        action = arguments.get("action")
        if not isinstance(action, str):
            return ToolOutcome(False, {"error": "action must be a string"})
        check = check_preconditions(self.evidence, action)
        return ToolOutcome(True, check.model_dump(mode="json"))

    def _submit(self, arguments: dict[str, Any]) -> ToolOutcome:
        try:
            proposal = FollowUpProposal.model_validate(arguments)
            validate_proposal_binding(self.evidence, proposal)
        except ValidationError as error:
            problems = [
                f"{'.'.join(str(part) for part in item['loc']) or 'proposal'}: {item['msg']}"
                for item in error.errors(include_url=False)
            ]
            return ToolOutcome(False, {"accepted": False, "errors": problems})
        except ValueError as error:
            return ToolOutcome(False, {"accepted": False, "errors": [str(error)]})
        return ToolOutcome(True, {"accepted": True, "action": proposal.action}, proposal)


def _string_array(values: tuple[str, ...]) -> dict[str, Any]:
    return {"type": "array", "items": {"type": "string", "enum": list(values)}}


def tool_specs(evidence: InvestigationEvidence) -> tuple[ToolSpec, ...]:
    """JSON-schema descriptions of every tool, specialised to the data mode."""
    actions = list(catalog_actions(evidence.data_mode))
    empty = {"type": "object", "properties": {}, "additionalProperties": False}
    text = {"type": "string", "minLength": 1, "maxLength": 1000}
    proposal_schema = {
        "type": "object",
        "properties": {
            "study_id": {"type": "string"},
            "parent_analysis_id": {"type": "string"},
            "parent_result_hash": {"type": "string"},
            "round_index": {"type": "integer"},
            "action": {"type": "string", "enum": actions},
            "evidence_fields": _string_array(EVIDENCE_FIELDS),
            "changed_contract_fields": _string_array(CONTRACT_FIELDS),
            "expected_interpretive_value": text,
            "disclosure_cost": text,
            "stop_condition": text,
            "rationale": text,
        },
        "required": [
            "study_id", "parent_analysis_id", "parent_result_hash", "round_index",
            "action", "evidence_fields", "changed_contract_fields",
            "expected_interpretive_value", "disclosure_cost", "stop_condition", "rationale",
        ],
        "additionalProperties": False,
    }
    round_schema = {
        "type": "object",
        "properties": {"round_index": {"type": "integer", "minimum": 0}},
        "additionalProperties": False,
    }
    action_schema = {
        "type": "object",
        "properties": {"action": {"type": "string", "enum": actions}},
        "required": ["action"],
        "additionalProperties": False,
    }
    return (
        ToolSpec(
            "get_current_evidence",
            "Binding fields, query budget and the latest released round "
            "(network aggregates only).",
            empty,
        ),
        ToolSpec(
            "get_history",
            "Every released round so far, primary first; aggregates only.",
            empty,
        ),
        ToolSpec(
            "heterogeneity_diagnostics",
            "Cochran Q, df, Q p-value, I2, H2 and distance from the I2 threshold "
            "for each pooled estimate of a round (default: latest).",
            round_schema,
        ),
        ToolSpec(
            "list_actions",
            "The closed catalog of follow-up actions with what each tests, reveals and costs.",
            empty,
        ),
        ToolSpec(
            "check_action",
            "Deterministic preconditions for one action: budget, not already run, "
            "support, heterogeneity flag.",
            action_schema,
        ),
        ToolSpec(
            SUBMIT_TOOL,
            "Submit exactly one proposal. It is validated strictly; errors are returned "
            "so you can correct and resubmit. Ends the investigation when accepted.",
            proposal_schema,
        ),
    )
