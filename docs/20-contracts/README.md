# Contracts

Source of truth: Pydantic models in `src/analystos/contracts/`. JSON Schemas are generated into
[`/contracts`](../../contracts) by `analystos export-contracts` — regenerate, never hand-edit.

The [workbench API evolution](02-workbench-api.md) specifies future workspace briefs, work orders,
ML/pipeline resources and common concurrency/recovery semantics. It is a design contract, not
generated schema or evidence of live endpoints; implementation is tracked in P4–P6. The implemented
brief, readiness, Start-work job kinds, steps, branches, pins and notebooks routes are specified in
[04-brief-steps-api.md](04-brief-steps-api.md).
The Goal API v2 (goals, run signals, the SSE event contract, budgets, replan reason codes) is
specified in [05-goal-api-v2.md](05-goal-api-v2.md) (S06). A finding as one Evidence v2 object (label, versions,
statements, method, effect, sample, limitations, review status, fingerprint, void reason) is
[06-evidence-api-v2.md](06-evidence-api-v2.md) (S07).

| Contract | Model | Spec |
|---|---|---|
| Agent manifest body (`kind: Agent`, YAML in `config/agents/`) | `capabilities.agents.AgentBody` → `agent_manifest.schema.json`; field enforcement in [03-agent-contract.md](03-agent-contract.md) | v1 §12.1, v3 §3.4 |
| Agent definition (view derived from the manifest) | `registry.AgentSpec` | v1 §12.1, FND-006 |
| Skill definition | `registry.SkillSpec` | v1 §14 |
| Tool definition | `registry.ToolSpec` | v1 §15 |
| Workspace policy | `policy.WorkspacePolicyDoc` (versioned) | v1 §45–§46 |
| Authorized scope | `policy.DataScope` | v1 §12.2 |
| Policy decision | `policy.PolicyDecision` (allow/deny/approval_required + reasons) | v1 §46 |
| Analysis spec (executable hypothesis) | `analysis.AnalysisSpec`, `Derivation`, `Filter` | v1 §20, v2 §7 |
| Statistical result | `analysis.StatResult` | v1 §21 |
| Semantic metric | `bi.MetricDef` | v1 §24 |
| Dataset / chart / dashboard | `bi.DatasetDef`, `bi.ChartSpec`, `bi.DashboardSpec` | v1 §32–§34 |
| Publication unit (what an approval binds to) | `bi.PublishBundle` | v1 §39 |
| Events | `events.EVENT_TYPES` | v1 §42 |
| Goal, run limits, signals, complexity decision | `goals.GoalIn`, `RunLimits`, `SignalIn`, `ComplexityDecision` | S06 §3, [05](05-goal-api-v2.md) |
| Evidence object | `evidence.EvidenceV2` → `evidence_v2.schema.json` | S07 §3.3, [06](06-evidence-api-v2.md) |
| Connector | `connectors.base.Connector` protocol | v1 §16 |
| BI publisher | `publishing.base.BIPublisher` protocol | v1 §32 |
| Model routing | `config/models.yaml` → `llm.config.ModelsConfig` | v1 §27 |
| Workspace brief, readiness, job kinds | `brief.WorkspaceBriefDoc`, `ReadinessAssessmentDoc`, `JobKindAvailability` | workspace spec §2, [04](04-brief-steps-api.md) |
| Step, branch (Data Thread) | `step.Step`, `step.Branch`, `step.ArtifactRef` | v4 §7, [04](04-brief-steps-api.md) |
