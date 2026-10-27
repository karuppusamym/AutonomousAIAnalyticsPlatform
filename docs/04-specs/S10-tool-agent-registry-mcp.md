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
