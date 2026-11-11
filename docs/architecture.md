# Architecture

## The idea in one paragraph

A statistical question is written down as a contract, hashed, and approved by a person. The
coordinator sends that contract to every site. Each site checks it against its own rules, runs the
regression on rows that never leave the machine, and returns an effect, a standard error and a
count. The coordinator pools the estimates and measures how much the sites disagree. Agreement ends
the study. Disagreement lets a language model look at the pooled numbers and suggest one
predeclared follow-up, which runs only after a person approves a new contract.

## The moves

1. **Contract.** `AnalysisContract` fixes the study, variant, phenotype, covariates, estimator,
   site roster and release policy. Its canonical JSON (sorted keys, compact) is hashed with
   SHA-256. An `Approval` names that digest, a scope and an expiry.
2. **Preflight.** The ServerApp sends `StudyPreflightRequest` to each SuperNode. The ClientApp
   re-validates the approval, then checks scope, variant, phenotype, covariates, policy and
   template against a hard-coded allowlist, then minimum N and minimum group support. It answers
   `READY` or one of seven refusal codes.
3. **Local association.** Ready sites load a two-array NPZ (`design`, `outcome`), fit OLS and
   compute the HC3 robust standard error of the genotype coefficient. The reply carries β, SE,
   N used, the estimator name, aliases and the contract hash. Payloads are capped at 64 KiB.
4. **Aggregation.** The ServerApp binds each reply to its node, alias, study, analysis, query,
   contract hash and stratum. Missing, invalid, duplicate and mismatched replies become explicit
   states. With two or more estimates it runs an inverse-variance fixed-effect meta-analysis and
   reports β, SE, a 95% interval, Cochran Q and I².
5. **Gate.** Plain code, no model. Fewer than two estimates: stop. I² ≥ 0.50: investigate.
   Otherwise: finish.
6. **Investigator.** `AgentEvidence` holds network-level numbers and the parent result hash; site
   results are stripped. The model returns a `FollowUpProposal`, which is parsed strictly and
   checked for an unchanged study, analysis and parent hash. One repair attempt, then failure.
7. **Approval.** `prepare-followup --approved-by` compiles the only permitted follow-up contract
   (two fixed strata, intercept-only within each), with a new analysis ID, digest and approval.
8. **Second run and report.** The ServerApp verifies the strata, parent hash and proposal, then
   repeats moves 2 to 4 per stratum. A reporter call summarises the released aggregates.

## Where things live

| Concern | File |
|---|---|
| Schemas, digests, approval validation, gate, evidence reduction, follow-up compiler | `shared/cohortlens_shared/schemas.py` |
| OLS-HC3, fixed-effect meta-analysis, strata selection, synthetic generator | `shared/cohortlens_shared/statistics.py` |
| GDSC2 preparation and partitioning | `shared/cohortlens_shared/gdsc.py` |
| Coordinator | `apps/cohort-analytics/cohortlens_analytics/server_app.py` |
| Site handlers (`query.preflight`, `query.associate`) | `apps/cohort-analytics/cohortlens_analytics/client_app.py` |
| LLM modes and output validation | `apps/cohort-agent/cohortlens_agent/agent_app.py` |
| Operator CLI | `operator/cohortlens_operator/cli.py` |
| Result and proposal framing | `operator/cohortlens_operator/result_frames.py`, `agent_frames.py` |

## Why two bundles

A Flower App Bundle contains either an AgentApp or a ServerApp + ClientApp pair. The analytics
bundle therefore never contains model-calling code, and the agent bundle never contains site
handlers. They share no runtime channel: each prints one framed line
(`COHORTLENS_RESULT_V1 {...}` or `COHORTLENS_AGENT_RESULT_V1 {...}`), and the operator CLI carries
validated artifacts from one to the other.

Both bundles must be self-contained, so `scripts/stage_apps.py` copies `shared/cohortlens_shared`
into each app directory and verifies a tree digest. `tests/test_packaging.py` fails if the copies
drift. Edit `shared/` only.

## The model step

The agent makes one streamed call to `flower-endeavor` through the OpenAI-compatible Responses API.
The Flower Runtime injects the endpoint and task token, so no provider key is in the bundle. There
are no tools. Actions are `Literal` types, the proposal must declare exactly the contract fields it
changes, and the prompt offers a single permitted follow-up. During development Endeavor once
returned an unapproved JSON shape and validation stopped the run before any analytics; the single
repair retry was added after that.

## Data protection

- Site snapshots hold numeric arrays only. Cell-line identifiers stay in source-side preparation.
- Run configs carry contracts, approvals and proposals inline; no file paths cross.
- `cohortlens inspect-fab` allowlists bundle contents and scans for forbidden sentinel strings.
- Partitioning is response-blind: `sha256(SANGER_MODEL_ID) mod 3`. The optional
  `lineage-enriched-v1` split is also response-blind and deliberately non-IID, to exercise the
  heterogeneity path; a test shifts every outcome by +1000 and asserts the assignment is unchanged.

## Limits

- No secure aggregation or differential privacy. The coordinator receives site summaries, and
  result frames are visible to anyone who can read run logs.
- The site roster is a label on the contract, not an enforced list of node identities.
- Hashes, approvals and reply validation reduce protocol mistakes. They do not prove that a site
  used honest records or calculations.
- Three hash partitions of one dataset are not independent replications.
- Production use would need governed access, audit trails, code attestation and data-use review.
