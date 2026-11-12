"""Static, dependency-free HTML reporting for released primary and follow-up results."""

from __future__ import annotations

from html import escape
from typing import Any

from cohortlens_shared.schemas import (
    AssociationResult,
    LeaveOneSiteOutResult,
    StratifiedAssociationResult,
)


def render_report(
    result: AssociationResult,
    *,
    run_id: str = "not-recorded",
    agent_model: str = "flower-endeavor",
    flower_version: str = "not-recorded",
    agent_fab: str = "not-recorded",
    analytics_fab: str = "not-recorded",
) -> str:
    """Render only released summaries; no local records are accepted as input."""
    rows = "\n".join(
        "<tr>"
        f"<td>{escape(item.site_alias)}</td>"
        f"<td>{escape(item.status)}</td>"
        f"<td>{'' if item.beta is None else f'{item.beta:.6f}'}</td>"
        f"<td>{'' if item.standard_error is None else f'{item.standard_error:.6f}'}</td>"
        f"<td>{'' if item.n_used is None else item.n_used}</td>"
        "</tr>"
        for item in result.site_results
    )
    effect_label = "per effect-allele copy" if result.synthetic else "for BRAF V600E versus comparator"
    aggregate = (
        "Insufficient compatible site estimates for a network result."
        if result.beta_meta is None
        else (
            f"Combined effect: {result.beta_meta:.6f} {escape(result.phenotype_unit)} "
            f"{effect_label}; 95% CI "
            f"[{result.ci95_low:.6f}, {result.ci95_high:.6f}]. "
            f"I²: {100 * result.i2:.1f}% (Q={result.q:.3f}, "
            f"df={result.q_degrees_freedom})."
        )
    )
    heterogeneity = (
        "The site estimates differ substantially. A single common-effect summary "
        "may be misleading; this is a review flag, not proof of its cause."
        if result.heterogeneity_flag
        else "No predeclared heterogeneity warning was triggered."
    )
    return f"""<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>CohortLens primary report</title></head>
<body>
<h1>CohortLens association</h1>
<dl>
<dt>Study</dt><dd>{escape(result.study_id)}</dd>
<dt>Analysis</dt><dd>{escape(result.analysis_id)}</dd>
<dt>Contract</dt><dd>{escape(result.contract_hash)}</dd>
<dt>Flower run</dt><dd>{escape(run_id)}</dd>
<dt>Flower version</dt><dd>{escape(flower_version)}</dd>
<dt>Analytics FAB</dt><dd>{escape(analytics_fab)}</dd>
<dt>Agent FAB</dt><dd>{escape(agent_fab)}</dd>
<dt>Statistical method</dt><dd>{escape(result.estimator)}</dd>
<dt>Agent model</dt><dd>{escape(agent_model)}</dd>
<dt>Provenance</dt><dd>{escape(result.data_provenance)}</dd>
</dl>
<h2>Released evidence</h2>
<p>{aggregate}</p>
<p>{heterogeneity}</p>
<table>
<thead><tr><th>Site</th><th>Status</th><th>Effect</th><th>Standard error</th><th>N used</th></tr></thead>
<tbody>{rows}</tbody>
</table>
<h2>Interpretation limits</h2>
<ul>
<li>{"This is generated synthetic data." if result.synthetic else "This uses public cancer cell-line measurements, not patients or a clinical trial."}</li>
<li>Association does not establish causation.</li>
<li>Summary statistics can still be sensitive; keeping rows local is not a formal privacy guarantee.</li>
<li>Message validation does not prove that a participating site used honest records or calculations. Selective reporting or collusion requires independent governance, audit, and replication controls.</li>
<li>With only three sites, heterogeneity estimates are unstable.</li>
</ul>
</body>
</html>
"""


def _num(value: float | None, digits: int = 4) -> str:
    return "" if value is None else f"{value:.{digits}f}"


def _stratum_label(item: AssociationResult, index: int) -> str:
    ids = {reply.stratum_id for reply in item.site_results}
    return ids.pop() if len(ids) == 1 else f"stratum-{index + 1}"


