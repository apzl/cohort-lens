# Architecture

## The idea in one paragraph

A statistical question is written down as a contract, hashed, and approved by a person. The
coordinator sends that contract to every site. Each site checks it against its own rules, runs the
regression on rows that never leave the machine, and returns an effect, a standard error and a
count. The coordinator pools the estimates and measures how much the sites disagree. Agreement ends
the study. Disagreement opens an investigation: a language model, seeing only pooled numbers,
uses deterministic tools to decide which follow-up from a closed catalog is worth its disclosure
cost, and proposes it. A person approves a new contract, the sites run it, and the model re-plans
with the new aggregates, until it stops or the contract's query budget is spent.

## The moves

1. **Contract.** `AnalysisContract` fixes the study, variant, phenotype, covariates, estimator,
   site roster, release policy, round index and query budget (`max_association_rounds`, default 3:
   primary + 2 follow-ups). Its canonical JSON (sorted keys, compact) is hashed with SHA-256. An
   `Approval` names that digest, a scope and an expiry. A follow-up contract must bind
   `parent_result_hash` and `proposal_hash`; a primary must bind neither.
2. **Preflight.** The ServerApp sends `StudyPreflightRequest` to each SuperNode. The ClientApp
   re-validates the approval, then checks variant, phenotype, template, covariates, strata, round
   and policies against its own allowlist table (`client_app.SITE_POLICIES`, independent of the
   shared catalog), then its budget cap (`SITE_MAX_ASSOCIATION_ROUNDS = 3` and
   `round_index < max_association_rounds`), then minimum N and group support. It answers `READY` or
   a refusal with a reason code (for example `QUERY_BUDGET_EXHAUSTED`, `ROUND_TEMPLATE_MISMATCH`).
3. **Local association.** Ready sites load a two-array NPZ (`design`, `outcome`), fit OLS and
   compute the HC3 robust standard error of the genotype coefficient. The reply carries β, SE,
   N used, the estimator name, aliases and the contract hash. Payloads are capped at 64 KiB.
4. **Aggregation.** The ServerApp binds each reply to its node, alias, study, analysis, query,
   contract hash and stratum. Missing, invalid, duplicate and mismatched replies become explicit
   states. With two or more estimates it runs an inverse-variance fixed-effect meta-analysis and
   reports β, SE, a 95% interval, Cochran Q and I².
5. **Gate.** Plain code, no model. After the primary (`investigation_decision_from_result`): fewer
   than two estimates, stop; I² ≥ 0.50, investigate; otherwise finish. After a follow-up
   (`continuation_decision`): an insufficient round stops, a spent budget stops
   (`BUDGET_EXHAUSTED`), otherwise the investigator may re-plan. The gate never judges whether
   another query is worthwhile; that is the investigator's proposal and the operator's approval.
6. **Investigator.** `InvestigationEvidence` holds the aggregate-only history of every released
   round (`RoundSummary`: network, per-stratum and leave-one-site-out reductions), the binding
   hashes and the budget. The AgentApp runs `InvestigatorLoop` (see below) and frames an
   `InvestigatorRecord`: the validated `FollowUpProposal` or a recorded failure, plus the full tool
   trace.
7. **Approval.** The operator sees the proposal, its rationale and the exact digest of the contract
   `catalog.compile_followup` derives from it, and approves or declines. Compilation re-checks the
   parent binding and the action's preconditions, so a human cannot approve something the catalog
   forbids.
8. **Follow-up round.** The ServerApp checks the contract against the proposal
   (`catalog.verify_followup_binding`: proposal hash, parent hash, round, template ↔ action, budget)
   and dispatches by template. Leave-one-site-out re-runs the primary model and additionally
   releases the pooled result with each site omitted; stratification runs moves 2–4 per stratum.
   The released aggregates are appended to the history and the loop returns to move 5.

## The investigator loop

`shared/cohortlens_shared/investigator.py`, used unchanged by the AgentApp (with the Flower runtime
endpoint) and by the operator's local backend (with a scripted policy or an explicit endpoint).

- **Plan → act → observe.** Each step is one model request. The model calls a tool; the loop runs
  it locally and returns the output; the model decides the next call. It ends when
  `submit_proposal` accepts a proposal or a cap is hit.
- **Caps.** `max_steps` (12), `max_tool_calls` (16), `max_invalid_turns` (3),
  `max_output_tokens` (2000), all overridable in run config. The OpenAI client is built with
  `max_retries=0`. A cap ends the run with a `FAILED` record; no query follows.
