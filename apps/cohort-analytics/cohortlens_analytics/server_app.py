"""Coordinator workflows for CohortLens Phase 0 and Phase 1."""

from __future__ import annotations

import time
from dataclasses import dataclass
from logging import INFO

from flwr.app import ConfigRecord, Context, Message, RecordDict
from flwr.serverapp import Grid, ServerApp
from flwr.supercore import log

from cohortlens_shared.schemas import (
    AnalysisContract,
    Approval,
    AssociationReply,
    AssociationRequest,
    AssociationResult,
    FollowUpProposal,
    Phase2AssociationResult,
    PreflightReply,
    PreflightRequest,
    ResultEnvelope,
    SiteState,
    StudyPreflightRequest,
    canonical_json,
    contract_digest,
    default_phase1_approval,
    default_phase1_contract,
    proposal_digest,
    validate_contract_approval,
)
from cohortlens_shared.statistics import SiteEstimate, fixed_effect_meta

RESULT_FRAME_PREFIX = "COHORTLENS_RESULT_V1 "
DEFAULT_PHASE1_TOKEN = "DEFAULT_SYNTHETIC_PHASE1"
REQUIRED_INPUT_PREFIX = "REQUIRED_OPERATOR_"
app = ServerApp()


@dataclass(frozen=True)
class Phase0Config:
    """Retained settings for the Phase 0 preflight regression test."""

    study_id: str
    query_id: str
    contract_hash: str
    expected_sites: int = 3
    node_wait_seconds: float = 30.0
    reply_timeout_seconds: float = 30.0

    def request(self) -> PreflightRequest:
        if not 1 <= self.expected_sites <= 256:
            raise ValueError("expected_sites must be between 1 and 256")
        if self.node_wait_seconds < 0 or self.reply_timeout_seconds <= 0:
            raise ValueError("timeouts must be positive")
        return PreflightRequest(
            study_id=self.study_id,
            query_id=self.query_id,
            contract_hash=self.contract_hash,
        )


@dataclass(frozen=True)
class Phase1Config:
    """Approved Phase 1 contract plus bounded transport settings."""

    contract: AnalysisContract
    approval: Approval
    query_id: str
    node_wait_seconds: float = 30.0
    reply_timeout_seconds: float = 30.0

    def validate(self) -> None:
        validate_contract_approval(self.contract, self.approval)
        if self.query_id != self.contract.analysis_id:
            raise ValueError("query_id must match the approved analysis_id")
        if self.node_wait_seconds < 0 or self.reply_timeout_seconds <= 0:
            raise ValueError("timeouts must be positive")


def config_from_context(context: Context) -> Phase0Config:
    """Read retained Phase 0 run configuration keys."""
    return Phase0Config(
        study_id=str(context.run_config["study-id"]),
        query_id=str(context.run_config["query-id"]),
        contract_hash=str(context.run_config["contract-hash"]),
        expected_sites=int(context.run_config["expected-sites"]),
        node_wait_seconds=float(context.run_config["node-wait-seconds"]),
        reply_timeout_seconds=float(context.run_config["reply-timeout-seconds"]),
    )


def phase1_config_from_context(context: Context) -> Phase1Config:
    """Decode an inline approved contract; remote filesystem paths are forbidden."""
    contract_payload = str(context.run_config["contract-json"])
    approval_payload = str(context.run_config["approval-json"])
    if contract_payload.startswith(REQUIRED_INPUT_PREFIX) or approval_payload.startswith(
        REQUIRED_INPUT_PREFIX
    ):
        raise ValueError(
            "Phase 1 requires operator-generated contract-json and approval-json"
        )
    contract = (
        default_phase1_contract()
        if contract_payload == DEFAULT_PHASE1_TOKEN
        else AnalysisContract.model_validate_json(contract_payload)
    )
    approval = (
        default_phase1_approval(contract)
        if approval_payload == DEFAULT_PHASE1_TOKEN
        else Approval.model_validate_json(approval_payload)
    )
    config = Phase1Config(
        contract=contract,
        approval=approval,
        query_id=str(context.run_config["query-id"]),
        node_wait_seconds=float(context.run_config["node-wait-seconds"]),
        reply_timeout_seconds=float(context.run_config["reply-timeout-seconds"]),
    )
    config.validate()
    return config


def wait_for_nodes(grid: Grid, expected_sites: int, timeout: float) -> list[int]:
    """Wait for the required node count, with a bounded deadline."""
    deadline = time.monotonic() + timeout
    while True:
        node_ids = sorted(set(grid.get_node_ids()))
        if len(node_ids) >= expected_sites:
            return node_ids[:expected_sites]
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"expected {expected_sites} nodes, found {len(node_ids)} before timeout"
            )
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))


