"""Bounded tool loop under both protocols with fake model clients."""

import json

import pytest

from cohortlens_operator.scripted_policy import ScriptedPolicyTransport
from cohortlens_shared.evidence import investigation_evidence
from cohortlens_shared.investigator import (
    InvestigatorLoop,
    LoopLimits,
    ModelRequestRejected,
    ModelTurn,
    OpenAIResponsesTransport,
    build_instructions,
    extract_json_action,
)
from support import (
    RecordingTransport,
    assert_aggregate_only,
    followup,
    json_turn,
    native_turn,
    primary_round,
    proposal_for,
)


@pytest.fixture(scope="module")
def evidence():
    return investigation_evidence([primary_round()])


def _run(evidence, transport, protocol="json", **limits):
    loop = InvestigatorLoop(evidence, transport, model="fake", protocol=protocol, limits=LoopLimits(**limits))
    return loop.run(policy_kind="MODEL", policy_label="fake-model")


def _submit(evidence, action="LEAVE_ONE_SITE_OUT", **changes):
    return proposal_for(evidence, action, **changes).model_dump(mode="json")


def test_json_protocol_reaches_a_validated_proposal(evidence) -> None:
    transport = RecordingTransport([
        json_turn("get_current_evidence"),
        json_turn("heterogeneity_diagnostics"),
        json_turn("check_action", {"action": "LEAVE_ONE_SITE_OUT"}),
        json_turn("submit_proposal", _submit(evidence)),
    ])
    record = _run(evidence, transport)
    assert record.status == "PROPOSED" and record.proposal.action == "LEAVE_ONE_SITE_OUT"
    assert (record.steps, record.tool_calls, record.invalid_turns) == (4, 4, 0)
    assert [entry.kind for entry in record.trace].count("tool_result") == 4
    assert "tools" not in transport.requests[0]
    assert_aggregate_only(transport.requests)


def test_native_protocol_uses_function_calls_and_rebuilds_input(evidence) -> None:
    transport = RecordingTransport([
        native_turn("list_actions", call_id="a"),
        native_turn("submit_proposal", _submit(evidence, "STOP_NO_FOLLOWUP"), call_id="b"),
    ])
    record = _run(evidence, transport, protocol="native")
    assert record.status == "PROPOSED" and record.protocol_used == "native"
    first, second = transport.requests
    assert first["tools"] and first["tool_choice"] == "auto"
    assert set(first) <= {"model", "input", "instructions", "tools", "tool_choice", "max_output_tokens"}
    kinds = [item.get("type", item.get("role")) for item in second["input"]]
    assert kinds == ["user", "function_call", "function_call_output"]
    assert "previous_response_id" not in second
    assert_aggregate_only(transport.requests)


def test_malformed_turns_are_counted_and_capped(evidence) -> None:
    transport = RecordingTransport([
        ModelTurn(text="Let me think about this."),
        ModelTurn(text='{"tool": "get_history", "arguments": {}} and more'),
        ModelTurn(text='{"tool": "get_history"}'),
    ])
    record = _run(evidence, transport, max_invalid_turns=3)
    assert record.status == "FAILED" and "invalid-turn cap" in record.failure_reason
    assert record.invalid_turns == 3 and record.tool_calls == 0
    assert transport.requests[1]["input"][-1]["content"].startswith("INVALID_TURN ")


def test_step_and_tool_caps_bound_a_looping_model(evidence) -> None:
    looping = RecordingTransport([json_turn("get_history") for _ in range(50)])
    record = _run(evidence, looping, max_steps=5, max_tool_calls=100)
    assert record.status == "FAILED" and "step cap" in record.failure_reason and record.steps == 5
    looping = RecordingTransport([json_turn("get_history") for _ in range(50)])
    record = _run(evidence, looping, max_steps=50, max_tool_calls=3)
    assert record.status == "FAILED" and "tool-call cap" in record.failure_reason and record.tool_calls == 3


def test_validation_errors_are_returned_and_repaired_within_caps(evidence) -> None:
    broken = _submit(evidence) | {"parent_result_hash": "sha256:" + "0" * 64}
    transport = RecordingTransport([
        json_turn("submit_proposal", broken),
        json_turn("submit_proposal", _submit(evidence)),
    ])
    record = _run(evidence, transport)
    assert record.status == "PROPOSED" and record.tool_calls == 2 and record.invalid_turns == 0
    feedback = transport.requests[1]["input"][-1]["content"]
    assert feedback.startswith("TOOL_RESULT ")
    assert "parent_result_hash" in json.loads(feedback.removeprefix("TOOL_RESULT "))["output"]["errors"][0]


