# Evidence API v2 (spec S07-T4, v1 §7.3)

One finding as one object: the existing `Insight`, its primary `Experiment`, its newest `VerificationRecord`, the
run's data manifest and binding and its citation set, read through `contracts.evidence.EvidenceV2`
(`contracts/evidence_v2.schema.json`). Nothing new is stored; a run's evidence is built in a fixed number of
statements (`evidence/api_v2.py`).

    GET /v2/evidence/{evidence_id}      one finding (viewer of its workspace; 404 elsewhere)
    GET /v2/runs/{run_id}/evidence      the S06 view plus `evidence: [EvidenceV2]`

| Field | From | Notes |
|---|---|---|
| `evidence_id`, `run_id`, `code` | insight | `evidence_id` is the insight id |
| `claim_id` | `services.changes.claim_key` | `clm_` + hash of (method, outcome, subject, filters, top group): stable across runs, wording excluded |
| `label` | `insight.claim_label` | observed · supported_association · possible_contributor · rejected · unknown (S07-T3, `evidence/claims.py`) |
| `title`, `description` | insight | the bound narrative (numbers guard + causal-language guard) |
| `dataset_versions[]` | run data manifest | `{asset, source_id, version, mode, rows, staged_at}` per asset the spec reads |
| `metric_version` | record dependency `semantic` | `{metric, version}`; null for an ad-hoc finding |
| `query_execution_ids[]` | insight evidence / experiment | the statements the number came from |
| `method` | spec + binding | `{name, version, test, claim_kind}`; version = the method manifest the run bound |
| `effect` | statistic | `{value, unit, ci: {low, high} | null}` |
| `sample` | statistic + manifest | `{n, coverage}`; coverage = rows read / source rows when known |
| `limitations[]` | caveats, failed REV checks, investigation gates, warnings | gate entries are prefixed with the check (`baseline: ...`) |
| `review_status` | verification record | verified · failed · void · pending · superseded · legacy · unverified |
| `validation` | insight | exploratory · replicated · confirmed · ... (P4-03) |
| `fingerprint`, `verification_record_id` | verification record | ADR-0020 |
| `void_reason` | verification record | set whenever `review_status` is `void` |
| `citations` | citation set | counts `{quantitative, documents, conflicts}`; full set at `/api/insights/{id}/citations` |
