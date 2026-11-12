# Usage

Every command, in workflow order. `cohortlens` is the operator CLI; `flwr` is the Flower CLI.
Replace `@ACCOUNT/FEDERATION`, `RUN_ID`, `CONTRACT_HASH` and `OPERATOR` with real values.

## CLI reference

| Command | Purpose |
|---|---|
| `run-study` | Drive the whole loop (primary, gate, investigator, approval, follow-ups) with a ledger |
| `verify-ledger` | Verify a run ledger's hash chain |
| `reference` | Compute the offline three-site synthetic reference |
| `prepare-synthetic` | Primary contract, approval and run config for the synthetic track |
| `prepare-supernode-data` | Three synthetic snapshots for deployment SuperNodes |
| `prepare-gdsc` | GDSC2 site snapshots, manifest, primary contract, approval, run config |
| `collect` | Extract one validated result frame from a saved run log |
| `prepare-investigator` | Apply the gate; write evidence and an investigator config only on `INVESTIGATE` |
| `collect-investigator` | Extract one validated, evidence-bound investigator record from a saved log |
| `prepare-followup` | Approve and compile the contract a reviewed proposal permits |
| `prepare-reporter` | Aggregate-only reporter config over the released chain |
| `report` | Static HTML report for a primary, stratified or leave-one-site-out result |
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

## Orchestrated run

`run-study` is the recommended path. It approves the primary contract, runs it, applies the gate,
runs the investigator when the gate says `INVESTIGATE`, shows you each proposal with its rationale
and exact contract digest, runs what you approve, and repeats until the investigator stops, the
gate ends the study, you decline, or the budget (`--followup-budget`, default 2) is spent.

```bash
# Offline demo on real GDSC2 snapshots: local backend, scripted policy, non-interactive approvals
uv run cohortlens run-study --data-mode gdsc --snapshots data/gdsc/lineage-enriched-v1 \
  --backend local --investigator scripted --approve-as demo-operator \
  --ledger runs/demo-lineage/ledger.jsonl --output-dir runs/demo-lineage
uv run cohortlens verify-ledger runs/demo-lineage/ledger.jsonl
```

| Option | Meaning |
|---|---|
| `--data-mode synthetic\|gdsc` | Synthetic sites are generated; GDSC needs `--snapshots DIR` with `site-{1,2,3}.npz` and `manifest.json` |
| `--backend local\|supergrid` | In-process handlers over an in-memory grid, or `flwr run`/`flwr log` against SuperGrid |
| `--investigator scripted\|openai-compatible\|agentapp` | Scripted policy (local), an explicit endpoint (local), or the Flower AgentApp (supergrid) |
| `--tool-protocol json\|native`, `--no-protocol-fallback` | Tool protocol; native falls back to json on a rejected request unless disabled |
| `--max-steps`, `--max-tool-calls`, `--max-invalid-turns` | Loop caps (12, 16, 3) |
| `--approver NAME` | Interactive approval: you retype the first 12 hex characters of each digest |
| `--approve-as NAME` | Non-interactive approval for demos and tests; recorded as `SCRIPTED_NON_INTERACTIVE` |
| `--ledger PATH`, `--output-dir DIR` | New ledger file (never appended to); per-round contracts, approvals, results, records, `study-report.html` |

With `--investigator openai-compatible`, give `--model-base-url`, `--model-name`, and the API key
in the environment variable named by `--model-api-key-env` (default `COHORTLENS_MODEL_API_KEY`).

### Live SuperGrid run

Not yet exercised against SuperGrid in this version. Steps:

1. Prepare snapshots (`prepare-gdsc`, below) and mount each `site-N.npz` read-only on its own
   SuperNode ([deployment.md](deployment.md)).
2. Stage the shared code: `uv run python scripts/stage_apps.py`.
3. Log in to SuperGrid with the Flower CLI as for the earlier runs.
4. Run:

   ```bash
   uv run cohortlens run-study --data-mode gdsc --snapshots data/gdsc/prepared \
     --backend supergrid --investigator agentapp --federation @ACCOUNT/FEDERATION \
     --tool-protocol json --approver OPERATOR \
     --ledger runs/live/ledger.jsonl --output-dir runs/live
   ```

   For the heterogeneity path use `--snapshots data/gdsc/lineage-enriched-v1` (the SuperNodes must
   then hold that split's snapshots). Each analytics and investigator run's config and log are
   written to `--output-dir`.
5. `uv run cohortlens verify-ledger runs/live/ledger.jsonl`.

Try `--tool-protocol native` once to learn whether Endeavor accepts Responses `tools`; a rejected
request falls back to `json` and the ledger records `AGENT_PROTOCOL_FALLBACK`.

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
inference. `--followup-budget` (0–2, default 2) sets the primary contract's query budget.

Mount each `site-N.npz` read-only on its own SuperNode. Node setup: [deployment.md](deployment.md).

## Step by step

The same loop by hand. Each follow-up round needs the released results and contracts of every
earlier round, in order, as `--round RESULT CONTRACT` pairs (`--result PRIMARY` is a shorthand for
a lone primary produced from a default contract).

### Primary run

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

### Gate and investigator

`prepare-investigator` writes `investigation-decision.json` and `investigation-evidence.json` next
to its output and refuses to write an agent config unless the decision is `INVESTIGATE`.

```bash
uv run cohortlens prepare-investigator \
  --round data/gdsc/prepared/primary-result.json data/gdsc/prepared/contract.json \
  --output data/gdsc/prepared/r1/agent-investigate.toml

uv run flwr run apps/cohort-agent supergrid \
  --federation @ACCOUNT/FEDERATION \
  --run-config data/gdsc/prepared/r1/agent-investigate.toml --stream
uv run flwr log AGENT_RUN_ID supergrid --show > data/gdsc/prepared/r1/investigator.log
uv run cohortlens collect-investigator \
  --log data/gdsc/prepared/r1/investigator.log \
  --evidence data/gdsc/prepared/r1/investigation-evidence.json \
  --output data/gdsc/prepared/r1/followup-proposal.json
```

`collect-investigator` also writes `investigator-record.json` with the full tool trace. A
`STOP_NO_FOLLOWUP` proposal ends the study; there is nothing to approve.

### Approve and run the follow-up

```bash
uv run cohortlens prepare-followup \
  --evidence data/gdsc/prepared/r1/investigation-evidence.json \
  --parent-contract data/gdsc/prepared/contract.json \
  --proposal data/gdsc/prepared/r1/followup-proposal.json \
  --output-dir data/gdsc/prepared/r1/followup --approved-by OPERATOR --valid-hours 24

uv run flwr run apps/cohort-analytics supergrid \
  --federation @ACCOUNT/FEDERATION \
  --run-config data/gdsc/prepared/r1/followup/followup-analytics-run.toml --stream
```

Collect the result with `collect` (query id = the follow-up `analysis_id`, contract hash = the
printed `contract_hash`), then repeat the gate step with one more `--round`:

```bash
uv run cohortlens prepare-investigator \
  --round data/gdsc/prepared/primary-result.json data/gdsc/prepared/contract.json \
  --round data/gdsc/prepared/r1/result.json data/gdsc/prepared/r1/followup/followup-contract.json \
  --output data/gdsc/prepared/r2/agent-investigate.toml
```

### Report

```bash
uv run cohortlens prepare-reporter \
  --round data/gdsc/prepared/primary-result.json data/gdsc/prepared/contract.json \
  --output data/gdsc/prepared/agent-report.toml
uv run cohortlens report \
  --result data/gdsc/prepared/primary-result.json \
  --output data/gdsc/prepared/report.html --run-id RUN_ID
```
