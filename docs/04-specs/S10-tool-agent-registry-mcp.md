# S10 — Tool, agent and data-product registry; MCP server and client

**Priority:** P1 · **Closes:** FR-023, FR-035, FR-046, FR-047 · **ADRs:** 007

## 1. Purpose
One registry of everything that can be *called*: built-in tools, agents, playbooks, admitted external MCP tools,
and **data products** (certified metrics, saved queries, datasets) published as typed tools for other agents.

## 2. Baseline to reuse
`ToolDefinition`, `SkillDefinition`, `AgentDefinition`, `WorkspaceCapability`, `capabilities/` (discovery, validation,
reload, per-workspace enablement, certification gate), `tools/registry.py`, `tools/http.py`, `tools/query_tools.py`,
`mcp/server.py` (tools: `ask`, `investigate`, `get_finding_evidence`, `validate_sql`, `query.<key>`, http capabilities;
resources over knowledge, metrics, findings, datasets), `mcp/client.py` (workspace-registered servers), `mcp/grants.py`
(`McpClient`, `McpGrant`, `McpUsage`). Donor: Atlas agent gateway screen and MCP catalogue (R11-MP10).

## 3. Manifest (v1 §10.2, extended)
```yaml
tool_id: metric.payment_failure_rate
version: 4.0.0
kind: data_product            # builtin | agent | playbook | external_mcp | data_product
owner: payments-analytics
input_schema:  {$ref: schemas/metric_query_input.json}   # dimensions[], filters[], grain, period
output_schema: {$ref: schemas/metric_query_output.json}  # rows|handle, governed:true, metric_version, freshness
side_effect: none             # none | write_internal | write_external
permissions: [dataset.read]
data_classification_max: internal_aggregate
execution: {via: semantic_compiler, source: payments_ro}
limits: {timeout_s: 30, max_rows: 5000, rate_per_min: 60}
exposure: {rest: true, mcp: true, grants_required: [metric.read]}
signature: {alg: ed25519, key_id: tenant-signing-1, value: ...}
```

## 4. Build
1. **Data products as tools (FR-046).** "Publish as tool" on a certified metric, verified query, dataset or playbook generates a
   manifest. The input schema is derived from allowed dimensions and parameters. It is exposed at `POST /v2/tools/{tool_id}:invoke`
   and as MCP tool `{tool_id}`, and it executes through the same compiler or gateway path. Version pinning: callers may pin `@4`.
   An unpinned call gets the latest certified version.
2. **Grants.** `McpGrant` / API client grants are `{client, workspace, tools[] | tool_patterns[], rate, expires_at}`.
   Every invocation is audited with `McpUsage` + cost.
3. **External MCP admission (FR-047, finishes tracker P4-X05).** An admin registers a server URL from an allowlist.
   Tool descriptions are injection-screened (stored with a hash; a change re-screens). Tools import as
   `side_effect: write_external` and `status: quarantined` until an admin classifies them. Invocations from agents require a
   playbook that declares the tool and an approval if `side_effect != none`. Network egress goes only to the admitted host.
4. **Signing.** Manifests for playbooks, skills and external tools are signed with a tenant key on publish. The runtime refuses
   unsigned or tampered manifests (v1 threat "tool supply-chain").
5. **Registry UI and API**: `GET /v2/tools?kind=&q=` (only tools the caller may use), manifest view, usage, health,
   owner, versions, deprecate.

## 4a. As built (2026-10-27, tasks T1–T5, backend)
* **Package** `src/analystos/toolreg/` (`store`, `publish`, `invoke`, `external`, `signing`); contract
  `contracts/tool_manifest.py::ToolManifestV2` (`apiVersion: analystos/tool.v2`, exported as
  `contracts/tool_manifest.schema.json`); API `api/routers/tools_v2.py`; migration `0065_tool_registry`.
* **Manifest.** As §3, plus `title`, `description` (screened text, data not instructions) and `execution.ref` (what
  exactly runs: metric name + version + definition hash, verified-query id + template hash, dataset artifact id +
  content hash + columns, playbook definition key + version or built-in capability). Schemas are inlined, not `$ref`.
  `kind ↔ execution.via` is enforced (a data product can only use the compiler, a verified query or a dataset) and a
  data product must be `side_effect: none`. Versions are strict `MAJOR.MINOR.PATCH`.
* **Tables.** `registered_tool` (one signed, immutable version per row; only `status` changes), `tool_invocation`
  (usage per call: caller, client, channel, version, status, latency, rows, query id, cost), `tool_signing_key`
  (tenant-level, `tenancy/schema.py` `DIRECT`). `mcp_grant` gains `tool_patterns`, `rate_per_min`, `expires_at`;
  `mcp_usage.day` widens to 16 characters for the per-minute rate counter (`tool = __rate__`).
