"""Reference-statistics tests reused by later analytics phases."""

import math

import numpy as np
import pytest

from cohortlens_shared.statistics import (
    SiteEstimate,
    fixed_effect_meta,
    generate_synthetic_site,
    load_site_dataset,
    phase0_reference,
)


def test_synthetic_generation_is_reproducible_and_site_local() -> None:
    design_a, outcome_a = generate_synthetic_site(0)
    design_b, outcome_b = generate_synthetic_site(0)
    design_other, _ = generate_synthetic_site(1)
    np.testing.assert_array_equal(design_a, design_b)
    np.testing.assert_array_equal(outcome_a, outcome_b)
    assert not np.array_equal(design_a, design_other)


def test_reference_exposes_context_mixture_heterogeneity() -> None:
    sites, aggregate = phase0_reference()
    assert [site.site_alias for site in sites] == ["site-1", "site-2", "site-3"]
    assert sites[0].beta < sites[1].beta < sites[2].beta
    assert aggregate.sites == 3
    assert aggregate.i2 > 0.5


def test_fixed_effect_meta_matches_direct_formula() -> None:
    estimates = [
        SiteEstimate(site_alias="a", beta=0.1, standard_error=0.2, n_used=100),
        SiteEstimate(site_alias="b", beta=0.4, standard_error=0.1, n_used=120),
    ]
    result = fixed_effect_meta(estimates)
    weights = [25.0, 100.0]
    expected_beta = (weights[0] * 0.1 + weights[1] * 0.4) / sum(weights)
    assert result.beta == pytest.approx(expected_beta)
    assert result.standard_error == pytest.approx(math.sqrt(1.0 / sum(weights)))


def test_meta_analysis_rejects_one_site() -> None:
    estimate = SiteEstimate(
        site_alias="a", beta=0.1, standard_error=0.2, n_used=100
    )
    with pytest.raises(ValueError, match="at least two"):
        fixed_effect_meta([estimate])


def test_node_local_npz_round_trip(tmp_path) -> None:
    design, outcome = generate_synthetic_site(0)
    snapshot = tmp_path / "site-1.npz"
    np.savez(snapshot, design=design, outcome=outcome)
    loaded_design, loaded_outcome = load_site_dataset(snapshot)
    np.testing.assert_array_equal(loaded_design, design)
    np.testing.assert_array_equal(loaded_outcome, outcome)


def test_node_local_npz_rejects_unexpected_arrays(tmp_path) -> None:
    design, outcome = generate_synthetic_site(0)
    snapshot = tmp_path / "site-1.npz"
    np.savez(snapshot, design=design, outcome=outcome, patient_ids=np.arange(1500))
    with pytest.raises(ValueError, match="only design and outcome"):
        load_site_dataset(snapshot)
