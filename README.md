# CohortLens

CohortLens is a Flower workflow for a bounded public-data pharmacogenomics
study. It evaluates whether **BRAF p.V600E** is associated with GDSC2
**PLX-4720** response (`LN_IC50`) across three deterministic virtual
laboratories, then conditionally considers a predeclared lineage follow-up.

This is an emulated federation over already-public cancer cell-line data, not
a patient study, clinical trial, or the original GDSC contributing laboratories.
Its data mode is `PUBLIC_GDSC_EMULATED_FEDERATION`; its intended execution mode
is deployment SuperNodes.

## Fixed study

The preparation manifest records GDSC2 release `8.5-27Oct23`, source-file
checksums, compound 1036 (`PLX-4720`), BRAF p.V600E presence, and `LN_IC50` in
logged micromolar units. The approved contract binds the supported compound,
biomarker, response, statistical policy, roster, and release policy. Lower
`LN_IC50` means greater measured sensitivity in this assay.

Preparation consumes three source-side inputs: the GDSC2 fitted-response
workbook, Cell Model Passports model list, and the compact Cell Model Passports
driver-mutation summary. It retains mutation-covered models with BRAF p.V600E
or no recorded BRAF driver mutation, excludes other recorded BRAF driver
alterations, and adjusts the primary model for SKCM versus other lineages.

The compact mutation summary is an important limitation: the comparator is
**not** definitive BRAF wild type. A full mutation-call dataset is needed to
make that claim. This workflow concerns public in-vitro measurements, not
clinical response.

Each stable `SANGER_MODEL_ID` is allocated by `sha256(SANGER_MODEL_ID) modulo
3`, without inspecting response, mutation, or lineage. Preparation writes one
local numeric NPZ matrix per SuperNode plus a manifest containing source
checksums, counts, the partition rule, and the exact contract. Model IDs remain
in source-side preparation and are not in the snapshots or coordinator output.

## Workflow and disclosure boundary

The ClientApp loads only its configured local NPZ file, verifies the approved
contract, fits OLS with HC3 robust standard errors, and releases an allowlisted
site-level summary. The ServerApp validates replies and performs
inverse-variance fixed-effect meta-analysis. Released analytics contain summary
effects, uncertainty, analysed counts, participation/status, and heterogeneity
diagnostics—not raw design matrices or model IDs.

The deterministic post-primary gate has three outcomes:

- insufficient compatible estimates: stop;
- no heterogeneity warning (`I² < 0.50`): finish, with no second query;
- warning with adequate support: authorize aggregate-only investigator input.

Only the third path permits the Flower Endeavor 1.0 (`flower-endeavor`)
investigator. The current investigator is fixed to the bounded
`STRATIFY_GDSC_LINEAGE` proposal and sees aggregate network evidence only. Its
output is never authorization. An operator must approve the exact new contract
digest before a second run. The calculation uses `non_skcm` and `skcm`, omits
the constant lineage column within each stratum, and repeats local OLS-HC3 and
meta-analysis. It is exploratory and uses the same records; it is not
replication or causal evidence. A separate aggregate-only Endeavor reporter can
summarize a terminal result without authorizing more analytics.

## Install and validate

```shell
uv sync --extra test
uv run python scripts/stage_apps.py
uv run pytest
uv run flwr build --app apps/cohort-agent
uv run flwr build --app apps/cohort-analytics
```

Inspect the actual FAB filenames before submission:

```shell
uv run cohortlens inspect-fab AGENT.fab \
  --forbid-text PRIVATE_PATIENT_SENTINEL --forbid-text DUMMY_SECRET_SENTINEL
uv run cohortlens inspect-fab ANALYTICS.fab \
  --forbid-text PRIVATE_PATIENT_SENTINEL --forbid-text DUMMY_SECRET_SENTINEL
```

## Prepare local SuperNode inputs

Download permitted GDSC2 and Cell Model Passports files yourself; keep source
data outside the repository and FABs. Prepare local snapshots, the contract,
time-limited approval, and run config:

```shell
uv run cohortlens prepare-gdsc \
  --responses data/gdsc/source/GDSC2_fitted_dose_response_27Oct23.xlsx \
  --models data/gdsc/source/model_list_latest.csv.gz \
  --mutations data/gdsc/source/mutations_summary_latest.csv.gz \
  --output-dir data/gdsc/prepared --approved-by OPERATOR --valid-hours 24
```

The command rejects missing inputs, absent/duplicate PLX-4720 responses,
insufficient local support, and rank-deficient designs. It will not overwrite
existing snapshots unless `--overwrite` is explicitly given.