* **Tool ids.** `metric.<name>`, `verified_query.<name>`, `dataset.<name>`, `playbook.<key>`, `mcp.<server>.<tool>`.
  `query.<certified_query>` (ADR-007) stays the existing P7-11 query-tool family; a published verified query is
  `verified_query.*` so the two never collide.
* **Versions.** A metric/dataset/playbook tool's major version is the source version (`metric@4` ⇒ `4.0.0`); a changed
  manifest from the same source is a minor bump; identical content is idempotent. Publishing a new certified version
  deprecates the older ones (callable only when pinned). A source that changed after publishing makes the tool stale:
  invoke answers 409 and `health.source_current` is false (no silent re-binding).
* **Who may.** Publish/deprecate: workspace `editor`. Admission and classification: `owner`. Invoke: the role the
  permissions imply (`dataset.read`, `run.start`, `external.call` ⇒ `analyst`); MCP/API clients additionally need a
  grant covering the id (exact, family `x.*`, or a `tool_patterns` glob; patterns never cover built-in tools) that has
  not expired, inside the grant's per-minute rate and per-tool daily quota. `POST /v2/tools/{id}:invoke` accepts a
  client's `client_id.secret` bearer; the request then acts for the client's tenant as its service user.
* **Approvals.** `side_effect != none` needs a hash-bound approval of `{workspace, tool, version, manifest hash,
  arguments}`, verified and consumed immediately before acting. Deviation: a **playbook tool** (`write_internal`) only
  starts a run, like MCP `investigate`; the run carries its own gates (plan approval at autonomy ≤ 2, approvals for
  every publication), so no extra approval is requested. (The approval path also requires the requester to hold
  `editor`, which would lock analysts and MCP clients out of playbooks.) External tools use the MCP client's own gate.
* **External MCP.** Admission (`POST /v2/mcp-servers/{id}:admit`) runs the existing allowlisted, pinned client
  (`mcp/client.py`), then mirrors each screened tool as a registry version: `quarantined` + `write_external`, or
  `refused` when screening flagged it. `description_hash` is the hash of the raw definition (name, description,
  schema); a change re-screens into a new quarantined major version and retires the older ones, and until re-admission
  invocation refuses because the server no longer offers the screened definition. Classification re-signs a new minor
  version as `certified` with the chosen side effect and records the same hash-bound classification in the MCP client.
  Admitted tools are exposed over REST only (`exposure.mcp: false`) by default: the platform does not re-export
  third-party tools to its own MCP clients. Agents need a run whose bound playbook lists the tool in `spec.tools`
  (glob patterns).
* **Signing.** ed25519 via `cryptography` (now a runtime dependency). No private key is stored: a tenant key is derived
  from `tool_signing_secret` (else a domain-separated key from `jwt_secret`), the tenant and the key id; the table keeps
  public keys, and verification re-derives the public key, so swapping a row cannot bless a forgery. Rotation
  (`POST /v2/tool-keys:rotate`, platform admin) retires or revokes the previous key and re-signs every untampered
  version of the tenant. Deviation: built-in playbooks and skills shipped in the repository are trusted code and are
  not signed per tenant; what the registry runs (published playbook/data-product tools, admitted external tools) is.
* **Cost.** The data-product paths make no model call, so `tool_invocation.cost_usd` is 0 for them; the column is there
  for paths that do.
* **UI** (2026-10-27): Settings → Capability registry gains three tabs, *Data tools* (list, publish-as-tool, signed manifest and health, versions, usage, try it, deprecate/retire, signing keys), *MCP servers* (register, allow, admit, quarantine review and classification) and *MCP clients* (clients, grants with patterns/rate/expiry, and the MCP catalogue of what a workspace offers and which client reaches each tool). The registry screen is for platform admins (IA budget): workspace owners who are not admins publish, admit and classify through the API.
* **Not built.** Registering built-in tools and agents as
  registry rows (they stay in the capability registry), an external MCP server outside the test double, AT-22 as a
  full end-to-end scenario.

## 5. Acceptance
AT-22; `test_quarantined_tool_not_invocable`; `test_description_change_rescreens`; `test_unsigned_manifest_refused`;
`test_grant_scope_enforced`; `test_data_product_uses_compiler_path` (no raw SQL path).

## 6. Tasks
| ID | Task | Est |
|---|---|---|
| S10-T1 | Manifest schema v2 + registry migration | 1.5d |
| S10-T2 | Publish-as-tool for metric/verified query/dataset/playbook | 3d |
| S10-T3 | Invoke endpoint + MCP exposure + grants + usage | 2d |
| S10-T4 | External MCP admission, quarantine, classification UI | 3d |
| S10-T5 | Manifest signing + verification | 1.5d |
