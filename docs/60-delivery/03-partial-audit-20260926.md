# Partial-item audit — 2026-09-26

This audit covers the 14 rows marked **Partial** in [01-tracker.md](01-tracker.md). It distinguishes repository evidence from live certification. Nineteen rows are still **Not started** and are not counted as partial implementations.

## Verified locally today

- Rebuilt API and web images; API, PostgreSQL, Redis, web and Superset are healthy. Browser login through the web proxy works after an API container recreate.
- The Skills tab renders 33 entries. Platform Settings and Usage & cost load without browser JavaScript errors.
- The seeded incident table has 20,000 rows. The Ask SQL console loads five schema-aware examples; the count and reopened-incident examples ran through the live gateway.
- The model-policy and execution-ladder unit tests passed (20 tests). The web suite passed (179 tests, one skipped).
- The 30-day ledger reports zero provider calls and $0 recorded model spend. Its 65,604 avoided tokens across 43 deterministic skips are estimates, not billed savings. No chat provider is available in the current container configuration.

## Remaining partial items

| Item | Present evidence | Required to close |
| --- | --- | --- |
| P4-X05 MCP client | Allowlist, approval, audit and a test server exercise the client. | Certify a real Superset or dbt MCP server and add a platform host allowlist. |
| P4-T04 Prompt caching | Stable-prefix layout and cache-token accounting exist. | Measure the target cached share with a funded, cache-capable provider; improve the prefix if it stays below target. |
| P4-K09 Context2AI interop | Local, OKF import and MCP providers work against a labelled mock. | Certify a live Atlas instance. The earlier claim that external context cannot reach prompts is superseded by P4-K05. |
| P4-E01 Engine protocol | Dialect security suite and draft warehouse engines exist. | Certify an in-place query on a live Snowflake or Databricks instance. |
| P4-S04 Self-hosted pilot | Helm, offline bundle, provider adapters, OIDC and sandbox controls have component tests. | Run the air-gapped scenario with a local model, test SSO with a real identity provider, and certify Bedrock. |
| P4-S05 Load target | 500 SSE streams and 1,000 workspaces met their targets. | Add connection pooling / PgBouncer and rerun 50 concurrent runs; the recorded run failed on the 4-core host. |
| P4-V01 Analytical benchmark | Deterministic and platform-off tiers have dated evidence. | Run a live-model tier and measure power near the materiality threshold. |
| P4-V02 Ask accuracy benchmark | Frozen 162-question corpus and off/fake tiers exist. | Run the live tier and set a pilot threshold before using the result for acceptance. |
| FND-004 Delivery | Compose images build. **Closed on 2026-10-29:** `release.yml` + `deploy.yml` publish signed images and the chart to GHCR and deploy through a gated GitHub Environment (`docs/30-runbooks/14-release-and-deploy.md`); the packaged chart was installed on kind ([evidence](evidence/fnd-004-s07-t1-kind-gvisor-20261029.md)). | The first tagged release on GitHub once Actions runs again; the production Environment is the owner's choice. |
| FND-006 Agent contract | Declarative manifest runtime enforces purposes, policies, budgets and tools (P4-X03). **Closed in code on 2026-09-26:** `AgentSpec` is now a view derived from the manifest. `GET /api/agents`, the `agent_definition` cache and the tool gate derive from the manifest, and spec edits through the API are refused. `knowledge` limits the sections the context compiler puts in an agent's prompts and caps their budget. `output_contract` checks the declared types and schemas before artifacts and run records are written (`OutputContractViolation`). Every field is listed as enforced or informational in [`20-contracts/03-agent-contract.md`](../20-contracts/03-agent-contract.md). Evidence: `tests/unit/test_agent_contract.py`. | Nothing further for the contract: tracker row Done, register entry 2026-09-26 FND-006. The eval-set publishing gate is tracked under P4-X03. |
| CTX-001 Context2AI client | Atlas MCP path has mock evidence (P4-K09). | Live Atlas certification; this shares the P4-K09 blocker. |
| CTX-005 Context caching | Response cache keys include knowledge versions (P4-T06). | Add and measure compiled-context reuse. |
| META-002 SQL Server | Metadata, dialect and validator have tests. | Run against a live SQL Server. |
| DEX-001 DuckDB engine | File inspection and skill tests use DuckDB. | Demonstrate a governed analysis run through the DuckDB engine. |

Current cost controls are per-purpose `off` / `auto` / `always` modes, deterministic rule and registry rungs, prompt-size refusal, cache, versioned model prices, a pre-call approval threshold for expensive or unpriced models, per-purpose caps, run token and dollar budgets, and a monthly workspace dollar budget. The router checks budgets before a call, but it does not reserve estimated spend atomically across concurrent calls; P4-06 still owns that hard-cap work. Model answers themselves are not deterministic. The demo workspace currently has a 400,000-token / $2 run budget and a $50 monthly workspace budget.
