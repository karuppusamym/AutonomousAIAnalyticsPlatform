# S05 — Model gateway: configurable providers, routing, budgets, evaluation

**Priority:** P0 · **Closes:** FR-027, FR-038, FR-039, FR-055, FR-056 · **ADRs:** 005 (refines AnalystOS 0006, 0012, 0015)

## 1. Purpose
Any approved model, from any provider, configurable per purpose, tenant and workspace by an admin, with
cost, residency and quality controls, and with **no model ever on the authorization path**.

## 2. Baseline to reuse
`llm/router.py` (purpose → profile → models with fallback, escalation tier, budgets), `llm/providers.py`,
`llm/jev.py` (TypeSafe JEV decisions: choice/score with probabilities), `llm/redaction.py`, `llm/cache.py`,
`llm/replay.py`, `decisions/` (DecisionService, calibration), `config/models.yaml` (providers, allowlist, prices,
profiles, ladders), `config/models.providers.example.yaml` (Anthropic, Azure, Bedrock), `config/models.airgapped.yaml`,
`services/platform_settings.py` (versioned runtime settings, presets), `services/model_health.py`.
Donor (Atlas): OpenAI Responses, Gemini GenerateContent and Azure OpenAI adapters; Bedrock/Vertex routing.

## 3. Concepts (unchanged, now admin-editable)
* **Purpose**: what the code needs (`sql_generation`, `hypothesis_generation`, `insight_narrative`, `ask_route`, …).
  Code only ever asks for a purpose.
* **Ladder** per purpose: `cache → registry → rules → decision → llm_small → llm_large`. The cheapest sufficient rung answers.
* **Profile**: an ordered model list plus params (temperature, max_tokens, timeout, reasoning_effort, escalation_models,
  exclude_families).
* **Provider**: a transport and credentials. **Model**: id, provider, family, region, prices, capabilities
  (`json_mode`, `tools`, `prompt_cache`, `vision`, `context_window`).

## 4. Provider types
| `type` | Covers | Notes |
|---|---|---|
| `openrouter` (default) | 300+ hosted models | one key; provider routing prefs (`order`, `allow_fallbacks`, `data_collection: deny`) are passed through |
| `decision` | TypeSafe **JEV** via the OpenRouter Decisions API | typed answers; **must be priced** (tracker N-13) or JEV purposes fall back to rules |
| `anthropic` | Claude family direct | prompt caching, residency per region |
| `openai` | OpenAI direct (Responses API) | port the Atlas adapter |
| `azure_openai` | Azure deployments | deployment map, api_version pinned |
| `gemini` / `vertex` | Google | port the Atlas adapter |
| `bedrock` | AWS | IAM/IRSA credentials chain |
| `openai_compatible` (new) | vLLM, Ollama, LM Studio, llama.cpp server, TGI, Groq, Together, Fireworks, **LiteLLM proxy** | `base_url`, optional key, `model_map`; capability flags declared because they cannot be discovered |

## 5. Configuration model
Effective config = `file (models.yaml) ⊕ platform setting (global) ⊕ tenant override ⊕ workspace override`.
Each layer can only **narrow** the one above (allowlist ∩, budgets min, residency stricter), except the platform
admin layer. It is stored as versioned `PlatformSetting` documents with author, diff and rollback.

```yaml
# workspace override example
models:
  allowlist: [qwen/qwen3-32b, deepseek/deepseek-v4-flash, local/sqlcoder-ft]
  residency: eu
  budgets: {per_run_usd: 2.0, per_day_usd: 40, per_user_day_usd: 5}
  purposes:
    sql_generation: {ladder: [cache, registry, llm_small, llm_large], profile: coding_local}
    insight_narrative: {mode: rules_only}          # off | auto | rules_only | model_first
  profiles:
    coding_local:
      provider: onprem_vllm
      models: [local/sqlcoder-ft]
      escalation_models: [qwen/qwen3-32b]
providers:
  onprem_vllm: {type: openai_compatible, base_url_env: VLLM_URL, api_key_env: VLLM_KEY,
                region: eu, model_map: {local/sqlcoder-ft: sqlcoder-7b-ft-v3},
                capabilities: {json_mode: true, tools: false, context_window: 16384}}
```

## 6. Admin UI and API (new)
```
GET   /v2/admin/models/effective?workspace=     → resolved config with the source of each value
GET   /v2/admin/models/providers                → providers + health (latency p50/p95, error rate, last ok)
POST  /v2/admin/models/providers/{id}/test      → live ping with a fixed prompt (records model_call)
PUT   /v2/admin/models/config?scope=tenant|workspace  (If-Match version) → new version
POST  /v2/admin/models/config/rollback {version}
GET   /v2/admin/models/evaluations?profile=     → golden-suite scores per model/profile (S12)
POST  /v2/admin/models/evaluations/run {profile, suite}
```
Screens: Providers (keys are referenced, never displayed), Purposes (ladder + mode + profile per purpose),
Profiles, Budgets, Presets (`cheapest`, `balanced`, `max_quality`, `airgapped`), Evaluation matrix, Spend.

## 7. Rules
1. **Fail closed:** no allowed model for a purpose means a visible error (`model_unavailable{purpose, reason}`) and a rules fallback only if the
   ladder has a rules rung.
2. **Residency:** a model with `region` unknown or outside policy is excluded (AT-15).
3. **Data minimization:** redaction runs before every call; restricted columns never enter prompts; samples only when
   `allow_samples` is set on the asset and the column is non-sensitive.
4. **Independent verification** uses a different family (`exclude_families`).
5. **Model switch gate (FR-055):** saving a config that changes a purpose's primary model triggers that purpose's golden
   suite on the new model. A regression beyond tolerance blocks activation unless a platform admin overrides with a reason.
6. **Prompt templates** are versioned (`prompt_version`). `model_call` records provider, model, prompt version, tokens,
   cached tokens, cost, cost_source, latency, outcome and rung.
7. **JEV authority:** it may rank, select among already-valid options, or escalate risk. Its outputs never grant
   access, approve or bypass a validator (unit test asserts no JEV purpose is wired to a grant path).

## 8. Local and air-gapped (FR-056)
`airgapped` preset: every profile points to `openai_compatible` local endpoints, decisions run on rules or a local
classifier, and egress policy denies public model hosts. Optional: fine-tune a local Text2SQL model using the **DB-GPT-Hub**
recipe on the tenant's certified verified-query corpus, and benchmark it on the golden suite before enabling it.

## 9. Acceptance
AT-14, AT-15; `test_layer_can_only_narrow`; `test_jev_never_grants`; `test_unpriced_model_blocked_or_flagged`;
`test_openai_compatible_roundtrip` (against a local stub); provider health and test endpoint.

## 10. Tasks
| ID | Task | Est |
|---|---|---|
| S05-T1 | `openai_compatible` provider + capability flags | 1.5d |
| S05-T2 | Port OpenAI/Gemini/Azure adapters from Atlas under the router protocol | 3d |
| S05-T3 | Layered config (tenant/workspace narrowing) + versioning + rollback API | 3d |
| S05-T4 | Admin UI: providers, purposes, profiles, budgets, presets, spend | 4d |
| S05-T5 | Model-switch evaluation gate | 2d |
| S05-T6 | Price JEV (N-13) and run JEV purposes live; record calibration | 1d (needs owner price) |
| S05-T7 | Airgapped preset hardening + vLLM/Ollama runbook | 1.5d |
| S05-T8 | (P2) DB-GPT-Hub fine-tune pipeline + benchmark | 5d |
