"""Tests for strict, versioned contracts."""

import json

import pytest
from pydantic import ValidationError

from cohortlens_shared.schemas import PreflightRequest, ResultEnvelope, canonical_json


def test_contract_rejects_unknown_fields(preflight_request: PreflightRequest) -> None:
    payload = preflight_request.model_dump()
    payload["patient_id"] = "must-not-pass"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        PreflightRequest.model_validate(payload)


def test_canonical_json_is_stable(preflight_request: PreflightRequest) -> None:
    first = canonical_json(preflight_request)
    second = canonical_json(
        PreflightRequest.model_validate(json.loads(first))
    )
    assert first == second
    assert " " not in first


def test_result_counts_and_status_must_agree() -> None:
    with pytest.raises(ValidationError, match="status does not match"):
        ResultEnvelope(
            study_id="study",
            query_id="query",
            contract_hash="sha256:" + "1" * 64,
            expected_sites=3,
            responding_sites=2,
            ready_sites=["site-1", "site-2"],
            status="COMPLETE",
        )


def test_ready_sites_must_be_sorted_and_unique() -> None:
    with pytest.raises(ValidationError, match="sorted and unique"):
        ResultEnvelope(
            study_id="study",
            query_id="query",
            contract_hash="sha256:" + "1" * 64,
            expected_sites=3,
            responding_sites=2,
            ready_sites=["site-2", "site-1"],
            status="PARTIAL",
        )

