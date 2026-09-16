"""Operator commands for the CohortLens synthetic demonstration."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NoReturn

import numpy as np

from cohortlens_shared.schemas import (
    Approval,
    AssociationResult,
    Phase2AssociationResult,
    FollowUpProposal,
    canonical_json,
    contract_digest,
    compile_stratified_followup,
    default_phase1_contract,
    default_gdsc_contract,
    evidence_from_result,
    followup_evidence_from_result,
    investigation_decision_from_result,
    parse_released_result_json,
)
from cohortlens_shared.statistics import generate_synthetic_site, phase1_reference
from cohortlens_shared.gdsc import prepare_gdsc_snapshots

from .fab import FabInspectionError, inspect_fab
from .agent_frames import AgentFrameError, extract_followup_proposal
from .report import render_phase2_report, render_report
from .result_frames import ResultFrameError, extract_result


def _fail(message: str) -> NoReturn:
    raise SystemExit(message)


def _write(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")


def _reference(args: argparse.Namespace) -> None:
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


def _prepare_phase1(args: argparse.Namespace) -> None:
    output_dir = Path(args.output_dir)
    contract = default_phase1_contract()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    approval = Approval(
        approval_id=f"{contract.analysis_id}-approval",
        study_id=contract.study_id,
        analysis_id=contract.analysis_id,
        contract_hash=contract_digest(contract),
        approved_by=args.approved_by,
        approved_at=now,
        expires_at=now + timedelta(hours=args.valid_hours),
    )
    contract_json = canonical_json(contract)
    approval_json = canonical_json(approval)
    run_config = (
        f"contract-json = {json.dumps(contract_json)}\n"
        f"approval-json = {json.dumps(approval_json)}\n"
        f"query-id = {json.dumps(contract.analysis_id)}\n"
        "node-wait-seconds = 30.0\n"
        "reply-timeout-seconds = 30.0\n"
    )
    planner_input = json.dumps(
        {
            "study_id": contract.study_id,
            "question": (
                "Is the fictional variant associated with the synthetic biomarker "
                "across all three sites?"
            ),
        },
        separators=(",", ":"),
    )
    agent_config = (
        "[agent]\n"
        'mode = "planner"\n'
        f"input = {json.dumps(planner_input)}\n"
    )
    _write(output_dir / "contract.json", contract_json + "\n")
    _write(output_dir / "approval.json", approval_json + "\n")
    _write(output_dir / "analytics-run.toml", run_config)
    _write(output_dir / "agent-plan.toml", agent_config)
    print(
        json.dumps(
            {
                "contract_hash": approval.contract_hash,
                "approved_by": approval.approved_by,
                "expires_at": approval.expires_at.isoformat(),
                "output_dir": str(output_dir),
            },
            indent=2,
        )
    )


def _prepare_supernode_data(args: argparse.Namespace) -> None:
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
        sites.append(
            {
                "site_alias": f"site-{site_index + 1}",
                "snapshot_alias": f"deployment-site-{site_index + 1}-v1",
                "dataset_path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "rows": int(design.shape[0]),
            }
        )
    manifest = {
        "schema_version": "1.0",
        "synthetic": True,
        "format": "COHORTLENS_PHASE1_NPZ_V1",
        "sites": sites,
    }
    manifest_path = output_dir / "manifest.json"
    _write(manifest_path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    manifest_path.chmod(0o600)
    print(json.dumps(manifest, indent=2, sort_keys=True))


def _prepare_gdsc(args: argparse.Namespace) -> None:
    """Prepare the minimal public GDSC2 snapshots and approved run inputs."""
    output_dir = Path(args.output_dir).resolve()
    try:
        manifest = prepare_gdsc_snapshots(
            responses_path=Path(args.responses),
            models_path=Path(args.models),
            mutations_path=Path(args.mutations),
            output_dir=output_dir,
            overwrite=args.overwrite,
        )
    except (OSError, ValueError) as error:
        _fail(str(error))

    contract = default_gdsc_contract()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    approval = Approval(
        approval_id=f"{contract.analysis_id}-approval",
        study_id=contract.study_id,
        analysis_id=contract.analysis_id,
        contract_hash=contract_digest(contract),
        approved_by=args.approved_by,
        scope="PUBLIC_GDSC_EMULATED_FEDERATION",
        approved_at=now,
        expires_at=now + timedelta(hours=args.valid_hours),
    )
    _write(output_dir / "contract.json", canonical_json(contract) + "\n")
    _write(output_dir / "approval.json", canonical_json(approval) + "\n")
    _write(
        output_dir / "analytics-run.toml",
        f"contract-json = {json.dumps(canonical_json(contract))}\n"
        f"approval-json = {json.dumps(canonical_json(approval))}\n"
        f"query-id = {json.dumps(contract.analysis_id)}\n"
        "node-wait-seconds = 30.0\n"
        "reply-timeout-seconds = 30.0\n",
    )
    print(json.dumps(manifest, indent=2, sort_keys=True))


def _collect(args: argparse.Namespace) -> None:
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


def _prepare_reporter(args: argparse.Namespace) -> None:
    try:
        result = parse_released_result_json(
            Path(args.result).read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as error:
        _fail(str(error))
    try:
        if isinstance(result, Phase2AssociationResult):
            evidence = followup_evidence_from_result(result)
            mode = "followup_reporter"
        elif isinstance(result, AssociationResult):
            evidence = evidence_from_result(result)
            mode = "reporter"
        else:
            _fail("reporter input must be an association result")
    except ValueError as error:
        _fail(str(error))
    config = (
        "[agent]\n"
        f"mode = {json.dumps(mode)}\n"
        f"input = {json.dumps(canonical_json(evidence))}\n"
    )
    _write(Path(args.output), config)


def _prepare_investigator(args: argparse.Namespace) -> None:
    """Create an aggregate-only investigator input; no site result is included."""
    try:
        result = parse_released_result_json(Path(args.result).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        _fail(str(error))
    if not isinstance(result, AssociationResult):
        _fail("investigator input must be a primary association result")
    decision = investigation_decision_from_result(result)
    decision_path = Path(args.output).with_name("investigation-decision.json")
    _write(decision_path, canonical_json(decision) + "\n")
    if decision.decision != "INVESTIGATE":
        _fail(
            f"follow-up decision is {decision.decision}; no investigator run was prepared"
        )
    try:
        evidence = evidence_from_result(result)
    except ValueError as error:
        _fail(str(error))
    _write(Path(args.output), "[agent]\nmode = \"investigator\"\n" + f"input = {json.dumps(canonical_json(evidence))}\n")


def _collect_investigator(args: argparse.Namespace) -> None:
    """Collect one schema-valid, parent-bound Endeavor proposal from logs."""
    try:
        parent = parse_released_result_json(
            Path(args.parent_result).read_text(encoding="utf-8")
        )
        if not isinstance(parent, AssociationResult):
            _fail("investigator parent must be a primary association result")
        parent_hash = evidence_from_result(parent).parent_result_hash
        assert parent_hash is not None
        with Path(args.log).open(encoding="utf-8") as handle:
            proposal = extract_followup_proposal(
                handle, expected_parent_result_hash=parent_hash
            )
    except (OSError, ValueError, AgentFrameError) as error:
        _fail(str(error))
    _write(Path(args.output), canonical_json(proposal) + "\n")


def _prepare_phase2(args: argparse.Namespace) -> None:
    """Compile a proposed follow-up only when a named operator approves it."""
    try:
        parent = parse_released_result_json(Path(args.parent_result).read_text(encoding="utf-8"))
        proposal = FollowUpProposal.model_validate_json(Path(args.proposal).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        _fail(str(error))
    if not isinstance(parent, AssociationResult):
        _fail("follow-up parent must be a primary association result")
    try:
        contract = compile_stratified_followup(parent, proposal)
    except ValueError as error:
        _fail(str(error))
    now = datetime.now(timezone.utc).replace(microsecond=0)
    approval = Approval(
        approval_id=f"{contract.analysis_id}-approval",
        study_id=contract.study_id, analysis_id=contract.analysis_id,
        contract_hash=contract_digest(contract), approved_by=args.approved_by,
        scope=(
            "PUBLIC_GDSC_EMULATED_FEDERATION"
            if contract.variant.reference_build == "CMP_DRIVER_SUMMARY_PINNED"
            else "SYNTHETIC_DEMO"
        ),
        approved_at=now, expires_at=now + timedelta(hours=args.valid_hours),
    )
    output_dir = Path(args.output_dir)
    _write(output_dir / "followup-contract.json", canonical_json(contract) + "\n")
    _write(output_dir / "followup-approval.json", canonical_json(approval) + "\n")
    _write(output_dir / "followup-proposal.json", canonical_json(proposal) + "\n")
    run_config = (
        f"contract-json = {json.dumps(canonical_json(contract))}\n"
        f"approval-json = {json.dumps(canonical_json(approval))}\n"
        f"followup-proposal-json = {json.dumps(canonical_json(proposal))}\n"
        f"query-id = {json.dumps(contract.analysis_id)}\nnode-wait-seconds = 30.0\nreply-timeout-seconds = 30.0\n"
    )
    _write(output_dir / "phase2-analytics-run.toml", run_config)
    print(json.dumps({"contract_hash": approval.contract_hash, "parent_result_hash": contract.parent_result_hash, "approved_by": args.approved_by, "output_dir": str(output_dir)}, indent=2))


def _report(args: argparse.Namespace) -> None:
    try:
        result = parse_released_result_json(
            Path(args.result).read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as error:
        _fail(str(error))
    if isinstance(result, Phase2AssociationResult):
        _write(Path(args.output), render_phase2_report(result))
        return
    if not isinstance(result, AssociationResult):
        _fail("HTML reports require an association result")
    _write(
        Path(args.output),
        render_report(
            result,
            run_id=args.run_id,
            agent_model=args.agent_model,
            flower_version=args.flower_version,
            agent_fab=args.agent_fab,
            analytics_fab=args.analytics_fab,
        ),
    )


def _inspect_fab(args: argparse.Namespace) -> None:
    try:
        members = inspect_fab(Path(args.fab), tuple(args.forbid_text))
    except (OSError, FabInspectionError) as error:
        _fail(str(error))
    print(json.dumps({"fab": args.fab, "members": members}, indent=2))


def build_parser() -> argparse.ArgumentParser:
    """Build the stable operator CLI surface."""
    parser = argparse.ArgumentParser(prog="cohortlens")
    commands = parser.add_subparsers(dest="command", required=True)

    reference = commands.add_parser(
        "reference", help="compute the offline three-site synthetic reference"
    )
    reference.add_argument("--output")
    reference.set_defaults(handler=_reference)

    prepare = commands.add_parser(
        "prepare-phase1", help="create and explicitly approve the fixed demo contract"
    )
    prepare.add_argument("--output-dir", required=True)
    prepare.add_argument("--approved-by", required=True)
    prepare.add_argument("--valid-hours", type=int, default=24)
    prepare.set_defaults(handler=_prepare_phase1)

    deployment_data = commands.add_parser(
        "prepare-supernode-data",
        help="create three separate synthetic snapshots for deployment SuperNodes",
    )
    deployment_data.add_argument("--output-dir", required=True)
    deployment_data.add_argument("--overwrite", action="store_true")
    deployment_data.set_defaults(handler=_prepare_supernode_data)

    gdsc = commands.add_parser(
        "prepare-gdsc",
        help="adapt public GDSC2 PLX-4720 data into three local snapshots",
    )
    gdsc.add_argument("--responses", required=True)
    gdsc.add_argument("--models", required=True)
    gdsc.add_argument("--mutations", required=True)
    gdsc.add_argument("--output-dir", required=True)
    gdsc.add_argument("--approved-by", required=True)
    gdsc.add_argument("--valid-hours", type=int, default=24)
    gdsc.add_argument("--overwrite", action="store_true")
    gdsc.set_defaults(handler=_prepare_gdsc)

    collect = commands.add_parser(
        "collect", help="extract a validated result frame from saved Flower logs"
    )
    collect.add_argument("--log", required=True)
    collect.add_argument("--study-id", required=True)
    collect.add_argument("--query-id", required=True)
    collect.add_argument("--contract-hash", required=True)
    collect.add_argument("--output")
    collect.set_defaults(handler=_collect)

    reporter = commands.add_parser(
        "prepare-reporter", help="create an aggregate-only Reporter AgentApp config"
    )
    reporter.add_argument("--result", required=True)
    reporter.add_argument("--output", required=True)
    reporter.set_defaults(handler=_prepare_reporter)

    investigator = commands.add_parser("prepare-investigator", help="create an aggregate-only investigator AgentApp config")
    investigator.add_argument("--result", required=True)
    investigator.add_argument("--output", required=True)
    investigator.set_defaults(handler=_prepare_investigator)

    collect_investigator = commands.add_parser(
        "collect-investigator",
        help="extract a validated parent-bound investigator proposal from saved logs",
    )
    collect_investigator.add_argument("--log", required=True)
    collect_investigator.add_argument("--parent-result", required=True)
    collect_investigator.add_argument("--output", required=True)
    collect_investigator.set_defaults(handler=_collect_investigator)

    phase2 = commands.add_parser("prepare-phase2", help="explicitly approve and compile one bounded context stratification")
    phase2.add_argument("--parent-result", required=True)
    phase2.add_argument("--proposal", required=True)
    phase2.add_argument("--output-dir", required=True)
    phase2.add_argument("--approved-by", required=True)
    phase2.add_argument("--valid-hours", type=int, default=24)
    phase2.set_defaults(handler=_prepare_phase2)

    report = commands.add_parser("report", help="render a static Phase 1 HTML report")
    report.add_argument("--result", required=True)
    report.add_argument("--output", required=True)
    report.add_argument("--run-id", default="not-recorded")
    report.add_argument("--agent-model", default="flower-endeavor")
    report.add_argument("--flower-version", default="not-recorded")
    report.add_argument("--agent-fab", default="not-recorded")
    report.add_argument("--analytics-fab", default="not-recorded")
    report.set_defaults(handler=_report)

    inspect = commands.add_parser(
        "inspect-fab", help="check FAB contents and forbidden sentinel strings"
    )
    inspect.add_argument("fab")
    inspect.add_argument("--forbid-text", action="append", default=[])
    inspect.set_defaults(handler=_inspect_fab)
    return parser


def main() -> None:
    """Run the CohortLens operator CLI."""
    args = build_parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
