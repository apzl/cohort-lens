"""Two-stage distributed analytics tests using the real handlers."""

from __future__ import annotations

import json

import pytest
from flwr.app import ConfigRecord, Context, Message, RecordDict

from cohortlens_analytics.client_app import handle_association, handle_preflight
from cohortlens_analytics.server_app import (
    Phase1Config,
    phase1_config_from_context,
    run_phase1,
)
from cohortlens_operator.result_frames import extract_result
from cohortlens_shared.schemas import AnalysisContract, Approval
from cohortlens_shared.statistics import phase1_reference


class Phase1Grid:
    def __init__(
        self,
        *,
        drop_association_site: int | None = None,
        duplicate_association_site: int | None = None,
        invalid_standard_error_site: int | None = None,
        changed_alias_site: int | None = None,
    ) -> None:
        self.node_ids = [101, 102, 103]
        self.drop_association_site = drop_association_site
        self.duplicate_association_site = duplicate_association_site
        self.invalid_standard_error_site = invalid_standard_error_site
        self.changed_alias_site = changed_alias_site

    def get_node_ids(self) -> list[int]:
        return self.node_ids

    def send_and_receive(
        self, messages: list[Message], *, timeout: float | None = None
    ) -> list[Message]:
        replies = []
        for message in messages:
            node_id = message.metadata.dst_node_id
            site_number = self.node_ids.index(node_id) + 1
            if (
                message.metadata.message_type == "query.associate"
                and site_number == self.drop_association_site
            ):
                continue
            alias = f"site-{site_number}"
            if message.metadata.message_type == "query.preflight":
                replies.append(handle_preflight(message, alias))
            elif message.metadata.message_type == "query.associate":
                reply = handle_association(message, alias)
                if site_number in {
                    self.invalid_standard_error_site,
                    self.changed_alias_site,
                }:
                    record = reply.content["association_reply"]
                    assert isinstance(record, ConfigRecord)
                    payload = json.loads(record["json"])
                    if site_number == self.invalid_standard_error_site:
                        payload["standard_error"] = -1.0
                    if site_number == self.changed_alias_site:
                        payload["site_alias"] = "site-99"
                    reply = Message(
                        RecordDict(
                            {
                                "association_reply": ConfigRecord(
                                    {"json": json.dumps(payload)}
                                )
                            }
                        ),
                        reply_to=message,
                    )
                replies.append(reply)
                if site_number == self.duplicate_association_site:
                    replies.append(reply)
            else:
                raise AssertionError(message.metadata.message_type)
        return replies


def config(contract: AnalysisContract, approval: Approval) -> Phase1Config:
    return Phase1Config(
        contract=contract,
        approval=approval,
        query_id=contract.analysis_id,
        node_wait_seconds=0,
        reply_timeout_seconds=2,
    )


def test_default_config_cannot_bypass_operator_approval() -> None:
    context = Context(
        run_id=1,
        node_id=0,
        node_config={},
        state=RecordDict(),
        run_config={
            "contract-json": "REQUIRED_OPERATOR_CONTRACT_JSON",
            "approval-json": "REQUIRED_OPERATOR_APPROVAL_JSON",
            "query-id": "primary-001",
            "node-wait-seconds": 1.0,
            "reply-timeout-seconds": 1.0,
        },
    )
    with pytest.raises(ValueError, match="operator-generated"):
        phase1_config_from_context(context)


def test_phase1_matches_offline_reference(
    phase1_contract: AnalysisContract,
    phase1_approval: Approval,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = run_phase1(Phase1Grid(), config(phase1_contract, phase1_approval))
    captured = capsys.readouterr().out.splitlines()
    collected = extract_result(
        captured,
        expected_study_id=phase1_contract.study_id,
        expected_query_id=phase1_contract.analysis_id,
        expected_contract_hash=result.contract_hash,
    )
    sites, aggregate = phase1_reference()
    assert collected == result
    assert result.status == "COMPLETE"
    assert result.estimated_sites == 3
    assert [item.beta for item in result.site_results] == [item.beta for item in sites]
    assert result.beta_meta == aggregate.beta
    assert result.standard_error_meta == aggregate.standard_error
    assert result.i2 == aggregate.i2
    assert result.heterogeneity_flag is True


def test_missing_association_reply_is_partial_not_zero(
    phase1_contract: AnalysisContract,
    phase1_approval: Approval,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = run_phase1(
        Phase1Grid(drop_association_site=3),
        config(phase1_contract, phase1_approval),
    )
    capsys.readouterr()
    assert result.status == "PARTIAL"
    assert result.estimated_sites == 2
    assert any(
        item.site_alias == "site-3" and item.status == "NO_RESPONSE"
        for item in result.site_states
    )


@pytest.mark.parametrize(
    ("grid", "expected_status", "expected_reason"),
    [
        (
            Phase1Grid(invalid_standard_error_site=3),
            "INVALID_REPLY",
            None,
        ),
        (
            Phase1Grid(duplicate_association_site=3),
            "DUPLICATE_REPLY",
            None,
        ),
        (
            Phase1Grid(changed_alias_site=3),
            "INVALID_REPLY",
            "SITE_IDENTITY_MISMATCH",
        ),
    ],
)
def test_invalid_duplicate_and_identity_switched_replies_are_excluded(
    phase1_contract: AnalysisContract,
    phase1_approval: Approval,
    capsys: pytest.CaptureFixture[str],
    grid: Phase1Grid,
    expected_status: str,
    expected_reason: str | None,
) -> None:
    result = run_phase1(grid, config(phase1_contract, phase1_approval))
    capsys.readouterr()
    state = next(item for item in result.site_states if item.site_alias == "site-3")
    assert state.status == expected_status
    assert state.reason_code == expected_reason
    assert result.status == "PARTIAL"
    assert result.estimated_sites == 2
