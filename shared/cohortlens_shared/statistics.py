"""Synthetic data and ordinary, testable statistical calculations."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import numpy as np
from pydantic import BaseModel, ConfigDict, Field


class SiteEstimate(BaseModel):
    """A local effect estimate on the common synthetic measurement scale."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    site_alias: str
    beta: float
    standard_error: Annotated[float, Field(gt=0)]
    n_used: Annotated[int, Field(gt=5)]
    estimator: str = "OLS_HC3"


class MetaEstimate(BaseModel):
    """Fixed-effect inverse-variance aggregate."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    beta: float
    standard_error: Annotated[float, Field(gt=0)]
    ci95_low: float
    ci95_high: float
    q: Annotated[float, Field(ge=0)]
    i2: Annotated[float, Field(ge=0, le=1)]
    sites: Annotated[int, Field(ge=2)]


def generate_synthetic_site(
    site_index: int, *, n: int = 1500, base_seed: int = 20260916
) -> tuple[np.ndarray, np.ndarray]:
    """Generate one public cohort with a deliberately different context mix."""
    prevalences = (0.15, 0.50, 0.85)
    if site_index not in range(len(prevalences)):
        raise ValueError("site_index must be 0, 1, or 2")
    if n < 20:
        raise ValueError("n must be at least 20")

    rng = np.random.default_rng(base_seed + site_index)
    genotype = rng.binomial(2, 0.30, size=n).astype(float)
    age_z = rng.normal(0.0, 1.0, size=n)
    sex_code = rng.binomial(1, 0.50, size=n).astype(float)
    context = rng.binomial(1, prevalences[site_index], size=n).astype(float)
    noise = rng.normal(0.0, 1.0, size=n)
    biomarker = (
        0.15 * genotype
        + 0.75 * genotype * context
        + 0.25 * age_z
        + 0.15 * sex_code
        + 0.40 * context
        + noise
    )
    design = np.column_stack([np.ones(n), genotype, age_z, sex_code, context])
    return design, biomarker


def load_site_dataset(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Load one node-local snapshot from a non-pickled NumPy archive."""
    if path.suffix != ".npz":
        raise ValueError("site dataset must be a .npz archive")
    try:
        with np.load(path, allow_pickle=False) as archive:
            if set(archive.files) != {"design", "outcome"}:
                raise ValueError("site dataset must contain only design and outcome")
            design = np.asarray(archive["design"], dtype=float)
            outcome = np.asarray(archive["outcome"], dtype=float)
    except OSError as error:
        raise ValueError(f"unable to read site dataset: {path}") from error
    if design.ndim != 2 or design.shape[1] < 2:
        raise ValueError("site design must have at least two columns")
    if outcome.ndim != 1 or design.shape[0] != outcome.shape[0]:
        raise ValueError("site design and outcome dimensions are incompatible")
    return design, outcome


