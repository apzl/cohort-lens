"""Report safety and provenance tests."""

from cohortlens_operator.report import render_report
from cohortlens_shared.schemas import AssociationReply, AssociationResult, SiteState


def test_report_discloses_collusion_boundary() -> None:
    reply = AssociationReply(
        study_id="phase1-demo",
        analysis_id="primary-001",
        query_id="primary-001",
        contract_hash="sha256:" + "1" * 64,
        site_alias="site-1",
        snapshot_alias="synthetic-site-1-v1",
        status="ESTIMATED",
        beta=0.5,
        standard_error=0.1,
        n_used=1500,
        estimator="OLS_HC3",
    )
    second = reply.model_copy(update={"site_alias": "site-2"})
    result = AssociationResult(
        study_id="phase1-demo",
        analysis_id="primary-001",
        query_id="primary-001",
        contract_hash="sha256:" + "1" * 64,
        expected_sites=3,
        responding_sites=2,
        estimated_sites=2,
        site_states=(
            SiteState(site_alias="site-1", stage="ASSOCIATION", status="ESTIMATED"),
            SiteState(site_alias="site-2", stage="ASSOCIATION", status="ESTIMATED"),
            SiteState(site_alias="site-3", stage="ASSOCIATION", status="NO_RESPONSE"),
        ),
        site_results=(reply, second),
        beta_meta=0.5,
        standard_error_meta=0.07,
        ci95_low=0.36,
        ci95_high=0.64,
        q=0.0,
        q_degrees_freedom=1,
        i2=0.0,
        heterogeneity_flag=False,
        status="PARTIAL",
        phenotype_unit="demo_unit",
    )
    html = render_report(
        result,
        run_id="123",
        flower_version="1.37.0",
        agent_fab="agent.fab",
        analytics_fab="analytics.fab",
    )
    assert "Selective reporting or collusion" in html
    assert "patient-level" not in html.lower()
    assert "123" in html
    assert "1.37.0" in html
    assert "agent.fab" in html
    assert "analytics.fab" in html