def _record_message(
    node_id: int,
    *,
    record_name: str,
    payload: str,
    message_type: str,
    group_id: str,
    timeout: float,
) -> Message:
    return Message(
        content=RecordDict({record_name: ConfigRecord({"json": payload})}),
        dst_node_id=node_id,
        message_type=message_type,
        group_id=group_id,
        ttl=max(1.0, timeout + 5.0),
    )


def _payload(message: Message, record_name: str) -> str:
    if message.has_error():
        raise ValueError(f"site returned an error: {message.error}")
    record = message.content.get(record_name)
    if not isinstance(record, ConfigRecord):
        raise ValueError(f"{record_name} ConfigRecord is required")
    payload = record.get("json")
    if not isinstance(payload, str):
        raise ValueError(f"{record_name}.json must be a string")
    return payload


def _source_node_id(message: Message) -> int:
    return int(message.metadata.src_node_id)


def _validated_preflight_reply(
    message: Message,
    *,
    study_id: str,
    query_id: str,
    expected_contract_hash: str,
) -> PreflightReply:
    reply = PreflightReply.model_validate_json(_payload(message, "preflight_reply"))
    if (
        reply.study_id != study_id
        or reply.query_id != query_id
        or reply.contract_hash != expected_contract_hash
    ):
        raise ValueError("preflight reply does not match the approved request")
    return reply


def _validated_association_reply(
    message: Message, request: AssociationRequest
) -> AssociationReply:
    reply = AssociationReply.model_validate_json(
        _payload(message, "association_reply")
    )
    if (
        reply.study_id != request.contract.study_id
        or reply.analysis_id != request.contract.analysis_id
        or reply.query_id != request.query_id
        or reply.contract_hash != contract_digest(request.contract)
        or reply.stratum_id != request.stratum_id
    ):
        raise ValueError("association reply does not match the approved request")
    return reply


def run_preflight(grid: Grid, config: Phase0Config) -> ResultEnvelope:
    """Execute the retained Phase 0 distributed readiness check."""
    request = config.request()
    node_ids = wait_for_nodes(grid, config.expected_sites, config.node_wait_seconds)
    messages = [
        _record_message(
            node_id,
            record_name="preflight_request",
            payload=canonical_json(request),
            message_type="query.preflight",
            group_id=request.query_id,
            timeout=config.reply_timeout_seconds,
        )
        for node_id in node_ids
    ]
    replies = list(
        grid.send_and_receive(messages, timeout=config.reply_timeout_seconds)
    )
    by_site: dict[str, PreflightReply] = {}
    for message in replies:
        try:
            reply = _validated_preflight_reply(
                message,
                study_id=request.study_id,
                query_id=request.query_id,
                expected_contract_hash=request.contract_hash,
            )
        except ValueError as error:
            log(INFO, "Ignoring invalid preflight reply: %s", error)
            continue
        if reply.status != "READY":
            continue
        if reply.site_alias in by_site:
            raise ValueError(f"duplicate site alias: {reply.site_alias}")
        by_site[reply.site_alias] = reply
    ready_sites = sorted(by_site)
    result = ResultEnvelope(
        study_id=request.study_id,
        query_id=request.query_id,
        contract_hash=request.contract_hash,
        expected_sites=config.expected_sites,
        responding_sites=len(ready_sites),
        ready_sites=ready_sites,
        status="COMPLETE" if len(ready_sites) == config.expected_sites else "PARTIAL",
    )
    print(f"{RESULT_FRAME_PREFIX}{canonical_json(result)}", flush=True)
    return result


