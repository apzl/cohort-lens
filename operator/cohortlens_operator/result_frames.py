"""Validated collection of machine-readable result frames from Flower logs."""

from __future__ import annotations

from collections.abc import Iterable

from cohortlens_shared.schemas import (
    ReleasedResult,
    canonical_json,
    parse_released_result_json,
)

RESULT_FRAME_PREFIX = "COHORTLENS_RESULT_V1 "


class ResultFrameError(ValueError):
    """A result frame was missing, conflicting, or did not match expectations."""


def format_result_frame(result: ReleasedResult) -> str:
    """Format one canonical, single-line result frame."""
    return f"{RESULT_FRAME_PREFIX}{canonical_json(result)}"


def extract_result(
    lines: Iterable[str],
    *,
    expected_study_id: str | None = None,
    expected_query_id: str | None = None,
    expected_contract_hash: str | None = None,
) -> ReleasedResult:
    """Extract one unique validated result, accepting identical replayed frames."""
    frames: dict[str, ReleasedResult] = {}
    for line in lines:
        _, marker, payload = line.partition(RESULT_FRAME_PREFIX)
        if not marker:
            continue
        try:
            result = parse_released_result_json(payload.strip())
        except ValueError as error:
            raise ResultFrameError(f"invalid result frame: {error}") from error
        frames[canonical_json(result)] = result

    if not frames:
        raise ResultFrameError("no CohortLens result frame found")
    if len(frames) != 1:
        raise ResultFrameError("conflicting CohortLens result frames found")
    result = next(iter(frames.values()))
    expected = {
        "study_id": expected_study_id,
        "query_id": expected_query_id,
        "contract_hash": expected_contract_hash,
    }
    for field, value in expected.items():
        if value is not None and getattr(result, field) != value:
            raise ResultFrameError(
                f"result {field}={getattr(result, field)!r} does not match {value!r}"
            )
    return result
