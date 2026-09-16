---
tags: [agentapp]
dataset: []
framework: []
---

# CohortLens AgentApp

This Flower `AgentApp` uses Flower Endeavor 1.0 (`flower-endeavor`) for two
strictly bounded Phase 1 roles:

- `planner` can propose only the fixed synthetic primary association template;
- `reporter` receives a validated aggregate-only `AgentEvidence` packet and
  explains the released result and its limitations.

Model output is parsed into a strict schema and checked against the input study
and analysis identifiers before one `COHORTLENS_AGENT_RESULT_V1` frame is
emitted. The model cannot authorize a contract or supply computed result
fields. Flower Runtime provides the model endpoint and task token, so provider
credentials are not stored in the FAB.

Build from the repository root:

```shell
uv run python scripts/stage_apps.py
uv run flwr build --app apps/cohort-agent
```

Run with a generated planner or reporter TOML file:

```shell
uv run flwr run apps/cohort-agent supergrid \
  --federation @ACCOUNT/FEDERATION --run-config RUN_CONFIG.toml --stream
```

Federated analytics live in the separate `cohortlens-analytics` FAB.
