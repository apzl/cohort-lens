"""Client/server protocol tests without a network runtime."""

from __future__ import annotations

import pytest
from flwr.app import ConfigRecord, Message, RecordDict

from cohortlens_analytics.client_app import handle_preflight
from cohortlens_analytics.server_app import Phase0Config, run_preflight
from cohortlens_operator.result_frames import extract_result
from cohortlens_shared.schemas import PreflightRequest, canonical_json


class FakeGrid:
    """Minimal Grid double that routes requests through the real client handler."""

    def __init__(self, node_ids: list[int], *, drop_last: bool = False) -> None:
        self.node_ids = node_ids
        self.drop_last = drop_last
        self.timeout_seen: float | None = None

    def get_node_ids(self) -> list[int]:
        return self.node_ids

    def send_and_receive(
        self, messages: list[Message], *, timeout: float | None = None
    ) -> list[Message]:
        self.timeout_seen = timeout
        routed = messages[:-1] if self.drop_last else messages
        return [
            handle_preflight(message, f"site-{index + 1}")
            for index, message in enumerate(routed)
        ]


def config() -> Phase0Config:
    return Phase0Config(
        study_id="phase0-demo",
        query_id="preflight-001",
        contract_hash=(
            "sha256:768b951dbd3b1d51aba17b842d29aaa568b155a24944d42c196b4aee91938999"
        ),
        expected_sites=3,
        node_wait_seconds=0.0,
        reply_timeout_seconds=2.0,
    )


def test_client_rejects_unexpected_request_fields(
    preflight_request: PreflightRequest,
) -> None:
    payload = preflight_request.model_dump()
    payload["patient_rows"] = ["PRIVATE_SENTINEL"]
    message = Message(
        content=RecordDict(
            {"preflight_request": ConfigRecord({"json": canonical_json(preflight_request)[:-1] + ',"patient_rows":["PRIVATE_SENTINEL"]}'})}
        ),
        dst_node_id=1,
        message_type="query.preflight",
    )
    with pytest.raises(ValueError):
        handle_preflight(message, "site-1")


def test_three_site_round_trip_emits_collectable_frame(capsys: pytest.CaptureFixture[str]) -> None:
    grid = FakeGrid([103, 101, 102])
    result = run_preflight(grid, config())
    captured = capsys.readouterr().out.splitlines()
    collected = extract_result(
        captured,
        expected_study_id="phase0-demo",
        expected_query_id="preflight-001",
        expected_contract_hash=config().contract_hash,
    )
    assert result == collected
    assert result.status == "COMPLETE"
    assert result.ready_sites == ["site-1", "site-2", "site-3"]
    assert grid.timeout_seen == 2.0


def test_missing_reply_is_partial_not_zero(capsys: pytest.CaptureFixture[str]) -> None:
    result = run_preflight(FakeGrid([1, 2, 3], drop_last=True), config())
    capsys.readouterr()
    assert result.status == "PARTIAL"
    assert result.responding_sites == 2
    assert len(result.ready_sites) == 2


def test_node_wait_timeout_is_bounded() -> None:
    with pytest.raises(TimeoutError, match="expected 3 nodes"):
        run_preflight(FakeGrid([]), config())