Mount `site-1.npz`, `site-2.npz`, and `site-3.npz` separately and read-only on
the three SuperNodes. Each local node configuration needs its matching
`site-alias`, `dataset-path`, and `snapshot-alias`; paths never appear in the
analytics run config.

## Submit and collect the primary run

```shell
uv run flwr run apps/cohort-analytics supergrid \
  --federation @ACCOUNT/FEDERATION \
  --run-config data/gdsc/prepared/analytics-run.toml --stream

uv run flwr log RUN_ID supergrid --show > data/gdsc/prepared/primary.log
uv run cohortlens collect \
  --log data/gdsc/prepared/primary.log \
  --study-id gdsc2-plx4720-braf-v600e --query-id primary-001 \
  --contract-hash CONTRACT_HASH \
  --output data/gdsc/prepared/primary-result.json
```

The collector accepts one framed schema-valid result bound to the stated study,
query, and digest. Record the actual run ID and FAB identifiers with the
manifest; they are not inferred or fabricated.

## Conditional lineage follow-up

Apply the deterministic gate first. It writes `investigation-decision.json` and
refuses to prepare Endeavor unless the decision is `INVESTIGATE`.

```shell
uv run cohortlens prepare-investigator \
  --result data/gdsc/prepared/primary-result.json \
  --output data/gdsc/prepared/agent-investigate.toml
```

If authorized, run the aggregate-only investigator and validate its proposal
against the primary result. Do not submit an analytics follow-up for a rejected
proposal, metadata recheck, or stop decision.

```shell
uv run flwr run apps/cohort-agent supergrid \
  --federation @ACCOUNT/FEDERATION \
  --run-config data/gdsc/prepared/agent-investigate.toml --stream
uv run flwr log AGENT_RUN_ID supergrid --show > data/gdsc/prepared/investigator.log
uv run cohortlens collect-investigator \
  --log data/gdsc/prepared/investigator.log \
  --parent-result data/gdsc/prepared/primary-result.json \
  --output data/gdsc/prepared/followup-proposal.json
```

An operator explicitly compiles and approves the validated proposal, producing
a distinct digest and the only permitted follow-up config:

```shell
uv run cohortlens prepare-phase2 \
  --parent-result data/gdsc/prepared/primary-result.json \
  --proposal data/gdsc/prepared/followup-proposal.json \
  --output-dir data/gdsc/prepared/followup --approved-by OPERATOR --valid-hours 24
uv run flwr run apps/cohort-analytics supergrid \
  --federation @ACCOUNT/FEDERATION \
  --run-config data/gdsc/prepared/followup/phase2-analytics-run.toml --stream
```

## Verified SuperGrid execution

On 2026-09-16, analytics FAB
`apsal.cohortlens-analytics.0-1-0.418b9833.fab` completed on the three registered
deployment SuperNodes in `@apsal/cohortlens-deployment`:

- analytics run `6039286529145760171`: `COMPLETE`, three of three estimates;
- snapshot aliases `gdsc2-plx4720-site-1-v1` through
  `gdsc2-plx4720-site-3-v1`;
- combined coefficient `-2.3147527938 ln_micromolar`, 95% CI
  `[-2.8231890054, -1.8063165823]`, and I² `0.0`;
- deterministic decision `FINISH_COMPATIBLE`, so no investigator or second
  analytics query was authorized;
- aggregate-only Endeavor reporter run `16673699275914942267` completed using
  agent FAB `apsal.cohortlens-agent.0-1-0.10adca82.fab`.

The captured result, decision, and logs are under `runs/phase4-deployment/`.
An earlier cold-start run was partial because one node's first dependency
installation exceeded the reply window; the unchanged warm retry above
completed successfully.

## Scientific and privacy limits

This estimates associations in public cancer cell-line assays. It does not
establish a drug effect in people, clinical benefit, causality, or a validated
biomarker. The three hash partitions are virtual laboratories, not independent
replications, and heterogeneity estimates from three sites are unstable.

Rows remain local to ClientApps, but this is not secure aggregation,
differential privacy, or a confidentiality guarantee: the coordinator receives
site summaries and result frames are disclosed to log readers. Contract hashes,
approvals, fixed rosters, and reply validation reduce protocol mistakes; they
do not prove a site used honest records or calculations. Production use needs
governed access, audit trails, code/image attestation, data-use review, and
independent replication.

The recorded deployment proves this workflow executed successfully on Flower
SuperGrid. It does not turn the resulting association into a biological or
clinical finding.
