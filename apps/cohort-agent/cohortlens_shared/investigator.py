"""Bounded plan-act-observe investigator loop with two tool protocols.

The loop owns tool execution and every cap. A ``ModelTransport`` performs one
model request per step and returns text and/or function calls; it never sees
anything except the instructions, the task message and tool outputs, all of
which are derived from aggregate-only ``InvestigationEvidence``.

Protocols:
- ``native``: Responses ``tools`` + ``function_call`` items.
- ``json``: provider-agnostic; the model replies with one strict JSON object
  ``{"tool": "...", "arguments": {...}}`` per turn, which we parse and validate.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from .schemas import (
    FollowUpProposal,
    InvestigationEvidence,
    InvestigatorRecord,
    StrictModel,
    TraceEntry,
    canonical_json,
    sha256_digest,
)
from .tools import SUBMIT_TOOL, InvestigationToolbox, ToolSpec, tool_specs

Protocol_ = Literal["native", "json"]
TOOL_RESULT_PREFIX = "TOOL_RESULT "
INVALID_TURN_PREFIX = "INVALID_TURN "
MAX_TRACE_TEXT = 4000


class LoopLimits(StrictModel):
    """Hard caps on one investigator run."""

    max_steps: int = 12
    max_tool_calls: int = 16
    max_invalid_turns: int = 3
    max_output_tokens: int = 2000


@dataclass(frozen=True)
class FunctionCall:
    """One native function call returned by the model."""

    call_id: str
    name: str
    arguments: str


@dataclass(frozen=True)
class ModelTurn:
    """Text and function calls produced by one model request."""

    text: str = ""
    function_calls: tuple[FunctionCall, ...] = ()


class ModelRequestRejected(RuntimeError):
    """The provider rejected the request shape (for example, unsupported tools)."""


class ModelTransport(Protocol):
    """Performs one Responses-style request and returns the parsed turn."""

    def create(self, request: dict[str, Any]) -> ModelTurn:
        """Send ``request`` (model, instructions, input, ...) and return a turn."""
        ...


TraceSink = Callable[[TraceEntry], None]


def _role_text() -> str:
    return (
        "You are the CohortLens investigator in a federated analysis. Several sites "
        "each fitted the same pre-approved regression on data that never leaves them; "
        "a coordinator pooled their estimates with a fixed-effect meta-analysis. A "
        "deterministic gate has asked you to decide whether one more federated query "
        "is worth its disclosure cost, and if so which one.\n\n"
        "What you can see: only network-level aggregates, exposed through tools. You "
        "cannot see rows, site identities or site-level estimates, and you cannot run "
        "any analysis yourself.\n\n"
        "What you can do: inspect the current evidence and its history, compute "
        "heterogeneity diagnostics, read the closed catalog of follow-up actions with "
        "their costs, check an action's preconditions, and submit exactly one "
        "proposal (a further query or STOP_NO_FOLLOWUP). Your proposal is a "
        "recommendation only: a human must approve a new hash-bound contract before "
        "anything runs, and sites re-validate it against their own allowlist.\n\n"
        "How to work: gather the evidence you need first, weigh what each available "
        "action would reveal against what it discloses and the remaining budget, "
        "check preconditions, then submit. Copy binding fields exactly from the "
        "tools. Use cautious, exploratory language; do not invent values, sites, "
        "mechanisms or results. If a submission is rejected, read the errors, fix "
        "them and resubmit."
    )


def _json_protocol_text(specs: tuple[ToolSpec, ...]) -> str:
    catalog = [
        {"name": spec.name, "description": spec.description, "parameters": spec.parameters}
        for spec in specs
    ]
    return (
        "\n\nTool protocol: reply with exactly one JSON object per turn and nothing "
        'else, of the form {"tool": "<tool name>", "arguments": {...}}. No Markdown, '
        "no prose, one tool per turn. Each tool result is returned to you in a message "
        f"starting with {TOOL_RESULT_PREFIX.strip()}.\n\nTools:\n"
        + json.dumps(catalog, sort_keys=True, separators=(",", ":"))
    )


def build_instructions(evidence: InvestigationEvidence, protocol: Protocol_) -> str:
    """System instructions: role, constraints and tools; never an answer."""
    text = _role_text()
    if protocol == "json":
        text += _json_protocol_text(tool_specs(evidence))
    return text


def build_task(evidence: InvestigationEvidence) -> str:
    """Opening user message; identifies the task without supplying a decision."""
    return (
        f"Investigation task for study {evidence.study_id}: the next federated round "
        f"would be round {evidence.next_round_index} of at most "
        f"{evidence.max_association_rounds - 1} follow-ups. Use the tools to decide "
        "and submit one proposal."
    )


def native_tools(specs: tuple[ToolSpec, ...]) -> list[dict[str, Any]]:
    """Responses API function-tool declarations."""
    return [
        {
            "type": "function",
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
        }
        for spec in specs
    ]


def extract_json_action(text: str) -> tuple[str, dict[str, Any]]:
    """Parse one strict ``{"tool", "arguments"}`` object; raise ValueError otherwise."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    try:
        payload, end = json.JSONDecoder().raw_decode(stripped)
    except json.JSONDecodeError as error:
        raise ValueError(f"reply is not a JSON object: {error.msg}") from error
    if stripped[end:].strip():
        raise ValueError("reply must contain exactly one JSON object and nothing else")
    if not isinstance(payload, dict) or set(payload) != {"tool", "arguments"}:
        raise ValueError('reply must be an object with exactly the keys "tool" and "arguments"')
    if not isinstance(payload["tool"], str) or not isinstance(payload["arguments"], dict):
        raise ValueError('"tool" must be a string and "arguments" an object')
    return payload["tool"], payload["arguments"]