def run_phase1(
    grid: Grid, config: Phase1Config, *, stratum_id: str = "all", emit: bool = True
) -> AssociationResult:
    """Run preflight, local regressions, and fixed-effect meta-analysis."""
    config.validate()
    contract_hash = contract_digest(config.contract)
    node_ids = wait_for_nodes(
        grid, config.contract.expected_sites, config.node_wait_seconds
    )
    preflight_request = StudyPreflightRequest(
        query_id=config.query_id,
        contract=config.contract,
        approval=config.approval,
    )
    preflight_messages = [
        _record_message(
            node_id,
            record_name="preflight_request",
            payload=canonical_json(preflight_request),
            message_type="query.preflight",
            group_id=f"{config.query_id}:preflight",
            timeout=config.reply_timeout_seconds,
        )
        for node_id in node_ids
    ]
    preflight_replies = list(
        grid.send_and_receive(
            preflight_messages, timeout=config.reply_timeout_seconds
        )
    )

    selected_node_ids = set(node_ids)
    states: dict[str, SiteState] = {
        f"node-{node_id}": SiteState(
            site_alias=f"node-{node_id}", stage="PREFLIGHT", status="NO_RESPONSE"
        )
        for node_id in node_ids
    }
    ready_nodes: dict[str, int] = {}
    preflight_alias_by_node: dict[int, str] = {}
    responding_nodes: set[int] = set()
    for message in preflight_replies:
        node_id = _source_node_id(message)
        if node_id not in selected_node_ids:
            log(INFO, "Ignoring preflight reply from unselected node %s", node_id)
            continue
        responding_nodes.add(node_id)
        placeholder = f"node-{node_id}"
        try:
            reply = _validated_preflight_reply(
                message,
                study_id=config.contract.study_id,
                query_id=config.query_id,
                expected_contract_hash=contract_hash,
            )
        except ValueError:
            states[placeholder] = SiteState(
                site_alias=placeholder, stage="PREFLIGHT", status="INVALID_REPLY"
            )
            continue
        previous_alias = preflight_alias_by_node.get(node_id)
        if previous_alias is not None:
            ready_nodes.pop(previous_alias, None)
            states[previous_alias] = SiteState(
                site_alias=previous_alias,
                stage="PREFLIGHT",
                status="DUPLICATE_REPLY",
                reason_code="DUPLICATE_NODE_RESPONSE",
            )
            continue
        preflight_alias_by_node[node_id] = reply.site_alias
        states.pop(placeholder, None)
        if reply.site_alias in states or reply.site_alias in ready_nodes:
            ready_nodes.pop(reply.site_alias, None)
            states[reply.site_alias] = SiteState(
                site_alias=reply.site_alias,
                stage="PREFLIGHT",
                status="DUPLICATE_REPLY",
            )
            continue
        states[reply.site_alias] = SiteState(
            site_alias=reply.site_alias,
            stage="PREFLIGHT",
            status=reply.status,
            reason_code=reply.reason_code,
        )
        if reply.status == "READY":
            ready_nodes[reply.site_alias] = node_id

    association_request = AssociationRequest(
        query_id=config.query_id,
        contract=config.contract,
        approval=config.approval,
        stratum_id=stratum_id,
    )
    association_messages = [
        _record_message(
            node_id,
            record_name="association_request",
            payload=canonical_json(association_request),
            message_type="query.associate",
            group_id=f"{config.query_id}:associate",
            timeout=config.reply_timeout_seconds,
        )
        for _, node_id in sorted(ready_nodes.items())
    ]
    association_replies = list(
        grid.send_and_receive(
            association_messages, timeout=config.reply_timeout_seconds
        )
    )
    by_site: dict[str, AssociationReply] = {}
    association_responders: set[int] = set()
    association_alias_by_node = {
        node_id: alias for alias, node_id in ready_nodes.items()
    }
    for message in association_replies:
        node_id = _source_node_id(message)
        expected_alias = association_alias_by_node.get(node_id)
        if expected_alias is None:
            log(INFO, "Ignoring association reply from unselected node %s", node_id)
            continue
        association_responders.add(node_id)
        try:
            reply = _validated_association_reply(message, association_request)
        except ValueError:
            states[expected_alias] = SiteState(
                site_alias=expected_alias,
                stage="ASSOCIATION",
                status="INVALID_REPLY",
            )
            continue
        if reply.site_alias != expected_alias:
            states[expected_alias] = SiteState(
                site_alias=expected_alias,
                stage="ASSOCIATION",
                status="INVALID_REPLY",
                reason_code="SITE_IDENTITY_MISMATCH",
            )
            continue
        if reply.site_alias in by_site:
            by_site.pop(reply.site_alias, None)
            states[reply.site_alias] = SiteState(
                site_alias=reply.site_alias,
                stage="ASSOCIATION",
                status="DUPLICATE_REPLY",
            )
            continue
        by_site[reply.site_alias] = reply
        states[reply.site_alias] = SiteState(
            site_alias=reply.site_alias,
            stage="ASSOCIATION",
            status=reply.status,
            reason_code=reply.reason_code,
        )

    for alias, node_id in ready_nodes.items():
        if node_id not in association_responders:
            states[alias] = SiteState(
                site_alias=alias, stage="ASSOCIATION", status="NO_RESPONSE"
            )

    site_results = tuple(by_site[key] for key in sorted(by_site))
    estimates = [
        SiteEstimate(
            site_alias=reply.site_alias,
            beta=reply.beta,
            standard_error=reply.standard_error,
            n_used=reply.n_used,
        )
        for reply in site_results
        if reply.status == "ESTIMATED"
        and reply.beta is not None
        and reply.standard_error is not None
        and reply.n_used is not None
    ]
    aggregate = fixed_effect_meta(estimates) if len(estimates) >= 2 else None
    estimated_sites = len(estimates)
    status = (
        "INSUFFICIENT"
        if estimated_sites < 2
        else "COMPLETE"
        if estimated_sites == config.contract.expected_sites
        else "PARTIAL"
    )
    result = AssociationResult(
        study_id=config.contract.study_id,
        analysis_id=config.contract.analysis_id,
        query_id=config.query_id,
        contract_hash=contract_hash,
        expected_sites=config.contract.expected_sites,
        responding_sites=len(responding_nodes),
        estimated_sites=estimated_sites,
        site_states=tuple(states[key] for key in sorted(states)),
        site_results=site_results,
        beta_meta=None if aggregate is None else aggregate.beta,
        standard_error_meta=None if aggregate is None else aggregate.standard_error,
        ci95_low=None if aggregate is None else aggregate.ci95_low,
        ci95_high=None if aggregate is None else aggregate.ci95_high,
        q=None if aggregate is None else aggregate.q,
        q_degrees_freedom=None if aggregate is None else aggregate.sites - 1,
        i2=None if aggregate is None else aggregate.i2,
        heterogeneity_flag=None if aggregate is None else aggregate.i2 >= 0.50,
        status=status,
        phenotype_unit=config.contract.phenotype_unit,
        data_provenance=(
            "GDSC2_8.5_CMP_DRIVER_SUMMARY_PINNED"
            if config.contract.variant.reference_build == "CMP_DRIVER_SUMMARY_PINNED"
            else "COHORTLENS_PUBLIC_GENERATOR_V1"
        ),
        synthetic=config.contract.variant.reference_build == "SYNTHETIC_V1",
    )
    if emit:
        print(f"{RESULT_FRAME_PREFIX}{canonical_json(result)}", flush=True)
    return result


