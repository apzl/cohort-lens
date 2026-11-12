"""Step-by-step operator commands: prepare, collect, gate, approve, report."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import NoReturn

import numpy as np

from cohortlens_shared.catalog import compile_followup
from cohortlens_shared.evidence import investigation_evidence
from cohortlens_shared.gdsc import prepare_gdsc_snapshots
from cohortlens_shared.schemas import (
    AnalysisContract,
    AssociationResult,
    FollowUpProposal,
    InvestigationEvidence,
    LeaveOneSiteOutResult,
    ReleasedResult,
    StratifiedAssociationResult,
    canonical_json,
    continuation_decision,
    contract_digest,
    default_gdsc_contract,
    default_phase1_contract,
    investigation_decision_from_result,
    parse_released_result_json,
    sha256_digest,
)
from cohortlens_shared.statistics import generate_synthetic_site, phase1_reference

from .agent_frames import AgentFrameError, extract_investigator_record
from .approvals import make_approval
from .backends import InvestigatorSettings, analytics_run_config, investigator_run_config
from .fab import FabInspectionError, inspect_fab
from .report import render_loso_report, render_phase2_report, render_report
from .result_frames import ResultFrameError, extract_result


def _fail(message: str) -> NoReturn:
    raise SystemExit(message)


def _write(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")


def _print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def reference(args: argparse.Namespace) -> None:
    """Compute the offline three-site synthetic reference."""
    sites, aggregate = phase1_reference()
    payload = {
        "synthetic": True,
        "sites": [site.model_dump(mode="json") for site in sites],
        "aggregate": aggregate.model_dump(mode="json"),
    }
    rendered = json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n"
    if args.output:
        _write(Path(args.output), rendered)
    else:
        print(rendered, end="")


def _write_primary_inputs(output_dir: Path, contract: AnalysisContract, approved_by: str, hours: int) -> str:
    approval = make_approval(contract, approved_by, valid_hours=hours)
    _write(output_dir / "contract.json", canonical_json(contract) + "\n")
    _write(output_dir / "approval.json", canonical_json(approval) + "\n")
    _write(output_dir / "analytics-run.toml", analytics_run_config(contract, approval, None))
    return approval.contract_hash


def prepare_synthetic(args: argparse.Namespace) -> None:
    """Create and approve the synthetic primary contract."""
    output_dir = Path(args.output_dir)
    contract = default_phase1_contract(followup_budget=args.followup_budget)
    digest = _write_primary_inputs(output_dir, contract, args.approved_by, args.valid_hours)
    _print_json({"contract_hash": digest, "approved_by": args.approved_by, "output_dir": str(output_dir)})


def prepare_supernode_data(args: argparse.Namespace) -> None:
    """Create three distinct node-local synthetic snapshot archives."""
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = [output_dir / f"site-{index}.npz" for index in range(1, 4)]
    existing = [str(path) for path in paths if path.exists()]
    if existing and not args.overwrite:
        _fail("refusing to overwrite existing snapshots: " + ", ".join(existing))
    sites = []
    for site_index, path in enumerate(paths):
        design, outcome = generate_synthetic_site(site_index)
        np.savez(path, design=design, outcome=outcome)
        path.chmod(0o600)
        sites.append({
            "site_alias": f"site-{site_index + 1}",
            "snapshot_alias": f"deployment-site-{site_index + 1}-v1",
            "dataset_path": str(path),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "rows": int(design.shape[0]),
        })
    manifest = {
        "schema_version": "1.0",
        "synthetic": True,
        "format": "COHORTLENS_PHASE1_NPZ_V1",
        "sites": sites,
    }
    manifest_path = output_dir / "manifest.json"
    _write(manifest_path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    manifest_path.chmod(0o600)
    _print_json(manifest)


def prepare_gdsc(args: argparse.Namespace) -> None:
    """Prepare the public GDSC2 snapshots and approved primary run inputs."""
    output_dir = Path(args.output_dir).resolve()
    try:
        manifest = prepare_gdsc_snapshots(
            responses_path=Path(args.responses),
            models_path=Path(args.models),
            mutations_path=Path(args.mutations),
            output_dir=output_dir,
            overwrite=args.overwrite,
            simulation_split=args.simulation_split,
        )
    except (OSError, ValueError) as error:
        _fail(str(error))
    contract = default_gdsc_contract(
        simulation_split=args.simulation_split, followup_budget=args.followup_budget
    )
    _write_primary_inputs(output_dir, contract, args.approved_by, args.valid_hours)
    _print_json(manifest)


def collect(args: argparse.Namespace) -> None:
    """Extract one validated result frame from a saved run log."""
    try:
        with Path(args.log).open(encoding="utf-8") as handle:
            result = extract_result(
                handle,
                expected_study_id=args.study_id,
                expected_query_id=args.query_id,
                expected_contract_hash=args.contract_hash,
            )
    except (OSError, ResultFrameError) as error:
        _fail(str(error))
    rendered = canonical_json(result) + "\n"
    if args.output:
        _write(Path(args.output), rendered)
    else:
        print(rendered, end="")


def _load_result(path: str) -> ReleasedResult:
    try:
        return parse_released_result_json(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        _fail(f"{path}: {error}")


def _load_contract(path: str) -> AnalysisContract:
    try:
        return AnalysisContract.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        _fail(f"{path}: {error}")


def _default_contract_for(result: ReleasedResult) -> AnalysisContract:
    """Find the default primary contract that produced a primary result."""
    candidates = [
        default_phase1_contract(),
        default_gdsc_contract(),
        default_gdsc_contract(simulation_split="lineage-enriched-v1"),
    ]
    for contract in candidates:
        if contract_digest(contract) == result.contract_hash:
            return contract
    _fail("no default contract matches this result; pass its contract with --round RESULT CONTRACT")


def load_rounds(args: argparse.Namespace) -> list[tuple[ReleasedResult, AnalysisContract]]:
    """Ordered (result, contract) pairs from ``--round`` or a lone ``--result``."""
    if args.round:
        return [(_load_result(result), _load_contract(contract)) for result, contract in args.round]
    if args.result:
        result = _load_result(args.result)
        return [(result, _default_contract_for(result))]
    _fail("give --round RESULT CONTRACT (repeat in order, primary first) or --result PRIMARY")


def _evidence(rounds: list[tuple[ReleasedResult, AnalysisContract]]) -> InvestigationEvidence:
    try:
        return investigation_evidence(rounds)
    except ValueError as error:
        _fail(str(error))


def prepare_investigator(args: argparse.Namespace) -> None:
    """Apply the gate; write aggregate-only evidence and an investigator config."""
    rounds = load_rounds(args)
    evidence = _evidence(rounds)
    primary = rounds[0][0]
    if len(rounds) == 1:
        if not isinstance(primary, AssociationResult):
            _fail("the first round must be a primary association result")
        decision = investigation_decision_from_result(primary)
    else:
        decision = continuation_decision(evidence)
    output = Path(args.output)
    _write(output.with_name("investigation-decision.json"), canonical_json(decision) + "\n")
    if decision.decision != "INVESTIGATE":
        _fail(f"gate decision is {decision.decision}; no investigator run was prepared")
    _write(output.with_name("investigation-evidence.json"), canonical_json(evidence) + "\n")
    settings = InvestigatorSettings(protocol=args.tool_protocol)
    _write(output, investigator_run_config(evidence, settings))


def _load_evidence(path: str) -> InvestigationEvidence:
    try:
        return InvestigationEvidence.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        _fail(f"{path}: {error}")


def collect_investigator(args: argparse.Namespace) -> None:
    """Collect one investigator record; write its proposal and the full trace."""
    evidence = _load_evidence(args.evidence)
    try:
        with Path(args.log).open(encoding="utf-8") as handle:
            record = extract_investigator_record(
                handle, expected_parent_result_hash=evidence.parent_result_hash
            )
    except (OSError, AgentFrameError) as error:
        _fail(str(error))
    if record.evidence_hash != sha256_digest(canonical_json(evidence)):
        _fail("investigator record was produced from different evidence")
    output = Path(args.output)
    _write(output.with_name("investigator-record.json"), canonical_json(record) + "\n")
    if record.proposal is None:
        _fail(f"investigator produced no proposal: {record.failure_reason}")
    _write(output, canonical_json(record.proposal) + "\n")


def prepare_followup(args: argparse.Namespace) -> None:
    """Compile and approve the contract a reviewed proposal permits."""
    evidence = _load_evidence(args.evidence)
    parent_contract = _load_contract(args.parent_contract)
    try:
        proposal = FollowUpProposal.model_validate_json(Path(args.proposal).read_text(encoding="utf-8"))
        contract = compile_followup(evidence, parent_contract, proposal)
    except (OSError, ValueError) as error:
        _fail(str(error))
    approval = make_approval(contract, args.approved_by, valid_hours=args.valid_hours)
    output_dir = Path(args.output_dir)
    _write(output_dir / "followup-contract.json", canonical_json(contract) + "\n")
    _write(output_dir / "followup-approval.json", canonical_json(approval) + "\n")
    _write(output_dir / "followup-proposal.json", canonical_json(proposal) + "\n")
    _write(output_dir / "followup-analytics-run.toml", analytics_run_config(contract, approval, proposal))
    _print_json({
        "action": proposal.action,
        "round_index": contract.round_index,
        "contract_hash": approval.contract_hash,
        "parent_result_hash": contract.parent_result_hash,
        "proposal_hash": contract.proposal_hash,
        "approved_by": args.approved_by,
        "output_dir": str(output_dir),
    })


def prepare_reporter(args: argparse.Namespace) -> None:
    """Aggregate-only reporter config over the released chain."""
    evidence = _evidence(load_rounds(args))
    config = '[agent]\nmode = "reporter"\n' + f"input = {json.dumps(canonical_json(evidence))}\n"
    _write(Path(args.output), config)


def report(args: argparse.Namespace) -> None:
    """Static HTML report for a primary, stratified or LOSO result."""
    result = _load_result(args.result)
    if isinstance(result, StratifiedAssociationResult):
        _write(Path(args.output), render_phase2_report(result))
    elif isinstance(result, LeaveOneSiteOutResult):
        _write(Path(args.output), render_loso_report(result))
    elif isinstance(result, AssociationResult):
        _write(Path(args.output), render_report(
            result,
            run_id=args.run_id,
            agent_model=args.agent_model,
            flower_version=args.flower_version,
            agent_fab=args.agent_fab,
            analytics_fab=args.analytics_fab,
        ))
    else:
        _fail("HTML reports require an association result")


def inspect_fab_command(args: argparse.Namespace) -> None:
    """Check bundle contents and forbidden sentinel strings."""
    try:
        members = inspect_fab(Path(args.fab), tuple(args.forbid_text))
    except (OSError, FabInspectionError) as error:
        _fail(str(error))
    print(json.dumps({"fab": args.fab, "members": members}, indent=2))
