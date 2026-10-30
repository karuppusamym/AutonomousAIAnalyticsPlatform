# Comparison review — AnalystOS vs AgentSwarms, Data Formulator, Vercel OSS Data Analyst, Insight Orchestra, WrenAI (2026-09-26)

**What this is.** A dated record of an external review supplied by the owner on 2026-09-26
(*"AIAnalystsAgentOS — Deep Architecture Comparison & Improvement Plan"*, Word document, not
checked in), and the disposition of each recommendation against the tree at `develop@dc5f79e`.
Like every file in `70-reviews/`, it is evidence, not a work queue: the adopted items live in
[spec v4](../00-intent/04-spec-v4-unified-data-platform.md), ADR-0018..0024 and the
[tracker](../60-delivery/01-tracker.md) (increment 7, P5-04..06, P6-04..08).

## 1. The review in brief

The review benchmarks the original product spec (`Context2AI_AnalystOS_Complete_Spec.md`) against
five public projects and concludes that the design is broader than theirs, and that the upgrade is
not more agents but making the design *executable and provable*:

* compile semantics instead of prompting them (WrenAI MDL, AgentSwarms governed steps);
* fingerprint verdicts and void them when any dependency changes;
* an Analyst Step model (plan → governed query → result snapshot → self-check → REV → claim) as the
  core UX, with branching exploration (Data Formulator's Data Thread);
* draft vs published workflow versions; schedules run frozen steps, never a re-plan;
* a versioned, bounded Context Package explored through tools (Vercel's semantic files in a sandbox);
* keep AnalystOS as control plane; make AIDataAnalyst and AIEngineeringAgent stateless specialist
  workers behind a shared `agentos-contracts` SDK (TaskEnvelope, CapabilityManifest, ArtifactRef…);
* evaluation suites as release gates; brokered secrets and scoped tokens for workers;
* borrow patterns, not code, from AgentSwarms (Elastic License 2.0).

**Its stated limitation matters.** It was written from the original spec, whose tracker it read as
"168 rows, all Not Started", and without access to any of the three codebases. At review time the
tree had increments 1–3 done and much of increment 4 delivered (102 non-merge commits after the
2026-09-25 architecture review). Many recommendations are therefore already built. It also
assumed AIDataAnalyst and AIEngineeringAgent were specialist add-ons. They are complete products
(Atlas, ~192k lines; DataPilot, ~35k lines), which changes the repository recommendation.

## 2. Owner decisions taken on this review (2026-09-26)

| Question | Decision |
|---|---|
| How do the three repositories relate? | **One platform + donors.** AnalystOS is the single product for analyst, DS, DE/ETL/ELT and ML work; Atlas and DataPilot donate proven code and tests ([ADR-0018](../10-architecture/adr/0018-one-platform-donor-repositories.md)). |
| ML scope | **Governed classical ML**: forecasting, classification/regression, clustering, anomaly detection as sandboxed methods with experiment records, model cards, REV and approved batch scoring; no online serving ([ADR-0024](../10-architecture/adr/0024-governed-classical-ml.md)). |
| This pass | Spec, ADRs and tracker; no code. |

## 3. Disposition of each recommendation

Legend: **Built** (in the tree, with the path) · **Adopted** (new ADR/row) · **Adapted** (adopted
in a different form, reason given) · **Deferred** · **Declined** (reason given).

| # | Recommendation (review §) | Disposition | Where / why |
|---|---|---|---|
| 1 | AnalystOS as control plane and system of record (§1, §8) | **Adapted, stronger** | One platform, not a control plane over two stateless repos. Both donors are stateful products, and stripping them costs more than porting what's proven (ADR-0018). |
| 2 | Shared `agentos-contracts` SDK across three repos (§9) | **Adapted** | No cross-repo SDK. Its contents become the in-repo worker protocol (`TaskEnvelope`, `ArtifactRef`, scoped tokens, conformance suite; ADR-0022). ADR-0017's three contracts govern exchange while the donors run. |
| 3 | Fewer always-on personas; skills behind 4–6 worker roles (§1, §4) | **Built** | Agents are behaviours dispatched by playbooks (`agents/dispatch.py`, `capabilities/playbook.py`); methods are plugins (`methods/*.yaml`). Spec v4 §3 adds DS/DE/ML as capability packs, not new agents. |
| 4 | Semantic compilation, not semantic prompting; fan-out refusal; ungoverned label (§10) | **Adopted** | ADR-0019, P7-02. Built so far: versioned, approval-gated metrics (`semantic/service.py`, P4-K03), Ossie round-trip, dbt import. Gap confirmed: Ask writes model SQL even for approved metrics, relationships have no cardinality, and nothing labels an answer governed or ad hoc. |
| 5 | Evidence fields per claim (§11) | **Built / Adopted** | Built: typed fact bindings, evidence bundle, data-version manifest, discovery vs confirmation (P4-03, `evidence/`); decision records (`decisions/`, ADR-0015); receipts per governed query. Adopted: the full dependency fingerprint (ADR-0020). |
| 6 | Verdicts void when SQL/context/model/data changes (§11) | **Adopted** | ADR-0020, P7-01. Built so far: data-version staleness only (`evidence/manifest.py`) and replan-scoped supersession (`runtime/engine.py`). |
| 7 | Analyst Step model as core UX: step objects, edit/re-run, pin (§12) | **Adopted** | P7-04. Run tasks, receipts and facts exist; editing one step and re-running it with automatic voiding does not. |
| 8 | Branching Data Thread (§6, §15) | **Adopted** | P7-05 (UI, after P7-04). |
| 9 | Draft vs published workflow versions; triggers run published (§13) | **Adopted** | ADR-0021, P7-03. Runs bind exact capability versions (`capabilities/binding.py`), but a schedule stores only the playbook id, so each fire binds whatever is current and silently picks up a new version. |
| 10 | Schedules run frozen steps, never re-plan (§12) | **Built / Adopted** | Built: scheduled re-analysis replays the hypothesis registry without a model call (P4-T05). Adopted: pin playbook, metric and method versions, and make upgrades explicit (ADR-0021). |
| 11 | Versioned Context Package, bounded, explored via tools (§14) | **Built / Adapted** | Built: the context compiler is the only route to prompts, with budgets, section receipts and NO_MATCH (`context/compiler.py`, P4-T03, P4-K05, CTX-005). Adapted: the package hash becomes a `context` dependency in fingerprints (ADR-0020). Tool-driven exploration of knowledge sections is left to the model router's existing tool path, not a new file sandbox. |
| 12 | Hypothesis objects; deterministic stats library (§15) | **Built** | Hypothesis registry and replay (`registries/hypotheses.py`, `registries/replay.py`), confirmation rules (`evidence/confirmation.py`), `methods/`, `skills/stats.py`, BH correction (ADR-0008). |
| 13 | Structured + unstructured evidence fusion (§15) | **Deferred** | Knowledge citations are already kept separate from quantitative evidence (receipts). Fusing documents into analysis is follow-on N-8. |
| 14 | What-if scenarios (§15) | **Deferred** | N-9. Needs the semantic compiler first (scenarios recompile a `SemanticQuery` with parameters). |
| 15 | BI-neutral chart/dashboard specs; Superset as adapter (§6) | **Built** | `contracts/bi.py` `PublishBundle`, `publishing/` adapter, deterministic viz skill. |
| 16 | Relationship validation before certifying cardinality (§16) | **Adopted** | Port from Atlas (`relationship_intelligence.py`, `composite_key_inference.py`, validation rules), P7-09, feeding ADR-0019. |
| 17 | Transformation recipe IR compiled to SQL/Polars/Spark (§16) | **Adopted** | ADR-0023, P6-04; DataPilot `pipeline_codegen/` ported for dbt/Dataform emission. Spark reserved. |
| 18 | Query folding / pushdown (§16) | **Built** | ADR-0004 minimum ETL, pushdown SQL (`skills/sqlbuild.py`), dialect suites (P4-C12, P4-E01). |
| 19 | Incremental processing (§16) | **Planned** | P6-02, ADR-0016 (watermark incremental in the loader; dlt only on the customer side). |
| 20 | Data-quality gates, blocking vs warning (§16) | **Adopted** | P6-05. Atlas `data_quality.py` baselines and DataPilot quarantine ported. |
| 21 | Executed column lineage (§16, §18) | **Adopted** | P6-07. Recipe IR emits lineage, and Atlas SQL/dbt lineage parsers are ported. OpenLineage export exists (`evidence/openlineage.py`). |
| 22 | Safe optimization: EXPLAIN, index advice, never auto-applied (§16) | **Built / Deferred** | SQL explain exists (increment 3). Index advice is deferred (DataPilot `index_advisor.py` is a later donor candidate). |
| 23 | Tool registry with availability check, permission gate, versions (§17) | **Built** | Capability manifests, validation at load, per-workspace enablement, connector certification (`capabilities/`, P4-X01). |
| 24 | Workers never see secrets; scoped capability tokens (§17) | **Adopted** | ADR-0022. |
| 25 | Python sandbox: no host FS, egress deny, quotas (§17) | **Planned** | P4-02 container sandbox. The current sandbox says it is not a boundary (`sandbox/runner.py`). |
| 26 | Prompt-injection handling (§17) | **Adopted** | Port Atlas `prompt_risk.py` and `injection_defense.py` with their corpus, P7-10. |
| 27 | SSRF guards on URL tools (§17) | **Adopted** | DataPilot HTTP tool runtime port with private-address block, P7-11. The MCP client already has an allowlist (P4-X05). |
| 28 | Tenant scope enforced everywhere (§17) | **Built** | Workspace binding on every child-id route (P4-01), per-workspace reader roles for staged data (increment 3). |
| 29 | Catalog certification, deprecation, drift; semantic PR-style review (§18) | **Built / Adopted** | Built: crawler drift, PII, owner tags, approvals, OKF/Ossie export (increment 3, P4-K*). Adopted: semantic diff from Atlas (`semantic_diff.py`) in P7-02. |
| 30 | Evaluation suites as release gates (§19) | **Adopted** | P7-07. Benchmarks exist (P4-V01, P4-V02) but don't gate merges. |
| 31 | Answer Passport / "why this number?" (§11, §25) | **Adopted** | P7-08: one endpoint resolving a displayed number to fact → query receipt → data version → semantic version → verdict. |
| 32 | Office artifacts only from saved verified snapshots (§6) | **Built / Deferred** | HTML/PDF/XLSX reports come from run artifacts (`services/reports.py`). PPTX/DOCX are deferred. |
| 33 | LangGraph inside bounded analytical flows (§13, §21) | **Declined** | Playbooks on Temporal already express the flows. A second graph runtime would add a second state and retry semantics to debug. *Update 2026-10-30:* [ADR-015](../03-decisions/adr-log.md) adds LangGraph as an optional orchestrator backend with no state of its own (no checkpointer) and the engine's retry rules; inside-flow graphs remain declined. |
| 34 | Roadmap phases A–E and "first 30 tasks" (§20) | **Adapted** | Mapped to tracker rows below. Phase A's "no specialist repo owns secrets/state" becomes "no worker holds secrets" (ADR-0022). |
| 35 | Defer own BI engine, large ML platform, persona sprawl, prod write-back, universal ETL (§22) | **Agreed** | Matches ADR-0014 (ELT on customer engines), ADR-0024 deferrals and CLAUDE.md rule 5. |
| 36 | Borrow patterns, not code, from ELv2/unknown-licence repos (§21) | **Agreed** | No third-party code is copied by this increment. The only ports are from the owners' own repositories (ADR-0018). |

### The review's "first 30 tasks" mapped

CON-001..003 → P7-06 (worker protocol + conformance) · TRC-001 → built (`decision_id` on
decisions; run/task ids) · TRC-002 → P7-04 · TRC-003 → built (result hashes on receipts) ·
TRC-004 → built (P4-03 fact bindings) · CTX-001 → built (context compiler) · SEM-001..004 → P7-02 +
P7-09 · QRY-001 → built (`QueryGateway`) · SBX-001 → P4-02 + P7-06 · REV-001..002 → P7-01 ·
EVAL-001..003 → P4-V01/V02 + P7-07 · WF-001..003 → built playbooks + P7-03 · ANA-001 → built ·
ANA-002 → P7-04 (self-check library) · ENG-001 → P6-04 · ENG-002 → P6-05 · CAT-001 → built
(crawler) · LIN-001 → P6-07 · BI-001 → built · UX-001 → P7-04/P7-05.

## 4. What this review adds that the 2026-09-25 review did not

The 2026-09-25 [architecture review](2026-09-25-architecture-review.md) focused on the platform
(extensibility, token economy, open formats, engines). This one adds four trust mechanisms the
earlier review didn't name: compile-don't-prompt semantics (ADR-0019), dependency-fingerprinted
verdicts (ADR-0020), published/pinned definitions (ADR-0021) and the step model with branching
(P7-04/05). Those are the adopted core of spec v4.