- **Tools** (`tools.py`, pure functions of the evidence, standard library only):

  | Tool | Returns |
  |---|---|
  | `get_current_evidence` | binding fields, next round, budget, threshold, latest round |
  | `get_history` | every released round's aggregate summary |
  | `heterogeneity_diagnostics` | Q, df, Q p-value, I², H², margin to the 0.50 threshold, per estimate; LOSO ranges |
  | `list_actions` | the catalog: what each action tests, releases, discloses, and which contract fields it changes |
  | `check_action` | named preconditions passed and failed, resulting round and budget |
  | `submit_proposal` | accepted, or the list of schema, binding and precondition errors |

- **Protocols.** `native` sends Responses `tools` and reads `function_call` items, then returns
  `function_call_output` items. `json` lists the tools in the instructions and expects exactly one
  `{"tool": ..., "arguments": {...}}` object per turn; anything else is an invalid turn with
  feedback. The request carries only `model`, `instructions`, `input`, `max_output_tokens` and, for
  native, `tools` and `tool_choice`; `input` is rebuilt from stored messages every turn because the
  runtime's default provider does not support `previous_response_id`. A native request rejected
  with HTTP 400 before any native turn succeeded restarts the conversation in `json`, and the trace
  records the fallback.
- **No answer in the prompt.** The instructions describe the role, what the model can see and do,
  and the tool protocol. Binding values reach the model only through tool outputs.
- **Events.** The AgentApp forwards every raw stream event and emits one
  `cohortlens.investigator.trace` event per tool call, tool result, invalid turn or fallback, and a
  final `cohortlens.investigator.outcome` event.

## Action catalog

`shared/cohortlens_shared/catalog.py`. Three actions per data mode:

| Action | Template | Sites release | Investigator receives | Preconditions |
|---|---|---|---|---|
| `LEAVE_ONE_SITE_OUT` | `ols_quantitative_loso_v1`, primary covariates | the primary β, SE, N again | range of pooled β and I² over omissions; how many omissions clear the flag | budget left, not run, primary flagged, ≥ 3 primary estimates |
| `STRATIFY_GDSC_LINEAGE` / `STRATIFY_SYNTHETIC_CONTEXT` | `ols_quantitative_lineage_stratified_v1` / `…context…`, two fixed strata | β, SE, N per stratum (new) | per-stratum pooled β, CI, Q, I² | budget left, not run, primary flagged, ≥ 2 primary estimates |
| `STOP_NO_FOLLOWUP` | none | nothing | nothing | always |

Leave-one-site-out discloses no new site-level quantity to the coordinator; it runs as its own
approved round so that releasing the omission summaries is a separately authorised act. Its result
(`LeaveOneSiteOutResult`) gives the operator each omission by site; the investigator's reduction
(`LosoAggregate`) drops site identities and per-omission values.

The earlier vocabulary `RECHECK_PHENOTYPE_METADATA`, `RECHECK_GDSC_METADATA`,
`REPORT_PRIMARY_RESULT` and the investigator's `STOP_INSUFFICIENT_EVIDENCE` was never runnable and
has been removed; `STOP_INSUFFICIENT_EVIDENCE` remains a gate decision.

## Orchestrator and ledger

`cohortlens run-study` (`operator/cohortlens_operator/orchestrator.py`) drives the loop behind one
backend interface:

- `local` runs the real `server_app.execute_contract` and ClientApp handlers in-process over an
  in-memory grid (`local_grid.InMemoryGrid`) and re-collects each printed result frame. It is an
  execution aid without a Flower runtime, network, isolation or timeouts.
- `supergrid` writes each round's run config and calls `flwr run … --stream`, then
  `flwr log RUN_ID supergrid --show`, and reuses the frame collectors. It has not been exercised
  in this repository's tests beyond command construction and log parsing with a fake runner.

Approval is interactive by default: the operator must retype the first 12 hex characters of the
contract digest. `--approve-as NAME` approves without asking and is recorded as
`SCRIPTED_NON_INTERACTIVE`. The model client on the local backend is either the scripted policy
(deterministic rules, labelled `SCRIPTED_POLICY` everywhere) or an OpenAI-compatible endpoint
given by explicit flags.