def _clip(value: Any) -> Any:
    """Bound very large values stored in the trace."""
    rendered = json.dumps(value, sort_keys=True)
    if len(rendered) <= MAX_TRACE_TEXT:
        return value
    return {"truncated": True, "sha256": sha256_digest(rendered), "preview": rendered[:MAX_TRACE_TEXT]}


@dataclass
class _LoopState:
    """Mutable bookkeeping of one run."""

    protocol: Protocol_
    messages: list[dict[str, Any]] = field(default_factory=list)
    trace: list[TraceEntry] = field(default_factory=list)
    steps: int = 0
    tool_calls: int = 0
    invalid_turns: int = 0
    native_succeeded: bool = False
    proposal: FollowUpProposal | None = None
    failure: str | None = None


class InvestigatorLoop:
    """Runs the bounded tool loop for one evidence packet."""

    def __init__(
        self,
        evidence: InvestigationEvidence,
        transport: ModelTransport,
        *,
        model: str,
        protocol: Protocol_ = "json",
        allow_fallback: bool = True,
        limits: LoopLimits | None = None,
        on_trace: TraceSink | None = None,
    ) -> None:
        self.evidence = evidence
        self.transport = transport
        self.model = model
        self.requested = protocol
        self.allow_fallback = allow_fallback
        self.limits = limits or LoopLimits()
        self.on_trace = on_trace
        self.toolbox = InvestigationToolbox(evidence)
        self.specs = tool_specs(evidence)

    def _record(self, state: _LoopState, **fields: Any) -> None:
        entry = TraceEntry(step=state.steps, **fields)
        state.trace.append(entry)
        if self.on_trace is not None:
            self.on_trace(entry)

    def _request(self, state: _LoopState) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self.model,
            "instructions": build_instructions(self.evidence, state.protocol),
            "input": list(state.messages),
            "max_output_tokens": self.limits.max_output_tokens,
        }
        if state.protocol == "native":
            request["tools"] = native_tools(self.specs)
            request["tool_choice"] = "auto"
        return request

    def _fresh_state(self, protocol: Protocol_, carried: _LoopState | None = None) -> _LoopState:
        state = _LoopState(protocol=protocol)
        if carried is not None:
            state.trace, state.steps = carried.trace, carried.steps
        state.messages.append({"role": "user", "content": build_task(self.evidence)})
        return state

    def _execute(
        self, state: _LoopState, name: str, arguments: dict[str, Any], call_id: str | None
    ) -> str:
        """Run one tool, trace it, and return the serialised output for the model."""
        state.tool_calls += 1
        self._record(
            state, kind="tool_call", tool=name, call_id=call_id, arguments=_clip(arguments)
        )
        outcome = self.toolbox.call(name, arguments)
        self._record(
            state,
            kind="tool_result",
            tool=name,
            call_id=call_id,
            ok=outcome.ok,
            output=_clip(outcome.output),
        )
        if outcome.proposal is not None:
            state.proposal = outcome.proposal
        return json.dumps({"tool": name, "ok": outcome.ok, "output": outcome.output}, sort_keys=True)

    def _invalid(self, state: _LoopState, reason: str) -> None:
        state.invalid_turns += 1
        self._record(state, kind="invalid_turn", message=reason[:MAX_TRACE_TEXT])

    def _handle_json_turn(self, state: _LoopState, turn: ModelTurn) -> None:
        text = turn.text
        state.messages.append({"role": "assistant", "content": text})
        try:
            name, arguments = extract_json_action(text)
        except ValueError as error:
            self._invalid(state, str(error))
            feedback = f"{INVALID_TURN_PREFIX}{error}. Reply with exactly one JSON action object."
            state.messages.append({"role": "user", "content": feedback})
            return
        if name not in self.toolbox.names:
            self._invalid(state, f"unknown tool {name!r}")
        result = self._execute(state, name, arguments, None)
        state.messages.append({"role": "user", "content": f"{TOOL_RESULT_PREFIX}{result}"})

    def _handle_native_turn(self, state: _LoopState, turn: ModelTurn) -> None:
        if turn.text:
            state.messages.append({"role": "assistant", "content": turn.text})
            self._record(state, kind="model_text", message=turn.text[:MAX_TRACE_TEXT])
        if not turn.function_calls:
            self._invalid(state, "no function call in a native-protocol turn")
            feedback = f"{INVALID_TURN_PREFIX}Call one of the provided tools."
            state.messages.append({"role": "user", "content": feedback})
            return
        for call in turn.function_calls:
            if state.tool_calls >= self.limits.max_tool_calls or state.proposal is not None:
                break
            state.messages.append({
                "type": "function_call",
                "call_id": call.call_id,
                "name": call.name,
                "arguments": call.arguments,
            })
            try:
                arguments = json.loads(call.arguments or "{}")
            except json.JSONDecodeError as error:
                self._invalid(state, f"function arguments are not JSON: {error.msg}")
                error_output = {"error": "arguments are not valid JSON"}
                output = json.dumps({"tool": call.name, "ok": False, "output": error_output})
            else:
                if call.name not in self.toolbox.names:
                    self._invalid(state, f"unknown tool {call.name!r}")
                output = self._execute(state, call.name, arguments, call.call_id)
            state.messages.append({"type": "function_call_output", "call_id": call.call_id, "output": output})

    def _step(self, state: _LoopState) -> _LoopState:
        """Perform one model request; may switch protocol on a rejected native request."""
        state.steps += 1
        try:
            turn = self.transport.create(self._request(state))
        except ModelRequestRejected as error:
            if state.protocol != "native" or state.native_succeeded or not self.allow_fallback:
                raise
            message = f"native tools rejected; switching to json: {error}"
            self._record(state, kind="protocol_fallback", message=message[:MAX_TRACE_TEXT])
            return self._fresh_state("json", carried=state)
        if state.protocol == "native":
            state.native_succeeded = True
            self._handle_native_turn(state, turn)
        else:
            self._handle_json_turn(state, turn)
        return state

    def _exhausted(self, state: _LoopState) -> str | None:
        limits = self.limits
        if state.invalid_turns >= limits.max_invalid_turns:
            return f"invalid-turn cap reached ({limits.max_invalid_turns})"
        if state.tool_calls >= limits.max_tool_calls:
            return f"tool-call cap reached ({limits.max_tool_calls})"
        if state.steps >= limits.max_steps:
            return f"step cap reached ({limits.max_steps})"
        return None

    def run(self, *, policy_kind: str, policy_label: str) -> InvestigatorRecord:
        """Run until an accepted submission or a cap; never raises on model misbehaviour."""
        state = self._fresh_state(self.requested)
        while state.proposal is None:
            reason = self._exhausted(state)
            if reason is not None:
                state.failure = reason
                break
            try:
                state = self._step(state)
            except (ModelRequestRejected, RuntimeError) as error:
                state.failure = f"model request failed: {error}"[:1000]
                break
        return InvestigatorRecord(
            study_id=self.evidence.study_id,
            parent_analysis_id=self.evidence.parent_analysis_id,
            parent_result_hash=self.evidence.parent_result_hash,
            evidence_hash=sha256_digest(canonical_json(self.evidence)),
            policy_kind=policy_kind,  # type: ignore[arg-type]
            policy_label=policy_label,
            protocol_requested=self.requested,
            protocol_used=state.protocol,
            status="PROPOSED" if state.proposal is not None else "FAILED",
            failure_reason=None if state.proposal is not None else state.failure,
            proposal=state.proposal,
            steps=state.steps,
            tool_calls=state.tool_calls,
            invalid_turns=state.invalid_turns,
            trace=tuple(state.trace),
        )


