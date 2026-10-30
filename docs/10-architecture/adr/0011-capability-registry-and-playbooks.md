# ADR-0011 — Capability manifests, playbooks and declarative agents

**Status:** proposed (2026-09-25, [architecture review](../../70-reviews/2026-09-25-architecture-review.md) C1)

**Context.** Agent behaviour is hardcoded Python, and the plan is a fixed 14-step list
(`runtime/plan.py:15-30`) with its step keys hardcoded in the engine. `agents/dispatch.py` is a
literal map. Most agent-YAML fields are never read, and registered agents and tools cannot run.
Adding an analysis method touches about 10 files. The product requirement is "add agents, tools,
skills anytime" without weakening governance.

Increment 3 ([ADR-0010](0010-universal-sources-crawler-token-economy.md)) proved the pattern for one
kind: `config/source_kinds.yaml` makes a new database kind a catalog entry instead of a connector.
This ADR generalises that to every kind of capability.

**Decision.**
1. One **capability manifest** (`contracts/capability.schema.json`) covers agents, skills, tools,
   methods, connectors, engines, publishers, decision purposes, detectors, crawlers, knowledge
   packs, playbooks and renderers. Each manifest declares its input and output schemas,
   `side_effect`, `cost_class`, `determinism`, `permissions`, `requires` and `certification`.
2. **Discovery.** Capabilities come from four places: built-in manifests, directory packs
   (config-only), the Python entry-point group `analystos.capabilities`, and workspace-registered
   MCP servers. Loading fails on unknown references.
3. **Playbooks.** Versioned YAML DAGs replace `BASE_STEPS`. Approval gates and side effects become
   step types (`approval_gate`, `side_effect`, `replan_boundary`), not magic keys.
   `investigate.v1` must reproduce today's plan hash.
4. **Declarative agents.** An agent runs under a generic propose → validate → execute runtime,
   bounded by its capability list and budget. Python behaviour is optional.
5. **Analysis methods are plugins** implementing one protocol. The vocabulary, prompt block,
   validation, template, chart intent and claim key are all derived from the registry.
6. **Defaults.** An unclassified capability counts as `write_external`, so it needs approval. A
   capability is disabled per workspace until enabled. Only `certified` capabilities run
   autonomously or on a schedule. The plan hash includes the capability versions it binds.

**Consequences.** The engine becomes smaller, and gates become testable in isolation. A new method,
tool or domain is one module or pack. Registry reload replaces the `lru_cache` restart. Cost: one
migration of the existing agents to manifests, plus a compatibility window during which the Python
dispatch map remains as a fallback. Frameworks such as LangGraph, ADK and the Microsoft Agent
Framework were considered. They were not adopted: our durability is Temporal, our governance must
sit on our own runtime, and framework churn is a risk. Pydantic AI may be used *inside* a
capability for typed model calls. (Note, 2026-10-30: [ADR-015](../../03-decisions/adr-log.md) (accepted 2026-10-29, built 2026-10-30) adds LangGraph as an optional orchestrator *backend*: a stateless driver over the same engine and Postgres state, never the flow definition. Agents and playbooks are unchanged.)