The ledger (`ledger.py`) is JSONL; each entry holds `seq`, `entry_type`, `recorded_at`,
`study_id`, `payload`, `prev_hash` and `entry_hash` (SHA-256 of the other fields' canonical JSON).
`verify-ledger` rejects edited, removed, inserted or reordered entries. Released results enter the
ledger as aggregate summaries and hashes; full results go to the output directory.

## Where things live

| Concern | File : function |
|---|---|
| Schemas, digests, approval validation, both gates | `shared/cohortlens_shared/schemas.py` |
| Catalog, preconditions, compiler, coordinator binding check | `shared/cohortlens_shared/catalog.py` : `catalog_for`, `check_preconditions`, `compile_followup`, `verify_followup_binding` |
| Aggregate-only reductions | `shared/cohortlens_shared/evidence.py` : `round_summary`, `investigation_evidence` |
| Tools | `shared/cohortlens_shared/tools.py` : `InvestigationToolbox`, `tool_specs`, `chi_square_sf` |
| Tool loop and transports | `shared/cohortlens_shared/investigator.py` : `InvestigatorLoop`, `OpenAIResponsesTransport` |
| OLS-HC3, meta-analysis, leave-one-out | `shared/cohortlens_shared/statistics.py` : `fit_genotype_ols_hc3`, `fixed_effect_meta`, `leave_one_site_out` |
| GDSC2 preparation and partitioning | `shared/cohortlens_shared/gdsc.py` |
| Coordinator | `apps/cohort-analytics/cohortlens_analytics/server_app.py` : `execute_contract`, `run_phase1`, `run_stratified`, `run_leave_one_site_out` |
| Site handlers and allowlist | `apps/cohort-analytics/cohortlens_analytics/client_app.py` : `_compatibility_status`, `SITE_POLICIES` |
| Agent modes | `apps/cohort-agent/cohortlens_agent/agent_app.py` : `run_investigator`, `run_reporter` |
| Orchestrator, backends, ledger, approvals | `operator/cohortlens_operator/` |

## Why two bundles

A Flower App Bundle contains either an AgentApp or a ServerApp + ClientApp pair, and an AgentApp
has no `Grid`: it cannot start a federated run or reach a SuperNode. The analytics bundle
therefore never contains model-calling code, and the agent bundle never contains site handlers.
They share no runtime channel: each prints one framed line (`COHORTLENS_RESULT_V1 {...}` or
`COHORTLENS_AGENT_RESULT_V2 {...}`), and the operator carries validated artifacts from one to the
other. That is why the multi-round loop lives in the operator, not in the AgentApp.

Both bundles must be self-contained, so `scripts/stage_apps.py` copies the shared modules each
bundle imports: `schemas`, `catalog`, `tools` and `investigator` into the agent (no NumPy code),
and `schemas`, `catalog`, `statistics` and `gdsc` into analytics (no investigator code).
`tests/test_packaging.py` fails if the copies drift. Edit `shared/` only.

## The model step

The AgentApp reaches `flower-endeavor` through the OpenAI-compatible Responses API. The Flower
Runtime injects the endpoint and task token, so no provider key is in the bundle. Earlier live
runs used a single call with no tools whose prompt contained the only permitted proposal; during
that period Endeavor once returned an unapproved JSON shape and validation stopped the run before
any analytics. The current loop replaces that design. It has been verified offline only, with
fake model clients and the scripted policy.

## Data protection

- Site snapshots hold numeric arrays only. Cell-line identifiers stay in source-side preparation.
- Run configs carry contracts, approvals, proposals and evidence inline; no file paths cross.
- Every request sent to a model in tests is checked for site-level fields.
- `cohortlens inspect-fab` allowlists bundle contents and scans for forbidden sentinel strings.
- Partitioning is response-blind: `sha256(SANGER_MODEL_ID) mod 3`. The optional
  `lineage-enriched-v1` split is also response-blind and deliberately non-IID, to exercise the
  heterogeneity path; a test shifts every outcome by +1000 and asserts the assignment is unchanged.

## Limits

- No secure aggregation or differential privacy. The coordinator receives site summaries, and
  result frames are visible to anyone who can read run logs.
- With three sites, the leave-one-site-out range and the full pooled estimate together are close
  to site-level information even without identities.
- The site roster is a label on the contract, not an enforced list of node identities.
- The budget is enforced per contract; sites keep no memory across runs, so a fresh primary
  approval starts a fresh chain.
- Hashes, approvals and reply validation reduce protocol mistakes. They do not prove that a site
  used honest records or calculations.
- Three hash partitions of one dataset are not independent replications.
- Production use would need governed access, audit trails, code attestation and data-use review.