def render_phase2_report(result: StratifiedAssociationResult) -> str:
    """Render the exploratory stratified comparison without accepting local inputs."""
    rows = "".join(
        f"<tr><td>{escape(_stratum_label(item, index))}</td><td>{_num(item.beta_meta, 6)}</td>"
        f"<td>{_num(item.standard_error_meta, 6)}</td><td>{_num(item.i2, 3)}</td>"
        f"<td>{escape(item.status)}</td></tr>"
        for index, item in enumerate(result.strata_results)
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>CohortLens follow-up report</title></head><body>
<h1>CohortLens exploratory stratified follow-up (round {result.round_index})</h1>
<dl><dt>Follow-up contract</dt><dd>{escape(result.contract_hash)}</dd><dt>Parent result</dt><dd>{escape(result.parent_result_hash)}</dd><dt>Approved proposal</dt><dd>{escape(result.proposal_hash)}</dd></dl>
<p>This is an exploratory, predeclared follow-up. It does not replace the primary result, establish causation, or provide independent replication.</p>
<table><thead><tr><th>Stratum</th><th>Fixed-effect estimate</th><th>SE</th><th>I²</th><th>Status</th></tr></thead><tbody>{rows}</tbody></table>
<p>Only allowlisted site summaries were released; individual rows were not sent. Summary releases remain sensitive and honest local computation is not proven.</p></body></html>"""


render_stratified_report = render_phase2_report


def render_loso_report(result: LeaveOneSiteOutResult) -> str:
    """Render the leave-one-site-out sensitivity release for the operator."""
    full = result.full_result
    rows = "".join(
        f"<tr><td>{escape(item.omitted_site_alias)}</td><td>{_num(item.beta_meta, 6)}</td>"
        f"<td>[{_num(item.ci95_low)}, {_num(item.ci95_high)}]</td><td>{_num(item.i2, 3)}</td>"
        f"<td>{'yes' if item.heterogeneity_flag else 'no'}</td></tr>"
        for item in result.omissions
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>CohortLens sensitivity report</title></head><body>
<h1>CohortLens leave-one-site-out sensitivity (round {result.round_index})</h1>
<dl><dt>Contract</dt><dd>{escape(result.contract_hash)}</dd><dt>Parent result</dt><dd>{escape(result.parent_result_hash)}</dd><dt>Approved proposal</dt><dd>{escape(result.proposal_hash)}</dd></dl>
<p>All sites: β = {_num(full.beta_meta, 6)}, 95% CI [{_num(full.ci95_low)}, {_num(full.ci95_high)}], I² = {_num(full.i2, 3)} ({escape(result.status)}).</p>
<table><thead><tr><th>Omitted site</th><th>Pooled β</th><th>95% CI</th><th>I²</th><th>Flag</th></tr></thead><tbody>{rows}</tbody></table>
<p>This table is an operator view. The investigator receives only the identity-free range of these values. With three sites each omission pools two, so these are unstable sensitivity checks, not replication.</p></body></html>"""


_POLICY_NOTES = {
    "SCRIPTED_POLICY": "SCRIPTED POLICY — deterministic rules, not a language model.",
    "MODEL": "Language model output, validated against the strict proposal schema.",
}


def _round_rows(entries: list[dict[str, Any]]) -> str:
    rows = []
    for entry in entries:
        if entry["entry_type"] != "RESULT_RELEASED":
            continue
        summary = entry["payload"]["aggregate_summary"]
        estimates = [("network", summary["network"])] if summary.get("network") else []
        estimates += [(f"stratum {s['stratum_id']}", s) for s in summary.get("strata", [])]
        for label, item in estimates:
            rows.append(
                f"<tr><td>{summary['round_index']}</td><td>{escape(summary['action'])}</td>"
                f"<td>{escape(label)}</td><td>{_num(item.get('beta_meta'))}</td>"
                f"<td>[{_num(item.get('ci95_low'))}, {_num(item.get('ci95_high'))}]</td>"
                f"<td>{_num(item.get('i2'), 3)}</td><td>{escape(item['status'])}</td></tr>"
            )
        loso = summary.get("loso")
        if loso:
            rows.append(
                f"<tr><td>{summary['round_index']}</td><td>LEAVE_ONE_SITE_OUT</td><td>omission range</td>"
                f"<td>[{_num(loso.get('pooled_beta_min'))}, {_num(loso.get('pooled_beta_max'))}]</td><td></td>"
                f"<td>[{_num(loso.get('i2_min'), 3)}, {_num(loso.get('i2_max'), 3)}]</td>"
                f"<td>{loso.get('omissions_clearing_flag')} clear the flag</td></tr>"
            )
    return "".join(rows)


def _event_items(entries: list[dict[str, Any]]) -> str:
    items = []
    for entry in entries:
        kind, payload = entry["entry_type"], entry["payload"]
        if kind == "GATE_DECISION":
            items.append(f"Gate after round {payload['round_index']}: <b>{escape(payload['decision'])}</b>")
        elif kind == "AGENT_PROPOSAL":
            note = _POLICY_NOTES.get(payload["policy_kind"], "")
            proposal = payload["proposal"]
            items.append(
                f"Investigator ({escape(payload['policy_label'])}; {escape(note)}) proposed "
                f"<b>{escape(proposal['action'])}</b> after {payload['tool_calls']} tool calls: "
                f"{escape(proposal['rationale'])}"
            )
        elif kind in {"CONTRACT_APPROVED", "APPROVAL_DECLINED"}:
            verb = "approved" if kind == "CONTRACT_APPROVED" else "declined"
            items.append(
                f"Round {payload['round_index']} contract {escape(payload['contract_hash'])} {verb} by "
                f"{escape(payload['approver'])} ({escape(payload['approval_mode'])})"
            )
        elif kind in {"AGENT_FAILED", "STUDY_FINISHED"}:
            items.append(f"{escape(kind)}: {escape(str(payload.get('failure_reason') or payload.get('reason')))}")
    return "".join(f"<li>{item}</li>" for item in items)


def render_study_report(entries: list[dict[str, Any]]) -> str:
    """Render a verified ledger: aggregates per round, gates, proposals, approvals."""
    start = entries[0]["payload"]
    note = _POLICY_NOTES.get(start.get("policy_kind", ""), "")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>CohortLens study report</title></head><body>
<h1>CohortLens investigation: {escape(entries[0]['study_id'])}</h1>
<p>Backend: {escape(str(start.get('backend')))}. Investigator: {escape(str(start.get('policy_label')))} — {escape(note)}
Approvals: {escape(str(start.get('approver_mode')))}. Ledger head: {escape(entries[-1]['entry_hash'])} ({len(entries)} entries).</p>
<h2>Released network aggregates</h2>
<table><thead><tr><th>Round</th><th>Action</th><th>Estimate</th><th>β</th><th>95% CI</th><th>I²</th><th>Status</th></tr></thead>
<tbody>{_round_rows(entries)}</tbody></table>
<h2>Decisions</h2><ol>{_event_items(entries)}</ol>
<p>Follow-up rounds reuse the same records: they are exploratory, not replication. Summary releases remain sensitive; message validation does not prove honest local computation.</p>
</body></html>
"""
