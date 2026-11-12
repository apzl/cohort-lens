"""Hash-chained ledger, the study orchestrator, approvals and the SuperGrid backend."""

import json
from pathlib import Path

import pytest

from cohortlens_operator.approvals import ApprovalRequest, InteractiveApprover, ScriptedApprover
from cohortlens_operator.backends import (
    InvestigatorSettings,
    LocalBackend,
    SuperGridBackend,
    analytics_run_config,
    parse_run_id,
)
from cohortlens_operator.ledger import Ledger, LedgerError, read_entries, verify_ledger
from cohortlens_operator.local_grid import synthetic_sites
from cohortlens_operator.orchestrator import StudyOrchestrator, StudyPlan
from cohortlens_operator.report import render_study_report
from cohortlens_operator.scripted_policy import POLICY_KIND, POLICY_LABEL, ScriptedPolicyTransport
from cohortlens_operator.agent_frames import AGENT_RESULT_FRAME_PREFIX
from cohortlens_operator.result_frames import format_result_frame
from cohortlens_shared.evidence import investigation_evidence
from cohortlens_shared.investigator import InvestigatorLoop
from cohortlens_shared.schemas import canonical_json, default_phase1_approval, default_phase1_contract
from support import assert_aggregate_only, primary_round


def _ledger(tmp_path: Path, name: str = "ledger.jsonl") -> Ledger:
    return Ledger(tmp_path / name, "phase1-demo", clock=lambda: "2026-11-11T00:00:00+00:00")


