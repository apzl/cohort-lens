# Sample run ledgers

Two hash-chained run ledgers from `cohortlens run-study`, kept so the explainer page can replay a
real multi-round investigation.

**How they were produced — read this before quoting them:**

- **Backend: `local`.** The real ServerApp and ClientApp handler code ran in one process over an
  in-memory grid. This is **not a SuperGrid run**: no Flower runtime, network, SuperNodes or
  timeouts were involved.
- **Investigator: the scripted policy (`cohortlens-scripted-policy-v1`), not a language model.**
  It is a fixed, deterministic rule set (see `operator/cohortlens_operator/scripted_policy.py`)
  that drives the same tool loop, tools, schema validation and ledger a model would. Every agent
  entry in these ledgers carries `"policy_kind": "SCRIPTED_POLICY"`. Nothing here is model output.
- **Approvals: non-interactive (`--approve-as demo-operator`).** Every `CONTRACT_APPROVED` entry
  records `"approval_mode": "SCRIPTED_NON_INTERACTIVE"`: no human was asked.
- **Data: real public GDSC2 release 8.5 (27 Oct 2023), PLX-4720 `LN_IC50` vs BRAF p.V600E**, from
  the prepared site snapshots `data/gdsc/lineage-enriched-v1/` and `data/gdsc/prepared/` (local,
  git-ignored). `lineage-enriched-v1` is a deliberately non-IID, response-blind split that exists to
  exercise the heterogeneity path; it is not an estimate of real between-laboratory heterogeneity.

Commands (run from the repository root):

```bash
uv run cohortlens run-study --data-mode gdsc --snapshots data/gdsc/lineage-enriched-v1 \
  --backend local --investigator scripted --approve-as demo-operator \
  --ledger docs/sample-run/gdsc-lineage-enriched-v1.ledger.jsonl --output-dir runs/demo-lineage
uv run cohortlens run-study --data-mode gdsc --snapshots data/gdsc/prepared \
  --backend local --investigator scripted --approve-as demo-operator \
  --ledger docs/sample-run/gdsc-hash-split.ledger.jsonl --output-dir runs/demo-hash
uv run cohortlens verify-ledger docs/sample-run/gdsc-lineage-enriched-v1.ledger.jsonl
```

The ledgers are sanitised by construction: they hold network-level aggregates, hashes, contracts,
gate decisions, tool calls and results, proposals and approvals. They contain no rows, site-level
estimates, local paths, cell-line identifiers or model identifiers. Full released results, which
include the per-site summaries the coordinator sees, went to `--output-dir` and are not committed.
The `snapshot_sha256` field identifies the exact NPZ files used.

## What happened

| Ledger | Round | Action | Released aggregate | Gate |
|---|---|---|---|---|
| `gdsc-hash-split` | 0 | primary | β = −2.3148, 95% CI [−2.8232, −1.8063], Q = 0.653 (p = 0.72), I² = 0 | `FINISH_COMPATIBLE` |
| `gdsc-lineage-enriched-v1` | 0 | primary | β = −2.4097, 95% CI [−2.8901, −1.9293], Q = 5.231 (p = 0.073), I² = 0.618 | `INVESTIGATE` |
| | 1 | `LEAVE_ONE_SITE_OUT` | pooled β with one site omitted ranges −2.7510 to −1.8875; I² 0 to 0.806; 2 of 3 omissions clear the flag | `INVESTIGATE` |
| | 2 | `STRATIFY_GDSC_LINEAGE` | non-SKCM β = −1.9558 [−2.4679, −1.4436], I² = 0.765; SKCM β = −3.4199 [−4.3969, −2.4428], I² = 0 | `BUDGET_EXHAUSTED` |

The hash-split primary reproduces the recorded SuperGrid primary (β = −2.3148) exactly.

## Ledger format

One JSON object per line:

```json
{"seq": 0, "entry_type": "STUDY_STARTED", "recorded_at": "2026-11-11T22:22:39+00:00",
 "study_id": "gdsc2-plx4720-braf-v600e", "payload": {...},
 "prev_hash": "sha256:000…000", "entry_hash": "sha256:…"}
```

`entry_hash` is the SHA-256 of the canonical JSON (sorted keys, compact) of all other fields;
`prev_hash` is the previous entry's `entry_hash`. Entry types, in the order they occur:
`STUDY_STARTED`, `CONTRACT_APPROVED` / `APPROVAL_DECLINED`, `RUN_SUBMITTED`, `RESULT_RELEASED`,
`GATE_DECISION`, `AGENT_INVOKED`, `AGENT_TOOL_CALL`, `AGENT_TOOL_RESULT`, `AGENT_INVALID_TURN`,
`AGENT_PROTOCOL_FALLBACK`, `AGENT_MODEL_TEXT`, `AGENT_PROPOSAL` / `AGENT_FAILED`, `STUDY_FINISHED`.
