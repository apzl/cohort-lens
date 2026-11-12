"""CohortLens AgentApp: a bounded tool-using investigator and a reporter.

``investigator`` runs a plan-act-observe loop over deterministic local tools
(``cohortlens_shared.tools``) and ends with a strictly validated, parent-bound
``FollowUpProposal`` or a recorded failure. ``reporter`` makes one
schema-validated call that summarises released aggregates. Both modes see
only ``InvestigationEvidence``: network aggregates, never site-level values.
"""

from __future__ import annotations

import json
import os
from typing import Any

from flwr.agentapp import AgentApp, AgentSession
from flwr.app import Context
from openai import OpenAI
from pydantic import ValidationError

from cohortlens_shared.investigator import (
    InvestigatorLoop,
    LoopLimits,
    OpenAIResponsesTransport,
)
from cohortlens_shared.schemas import (
    InvestigationEvidence,
    InvestigatorRecord,
    ReporterOutput,
    TraceEntry,
    canonical_json,
)

MODEL = "flower-endeavor"
RESULT_FRAME_PREFIX = "COHORTLENS_AGENT_RESULT_V2 "
TRACE_EVENT_TYPE = "cohortlens.investigator.trace"
PROPOSAL_EVENT_TYPE = "cohortlens.investigator.outcome"
app = AgentApp()


def _json_object(text: str) -> str:
    """Extract one JSON object while tolerating an accidental Markdown fence."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        raise ValueError("model output did not contain a JSON object")
    return stripped[start : end + 1]


def build_reporter_prompt(evidence: InvestigationEvidence) -> str:
    """Describe the reporter's role, data and output schema; supply no answer."""
    schema = json.dumps(ReporterOutput.model_json_schema(), sort_keys=True)
    return f"""You are the CohortLens reporting agent.
Use only the released network-level evidence below. Do not invent values, site
results, biology or causal claims, and do not imply that keeping rows local is a
formal privacy guarantee: the coordinator still sees site summaries and sites may
be dishonest or collude. Follow-up rounds reuse the same records, so they are
exploratory, not replication.

Evidence: {canonical_json(evidence)}

Reply with one JSON object and no Markdown that validates against this JSON
schema. study_id must equal the evidence study_id and analysis_id must equal the
evidence parent_analysis_id.
Schema: {schema}
"""


def parse_reporter_output(text: str, evidence: InvestigationEvidence) -> ReporterOutput:
    """Validate reporter prose strictly and check it names the evidence it used."""
    output = ReporterOutput.model_validate_json(_json_object(text))
    if output.study_id != evidence.study_id or output.analysis_id != evidence.parent_analysis_id:
        raise ValueError("reporter output changed evidence identifiers")
    return output


def loop_limits(context: Context) -> LoopLimits:
    """Read loop caps from run config, falling back to the conservative defaults."""
    config = context.run_config
    defaults = LoopLimits()
    return LoopLimits(
        max_steps=int(config.get("agent.max-steps", defaults.max_steps)),
        max_tool_calls=int(config.get("agent.max-tool-calls", defaults.max_tool_calls)),
        max_invalid_turns=int(config.get("agent.max-invalid-turns", defaults.max_invalid_turns)),
        max_output_tokens=int(config.get("agent.max-output-tokens", defaults.max_output_tokens)),
    )


def tool_protocol(context: Context) -> tuple[str, bool]:
    """Return the requested protocol and whether native may fall back to json."""
    protocol = str(context.run_config.get("agent.tool-protocol", "json"))
    if protocol not in {"native", "json"}:
        raise ValueError("agent.tool-protocol must be native or json")
    fallback = context.run_config.get("agent.protocol-fallback", True)
    return protocol, bool(fallback)


def _trace_event(entry: TraceEntry) -> dict[str, Any]:
    return {"type": TRACE_EVENT_TYPE, **entry.model_dump(mode="json", exclude_none=True)}


def _outcome_event(record: InvestigatorRecord) -> dict[str, Any]:
    return {
        "type": PROPOSAL_EVENT_TYPE,
        "status": record.status,
        "protocol_used": record.protocol_used,
        "steps": record.steps,
        "tool_calls": record.tool_calls,
        "invalid_turns": record.invalid_turns,
        "proposal": None if record.proposal is None else record.proposal.model_dump(mode="json"),
        "failure_reason": record.failure_reason,
    }


def run_investigator(
    agent: AgentSession, context: Context, client: Any, evidence: InvestigationEvidence
) -> InvestigatorRecord:
    """Run the bounded tool loop, emitting a structured event for every step."""
    protocol, fallback = tool_protocol(context)
    loop = InvestigatorLoop(
        evidence,
        OpenAIResponsesTransport(client, on_event=agent.events.emit),
        model=MODEL,
        protocol=protocol,  # type: ignore[arg-type]
        allow_fallback=fallback,
        limits=loop_limits(context),
        on_trace=lambda entry: agent.events.emit(_trace_event(entry)),
    )
    record = loop.run(policy_kind="MODEL", policy_label=MODEL)
    agent.events.emit(_outcome_event(record))
    return record


def run_reporter(agent: AgentSession, client: Any, evidence: InvestigationEvidence) -> ReporterOutput:
    """One streamed, schema-validated reporter call with no tools."""
    transport = OpenAIResponsesTransport(client, on_event=agent.events.emit)
    turn = transport.create({"model": MODEL, "input": build_reporter_prompt(evidence)})
    return parse_reporter_output(turn.text, evidence)


@app.main()
def main(agent: AgentSession, context: Context) -> None:
    """Run one bounded mode and print exactly one framed, schema-valid artifact."""
    mode = context.run_config.get("agent.mode")
    configured_input = context.run_config.get("agent.input")
    if mode not in {"investigator", "reporter"}:
        raise ValueError("agent.mode must be investigator or reporter")
    if not isinstance(configured_input, str) or not configured_input.strip():
        raise ValueError("agent.input must be a non-empty string")
    try:
        evidence = InvestigationEvidence.model_validate_json(configured_input.strip())
    except ValidationError as error:
        raise ValueError(f"agent.input is not valid investigation evidence: {error}") from error
    client = OpenAI(
        base_url=os.environ["FLWR_RUNTIME_BASE_URL"],
        api_key=os.environ["FLWR_RUNTIME_API_KEY"],
        max_retries=0,
    )
    if mode == "reporter":
        artifact = run_reporter(agent, client, evidence)
        print(f"{RESULT_FRAME_PREFIX}{canonical_json(artifact)}", flush=True)
        return
    record = run_investigator(agent, context, client, evidence)
    print(f"{RESULT_FRAME_PREFIX}{canonical_json(record)}", flush=True)
    if record.status != "PROPOSED":
        raise RuntimeError(f"investigator produced no valid proposal: {record.failure_reason}")
