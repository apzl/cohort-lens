"""Study orchestrator: primary -> gate -> investigator -> human approval -> follow-up ...

The loop is backend-agnostic. Every gate decision, investigator step, proposal,
approval, submitted run and released aggregate is appended to the hash-chained
ledger. Full released results (which include site-level summaries the
coordinator already sees) are written only to the optional output directory;
the ledger itself carries network-level aggregates and hashes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cohortlens_shared.catalog import compile_followup
from cohortlens_shared.evidence import investigation_evidence, round_summary
from cohortlens_shared.schemas import (
    AnalysisContract,
    Approval,
    AssociationResult,
    FollowUpProposal,
    InvestigationDecision,
    InvestigatorRecord,
    ReleasedResult,
    TraceEntry,
    canonical_json,
    contract_digest,
    continuation_decision,
    investigation_decision_from_result,
    proposal_digest,
    result_digest,
    sha256_digest,
)

from .approvals import ApprovalRequest, Approver, make_approval
from .backends import StudyBackend
from .ledger import Ledger

TRACE_ENTRY_TYPES = {
    "tool_call": "AGENT_TOOL_CALL",
    "tool_result": "AGENT_TOOL_RESULT",
    "invalid_turn": "AGENT_INVALID_TURN",
    "protocol_fallback": "AGENT_PROTOCOL_FALLBACK",
    "model_text": "AGENT_MODEL_TEXT",
}


@dataclass
class StudyPlan:
    """Inputs of one study run; ``description`` must hold no paths or rows."""

    primary_contract: AnalysisContract
    policy_kind: str
    policy_label: str
    valid_hours: int = 24
    description: dict[str, Any] = field(default_factory=dict)


@dataclass
class StudyOutcome:
    """What happened, in order."""

    rounds: list[tuple[ReleasedResult, AnalysisContract]] = field(default_factory=list)
    decisions: list[InvestigationDecision] = field(default_factory=list)
    records: list[InvestigatorRecord] = field(default_factory=list)
    finish_reason: str = ""


class StudyOrchestrator:
    """Drives one hash-chained investigation and records it in a ledger."""

    def __init__(
        self,
        plan: StudyPlan,
        backend: StudyBackend,
        approver: Approver,
        ledger: Ledger,
        *,
        output_dir: Path | None = None,
    ) -> None:
        self.plan = plan
        self.backend = backend
        self.approver = approver
        self.ledger = ledger
        self.output_dir = output_dir
        self.outcome = StudyOutcome()

    def _save(self, name: str, text: str) -> None:
        if self.output_dir is not None:
            self.output_dir.mkdir(parents=True, exist_ok=True)
            (self.output_dir / name).write_text(text + "\n", encoding="utf-8")

    def _agent_labels(self) -> dict[str, str]:
        return {"policy_kind": self.plan.policy_kind, "policy_label": self.plan.policy_label}

    def _approve(self, contract: AnalysisContract, proposal: FollowUpProposal | None) -> Approval | None:
        """Ask the approver; record either the approval or the refusal."""
        request = ApprovalRequest(contract=contract, proposal=proposal)
        base = {
            "round_index": contract.round_index,
            "analysis_id": contract.analysis_id,
            "contract_hash": request.digest,
            "proposal_hash": None if proposal is None else proposal_digest(proposal),
            "approver": self.approver.name,
            "approval_mode": self.approver.mode,
        }
        if not self.approver.review(request):
            self.ledger.append("APPROVAL_DECLINED", base)
            return None
        approval = make_approval(contract, self.approver.name, valid_hours=self.plan.valid_hours)
        self.ledger.append("CONTRACT_APPROVED", base | {
            "approval_id": approval.approval_id,
            "approved_at": approval.approved_at.isoformat(),
            "expires_at": approval.expires_at.isoformat(),
            "contract": contract.model_dump(mode="json", exclude_none=True),
        })
        self._save(f"round-{contract.round_index}-contract.json", canonical_json(contract))
        self._save(f"round-{contract.round_index}-approval.json", canonical_json(approval))
        return approval

    def _run_round(
        self, contract: AnalysisContract, approval: Approval, proposal: FollowUpProposal | None
    ) -> ReleasedResult:
        self.ledger.append("RUN_SUBMITTED", {
            "round_index": contract.round_index,
            "analysis_id": contract.analysis_id,
            "contract_hash": contract_digest(contract),
            "template_id": contract.template_id,
            "backend": self.backend.name,
        })
        result, meta = self.backend.run_round(contract, approval, proposal)
        self.outcome.rounds.append((result, contract))
        summary = round_summary(result, contract)
        self.ledger.append("RESULT_RELEASED", {
            "round_index": contract.round_index,
            "result_type": result.result_type,
            "result_hash": result_digest(result),
            "run_id": meta.get("run_id"),
            "aggregate_summary": summary.model_dump(mode="json", exclude_none=True),
        })
        self._save(f"round-{contract.round_index}-result.json", canonical_json(result))
        return result

    def _gate(self, result: ReleasedResult) -> InvestigationDecision:
        if len(self.outcome.rounds) == 1:
            if not isinstance(result, AssociationResult):
                raise ValueError("the primary round must release an association result")
            decision = investigation_decision_from_result(result)
        else:
            decision = continuation_decision(investigation_evidence(self.outcome.rounds))
        self.outcome.decisions.append(decision)
        self.ledger.append("GATE_DECISION", decision.model_dump(mode="json"))
        return decision

    def _trace_to_ledger(self, entry: TraceEntry) -> None:
        payload = entry.model_dump(mode="json", exclude_none=True) | self._agent_labels()
        self.ledger.append(TRACE_ENTRY_TYPES[entry.kind], payload)

    def _investigate(self) -> InvestigatorRecord:
        evidence = investigation_evidence(self.outcome.rounds)
        evidence_hash = sha256_digest(canonical_json(evidence))
        self.ledger.append("AGENT_INVOKED", self._agent_labels() | {
            "round_index": evidence.next_round_index,
            "evidence_hash": evidence_hash,
            "parent_result_hash": evidence.parent_result_hash,
            "budget_remaining": evidence.budget_remaining,
            "backend": self.backend.name,
        })
        record = self.backend.run_investigator(evidence, self._trace_to_ledger)
        if record.evidence_hash != evidence_hash or record.parent_result_hash != evidence.parent_result_hash:
            raise RuntimeError("investigator record is not bound to the supplied evidence")
        self.outcome.records.append(record)
        self._save(f"round-{evidence.next_round_index}-investigator-record.json", canonical_json(record))
        counters = {
            "protocol_requested": record.protocol_requested,
            "protocol_used": record.protocol_used,
            "steps": record.steps,
            "tool_calls": record.tool_calls,
            "invalid_turns": record.invalid_turns,
        }
        if record.proposal is None:
            failure = {"failure_reason": record.failure_reason}
            self.ledger.append("AGENT_FAILED", self._agent_labels() | counters | failure)
        else:
            self.ledger.append("AGENT_PROPOSAL", self._agent_labels() | counters | {
                "proposal_hash": proposal_digest(record.proposal),
                "proposal": record.proposal.model_dump(mode="json"),
            })
        return record

    def _finish(self, reason: str) -> StudyOutcome:
        self.outcome.finish_reason = reason
        self.ledger.append("STUDY_FINISHED", {
            "reason": reason,
            "rounds_run": len(self.outcome.rounds),
            "final_result_hash": (
                result_digest(self.outcome.rounds[-1][0]) if self.outcome.rounds else None
            ),
        })
        return self.outcome

    def _next_contract(self) -> tuple[AnalysisContract, FollowUpProposal] | str:
        """Investigate and compile the next contract, or return a finish reason."""
        record = self._investigate()
        if record.proposal is None:
            return "INVESTIGATOR_FAILED"
        if record.proposal.action == "STOP_NO_FOLLOWUP":
            return "INVESTIGATOR_PROPOSED_STOP"
        parent_contract = self.outcome.rounds[-1][1]
        evidence = investigation_evidence(self.outcome.rounds)
        return compile_followup(evidence, parent_contract, record.proposal), record.proposal

    def run(self) -> StudyOutcome:
        """Run the study until a gate, the investigator or the human ends it."""
        self.ledger.append("STUDY_STARTED", self.plan.description | self._agent_labels() | {
            "primary_contract_hash": contract_digest(self.plan.primary_contract),
            "max_association_rounds": self.plan.primary_contract.max_association_rounds,
            "backend": self.backend.name,
            "approver_mode": self.approver.mode,
        })
        contract, proposal = self.plan.primary_contract, None
        approval = self._approve(contract, None)
        if approval is None:
            return self._finish("PRIMARY_NOT_APPROVED")
        while True:
            result = self._run_round(contract, approval, proposal)
            decision = self._gate(result)
            if decision.decision != "INVESTIGATE":
                return self._finish(decision.decision)
            step = self._next_contract()
            if isinstance(step, str):
                return self._finish(step)
            contract, proposal = step
            approval = self._approve(contract, proposal)
            if approval is None:
                return self._finish("FOLLOWUP_NOT_APPROVED")
