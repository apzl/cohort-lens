"""Execution backends behind one interface: in-process ``local`` and ``supergrid``.

``local`` runs the real ServerApp/ClientApp handlers over an in-memory grid and
the investigator loop with a pluggable model transport. ``supergrid`` shells
out to ``flwr run`` / ``flwr log`` and reuses the frame collectors; it cannot be
exercised offline and is covered only by command-construction and log-parsing
tests with a fake runner.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from cohortlens_shared.investigator import InvestigatorLoop, LoopLimits, ModelTransport
from cohortlens_shared.schemas import (
    AnalysisContract,
    Approval,
    FollowUpProposal,
    InvestigationEvidence,
    InvestigatorRecord,
    ReleasedResult,
    TraceEntry,
    canonical_json,
    contract_digest,
)

from .agent_frames import extract_investigator_record
from .local_grid import InMemoryGrid, SiteSnapshot, import_app_module
from .result_frames import extract_result

TraceSink = Callable[[TraceEntry], None]


class StudyBackend(Protocol):
    """Runs approved rounds and investigator sessions for the orchestrator."""

    name: str

    def run_round(
        self, contract: AnalysisContract, approval: Approval, proposal: FollowUpProposal | None
    ) -> tuple[ReleasedResult, dict[str, Any]]:
        """Run one approved round; return the released result and run metadata."""
        ...

    def run_investigator(
        self, evidence: InvestigationEvidence, on_trace: TraceSink
    ) -> InvestigatorRecord:
        """Run one investigator session over aggregate-only evidence."""
        ...


@dataclass
class InvestigatorSettings:
    """Protocol and caps shared by every backend."""

    protocol: str = "json"
    allow_fallback: bool = True
    limits: LoopLimits = field(default_factory=LoopLimits)


@dataclass
class LocalBackend:
    """In-process execution of the real handlers and the real tool loop."""

    sites: list[SiteSnapshot]
    transport: ModelTransport
    model: str
    policy_kind: str
    policy_label: str
    settings: InvestigatorSettings = field(default_factory=InvestigatorSettings)
    name: str = "local"

    def run_round(
        self, contract: AnalysisContract, approval: Approval, proposal: FollowUpProposal | None
    ) -> tuple[ReleasedResult, dict[str, Any]]:
        """Run the ServerApp entry logic in-process and re-collect its result frame."""
        server = import_app_module("cohortlens_analytics.server_app")
        config = server.Phase1Config(
            contract=contract,
            approval=approval,
            query_id=contract.analysis_id,
            node_wait_seconds=0.0,
            reply_timeout_seconds=30.0,
        )
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            returned = server.execute_contract(InMemoryGrid(self.sites), config, proposal)
        collected = extract_result(
            captured.getvalue().splitlines(),
            expected_study_id=contract.study_id,
            expected_query_id=contract.analysis_id,
            expected_contract_hash=contract_digest(contract),
        )
        if collected != returned:
            raise RuntimeError("framed result differs from the returned result")
        return collected, {"backend": self.name, "run_id": "local-in-process"}

    def run_investigator(
        self, evidence: InvestigationEvidence, on_trace: TraceSink
    ) -> InvestigatorRecord:
        """Run the bounded loop with this backend's transport."""
        loop = InvestigatorLoop(
            evidence,
            self.transport,
            model=self.model,
            protocol=self.settings.protocol,  # type: ignore[arg-type]
            allow_fallback=self.settings.allow_fallback,
            limits=self.settings.limits,
            on_trace=on_trace,
        )
        return loop.run(policy_kind=self.policy_kind, policy_label=self.policy_label)


Runner = Callable[[Sequence[str]], str]
_RUN_ID = re.compile(r"Successfully started run (\d+)")


def subprocess_runner(command: Sequence[str]) -> str:
    """Run a command and return combined output; raise on a non-zero exit."""
    completed = subprocess.run(list(command), capture_output=True, text=True, check=False)
    output = completed.stdout + completed.stderr
    if completed.returncode != 0:
        raise RuntimeError(f"{command[0]} {command[1]} failed ({completed.returncode}): {output[-2000:]}")
    return output


def parse_run_id(output: str) -> str:
    """Extract the run id that ``flwr run`` prints after submission."""
    match = _RUN_ID.search(output)
    if match is None:
        raise RuntimeError("flwr run output did not report a run id")
    return match.group(1)


