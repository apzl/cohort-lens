"""The step-by-step operator commands still compose into a full multi-round chain."""

from pathlib import Path

import pytest

from cohortlens_operator.agent_frames import AGENT_RESULT_FRAME_PREFIX
from cohortlens_operator.cli import build_parser
from cohortlens_operator.scripted_policy import ScriptedPolicyTransport
from cohortlens_shared.investigator import InvestigatorLoop
from cohortlens_shared.schemas import (
    AnalysisContract,
    FollowUpProposal,
    InvestigationEvidence,
    canonical_json,
)
from support import run_round


def _cli(*argv: str) -> None:
    args = build_parser().parse_args(list(argv))
    args.handler(args)


def _investigate(tmp_path: Path, stem: str, rounds: list[str]) -> Path:
    """prepare-investigator -> (emulated AgentApp log) -> collect-investigator."""
    config = tmp_path / stem / "agent.toml"
    _cli("prepare-investigator", *rounds, "--output", str(config))
    evidence_path = config.with_name("investigation-evidence.json")
    evidence = InvestigationEvidence.model_validate_json(evidence_path.read_text())
    record = InvestigatorLoop(evidence, ScriptedPolicyTransport(), model="scripted").run(
        policy_kind="SCRIPTED_POLICY", policy_label="cohortlens-scripted-policy-v1"
    )
    log = tmp_path / stem / "investigator.log"
    log.write_text(f"INFO {AGENT_RESULT_FRAME_PREFIX}{canonical_json(record)}\n")
    proposal = tmp_path / stem / "proposal.json"
    _cli("collect-investigator", "--log", str(log), "--evidence", str(evidence_path), "--output", str(proposal))
    return proposal


def _follow(tmp_path: Path, stem: str, proposal: Path, parent_contract: Path) -> tuple[Path, Path]:
    evidence = proposal.with_name("investigation-evidence.json")
    out = tmp_path / stem / "followup"
    _cli(
        "prepare-followup", "--evidence", str(evidence), "--parent-contract", str(parent_contract),
        "--proposal", str(proposal), "--output-dir", str(out), "--approved-by", "OPERATOR",
    )
    contract = AnalysisContract.model_validate_json((out / "followup-contract.json").read_text())
    reviewed = FollowUpProposal.model_validate_json((out / "followup-proposal.json").read_text())
    result_path = out / "result.json"
    result_path.write_text(canonical_json(run_round(contract, reviewed)))
    assert "followup-proposal-json" in (out / "followup-analytics-run.toml").read_text()
    return result_path, out / "followup-contract.json"


def test_step_by_step_chain_reaches_the_budget(tmp_path: Path) -> None:
    _cli("prepare-synthetic", "--output-dir", str(tmp_path), "--approved-by", "OPERATOR")
    contract = AnalysisContract.model_validate_json((tmp_path / "contract.json").read_text())
    primary = tmp_path / "primary-result.json"
    primary.write_text(canonical_json(run_round(contract)))
    rounds = ["--round", str(primary), str(tmp_path / "contract.json")]

    proposal = _investigate(tmp_path, "r1", rounds)
    assert FollowUpProposal.model_validate_json(proposal.read_text()).action == "LEAVE_ONE_SITE_OUT"
    result, followup_contract = _follow(tmp_path, "r1", proposal, tmp_path / "contract.json")
    rounds += ["--round", str(result), str(followup_contract)]
    _cli("report", "--result", str(result), "--output", str(tmp_path / "loso.html"))
    assert "leave-one-site-out" in (tmp_path / "loso.html").read_text()

    proposal = _investigate(tmp_path, "r2", rounds)
    result, followup_contract = _follow(tmp_path, "r2", proposal, followup_contract)
    rounds += ["--round", str(result), str(followup_contract)]
    with pytest.raises(SystemExit, match="BUDGET_EXHAUSTED"):
        _cli("prepare-investigator", *rounds, "--output", str(tmp_path / "r3" / "agent.toml"))
    _cli("prepare-reporter", *rounds, "--output", str(tmp_path / "reporter.toml"))
    assert 'mode = "reporter"' in (tmp_path / "reporter.toml").read_text()


def test_run_study_and_verify_ledger_commands(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ledger = tmp_path / "ledger.jsonl"
    _cli(
        "run-study", "--data-mode", "synthetic", "--backend", "local", "--investigator", "scripted",
        "--tool-protocol", "native", "--approve-as", "demo-operator",
        "--ledger", str(ledger), "--output-dir", str(tmp_path / "out"),
    )
    assert '"finish_reason": "BUDGET_EXHAUSTED"' in capsys.readouterr().out
    _cli("verify-ledger", str(ledger))
    assert '"valid": true' in capsys.readouterr().out
    assert "SCRIPTED POLICY" in (tmp_path / "out" / "study-report.html").read_text()
    with pytest.raises(SystemExit, match="existing ledger"):
        _cli("run-study", "--data-mode", "synthetic", "--approve-as", "x", "--ledger", str(ledger), "--output-dir", str(tmp_path))
    with pytest.raises(SystemExit, match="--approver NAME"):
        _cli("run-study", "--data-mode", "synthetic", "--ledger", str(tmp_path / "b.jsonl"), "--output-dir", str(tmp_path))
    with pytest.raises(SystemExit, match="agentapp"):
        _cli(
            "run-study", "--data-mode", "synthetic", "--backend", "supergrid", "--federation", "@a/f",
            "--approve-as", "x", "--ledger", str(tmp_path / "c.jsonl"), "--output-dir", str(tmp_path),
        )
