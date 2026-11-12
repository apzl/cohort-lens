"""Strict collection of validated AgentApp frames from run logs."""

from __future__ import annotations

from collections.abc import Iterable

from cohortlens_shared.schemas import (
    AgentArtifact,
    FollowUpProposal,
    InvestigatorRecord,
    ReporterOutput,
    canonical_json,
    parse_agent_artifact_json,
)

AGENT_RESULT_FRAME_PREFIX = "COHORTLENS_AGENT_RESULT_V2 "


class AgentFrameError(ValueError):
    """An agent frame was missing, conflicting, invalid, or incorrectly bound."""


def extract_agent_artifact(lines: Iterable[str]) -> AgentArtifact:
    """Extract one unique validated agent frame, accepting identical replays."""
    frames: dict[str, AgentArtifact] = {}
    for line in lines:
        _, marker, payload = line.partition(AGENT_RESULT_FRAME_PREFIX)
        if not marker:
            continue
        try:
            artifact = parse_agent_artifact_json(payload.strip())
        except ValueError as error:
            raise AgentFrameError(f"invalid agent result frame: {error}") from error
        frames[canonical_json(artifact)] = artifact
    if not frames:
        raise AgentFrameError("no CohortLens agent result frame found")
    if len(frames) != 1:
        raise AgentFrameError("conflicting CohortLens agent result frames found")
    return next(iter(frames.values()))


def extract_investigator_record(
    lines: Iterable[str], *, expected_parent_result_hash: str | None = None
) -> InvestigatorRecord:
    """Extract one investigator record (proposal or recorded failure) and check its binding."""
    artifact = extract_agent_artifact(lines)
    if not isinstance(artifact, InvestigatorRecord):
        raise AgentFrameError("agent frame is not an investigator record")
    if (
        expected_parent_result_hash is not None
        and artifact.parent_result_hash != expected_parent_result_hash
    ):
        raise AgentFrameError("investigator record does not match the expected parent result")
    return artifact


def extract_followup_proposal(
    lines: Iterable[str], *, expected_parent_result_hash: str | None = None
) -> FollowUpProposal:
    """Extract the accepted proposal of one investigator record."""
    record = extract_investigator_record(
        lines, expected_parent_result_hash=expected_parent_result_hash
    )
    if record.proposal is None:
        raise AgentFrameError(f"investigator produced no proposal: {record.failure_reason}")
    return record.proposal


def extract_reporter_output(lines: Iterable[str]) -> ReporterOutput:
    """Extract one reporter output frame."""
    artifact = extract_agent_artifact(lines)
    if not isinstance(artifact, ReporterOutput):
        raise AgentFrameError("agent frame is not a reporter output")
    return artifact