def _toml_string(value: str) -> str:
    return json.dumps(value)


def analytics_run_config(
    contract: AnalysisContract, approval: Approval, proposal: FollowUpProposal | None
) -> str:
    """Inline run config for one analytics round; no file paths cross."""
    lines = [
        f"contract-json = {_toml_string(canonical_json(contract))}",
        f"approval-json = {_toml_string(canonical_json(approval))}",
        f"query-id = {_toml_string(contract.analysis_id)}",
        "node-wait-seconds = 30.0",
        "reply-timeout-seconds = 30.0",
    ]
    if proposal is not None:
        lines.append(f"followup-proposal-json = {_toml_string(canonical_json(proposal))}")
    return "\n".join(lines) + "\n"


def investigator_run_config(evidence: InvestigationEvidence, settings: InvestigatorSettings) -> str:
    """Agent run config: evidence, protocol and loop caps."""
    limits = settings.limits
    return (
        "[agent]\n"
        'mode = "investigator"\n'
        f"input = {_toml_string(canonical_json(evidence))}\n"
        f"tool-protocol = {_toml_string(settings.protocol)}\n"
        f"protocol-fallback = {'true' if settings.allow_fallback else 'false'}\n"
        f"max-steps = {limits.max_steps}\n"
        f"max-tool-calls = {limits.max_tool_calls}\n"
        f"max-invalid-turns = {limits.max_invalid_turns}\n"
        f"max-output-tokens = {limits.max_output_tokens}\n"
    )


@dataclass
class SuperGridBackend:
    """Submits each round and investigator session to SuperGrid via the flwr CLI."""

    federation: str
    work_dir: Path
    analytics_app: str = "apps/cohort-analytics"
    agent_app: str = "apps/cohort-agent"
    connection: str = "supergrid"
    settings: InvestigatorSettings = field(default_factory=InvestigatorSettings)
    runner: Runner = subprocess_runner
    name: str = "supergrid"

    def run_command(self, app: str, config_path: Path) -> list[str]:
        """``flwr run`` command for one app and run config."""
        return [
            "flwr", "run", app, self.connection,
            "--federation", self.federation,
            "--run-config", str(config_path),
            "--stream",
        ]

    def log_command(self, run_id: str) -> list[str]:
        """``flwr log`` command that prints a finished run's logs once."""
        return ["flwr", "log", run_id, self.connection, "--show"]

    def _submit(self, app: str, config_path: Path, log_path: Path) -> tuple[str, list[str]]:
        run_id = parse_run_id(self.runner(self.run_command(app, config_path)))
        log_text = self.runner(self.log_command(run_id))
        log_path.write_text(log_text, encoding="utf-8")
        return run_id, log_text.splitlines()

    def run_round(
        self, contract: AnalysisContract, approval: Approval, proposal: FollowUpProposal | None
    ) -> tuple[ReleasedResult, dict[str, Any]]:
        """Write the run config, submit, then collect exactly one matching frame."""
        stem = f"round-{contract.round_index}"
        config_path = self.work_dir / f"{stem}-analytics-run.toml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(analytics_run_config(contract, approval, proposal), encoding="utf-8")
        run_id, lines = self._submit(self.analytics_app, config_path, self.work_dir / f"{stem}-analytics.log")
        result = extract_result(
            lines,
            expected_study_id=contract.study_id,
            expected_query_id=contract.analysis_id,
            expected_contract_hash=contract_digest(contract),
        )
        return result, {"backend": self.name, "run_id": run_id}

    def run_investigator(
        self, evidence: InvestigationEvidence, on_trace: TraceSink
    ) -> InvestigatorRecord:
        """Run the AgentApp, collect its framed record, then replay its trace."""
        stem = f"round-{evidence.next_round_index}-investigator"
        config_path = self.work_dir / f"{stem}.toml"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(investigator_run_config(evidence, self.settings), encoding="utf-8")
        _, lines = self._submit(self.agent_app, config_path, self.work_dir / f"{stem}.log")
        record = extract_investigator_record(lines, expected_parent_result_hash=evidence.parent_result_hash)
        for entry in record.trace:
            on_trace(entry)
        return record
