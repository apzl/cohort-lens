"""``run-study`` and ``verify-ledger`` commands."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, NoReturn

from cohortlens_shared.investigator import LoopLimits
from cohortlens_shared.schemas import (
    AnalysisContract,
    default_gdsc_contract,
    default_phase1_contract,
)

from .approvals import Approver, InteractiveApprover, ScriptedApprover
from .backends import InvestigatorSettings, LocalBackend, StudyBackend, SuperGridBackend
from .ledger import Ledger, LedgerError, read_entries, verify_ledger
from .local_grid import SiteSnapshot, npz_sites, synthetic_sites
from .model_clients import InvestigatorClient, openai_compatible_client, scripted_client
from .orchestrator import StudyOrchestrator, StudyPlan
from .report import render_study_report


def _fail(message: str) -> NoReturn:
    raise SystemExit(message)


def _gdsc_manifest(snapshots: Path) -> dict[str, Any]:
    try:
        return json.loads((snapshots / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        _fail(f"cannot read the snapshot manifest: {error}")


def _split_name(manifest: dict[str, Any]) -> str | None:
    split = manifest.get("simulation_split")
    return split.get("name") if isinstance(split, dict) else None


def _primary_contract(
    args: argparse.Namespace,
) -> tuple[AnalysisContract, dict[str, Any], list[SiteSnapshot] | None]:
    """Primary contract, a path-free description, and local sites (local backend only)."""
    budget = args.followup_budget
    if args.data_mode == "synthetic":
        description = {"data_mode": "SYNTHETIC", "snapshot_set": "generated-synthetic-v1"}
        sites = synthetic_sites() if args.backend == "local" else None
        return default_phase1_contract(followup_budget=budget), description, sites
    if args.snapshots is None:
        _fail("--snapshots is required for --data-mode gdsc")
    snapshots = Path(args.snapshots)
    manifest = _gdsc_manifest(snapshots)
    split = _split_name(manifest)
    set_name = split or "hash-split-v1"
    provenance = {key: manifest.get(key) for key in ("gdsc_release", "assignment_sha256")}
    description = {
        "data_mode": "PUBLIC_GDSC",
        "snapshot_set": set_name,
        **{key: value for key, value in provenance.items() if value is not None},
    }
    sites = None
    if args.backend == "local":
        sites = npz_sites(snapshots, f"gdsc-{set_name}")
        description["snapshot_sha256"] = {site.site_alias: site.sha256 for site in sites}
    contract = default_gdsc_contract(simulation_split=split, followup_budget=budget)
    return contract, description, sites


def _client(args: argparse.Namespace) -> InvestigatorClient:
    if args.investigator == "scripted":
        return scripted_client()
    if not (args.model_base_url and args.model_name):
        _fail("--investigator openai-compatible needs --model-base-url and --model-name")
    try:
        return openai_compatible_client(args.model_base_url, args.model_name, args.model_api_key_env)
    except ValueError as error:
        _fail(str(error))


def _settings(args: argparse.Namespace) -> InvestigatorSettings:
    limits = LoopLimits(
        max_steps=args.max_steps,
        max_tool_calls=args.max_tool_calls,
        max_invalid_turns=args.max_invalid_turns,
    )
    return InvestigatorSettings(args.tool_protocol, not args.no_protocol_fallback, limits)


def _backend(args: argparse.Namespace, sites: list[SiteSnapshot] | None) -> tuple[StudyBackend, str, str]:
    """Return the backend plus the policy kind and label it will record."""
    settings = _settings(args)
    if args.backend == "supergrid":
        if args.investigator != "agentapp":
            _fail(
                "the supergrid backend runs the investigator as the Flower AgentApp "
                "(--investigator agentapp)"
            )
        if not args.federation:
            _fail("--federation is required for the supergrid backend")
        backend = SuperGridBackend(
            federation=args.federation,
            work_dir=Path(args.output_dir),
            analytics_app=args.analytics_app,
            agent_app=args.agent_app,
            settings=settings,
        )
        return backend, "MODEL", "flower-endeavor"
    if args.investigator == "agentapp":
        _fail("--investigator agentapp requires --backend supergrid")
    client = _client(args)
    assert sites is not None
    backend = LocalBackend(
        sites, client.transport, client.model, client.policy_kind, client.policy_label, settings
    )
    return backend, client.policy_kind, client.policy_label


def _approver(args: argparse.Namespace) -> Approver:
    if args.approve_as:
        return ScriptedApprover(args.approve_as)
    if not args.approver:
        _fail("give --approver NAME for interactive approval, or --approve-as NAME for scripted runs")
    return InteractiveApprover(args.approver)


def run_study(args: argparse.Namespace) -> None:
    """Drive primary -> gate -> investigator -> approval -> follow-ups, with a ledger."""
    contract, description, sites = _primary_contract(args)
    backend, policy_kind, policy_label = _backend(args, sites)
    approver = _approver(args)
    description |= {
        "investigator": args.investigator,
        "tool_protocol": args.tool_protocol,
        "protocol_fallback": not args.no_protocol_fallback,
        "loop_limits": _settings(args).limits.model_dump(),
    }
    try:
        ledger = Ledger(Path(args.ledger), contract.study_id)
    except LedgerError as error:
        _fail(str(error))
    plan = StudyPlan(contract, policy_kind, policy_label, args.valid_hours, description)
    output_dir = Path(args.output_dir)
    outcome = StudyOrchestrator(plan, backend, approver, ledger, output_dir=output_dir).run()
    report_path = output_dir / "study-report.html"
    report_path.write_text(render_study_report(read_entries(Path(args.ledger))), encoding="utf-8")
    report = verify_ledger(Path(args.ledger))
    print(json.dumps({
        "finish_reason": outcome.finish_reason,
        "rounds_run": len(outcome.rounds),
        "policy_kind": policy_kind,
        "policy_label": policy_label,
        "ledger_entries": report.entries,
        "ledger_head": report.head_hash,
        "report": str(report_path),
    }, indent=2))


def verify_ledger_command(args: argparse.Namespace) -> None:
    """Verify a ledger's hash chain and print a summary."""
    try:
        report = verify_ledger(Path(args.ledger))
    except (OSError, LedgerError) as error:
        _fail(f"ledger verification failed: {error}")
    print(json.dumps({
        "valid": True,
        "entries": report.entries,
        "head_hash": report.head_hash,
        "entry_types": report.entry_types,
    }, indent=2))