def _event_dict(event: Any) -> dict[str, Any]:
    if hasattr(event, "to_dict"):
        return event.to_dict()
    if isinstance(event, dict):
        return event
    raise TypeError("stream event must be a dict or expose to_dict()")


class OpenAIResponsesTransport:
    """Streams one Responses request through an OpenAI-SDK-compatible client.

    The client is duck-typed (``client.responses.create``), so this module does
    not import ``openai``. Every raw stream event is passed to ``on_event``.
    """

    def __init__(self, client: Any, *, on_event: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.client = client
        self.on_event = on_event

    def create(self, request: dict[str, Any]) -> ModelTurn:
        """Send one streamed request and assemble text and function calls."""
        try:
            stream = self.client.responses.create(**request, stream=True)
        except Exception as error:  # SDK error classes are provider-specific
            if getattr(error, "status_code", None) == 400:
                raise ModelRequestRejected(str(error)[:1000]) from error
            raise RuntimeError(str(error)[:1000]) from error
        text: list[str] = []
        calls: dict[str, FunctionCall] = {}
        for raw in stream:
            event = _event_dict(raw)
            if self.on_event is not None:
                self.on_event(event)
            kind = event.get("type")
            if kind in {"error", "response.failed"}:
                raise RuntimeError(f"model response failed: {json.dumps(event)[:1000]}")
            if kind == "response.output_text.delta":
                text.append(str(event.get("delta", "")))
            item = event.get("item") if kind == "response.output_item.done" else None
            if isinstance(item, dict) and item.get("type") == "function_call":
                call_id = str(item.get("call_id") or item.get("id") or f"call-{len(calls)}")
                name, arguments = str(item.get("name", "")), str(item.get("arguments", ""))
                calls[call_id] = FunctionCall(call_id, name, arguments)
        return ModelTurn(text="".join(text), function_calls=tuple(calls.values()))
