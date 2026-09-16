"""Shared Phase 0 fixtures that later phases can extend."""

from __future__ import annotations

import pytest

from cohortlens_shared.schemas import PreflightRequest, ResultEnvelope
from cohortlens_shared.schemas import (
    AnalysisContract,
    Approval,
    default_phase1_approval,
    default_phase1_contract,
)

CONTRACT_HASH = (
    "sha256:768b951dbd3b1d51aba17b842d29aaa568b155a24944d42c196b4aee91938999"
)


@pytest.fixture
def preflight_request() -> PreflightRequest:
    return PreflightRequest(
        study_id="phase0-demo",
        query_id="preflight-001",
        contract_hash=CONTRACT_HASH,
    )


@pytest.fixture
def complete_result() -> ResultEnvelope:
    return ResultEnvelope(
        study_id="phase0-demo",
        query_id="preflight-001",
        contract_hash=CONTRACT_HASH,
        expected_sites=3,
        responding_sites=3,
        ready_sites=["site-1", "site-2", "site-3"],
        status="COMPLETE",
    )


@pytest.fixture
def phase1_contract() -> AnalysisContract:
    return default_phase1_contract()


@pytest.fixture
def phase1_approval(phase1_contract: AnalysisContract) -> Approval:
    return default_phase1_approval(phase1_contract)