def complete_cases(
    design: np.ndarray, outcome: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Apply the fixed complete-case policy without imputing values."""
    if design.ndim != 2 or outcome.ndim != 1 or design.shape[0] != outcome.shape[0]:
        raise ValueError("design and outcome dimensions are incompatible")
    mask = np.isfinite(outcome) & np.isfinite(design).all(axis=1)
    return design[mask], outcome[mask]


def minor_allele_count(design: np.ndarray) -> int:
    """Return the smaller allele count for the dosage column after filtering."""
    if design.ndim != 2 or design.shape[1] < 2:
        raise ValueError("design must contain an effect-allele dosage column")
    dosage = design[:, 1]
    if not np.isfinite(dosage).all() or not np.isin(dosage, (0.0, 1.0, 2.0)).all():
        raise ValueError("effect-allele dosage must contain only 0, 1, or 2")
    effect_count = int(np.sum(dosage))
    total_alleles = 2 * len(dosage)
    return min(effect_count, total_alleles - effect_count)


def binary_group_count(design: np.ndarray) -> int:
    """Return the smaller support count for a binary feature in column one."""
    if design.ndim != 2 or design.shape[1] < 2:
        raise ValueError("design must contain a binary feature column")
    feature = design[:, 1]
    if not np.isfinite(feature).all() or not np.isin(feature, (0.0, 1.0)).all():
        raise ValueError("binary feature must contain only 0 or 1")
    positive = int(np.sum(feature))
    return min(positive, len(feature) - positive)


def fit_genotype_ols_hc3(
    site_alias: str, design: np.ndarray, outcome: np.ndarray
) -> SiteEstimate:
    """Fit the fixed OLS model after the predetermined complete-case filter."""
    design, outcome = complete_cases(design, outcome)
    if design.shape[1] < 2:
        raise ValueError("the design must contain at least two columns")
    if design.shape[0] <= design.shape[1]:
        raise ValueError("insufficient complete cases")
    if np.linalg.matrix_rank(design) != design.shape[1]:
        raise ValueError("design matrix is rank deficient")

    xtx_inv = np.linalg.inv(design.T @ design)
    coefficients = xtx_inv @ design.T @ outcome
    residuals = outcome - design @ coefficients
    leverage = np.sum((design @ xtx_inv) * design, axis=1)
    if np.any(leverage >= 1.0):
        raise ValueError("invalid leverage encountered")
    scaled = residuals / (1.0 - leverage)
    weighted_design = design * scaled[:, None]
    covariance = xtx_inv @ (weighted_design.T @ weighted_design) @ xtx_inv
    standard_error = float(np.sqrt(covariance[1, 1]))
    if not np.isfinite(standard_error) or standard_error <= 0:
        raise ValueError("genotype standard error is invalid")
    return SiteEstimate(
        site_alias=site_alias,
        beta=float(coefficients[1]),
        standard_error=standard_error,
        n_used=int(design.shape[0]),
    )


def context_stratum(design: np.ndarray, outcome: np.ndarray, stratum_id: str) -> tuple[np.ndarray, np.ndarray]:
    """Select a predeclared binary stratum and omit its constant last column."""
    values = {
        "context_0": 0.0,
        "context_1": 1.0,
        "non_skcm": 0.0,
        "skcm": 1.0,
    }
    if stratum_id not in values:
        raise ValueError("unsupported binary stratum")
    design, outcome = complete_cases(design, outcome)
    if design.ndim != 2 or design.shape[1] < 3:
        raise ValueError("stratification requires a final binary design column")
    selected = design[:, -1] == values[stratum_id]
    # The stratum column is constant after selection and would make OLS singular.
    return design[selected, :-1], outcome[selected]


def fixed_effect_meta(estimates: list[SiteEstimate]) -> MetaEstimate:
    """Combine independent estimates using inverse-variance fixed effects."""
    if len(estimates) < 2:
        raise ValueError("at least two site estimates are required")
    beta = np.asarray([item.beta for item in estimates], dtype=float)
    variance = np.square(
        np.asarray([item.standard_error for item in estimates], dtype=float)
    )
    if not np.isfinite(beta).all() or not np.isfinite(variance).all():
        raise ValueError("estimates must be finite")
    weights = 1.0 / variance
    pooled_beta = float(np.sum(weights * beta) / np.sum(weights))
    pooled_se = float(np.sqrt(1.0 / np.sum(weights)))
    q = max(0.0, float(np.sum(weights * np.square(beta - pooled_beta))))
    degrees_freedom = len(estimates) - 1
    i2 = 0.0 if q == 0 else max(0.0, (q - degrees_freedom) / q)
    return MetaEstimate(
        beta=pooled_beta,
        standard_error=pooled_se,
        ci95_low=pooled_beta - 1.96 * pooled_se,
        ci95_high=pooled_beta + 1.96 * pooled_se,
        q=q,
        i2=i2,
        sites=len(estimates),
    )


def phase1_reference() -> tuple[list[SiteEstimate], MetaEstimate]:
    """Compute the three-site analysis independently of Flower transport."""
    estimates = []
    for site_index in range(3):
        design, outcome = generate_synthetic_site(site_index)
        estimates.append(
            fit_genotype_ols_hc3(f"site-{site_index + 1}", design, outcome)
        )
    return estimates, fixed_effect_meta(estimates)


def phase0_reference() -> tuple[list[SiteEstimate], MetaEstimate]:
    """Backward-compatible name for the synthetic reference calculation."""
    return phase1_reference()


def leave_one_site_out(
    estimates: list[SiteEstimate],
) -> list[tuple[str, MetaEstimate]]:
    """Re-pool the estimates once per site with that site's estimate omitted.

    Requires at least three estimates so every omission still pools two.
    Results are ordered by the omitted site's alias.
    """
    if len(estimates) < 3:
        raise ValueError("leave-one-site-out needs at least three site estimates")
    aliases = [item.site_alias for item in estimates]
    if len(set(aliases)) != len(aliases):
        raise ValueError("site aliases must be unique")
    ordered = sorted(estimates, key=lambda item: item.site_alias)
    return [
        (left_out.site_alias, fixed_effect_meta([item for item in ordered if item is not left_out]))
        for left_out in ordered
    ]
