# CohortLens analytics FAB

This Flower app contains the CohortLens Phase 1 `ServerApp`/`ClientApp`
workflow. The coordinator sends one approved, hash-bound synthetic analysis
contract. Each of three ClientApps checks compatibility, loads its configured
node-local snapshot (or uses the explicit simulation fixture), fits the fixed
OLS-HC3 regression, and returns only an allowlisted summary. The ServerApp validates source identity and identifiers, records
non-response and invalid/duplicate states, and performs inverse-variance
fixed-effect meta-analysis.

From the repository root:

```shell
uv run python scripts/stage_apps.py
uv run flwr build --app apps/cohort-analytics
uv run cohortlens prepare-phase1 \
  --output-dir runs/phase1-demo --approved-by OPERATOR
uv run flwr run apps/cohort-analytics local \
  --federation-config 'num-supernodes=3' \
  --run-config runs/phase1-demo/analytics-run.toml --stream
```

Deployment SuperNodes must provide `site-alias`, `dataset-path`, and
`snapshot-alias` through local node configuration. The application releases coefficients, standard errors, sample sizes, site
states, and the aggregate result. It never releases the synthetic subject rows.
Simulation validates function and message discipline; it is not evidence of
independent institutional custody, formal privacy, or honest site computation.