def test_unknown_tool_is_an_invalid_turn_with_feedback(evidence) -> None:
    transport = RecordingTransport([json_turn("run_sql", {"q": "select *"}), json_turn("submit_proposal", _submit(evidence))])
    record = _run(evidence, transport)
    assert record.status == "PROPOSED" and record.invalid_turns == 1


def test_native_rejection_falls_back_to_json(evidence) -> None:
    transport = RecordingTransport([
        ModelRequestRejected("400 unsupported parameter: tools"),
        json_turn("submit_proposal", _submit(evidence)),
    ])
    record = _run(evidence, transport, protocol="native")
    assert record.protocol_requested == "native" and record.protocol_used == "json"
    assert record.trace[0].kind == "protocol_fallback"
    assert "tools" in transport.requests[0] and "tools" not in transport.requests[1]
    no_fallback = InvestigatorLoop(evidence, RecordingTransport([ModelRequestRejected("400")]), model="m", protocol="native", allow_fallback=False)
    assert no_fallback.run(policy_kind="MODEL", policy_label="m").status == "FAILED"


def test_instructions_describe_tools_but_never_supply_an_answer(evidence) -> None:
    for protocol in ("json", "native"):
        text = build_instructions(evidence, protocol)
        assert evidence.parent_result_hash not in text
        assert '"rationale":' not in text.replace('"rationale":{', "")
        assert "submit_proposal" in text or protocol == "native"


def test_extract_json_action_is_strict() -> None:
    assert extract_json_action('```json\n{"tool": "get_history", "arguments": {}}\n```') == ("get_history", {})
    for bad in ('{"tool": 1, "arguments": {}}', "[]", '{"tool": "x", "arguments": {}, "extra": 1}', "{"):
        with pytest.raises(ValueError):
            extract_json_action(bad)


@pytest.mark.parametrize("protocol", ["json", "native"])
def test_scripted_policy_drives_both_protocols_on_aggregates_only(protocol) -> None:
    rounds = [primary_round()]
    result, contract, _ = followup(rounds, "LEAVE_ONE_SITE_OUT")
    evidence = investigation_evidence(rounds + [(result, contract)])
    transport = ScriptedPolicyTransport()
    record = InvestigatorLoop(evidence, transport, model="scripted", protocol=protocol).run(
        policy_kind="SCRIPTED_POLICY", policy_label="cohortlens-scripted-policy-v1"
    )
    assert record.status == "PROPOSED" and record.proposal.action == "STRATIFY_SYNTHETIC_CONTEXT"
    checked = [e.arguments["action"] for e in record.trace if e.kind == "tool_call" and e.tool == "check_action"]
    assert checked == ["LEAVE_ONE_SITE_OUT", "STRATIFY_SYNTHETIC_CONTEXT"]
    assert_aggregate_only(transport.requests)


class _FakeEvent:
    def __init__(self, payload):
        self.payload = payload

    def to_dict(self):
        return self.payload


class _FakeResponses:
    def __init__(self, events=None, error=None):
        self.events, self.error, self.calls = events or [], error, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return iter(_FakeEvent(event) for event in self.events)


class _FakeClient:
    def __init__(self, **kwargs):
        self.responses = _FakeResponses(**kwargs)


class _BadRequest(Exception):
    status_code = 400


def test_openai_transport_assembles_text_and_function_calls() -> None:
    events = [
        {"type": "response.output_text.delta", "delta": "thinking "},
        {"type": "response.output_item.done", "item": {"type": "function_call", "call_id": "x1", "name": "get_history", "arguments": "{}"}},
        {"type": "response.completed"},
    ]
    seen = []
    client = _FakeClient(events=events)
    turn = OpenAIResponsesTransport(client, on_event=seen.append).create({"model": "m", "input": []})
    assert turn.text == "thinking " and turn.function_calls[0].name == "get_history"
    assert client.responses.calls[0]["stream"] is True and len(seen) == 3
    with pytest.raises(ModelRequestRejected):
        OpenAIResponsesTransport(_FakeClient(error=_BadRequest("bad tools"))).create({"model": "m"})
    with pytest.raises(RuntimeError, match="failed"):
        OpenAIResponsesTransport(_FakeClient(events=[{"type": "response.failed"}])).create({"model": "m"})
