# Usage

Every command, in workflow order. `cohortlens` is the operator CLI; `flwr` is the Flower CLI.
Replace `@ACCOUNT/FEDERATION`, `RUN_ID`, `CONTRACT_HASH` and `OPERATOR` with real values.

## CLI reference

| Command | Purpose |
|---|---|
| `reference` | Compute the offline three-site synthetic reference |
| `prepare-synthetic` | Contract, approval and run config for the synthetic track |
| `prepare-supernode-data` | Three synthetic snapshots for deployment SuperNodes |
| `prepare-gdsc` | GDSC2 site snapshots, manifest, contract, approval, run config |
| `collect` | Extract one validated result frame from a saved run log |
| `prepare-investigator` | Apply the gate; write the investigator config only on `INVESTIGATE` |
| `collect-investigator` | Extract one validated, parent-bound proposal from a saved log |
| `prepare-followup` | Approve and compile the one permitted stratified follow-up |
| `prepare-reporter` | Aggregate-only reporter config for a terminal result |
| `report` | Static HTML report for a primary or follow-up result |
| `inspect-fab` | Check bundle contents and forbidden sentinel strings |

`prepare-phase1` and `prepare-phase2` remain as aliases of `prepare-synthetic` and
`prepare-followup`.

## Build and inspect the bundles

```bash
uv run python scripts/stage_apps.py
uv run flwr build --app apps/cohort-analytics
uv run flwr build --app apps/cohort-agent
uv run cohortlens inspect-fab ANALYTICS.fab \
  --forbid-text PRIVATE_PATIENT_SENTINEL --forbid-text DUMMY_SECRET_SENTINEL
```

## Prepare GDSC2 inputs

Download the GDSC2 fitted dose-response workbook (release 8.5, 27 Oct 2023) and the Cell Model
Passports model list and driver-mutation summary. Keep them under `data/gdsc/source/`, which is
git-ignored.

```bash
uv run cohortlens prepare-gdsc \
  --responses data/gdsc/source/GDSC2_fitted_dose_response_27Oct23.xlsx \
  --models data/gdsc/source/model_list_latest.csv.gz \
  --mutations data/gdsc/source/mutations_summary_latest.csv.gz \
  --output-dir data/gdsc/prepared --approved-by OPERATOR --valid-hours 24
```

Preparation keeps mutation-covered models with BRAF p.V600E or no recorded BRAF driver, excludes
other BRAF driver alterations, and adjusts the primary model for SKCM versus other lineages. It
rejects missing inputs, absent or duplicate PLX-4720 responses, insufficient local support and
rank-deficient designs, and will not overwrite existing snapshots without `--overwrite`.

Because the mutation table is a driver summary, the comparator group is "no recorded BRAF driver",
not confirmed wild type.

Add `--simulation-split lineage-enriched-v1` for a deliberately non-IID split that makes the
heterogeneity gate fire. Its manifest carries a warning that it is for workflow testing, not
inference.

Mount each `site-N.npz` read-only on its own SuperNode. Node setup: [deployment.md](deployment.md).

## Primary run

```bash
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

## Gate and investigator

`prepare-investigator` writes `investigation-decision.json` next to its output and refuses to write
an agent config unless the decision is `INVESTIGATE`.

```bash
uv run cohortlens prepare-investigator \
  --result data/gdsc/prepared/primary-result.json \
  --output data/gdsc/prepared/agent-investigate.toml

uv run flwr run apps/cohort-agent supergrid \
  --federation @ACCOUNT/FEDERATION \
  --run-config data/gdsc/prepared/agent-investigate.toml --stream
uv run flwr log AGENT_RUN_ID supergrid --show > data/gdsc/prepared/investigator.log
uv run cohortlens collect-investigator \
  --log data/gdsc/prepared/investigator.log \
  --parent-result data/gdsc/prepared/primary-result.json \
  --output data/gdsc/prepared/followup-proposal.json
```

## Approve and run the follow-up

```bash
uv run cohortlens prepare-followup \
  --parent-result data/gdsc/prepared/primary-result.json \
  --proposal data/gdsc/prepared/followup-proposal.json \
  --output-dir data/gdsc/prepared/followup --approved-by OPERATOR --valid-hours 24

uv run flwr run apps/cohort-analytics supergrid \
  --federation @ACCOUNT/FEDERATION \
  --run-config data/gdsc/prepared/followup/phase2-analytics-run.toml --stream
```

## Report

```bash
uv run cohortlens prepare-reporter \
  --result data/gdsc/prepared/primary-result.json \
  --output data/gdsc/prepared/agent-report.toml
uv run cohortlens report \
  --result data/gdsc/prepared/primary-result.json \
  --output data/gdsc/prepared/report.html --run-id RUN_ID
```
