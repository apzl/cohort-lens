"""Operator CLI: argument parsing for step-by-step and orchestrated workflows."""

from __future__ import annotations

import argparse

from cohortlens_shared.gdsc import LINEAGE_ENRICHED_SIMULATION

from . import commands
from .study_commands import add_study_commands


def _add_rounds(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--round",
        nargs=2,
        action="append",
        metavar=("RESULT", "CONTRACT"),
        help="released result and its contract; repeat in order, primary first",
    )
    parser.add_argument("--result", help="shorthand for a lone primary result with a default contract")


def _add_preparation(commands_: argparse._SubParsersAction) -> None:
    reference = commands_.add_parser("reference", help="compute the offline three-site synthetic reference")
    reference.add_argument("--output")
    reference.set_defaults(handler=commands.reference)

    prepare = commands_.add_parser(
        "prepare-synthetic",
        aliases=["prepare-phase1"],
        help="create and approve the synthetic primary contract",
    )
    prepare.add_argument("--output-dir", required=True)
    prepare.add_argument("--approved-by", required=True)
    prepare.add_argument("--valid-hours", type=int, default=24)
    prepare.add_argument("--followup-budget", type=int, default=2, choices=[0, 1, 2])
    prepare.set_defaults(handler=commands.prepare_synthetic)

    deployment = commands_.add_parser(
        "prepare-supernode-data", help="create three separate synthetic snapshots for deployment SuperNodes"
    )
    deployment.add_argument("--output-dir", required=True)
    deployment.add_argument("--overwrite", action="store_true")
    deployment.set_defaults(handler=commands.prepare_supernode_data)

    gdsc = commands_.add_parser(
        "prepare-gdsc", help="adapt public GDSC2 PLX-4720 data into three local snapshots"
    )
    for name in ("--responses", "--models", "--mutations", "--output-dir", "--approved-by"):
        gdsc.add_argument(name, required=True)
    gdsc.add_argument("--valid-hours", type=int, default=24)
    gdsc.add_argument("--followup-budget", type=int, default=2, choices=[0, 1, 2])
    gdsc.add_argument("--overwrite", action="store_true")
    gdsc.add_argument(
        "--simulation-split",
        choices=[LINEAGE_ENRICHED_SIMULATION],
        help="emulate non-IID lineage-enriched sites to exercise the heterogeneity workflow",
    )
    gdsc.set_defaults(handler=commands.prepare_gdsc)


def _add_collection(commands_: argparse._SubParsersAction) -> None:
    collect = commands_.add_parser("collect", help="extract a validated result frame from saved Flower logs")
    for name in ("--log", "--study-id", "--query-id", "--contract-hash"):
        collect.add_argument(name, required=True)
    collect.add_argument("--output")
    collect.set_defaults(handler=commands.collect)

    investigator = commands_.add_parser(
        "prepare-investigator",
        help="apply the gate; write aggregate-only evidence and an investigator config",
    )
    _add_rounds(investigator)
    investigator.add_argument("--output", required=True)
    investigator.add_argument("--tool-protocol", choices=["json", "native"], default="json")
    investigator.set_defaults(handler=commands.prepare_investigator)

    collect_investigator = commands_.add_parser(
        "collect-investigator", help="extract a validated, evidence-bound investigator record from saved logs"
    )
    collect_investigator.add_argument("--log", required=True)
    collect_investigator.add_argument("--evidence", required=True)
    collect_investigator.add_argument("--output", required=True)
    collect_investigator.set_defaults(handler=commands.collect_investigator)


def _add_followup(commands_: argparse._SubParsersAction) -> None:
    followup = commands_.add_parser(
        "prepare-followup",
        aliases=["prepare-phase2"],
        help="approve and compile the contract a reviewed proposal permits",
    )
    for name in ("--evidence", "--parent-contract", "--proposal", "--output-dir", "--approved-by"):
        followup.add_argument(name, required=True)
    followup.add_argument("--valid-hours", type=int, default=24)
    followup.set_defaults(handler=commands.prepare_followup)

    reporter = commands_.add_parser(
        "prepare-reporter", help="create an aggregate-only reporter AgentApp config"
    )
    _add_rounds(reporter)
    reporter.add_argument("--output", required=True)
    reporter.set_defaults(handler=commands.prepare_reporter)

    report = commands_.add_parser("report", help="render a static HTML report for one released result")
    report.add_argument("--result", required=True)
    report.add_argument("--output", required=True)
    for name in ("--run-id", "--flower-version", "--agent-fab", "--analytics-fab"):
        report.add_argument(name, default="not-recorded")
    report.add_argument("--agent-model", default="flower-endeavor")
    report.set_defaults(handler=commands.report)

    inspect = commands_.add_parser("inspect-fab", help="check FAB contents and forbidden sentinel strings")
    inspect.add_argument("fab")
    inspect.add_argument("--forbid-text", action="append", default=[])
    inspect.set_defaults(handler=commands.inspect_fab_command)


def build_parser() -> argparse.ArgumentParser:
    """Build the stable operator CLI surface."""
    parser = argparse.ArgumentParser(prog="cohortlens")
    commands_ = parser.add_subparsers(dest="command", required=True)
    _add_preparation(commands_)
    _add_collection(commands_)
    _add_followup(commands_)
    add_study_commands(commands_)
    return parser


def main() -> None:
    """Run the CohortLens operator CLI."""
    args = build_parser().parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
