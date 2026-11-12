# Results

## SuperGrid runs (previous investigator)

All runs in this section used Flower 1.37.0 on SuperGrid with three registered deployment
SuperNodes, on 2026-09-16. The three SuperNodes ran on one development host. They predate the
tool-using investigator: where an investigator ran, it was the earlier single-call design with no
tools, whose prompt offered one permitted follow-up.

### GDSC2 primary

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

### Synthetic deployment, full loop

Three sites of 1,500 generated rows, with a hidden context variable at prevalence 0.15, 0.50 and
0.85 that changes the effect size.

| Stage | Result |
|---|---|
| Primary | β = 0.5108, I² = 97.1%, gate `INVESTIGATE` |
| Investigator (single call, no tools) | valid, parent-bound stratification proposal |
| Operator approval | new contract digest and approval |
| Follow-up, `context_0` | β = 0.1397, 95% CI [0.0741, 0.2054], I² = 0%, 3 of 3 |
| Follow-up, `context_1` | β = 0.8625, 95% CI [0.7982, 0.9267], I² = 0%, 3 of 3 |

Heterogeneity disappears within each stratum, consistent with the generator: the sites disagreed
because their context mixtures differed. This is exploratory reuse of the same records, not
replication.

Two failed live probes are part of the record: the model returned an unapproved JSON shape and
strict validation stopped before analytics; and a new agent mode was missing from the parser, with
no analytic state changed.

## Offline runs of the current loop (local backend, scripted policy)

Produced on 2026-11-11 with `cohortlens run-study --backend local --investigator scripted
--approve-as demo-operator` on the prepared GDSC2 snapshots. The ServerApp and ClientApp handler
code ran in-process over an in-memory grid; **no Flower runtime and no language model were
involved**. The investigator was the deterministic scripted policy, driving the same tools,
validation and ledger a model would. Ledgers: [`docs/sample-run/`](sample-run/README.md).

### Hash split (`data/gdsc/prepared`)

| Round | Pooled β | 95% CI | Q (df 2) | Q p | I² | Gate |
|---|---|---|---|---|---|---|
| primary, N = 291 / 306 / 341 | −2.3148 | [−2.8232, −1.8063] | 0.653 | 0.72 | 0 | `FINISH_COMPATIBLE` |

This reproduces the SuperGrid primary exactly. The investigator was not invoked.

### Lineage-enriched split (`data/gdsc/lineage-enriched-v1`)

A deliberately non-IID, response-blind split for exercising the heterogeneity path; not an
estimate of real between-laboratory heterogeneity.

| Round | Action (chosen by) | Released aggregate | Gate |
|---|---|---|---|
| 0 | primary, N = 84 / 656 / 198 | β = −2.4097, 95% CI [−2.8901, −1.9293], Q = 5.231, p = 0.073, I² = 0.618 | `INVESTIGATE` |
| 1 | `LEAVE_ONE_SITE_OUT` (scripted policy, 6 tool calls) | pooled β with one site omitted: −2.7510 to −1.8875; I² 0 to 0.806; 2 of 3 omissions clear the flag | `INVESTIGATE` |
| 2 | `STRATIFY_GDSC_LINEAGE` (scripted policy, 7 tool calls) | non-SKCM: β = −1.9558 [−2.4679, −1.4436], Q = 8.528, p = 0.014, I² = 0.765, N = 63 / 636 / 188; SKCM: β = −3.4199 [−4.3969, −2.4428], Q = 1.023, p = 0.60, I² = 0, N = 21 / 20 / 10 | `BUDGET_EXHAUSTED` |

Reading: two different single omissions each clear the heterogeneity flag, so no one site alone
accounts for the disagreement. Within SKCM the sites agree and the association is stronger; within
non-SKCM they still disagree, so lineage at this granularity does not explain the heterogeneity in
this artificial split. The budget of two follow-ups was then spent. In round 2 the policy first
checked `LEAVE_ONE_SITE_OUT` again and the precondition check refused it (`NOT_ALREADY_RUN`). The
choices follow the scripted rule set in `operator/cohortlens_operator/scripted_policy.py`; they
are not model judgements.

## What has not been run

- The tool-using investigator has not run against Flower Endeavor or any other model, and Endeavor's
  support for native tool calling is unconfirmed. Both protocols are tested with fake clients.
- `run-study --backend supergrid` has not been executed; only its command construction and log
  parsing are tested.
- The investigate, approve and follow-up loop has not run on GDSC2 data on SuperGrid.

## Numerical validation

The NumPy OLS-HC3 implementation matches `statsmodels` 0.14.5 to a relative tolerance of 1e-10
(`tests/test_statistical_reference.py`). Leave-one-site-out pooling matches direct recomputation
to 1e-12, and the standard-library chi-square tail matches SciPy to 1e-9.