def run_phase2(
    grid: Grid, config: Phase1Config, proposal: FollowUpProposal
) -> Phase2AssociationResult:
    """Run exactly two predeclared local context strata after explicit approval."""
    if config.contract.strata not in {
        ("context_0", "context_1"),
        ("non_skcm", "skcm"),
    }:
        raise ValueError("Phase 2 requires an approved pair of fixed strata")
    if config.contract.parent_result_hash != proposal.parent_result_hash:
        raise ValueError("approved contract is not bound to the proposal parent result")
    expected_action = (
        "STRATIFY_GDSC_LINEAGE"
        if config.contract.strata == ("non_skcm", "skcm")
        else "STRATIFY_SYNTHETIC_CONTEXT"
    )
    if proposal.action != expected_action:
        raise ValueError("only an approved stratification proposal can start a second run")
    results = tuple(
        run_phase1(grid, config, stratum_id=stratum, emit=False)
        for stratum in config.contract.strata
    )
    status = (
        "INSUFFICIENT" if any(item.status == "INSUFFICIENT" for item in results)
        else "PARTIAL" if any(item.status == "PARTIAL" for item in results)
        else "COMPLETE"
    )
    result = Phase2AssociationResult(
        study_id=config.contract.study_id,
        analysis_id=config.contract.analysis_id,
        query_id=config.query_id,
        contract_hash=contract_digest(config.contract),
        parent_result_hash=config.contract.parent_result_hash,
        proposal_hash=proposal_digest(proposal),
        strata_results=results,
        status=status,
    )
    print(f"{RESULT_FRAME_PREFIX}{canonical_json(result)}", flush=True)
    return result


@app.main()
def main(grid: Grid, context: Context) -> None:
    """Execute the approved primary or bounded Phase 2 association workflow."""
    config = phase1_config_from_context(context)
    proposal_payload = context.run_config.get("followup-proposal-json")
    if config.contract.strata:
        if not isinstance(proposal_payload, str) or proposal_payload.startswith(REQUIRED_INPUT_PREFIX):
            raise ValueError("Phase 2 requires the exact operator-approved followup proposal")
        proposal = FollowUpProposal.model_validate_json(proposal_payload)
        result = run_phase2(grid, config, proposal)
        log(
            INFO,
            "CohortLens Phase 2 finished with %s strata (%s)",
            len(result.strata_results),
            result.status,
        )
    else:
        result = run_phase1(grid, config)
        log(
            INFO,
            "CohortLens Phase 1 finished with %s/%s estimates (%s)",
            result.estimated_sites,
            result.expected_sites,
            result.status,
        )
