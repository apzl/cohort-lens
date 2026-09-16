"""Cross-check the local estimator against independent statistical software."""

import pytest
import statsmodels.api as sm

from cohortlens_shared.statistics import (
    fit_genotype_ols_hc3,
    generate_synthetic_site,
)


@pytest.mark.parametrize("site_index", [0, 1, 2])
def test_ols_hc3_matches_statsmodels(site_index: int) -> None:
    design, outcome = generate_synthetic_site(site_index)
    ours = fit_genotype_ols_hc3(f"site-{site_index + 1}", design, outcome)
    reference = sm.OLS(outcome, design).fit(cov_type="HC3")
    assert ours.beta == pytest.approx(reference.params[1], rel=1e-10, abs=1e-12)
    assert ours.standard_error == pytest.approx(
        reference.bse[1], rel=1e-10, abs=1e-12
    )


def test_complete_case_policy_matches_explicit_filter() -> None:
    design, outcome = generate_synthetic_site(0)
    design[0, 2] = float("nan")
    outcome[1] = float("nan")
    ours = fit_genotype_ols_hc3("site-1", design, outcome)
    reference = sm.OLS(outcome[2:], design[2:]).fit(cov_type="HC3")
    assert ours.n_used == 1498
    assert ours.beta == pytest.approx(reference.params[1], rel=1e-10)

