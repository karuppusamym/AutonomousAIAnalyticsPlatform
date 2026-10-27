# Local models and air-gapped installs (vLLM, Ollama)

Spec S05 §8 (FR-056), tasks S05-T1 and S05-T7. Read with [`03-self-hosted.md`](03-self-hosted.md) §3–4.

An air-gapped install answers every purpose from deterministic rungs first and from a local
OpenAI-compatible model when a model is needed. Nothing leaves the installation: the deployment flag
`ANALYSTOS_AIR_GAPPED=true` routes only to providers declared `egress: internal`, and the model transport
refuses every other host before it opens a connection.

## 1. Serve a model

**vLLM** (GPU; the recommended server):

```bash
docker run --gpus all -p 8000:8000 vllm/vllm-openai:latest \
  --model Qwen/Qwen3-32B --served-model-name qwen3-32b \
  --max-model-len 32768 --guided-decoding-backend xgrammar
```

`--served-model-name` is the name in `model_map`; `--max-model-len` is the `context_window` to declare.
Add `--api-key <key>` if the server should require one, and set `api_key_env` to the variable that holds it.

**Ollama** (CPU or a small GPU; slower, fine for a pilot):

```bash
ollama pull qwen3:32b
OLLAMA_HOST=0.0.0.0:11434 ollama serve
```

Ollama's OpenAI endpoint is `http://<host>:11434/v1` and takes no key. Its default context is small: raise
`num_ctx` in a Modelfile and declare the same value as `context_window`.

## 2. Point the platform at it

Use `config/models.airgapped.yaml` (`ANALYSTOS_MODELS_CONFIG=config/models.airgapped.yaml`) or a copy:

```yaml
providers:
  local:
    kind: chat
    type: openai_compatible
    egress: internal
    base_url: http://llm.analystos.svc:8000/v1     # Ollama: http://ollama.analystos.svc:11434/v1
    base_url_env: ANALYSTOS_LOCAL_LLM_URL           # the Helm value airgapped.localModel.url sets this
    region: on-prem                                 # needed for any residency rule (unknown = excluded)
    model_map: {qwen/qwen3-32b: qwen3-32b}
    capabilities: {json_mode: true, tools: false, context_window: 32768}
```

* `capabilities` are declared because a local server cannot be asked. `json_mode: false` (an old server
  without JSON mode) makes the router drop `response_format`; the prompt still asks for JSON and the answer is
  still parsed and validated. Prompts are fitted to `context_window` before they are sent.
* Local inference has no per-token price: give each model `input_usd_per_mtok: 0.0` /
  `output_usd_per_mtok: 0.0` so calls are recorded with a price-table cost, not `missing_price`.
* Claude (`anthropic/*`) and Gemini (`google/*`) are blocked platform-wide (`blocked_families` in
  `config/models.yaml`, inherited through `extends`); a local file cannot route to them either.

## 3. Apply the preset and check the posture

1. Set `ANALYSTOS_AIR_GAPPED=true` on the api and worker processes (Helm: `values-airgapped.yaml`).
2. Apply the preset once: `POST /api/admin/settings/preset {"preset": "air_gapped"}` (Admin › Models › Presets).
   It keeps deterministic rungs first, turns JEV off and puts decision purposes on rules.
3. `GET /api/admin/models/airgap` (Admin › Models › Air-gap) must answer `"ok": true`. It checks the flag, an
   internal provider with a URL and declared capabilities, no public provider routed, JEV off, the preset
   applied, no outbound MCP hosts, and that every purpose is answered by the local model or a rules rung.
4. `POST /api/admin/models/providers/local/test` sends one fixed prompt through the router and records it as a
   `model_call` (provider `local`, cost 0).
5. Before switching a purpose to a new local model, the model-switch gate scores the purpose's golden suite on
   the current and the new model (`config/model_switch_gate.yaml`); a regression beyond the tolerance blocks
   the save unless a platform administrator overrides with a reason.

## 4. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `provider 'local' has no base URL (set ANALYSTOS_LOCAL_LLM_URL)` | neither the env var nor `base_url` is set | set one; restart api and worker |
| `egress_blocked` | the URL's host is not the provider's host, or the provider is not `egress: internal` | declare `egress: internal`; the URL in the env var must be the one the guard allows |
| `prompt of ~N tokens does not fit the declared context_window` | the server's context is smaller than the prompt | raise `--max-model-len` / `num_ctx` and `context_window`, or lower the purpose's context budget |
| invalid JSON answers, escalations | small model, or JSON mode not enforced | `--guided-decoding-backend` (vLLM); a larger model; the purpose's golden suite shows the pass share |
| every purpose answers from rules | preset applied but no internal provider, or `available()` false | `GET /api/admin/models/airgap` names the purposes and the reason |

Optional, later (S05-T8): fine-tune a local Text2SQL model with the DB-GPT-Hub recipe on the tenant's
certified verified queries and enable it only after it passes the `sql` golden suite.
