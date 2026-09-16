"""Result collection remains strict across saved logs and retries."""

import pytest

from cohortlens_operator.result_frames import (
    ResultFrameError,
    extract_result,
    format_result_frame,
)
from cohortlens_shared.schemas import ResultEnvelope


def test_identical_replayed_frame_is_idempotent(complete_result: ResultEnvelope) -> None:
    frame = format_result_frame(complete_result)
    result = extract_result([frame, f"timestamp INFO {frame}"])
    assert result == complete_result


def test_conflicting_frames_fail(complete_result: ResultEnvelope) -> None:
    conflicting = complete_result.model_copy(
        update={
            "expected_sites": 4,
            "responding_sites": 3,
            "status": "PARTIAL",
        }
    )
    with pytest.raises(ResultFrameError, match="conflicting"):
        extract_result(
            [format_result_frame(complete_result), format_result_frame(conflicting)]
        )


def test_expected_identifiers_are_enforced(complete_result: ResultEnvelope) -> None:
    with pytest.raises(ResultFrameError, match="query_id"):
        extract_result(
            [format_result_frame(complete_result)], expected_query_id="different-query"
        )


def test_missing_frame_fails() -> None:
    with pytest.raises(ResultFrameError, match="no CohortLens"):
        extract_result(["ordinary Flower log line"])

