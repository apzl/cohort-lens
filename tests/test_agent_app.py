"""The AgentApp investigator entry point with a fake session and a fake model client."""

import json

from flwr.app import Context, RecordDict

from cohortlens_agent.agent_app import (
    MODEL,
    PROPOSAL_EVENT_TYPE,
    TRACE_EVENT_TYPE,
    loop_limits,
    run_investigator,
    tool_protocol,
)
from cohortlens_shared.evidence import investigation_evidence
from support import SITE_LEVEL_MARKERS, primary_round, proposal_for


class _Events:
    def __init__(self) -> None:
        self.emitted: list[dict] = []

    def emit(self, event: dict) -> None:
        assert event.get("type")
        self.emitted.append(event)


class _Session:
    def __init__(self) -> None:
        self.events = _Events()


class _Stream:
    """Fake Responses client: one json-protocol action per request."""

    def __init__(self, actions: list[dict]) -> None:
        self.actions = actions
        self.requests: list[dict] = []
        self.responses = self

    def create(self, **kwargs):
        self.requests.append(kwargs)
        text = json.dumps(self.actions.pop(0))
        return iter([{"type": "response.output_text.delta", "delta": text}, {"type": "response.completed"}])


def _context(**config) -> Context:
    return Context(run_id=1, node_id=0, node_config={}, state=RecordDict(), run_config=config)


def test_investigator_emits_structured_events_for_every_step() -> None:
    evidence = investigation_evidence([primary_round()])
    proposal = proposal_for(evidence, "LEAVE_ONE_SITE_OUT").model_dump(mode="json")
    client = _Stream([
        {"tool": "get_current_evidence", "arguments": {}},
        {"tool": "submit_proposal", "arguments": proposal},
    ])
    session = _Session()
    record = run_investigator(session, _context(**{"agent.max-steps": 4}), client, evidence)
    assert record.status == "PROPOSED" and record.policy_label == MODEL
    types = [event["type"] for event in session.events.emitted]
    assert types.count(TRACE_EVENT_TYPE) == 4 and types[-1] == PROPOSAL_EVENT_TYPE
    assert types.count("response.output_text.delta") == 2
    for request in client.requests:
        assert request["model"] == MODEL and request["stream"] is True
        rendered = json.dumps(request)
        assert not any(marker in rendered for marker in SITE_LEVEL_MARKERS)


def test_run_config_controls_protocol_and_caps() -> None:
    context = _context(**{"agent.tool-protocol": "native", "agent.protocol-fallback": False, "agent.max-tool-calls": 5})
    assert tool_protocol(context) == ("native", False)
    assert loop_limits(context).max_tool_calls == 5 and loop_limits(context).max_steps == 12