def add_study_commands(commands: argparse._SubParsersAction) -> None:
    """Register ``run-study`` and ``verify-ledger``."""
    study = commands.add_parser(
        "run-study", help="drive the full investigation loop with a hash-chained ledger"
    )
    study.add_argument("--data-mode", choices=["synthetic", "gdsc"], required=True)
    study.add_argument(
        "--snapshots", help="prepared GDSC snapshot directory (site-{1,2,3}.npz + manifest.json)"
    )
    study.add_argument("--backend", choices=["local", "supergrid"], default="local")
    study.add_argument(
        "--investigator", choices=["scripted", "openai-compatible", "agentapp"], default="scripted"
    )
    study.add_argument("--tool-protocol", choices=["json", "native"], default="json")
    study.add_argument("--no-protocol-fallback", action="store_true")
    study.add_argument("--max-steps", type=int, default=12)
    study.add_argument("--max-tool-calls", type=int, default=16)
    study.add_argument("--max-invalid-turns", type=int, default=3)
    study.add_argument("--model-base-url")
    study.add_argument("--model-name")
    study.add_argument("--model-api-key-env", default="COHORTLENS_MODEL_API_KEY")
    study.add_argument("--federation", help="@ACCOUNT/FEDERATION for the supergrid backend")
    study.add_argument("--analytics-app", default="apps/cohort-analytics")
    study.add_argument("--agent-app", default="apps/cohort-agent")
    study.add_argument("--followup-budget", type=int, default=2, choices=[0, 1, 2])
    study.add_argument("--valid-hours", type=int, default=24)
    study.add_argument("--approver", help="name of the human approving interactively")
    study.add_argument(
        "--approve-as", help="NON-INTERACTIVE: approve every contract as NAME (demos/tests only)"
    )
    study.add_argument("--ledger", required=True)
    study.add_argument("--output-dir", required=True)
    study.set_defaults(handler=run_study)

    verify = commands.add_parser("verify-ledger", help="verify a run ledger's hash chain")
    verify.add_argument("ledger")
    verify.set_defaults(handler=verify_ledger_command)
