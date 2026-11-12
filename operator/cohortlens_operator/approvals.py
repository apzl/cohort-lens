"""Human approval of contracts: interactive by default, scripted only on request."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Protocol

from cohortlens_shared.schemas import (
    AnalysisContract,
    Approval,
    FollowUpProposal,
    canonical_json,
    contract_digest,
    data_mode_for_contract,
)

ApprovalMode = Literal["INTERACTIVE", "SCRIPTED_NON_INTERACTIVE"]


def make_approval(
    contract: AnalysisContract,
    approved_by: str,
    *,
    valid_hours: int = 24,
    now: datetime | None = None,
) -> Approval:
    """Create an expiring approval bound to the exact contract digest."""
    granted = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    scope = (
        "PUBLIC_GDSC_EMULATED_FEDERATION"
        if data_mode_for_contract(contract) == "PUBLIC_GDSC"
        else "SYNTHETIC_DEMO"
    )
    return Approval(
        approval_id=f"{contract.analysis_id}-approval",
        study_id=contract.study_id,
        analysis_id=contract.analysis_id,
        contract_hash=contract_digest(contract),
        approved_by=approved_by,
        scope=scope,
        approved_at=granted,
        expires_at=granted + timedelta(hours=valid_hours),
    )


@dataclass(frozen=True)
class ApprovalRequest:
    """Everything a human sees before authorising a round."""

    contract: AnalysisContract
    proposal: FollowUpProposal | None = None

    @property
    def digest(self) -> str:
        """The contract digest the approval will bind to."""
        return contract_digest(self.contract)


class Approver(Protocol):
    """Decides whether a contract may run."""

    name: str
    mode: ApprovalMode

    def review(self, request: ApprovalRequest) -> bool:
        """Return True to authorise the contract in ``request``."""
        ...


def describe_request(request: ApprovalRequest) -> str:
    """Human-readable summary: proposal, rationale and exact contract digest."""
    contract = request.contract
    lines = [
        "=" * 72,
        f"Round {contract.round_index} of at most {contract.max_association_rounds - 1} follow-ups"
        if contract.round_index
        else "Primary analysis",
        f"Study:     {contract.study_id}",
        f"Analysis:  {contract.analysis_id}",
        f"Template:  {contract.template_id}",
        f"Covariates:{' ' + ', '.join(contract.covariate_ids)}",
        f"Strata:    {', '.join(contract.strata) or '(none)'}",
    ]
    if request.proposal is not None:
        proposal = request.proposal
        lines += [
            f"Proposed action: {proposal.action}",
            f"Rationale:       {proposal.rationale}",
            f"Expected value:  {proposal.expected_interpretive_value}",
            f"Disclosure cost: {proposal.disclosure_cost}",
        ]
    lines += [f"Contract digest: {request.digest}", "Contract JSON:", canonical_json(contract), "=" * 72]
    return "\n".join(lines)


@dataclass
class InteractiveApprover:
    """Asks a named human to retype the digest prefix before authorising."""

    name: str
    input_fn: Callable[[str], str] = input
    output_fn: Callable[[str], None] = print
    mode: ApprovalMode = "INTERACTIVE"

    def review(self, request: ApprovalRequest) -> bool:
        """Show the request and require the first 12 hex digits of the digest."""
        self.output_fn(describe_request(request))
        prefix = request.digest.removeprefix("sha256:")[:12]
        answer = self.input_fn(
            f"{self.name}: type the first 12 hex characters of the digest to approve, "
            "or press Enter to decline: "
        )
        return answer.strip().lower() == prefix


@dataclass
class ScriptedApprover:
    """Approves everything; for scripted demos and tests, and recorded as such."""

    name: str
    mode: ApprovalMode = "SCRIPTED_NON_INTERACTIVE"

    def review(self, request: ApprovalRequest) -> bool:
        """Always authorise; the ledger records that no human was asked."""
        return True
