"""Strict collection of validated AgentApp proposal frames."""

from __future__ import annotations

from collections.abc import Iterable

from cohortlens_shared.schemas import FollowUpProposal, canonical_json

AGENT_RESULT_FRAME_PREFIX = "COHORTLENS_AGENT_RESULT_V1 "


class AgentFrameError(ValueError):
    """An agent frame was missing, conflicting, invalid, or incorrectly bound."""


def extract_followup_proposal(
    lines: Iterable[str], *, expected_parent_result_hash: str | None = None
) -> FollowUpProposal:
    """Extract one unique validated investigator proposal from Flower logs."""
    frames: dict[str, FollowUpProposal] = {}
    for line in lines:
        _, marker, payload = line.partition(AGENT_RESULT_FRAME_PREFIX)
        if not marker:
            continue
        try:
            proposal = FollowUpProposal.model_validate_json(payload.strip())
        except ValueError as error:
            raise AgentFrameError(f"invalid investigator result frame: {error}") from error
        frames[canonical_json(proposal)] = proposal
    if not frames:
        raise AgentFrameError("no CohortLens investigator result frame found")
    if len(frames) != 1:
        raise AgentFrameError("conflicting CohortLens investigator result frames found")
    proposal = next(iter(frames.values()))
    if (
        expected_parent_result_hash is not None
        and proposal.parent_result_hash != expected_parent_result_hash
    ):
        raise AgentFrameError("investigator proposal does not match the expected parent result")
    return proposal
