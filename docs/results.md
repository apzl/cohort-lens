# Results

All runs used Flower 1.37.0 on SuperGrid with three registered deployment SuperNodes, on
2026-09-16. The three SuperNodes ran on one development host.

## GDSC2 primary

Study: BRAF p.V600E and PLX-4720 response (`LN_IC50`), GDSC2 release 8.5 (27 Oct 2023), 938 cell
lines after excluding 19 models with another BRAF driver and 5 without mutation coverage.

| Site | N used |
|---|---|
| site-1 | 291 |
| site-2 | 306 |
| site-3 | 341 |

| Pooled β | 95% CI | I² | Status | Gate |
|---|---|---|---|---|
| −2.3148 ln µM | [−2.8232, −1.8063] | 0% | `COMPLETE`, 3 of 3 | `FINISH_COMPATIBLE` |

Lower `LN_IC50` means greater measured sensitivity, so the negative coefficient is in the expected
direction for a BRAF inhibitor. The sites agreed, so no investigator call or second query was
authorised. A reporter run summarised the released aggregates.

An earlier cold-start run was `PARTIAL`: one node's first dependency installation outlasted the
reply window. The unchanged warm retry completed.

## Synthetic deployment, full loop

Three sites of 1,500 generated rows, with a hidden context variable at prevalence 0.15, 0.50 and
0.85 that changes the effect size.

| Stage | Result |
|---|---|
| Primary | β = 0.5108, I² = 97.1%, gate `INVESTIGATE` |
| Investigator | valid, parent-bound stratification proposal |
| Operator approval | new contract digest and approval |
| Follow-up, `context_0` | β = 0.1397, 95% CI [0.0741, 0.2054], I² = 0%, 3 of 3 |
| Follow-up, `context_1` | β = 0.8625, 95% CI [0.7982, 0.9267], I² = 0%, 3 of 3 |

Heterogeneity disappears within each stratum, consistent with the generator: the sites disagreed
because their context mixtures differed. This is exploratory reuse of the same records, not
replication.

Two failed live probes are part of the record: the model returned an unapproved JSON shape and
strict validation stopped before analytics; and a new agent mode was missing from the parser, with
no analytic state changed.

## What has not been run

The investigate, approve and stratify loop has not run on GDSC2 data on SuperGrid, because the
hash-partitioned sites agree. The `lineage-enriched-v1` split exists to exercise that path and is
covered by offline tests only.

## Numerical validation

The NumPy OLS-HC3 implementation matches `statsmodels` 0.14.5 to a relative tolerance of 1e-10
(`tests/test_statistical_reference.py`).
