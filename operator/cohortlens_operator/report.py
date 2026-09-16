"""Static, dependency-free HTML reporting for released Phase 1 results."""

from __future__ import annotations

from html import escape

from cohortlens_shared.schemas import AssociationResult, Phase2AssociationResult


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
<head><meta charset="utf-8"><title>CohortLens Phase 1 report</title></head>
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


def render_phase2_report(result: Phase2AssociationResult) -> str:
    """Render the exploratory comparison without accepting any local inputs."""
    rows = "".join(
        f"<tr><td>{escape(item.site_results[0].stratum_id if item.site_results else stratum)}</td><td>{'' if item.beta_meta is None else f'{item.beta_meta:.6f}'}</td><td>{'' if item.standard_error_meta is None else f'{item.standard_error_meta:.6f}'}</td><td>{escape(item.status)}</td></tr>"
        for stratum, item in zip(("context_0", "context_1"), result.strata_results, strict=True)
    )
    return f"""<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\"><title>CohortLens Phase 2 report</title></head><body>
<h1>CohortLens Phase 2 exploratory context stratification</h1>
<dl><dt>Follow-up contract</dt><dd>{escape(result.contract_hash)}</dd><dt>Parent result</dt><dd>{escape(result.parent_result_hash)}</dd><dt>Approved proposal</dt><dd>{escape(result.proposal_hash)}</dd></dl>
<p>This is one exploratory, predeclared follow-up. It does not replace the primary result, establish causation, or provide independent replication.</p>
<table><thead><tr><th>Context stratum</th><th>Fixed-effect estimate</th><th>SE</th><th>Status</th></tr></thead><tbody>{rows}</tbody></table>
<p>Only allowlisted site summaries were released; individual rows were not sent. Summary releases remain sensitive and honest local computation is not proven.</p></body></html>"""
