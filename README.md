# CohortLens

Federated analytics on [Flower](https://flower.ai): one approved statistical question travels to
every site, each site answers it on its own data, and only three numbers per site come back.
If the sites disagree, a tool-using LLM investigator plans over the pooled numbers, proposes one
follow-up from a closed catalog, and a human signs off each round within a fixed query budget.

![Investigation loop: federated round, gate, tool-using investigator, human approval, next round](docs/investigation-loop.gif)

Step-by-step explainer with animated diagrams: [apzl.github.io/cohort-lens](https://apzl.github.io/cohort-lens/).

## What it is

Several data holders often want to answer the same question but cannot pool their records.
CohortLens sends the question instead. A coordinator distributes a hash-bound analysis contract,
each site fits the same regression locally and returns an effect, a robust standard error and a
count, and the coordinator combines them with a fixed-effect meta-analysis.

When the sites disagree, an investigator agent works out what to ask next. It sees only
network-level aggregates, inspects them through deterministic tools, weighs a closed catalog of
follow-up analyses by what each reveals and discloses, and proposes one. Nothing runs until a
person approves a new contract, and every site re-checks that contract against its own allowlist.
After each approved follow-up the agent re-plans with the new aggregates, until it stops or the
contract's query budget is spent.

The demonstration study asks whether **BRAF p.V600E** is associated with response to **PLX-4720**
in the public GDSC2 cancer cell-line screen, split across three emulated laboratories running as
Flower SuperNodes. This is a personal project on public in-vitro data: an emulated federation, not
a patient study.

## How it works

| # | Step | Where it runs | What crosses the wire |
|---|---|---|---|
| 1 | Prepare site snapshots; a human approves the primary contract | operator CLI | nothing |
| 2 | Preflight: each site re-validates the approval, allowlist and query budget | ServerApp → ClientApps | `StudyPreflightRequest` → `PreflightReply` |
| 3 | Local association: OLS with HC3 robust standard errors | each ClientApp | `AssociationReply` (β, SE, N) |
| 4 | Validate replies, inverse-variance meta-analysis, Cochran Q and I² | ServerApp | `AssociationResult` |
| 5 | Deterministic gate: finish, investigate (I² ≥ 0.50), stop, or budget spent | operator CLI | `InvestigationDecision` |
| 6 | Investigator: bounded tool loop over network aggregates only | AgentApp | `InvestigationEvidence` → `InvestigatorRecord` (`FollowUpProposal` + trace) |
| 7 | Human approval compiles the proposal into a new, parent-bound contract | operator CLI | `AnalysisContract` + `Approval` |
| 8 | Follow-up round: leave-one-site-out or stratified; then back to step 5 | ServerApp, ClientApps | `LeaveOneSiteOutResult` or `StratifiedAssociationResult` |

`cohortlens run-study` drives steps 1–8 and writes a hash-chained run ledger; every step can also
be run by hand.

Three boundaries hold throughout:

- **Rows stay on the node.** Only the allowlisted summary leaves a site.
- **The model sees aggregates only.** Site-level results are stripped before any LLM call; the
  evidence and tool outputs have no field that could carry them.
- **The model proposes, a human authorises.** A follow-up runs only under a new contract digest and
  a fresh, expiring approval, within the budget the primary contract declared.

Design and trade-offs: [docs/architecture.md](docs/architecture.md).

## Technical details

- **Hash-bound contract chain.** Every analysis is a frozen Pydantic model (`extra="forbid"`, no
  NaN/Inf), serialised to canonical JSON and identified by its SHA-256. Approvals bind to that
  digest and expire. Each follow-up contract also binds the hash of the released parent result and
  of the exact proposal the operator reviewed, and carries its round index and the chain's budget
  (default: primary + 2 follow-ups).
- **Defence in depth at the site.** A ClientApp re-validates the approval and checks the request
  against its own closed allowlist (template, covariates, strata, round), its own budget cap,
  minimum sample size and minimum group support before touching data. A valid approval for an
  analysis outside the allowlist or beyond budget is refused.
- **A tool-using investigator.** The agent runs a plan → act → observe loop with hard caps on
  steps, tool calls and invalid turns. Six deterministic tools (current evidence, history,
  heterogeneity diagnostics, action catalog, precondition check, proposal submission) run locally
  over aggregate-only evidence. The prompt describes the role, constraints and tools; it never
  contains the answer. Submissions are validated against the strict schema, the parent-result
  binding and the action's preconditions, and errors go back to the model so it can repair them.
- **Two tool protocols.** Native Responses function calling, or a provider-agnostic protocol in
  which the model replies with one strict JSON action per turn. A native request the provider
  rejects with HTTP 400 before any native turn has succeeded falls back to JSON. Whether Flower Endeavor supports native tool calling is
  unconfirmed, so JSON is the default.
- **A real action catalog.** Leave-one-site-out sensitivity (no new site-level quantity), a
  predeclared lineage/context stratification (new per-stratum summaries), or stop. Each query
  action compiles deterministically to one contract shape that sites allowlist.
- **Auditable runs.** Every gate decision, tool call and result, proposal, approval (who, when,
  digest, interactive or scripted), submitted run and released aggregate goes into an append-only
  JSONL ledger in which each entry hashes the previous one. `cohortlens verify-ledger` checks it.
- **Statistics without heavy dependencies.** OLS with HC3 sandwich errors is written in NumPy and
  matches `statsmodels` 0.14.5 to a relative tolerance of 1e-10. The agent bundle has no numerical
  dependency: its Q p-value is a standard-library chi-square tail, checked against SciPy.
- **Explicit participation states.** Missing, invalid, duplicate and identity-mismatched replies are
  recorded, never silently dropped; a result is `COMPLETE`, `PARTIAL` or `INSUFFICIENT`.
- **Log-framed transport.** Apps emit one framed, schema-valid result line; the operator collects it
  from run logs, accepts identical replays and rejects conflicts.
- **Two self-contained bundles.** A Flower App Bundle holds either an AgentApp or a
  ServerApp + ClientApp pair, so each bundle is staged with only the shared modules it imports and
  a test fails on drift.

## Requirements

- Python 3.11 or 3.12
- [uv](https://docs.astral.sh/uv/)
- For live runs: a Flower account with SuperGrid access and three registered SuperNodes
  ([docs/deployment.md](docs/deployment.md)). Tests and the offline demo need neither.

## Setup

```bash
git clone https://github.com/apzl/cohort-lens.git && cd cohort-lens
uv sync --extra test
uv run python scripts/stage_apps.py     # copy shared code into both app bundles
uv run pytest
```

## Run

```bash
# Offline, whole loop: the real handlers in-process, a deterministic scripted policy in place of
# the model, non-interactive approvals. Synthetic sites need no data.
uv run cohortlens run-study --data-mode synthetic --backend local --investigator scripted \
  --approve-as demo-operator --ledger runs/demo/ledger.jsonl --output-dir runs/demo

# Public GDSC2: build three site snapshots, the contract, the approval and the run config
uv run cohortlens prepare-gdsc \
  --responses data/gdsc/source/GDSC2_fitted_dose_response_27Oct23.xlsx \
  --models data/gdsc/source/model_list_latest.csv.gz \
  --mutations data/gdsc/source/mutations_summary_latest.csv.gz \
  --output-dir data/gdsc/prepared --approved-by OPERATOR --valid-hours 24

# Live: the same loop on SuperGrid, pausing for your approval at every contract
uv run cohortlens run-study --data-mode gdsc --snapshots data/gdsc/prepared \
  --backend supergrid --investigator agentapp --federation @ACCOUNT/FEDERATION \
  --approver YOUR_NAME --ledger runs/live/ledger.jsonl --output-dir runs/live
```

The scripted policy is labelled as such in the ledger and report and is never presented as model
output. To try a real model offline, point `--investigator openai-compatible` at an endpoint with
`--model-base-url`, `--model-name` and an API key in `COHORTLENS_MODEL_API_KEY`. The step-by-step
commands (collect, gate, investigator, approval, follow-up) are in [docs/usage.md](docs/usage.md).

## Outputs

| Path | Content |
|---|---|
| `data/gdsc/prepared/site-{1,2,3}.npz` | Site-local design matrix and outcome, one per SuperNode |
| `data/gdsc/prepared/manifest.json` | Source checksums, counts, partition rule, assignment digest |
| `contract.json`, `approval.json`, `analytics-run.toml` | Hash-bound primary contract, expiring approval, run config |
| `ledger.jsonl` | Hash-chained run ledger: gates, tool trace, proposals, approvals, released aggregates |
| `round-N-{contract,approval,result}.json` | Each round's contract, approval and full released result |
| `round-N-investigator-record.json` | Investigator outcome with its complete tool trace |
| `study-report.html` | Static report rendered from the verified ledger |
| `docs/sample-run/` | Sanitised ledgers from the offline GDSC2 demo, for the explainer page |

`data/` and `runs/` are git-ignored; source data never enters the repository or a bundle.

## Project layout

```
shared/cohortlens_shared/        single source of truth, staged into the apps
  schemas.py                     contracts, approvals, results, evidence, proposals, gates
  catalog.py                     action catalog, preconditions, follow-up compiler, budget checks
  evidence.py                    aggregate-only reductions (operator only)
  tools.py                       investigator tools and their JSON schemas (agent bundle)
  investigator.py                bounded tool loop, native and JSON protocols (agent bundle)
  statistics.py                  OLS-HC3, fixed-effect and leave-one-out meta-analysis (analytics)
  gdsc.py                        GDSC2 snapshot preparation and partitioning (analytics)
apps/cohort-analytics/           Flower FederatedApp bundle
  cohortlens_analytics/
    server_app.py                coordinator: preflight, fan-out, validation, rounds, LOSO
    client_app.py                site: allowlist and budget checks, local regression
  cohortlens_shared/             generated copy, do not edit
apps/cohort-agent/               Flower AgentApp bundle
  cohortlens_agent/agent_app.py  investigator (tool loop) and reporter modes
  cohortlens_shared/             generated copy, do not edit
operator/cohortlens_operator/    `cohortlens` CLI
  cli.py, commands.py            parser and step-by-step commands
  study_commands.py              run-study, verify-ledger
  orchestrator.py                the multi-round loop
  backends.py, local_grid.py     local (in-process) and SuperGrid backends
  ledger.py                      hash-chained JSONL ledger
  approvals.py                   interactive and scripted approval
  scripted_policy.py             deterministic stand-in for the model (tests, offline demo)
  model_clients.py               scripted or OpenAI-compatible transports for the local backend
  result_frames.py, agent_frames.py  framed result extraction from run logs
  report.py, fab.py              static HTML reports, bundle content inspection
scripts/stage_apps.py            copies shared/ modules into each app, verifies digests
tests/                           110 offline tests against fake grids and fake model clients
docs/                            explainer page, architecture, usage, results, deployment, sample run
```

## Tests

```bash
uv run pytest
```

110 tests, offline, about 3 seconds. They cover contract, approval and chain binding; site refusals
including over-budget contracts with valid approvals; partial participation; the gates; the tools
and the chi-square tail against SciPy; both tool protocols with fake models, including malformed
turns, cap exhaustion, repair after a rejected submission and protocol fallback; that no request
to a model contains a site-level field; catalog-to-contract compilation; leave-one-site-out
statistics against direct recomputation; ledger tamper detection; the orchestrated loop end to end;
bundle packaging; and the numerical cross-check against statsmodels.

## Results

| Run | Sites | Pooled β | 95% CI | I² | Gate |
|---|---|---|---|---|---|
| GDSC2 primary, SuperGrid | 3 of 3 (N = 291 / 306 / 341) | −2.3148 ln µM | [−2.8232, −1.8063] | 0% | `FINISH_COMPATIBLE` |
| Synthetic primary, SuperGrid | 3 of 3 (3 × 1,500 rows) | 0.5108 | | 97.1% | `INVESTIGATE` |
| Synthetic follow-up, two strata | 3 of 3 in each | 0.140 and 0.862 | | 0% in each | finished |

These SuperGrid runs predate the tool-using investigator: the synthetic follow-up was proposed by
the earlier single-call investigator. The current loop is verified offline only, on the local
backend with the scripted policy and fake model clients, until a live run is recorded. Offline on
real GDSC2 data (`lineage-enriched-v1` split), the primary gave β = −2.4097, I² = 0.62; the
scripted policy chose leave-one-site-out (2 of 3 omissions clear the flag), then lineage
stratification (SKCM β = −3.42, I² = 0; non-SKCM β = −1.96, I² = 0.77), and the budget ended the
study. Details: [docs/results.md](docs/results.md).

## Limits

- An emulated federation: three partitions of one public dataset, not independent laboratories.
- Public cell-line assays, not patients. Associations here are not clinical findings.
- No secure aggregation or differential privacy. The coordinator sees site summaries.
- Validation catches protocol mistakes; it cannot prove a site computed honestly.
- With three sites, heterogeneity estimates are unstable, and leave-one-site-out pools two.
- Sites keep no cross-run memory: the budget is checked per contract, so an operator who
  re-approves a new primary contract starts a new chain. The ledger records it; nothing prevents it.
- The tool loop has not run against Flower Endeavor yet; its native tool-calling support is
  unconfirmed.

## License

Apache-2.0