def test_ledger_chains_entries_and_detects_tampering(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    for index in range(3):
        ledger.append("GATE_DECISION", {"index": index})
    report = verify_ledger(ledger.path)
    assert report.entries == 3 and report.entry_types == {"GATE_DECISION": 3}
    lines = ledger.path.read_text().splitlines()
    first, second = json.loads(lines[0]), json.loads(lines[1])
    assert second["prev_hash"] == first["entry_hash"]

    def check(mutated: list[str], message: str) -> None:
        path = tmp_path / "tampered.jsonl"
        path.write_text("\n".join(mutated) + "\n")
        with pytest.raises(LedgerError, match=message):
            verify_ledger(path)

    edited = json.loads(lines[1])
    edited["payload"]["index"] = 99
    check([lines[0], json.dumps(edited), lines[2]], "entry_hash does not match")
    check([lines[0], lines[2]], "sequence number")
    check([lines[1], lines[0], lines[2]], "sequence number")
    rehashed = dict(edited)
    from cohortlens_operator.ledger import entry_hash

    rehashed["entry_hash"] = entry_hash(rehashed)
    check([lines[0], json.dumps(rehashed), lines[2]], "prev_hash does not match")
    with pytest.raises(LedgerError, match="unknown ledger entry type"):
        ledger.append("FREE_TEXT", {})
    with pytest.raises(LedgerError, match="existing ledger"):
        _ledger(tmp_path)


def _orchestrate(tmp_path, approver, protocol="json", budget=2, transport=None):
    transport = transport or ScriptedPolicyTransport()
    backend = LocalBackend(
        synthetic_sites(), transport, POLICY_LABEL, POLICY_KIND, POLICY_LABEL, InvestigatorSettings(protocol=protocol)
    )
    plan = StudyPlan(default_phase1_contract(followup_budget=budget), POLICY_KIND, POLICY_LABEL, description={"data_mode": "SYNTHETIC"})
    ledger = _ledger(tmp_path, f"ledger-{protocol}-{budget}.jsonl")
    outcome = StudyOrchestrator(plan, backend, approver, ledger, output_dir=tmp_path / "out").run()
    return outcome, read_entries(ledger.path), transport


@pytest.mark.parametrize("protocol", ["json", "native"])
def test_synthetic_study_runs_end_to_end_with_scripted_policy(tmp_path: Path, protocol: str) -> None:
    outcome, entries, transport = _orchestrate(tmp_path, ScriptedApprover("demo-operator"), protocol)
    assert outcome.finish_reason == "BUDGET_EXHAUSTED"
    actions = [r.proposal.action for r in outcome.records]
    assert actions == ["LEAVE_ONE_SITE_OUT", "STRATIFY_SYNTHETIC_CONTEXT"]
    assert [type(r).__name__ for r, _ in outcome.rounds] == [
        "AssociationResult", "LeaveOneSiteOutResult", "StratifiedAssociationResult"
    ]
    chain = [c for _, c in outcome.rounds]
    for (parent_result, _), contract in zip(outcome.rounds, chain[1:]):
        from cohortlens_shared.schemas import result_digest

        assert contract.parent_result_hash == result_digest(parent_result)
    types = [e["entry_type"] for e in entries]
    assert types[0] == "STUDY_STARTED" and types[-1] == "STUDY_FINISHED"
    assert types.count("CONTRACT_APPROVED") == 3 and types.count("AGENT_PROPOSAL") == 2
    approvals = [e["payload"] for e in entries if e["entry_type"] == "CONTRACT_APPROVED"]
    assert {a["approval_mode"] for a in approvals} == {"SCRIPTED_NON_INTERACTIVE"}
    agent_entries = [e["payload"] for e in entries if e["entry_type"].startswith("AGENT_")]
    assert agent_entries and all(p["policy_kind"] == "SCRIPTED_POLICY" for p in agent_entries)
    released = [e["payload"] for e in entries if e["entry_type"] == "RESULT_RELEASED"]
    assert "site-1" not in json.dumps(released) and "n_used" not in json.dumps(released)
    assert_aggregate_only(transport.requests)
    assert "SCRIPTED POLICY" in render_study_report(entries)
    assert (tmp_path / "out" / "round-2-result.json").exists()


def test_investigator_may_choose_to_stop(tmp_path: Path) -> None:
    class StratifyFirst(ScriptedPolicyTransport):
        """Scripted variant that prefers stratification, then stops once it explains I2."""

        def create(self, request):
            from cohortlens_operator import scripted_policy

            original = scripted_policy.preferences
            scripted_policy.preferences = lambda current, catalog: (
                ["STOP_NO_FOLLOWUP"] if scripted_policy._explained_by_strata(current)
                else [scripted_policy._stratify_action(catalog), "STOP_NO_FOLLOWUP"]
            )
            try:
                return super().create(request)
            finally:
                scripted_policy.preferences = original

    outcome, entries, _ = _orchestrate(tmp_path, ScriptedApprover("demo"), transport=StratifyFirst())
    assert [r.proposal.action for r in outcome.records] == ["STRATIFY_SYNTHETIC_CONTEXT", "STOP_NO_FOLLOWUP"]
    assert outcome.finish_reason == "INVESTIGATOR_PROPOSED_STOP"
    assert len(outcome.rounds) == 2


def test_human_can_decline_a_followup(tmp_path: Path) -> None:
    answers = iter(["", ""])
    shown: list[str] = []
    approver = InteractiveApprover("alice", input_fn=lambda _: next(answers), output_fn=shown.append)
    outcome, entries, _ = _orchestrate(tmp_path, approver)
    assert outcome.finish_reason == "PRIMARY_NOT_APPROVED" and outcome.rounds == []
    assert entries[-2]["entry_type"] == "APPROVAL_DECLINED"
    assert "Contract digest: sha256:" in shown[0]


def test_interactive_approval_requires_the_digest_prefix() -> None:
    contract = default_phase1_contract()
    request = ApprovalRequest(contract)
    prefix = request.digest.removeprefix("sha256:")[:12]
    assert InteractiveApprover("alice", input_fn=lambda _: prefix, output_fn=lambda _: None).review(request)
    assert not InteractiveApprover("alice", input_fn=lambda _: "yes", output_fn=lambda _: None).review(request)


def test_failed_investigator_ends_the_study_without_a_query(tmp_path: Path) -> None:
    from support import RecordingTransport
    from cohortlens_shared.investigator import ModelTurn

    transport = RecordingTransport([ModelTurn(text="no json") for _ in range(5)])
    outcome, entries, _ = _orchestrate(tmp_path, ScriptedApprover("demo"), transport=transport)
    assert outcome.finish_reason == "INVESTIGATOR_FAILED" and len(outcome.rounds) == 1
    assert [e["entry_type"] for e in entries].count("AGENT_INVALID_TURN") == 3


class FakeRunner:
    """Records flwr commands and answers with canned output."""

    def __init__(self, logs: list[str]) -> None:
        self.logs = logs
        self.commands: list[list[str]] = []

    def __call__(self, command):
        self.commands.append(list(command))
        if command[1] == "run":
            return f"Loading project...\nSuccessfully started run {4242 + len(self.commands)}\n"
        return self.logs.pop(0)


def test_supergrid_backend_builds_commands_and_parses_logs(tmp_path: Path) -> None:
    result, contract = primary_round()
    approval = default_phase1_approval(contract)
    runner = FakeRunner([f"INFO noise\nINFO {format_result_frame(result)}\n"])
    backend = SuperGridBackend(federation="@acct/fed", work_dir=tmp_path, runner=runner)
    collected, meta = backend.run_round(contract, approval, None)
    assert collected == result and meta["run_id"] == "4243"
    run_cmd, log_cmd = runner.commands
    assert run_cmd[:4] == ["flwr", "run", "apps/cohort-analytics", "supergrid"]
    assert run_cmd[run_cmd.index("--federation") + 1] == "@acct/fed" and "--stream" in run_cmd
    assert log_cmd == ["flwr", "log", "4243", "supergrid", "--show"]
    written = (tmp_path / "round-0-analytics-run.toml").read_text()
    assert written == analytics_run_config(contract, approval, None) and "/" not in written.split("query-id")[1]

    evidence = investigation_evidence([(result, contract)])
    record = InvestigatorLoop(evidence, ScriptedPolicyTransport(), model="x").run(policy_kind="MODEL", policy_label="flower-endeavor")
    runner.logs.append(f"{AGENT_RESULT_FRAME_PREFIX}{canonical_json(record)}\n")
    replayed = []
    assert backend.run_investigator(evidence, replayed.append) == record
    assert len(replayed) == len(record.trace)
    config = (tmp_path / "round-1-investigator.toml").read_text()
    assert 'mode = "investigator"' in config and 'tool-protocol = "json"' in config
    with pytest.raises(RuntimeError, match="run id"):
        parse_run_id("nothing here")
