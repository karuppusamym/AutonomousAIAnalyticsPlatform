# AI Analyst Investigator — product and team guide

Prepared 25 October 2026 · current implementation on `codex/investigator-demo`

Implementation update: the [deeper context-path audit](../70-reviews/2026-10-25-context-and-hypothesis-deep-review.md) identified a missing prompt handoff. That path now explicitly carries scoped reviewed facts, versions and receipts. The new **Context & mappings** studio adds guided mappings, lifecycle controls and golden grounding checks. See the [onboarding runbook](context-mapping-and-domain-onboarding.md). A pinned fact count alone still does not prove a particular model was called; inspect its prompt receipts and per-test provenance.

## Executive explanation

AI Analyst Investigator helps an analyst turn a business question into testable hypotheses, evidence-backed findings and owned follow-up actions. It combines business definitions, data discovery, statistical methods and optional language models inside one governed workflow.

For example, a service manager can ask: “Why did SLA breaches increase this month?” The app discovers the selected incident data, checks its quality, proposes comparisons, tests them, checks the resulting findings and shows the supporting evidence. A person can assign follow-up work to an owner and record how success will be measured.

Its intended value is less repeated analysis work, consistent business definitions and conclusions that a reviewer can trace to data. These are product aims, not demonstrated commercial outcomes. The real analyst pilot has not yet been conducted.

The product's distinctive design is the chain from question → hypothesis → experiment → verification → evidence → human follow-up. Models can propose questions and wording. Code controls data access, analytical validation and execution. Approval gates protect plan execution where configured and external publication.

## What users can do today

- Ask scoped questions about connected data, or start a longer investigation.
- Choose guided SLA, team comparison, recurring-incident or data-quality investigations.
- Curate the catalog, business definitions, metrics, relationships and knowledge documents.
- Inspect findings, their current verification state, limitations, evidence and lineage.
- Ask bounded follow-up questions about saved investigation evidence. This conversation path explains stored results; it does not execute a new analysis.
- Create evidence-linked actions with owners, due dates, impact assessments and success measures.
- Record analyst pilot assessments; rehearsals and real observations are counted separately.

The four visible local workspaces use demo data. OpenRouter connectivity and a model-generated SQL query were checked separately. The optional Python sandbox is currently unavailable locally; use the verified SQL workflows for the demo.

## How hypotheses are generated

A hypothesis is a question the app can test, not an established explanation. “Breach rates differ by reassignment count” is a hypothesis. “Reassignment caused the breaches” requires evidence beyond that association.

1. **Understand the question and authorized data.** The run carries its objective, selected sources, constraints, instructions and capability versions. The context agent retrieves business terms, metrics, prior context and, for guided runs, the pinned reviewed brief.
2. **Discover and profile the data.** Metadata, semantic types, column roles, distinct counts, timestamps, quality warnings and admissible relationships describe what can actually be tested.
3. **Generate rule-based candidates.** Applicable domain packs contribute analytical templates. The ITSM pack includes breach-rate comparisons, resolution-time comparisons, concentration and volume trends. Generic rules also propose tests from numerical outcomes, categorical groups and dates. Templates bind to available column types, names, roles and profiles; a generic match still needs business review.
4. **Optionally add model proposals.** The configured hypothesis-generation mode decides whether a model is called. In automatic mode, sufficient rule proposals can avoid the call. Valid model candidates supplement the rule candidates. The app therefore does not require a model to invent every hypothesis.
5. **Validate and deduplicate.** Each candidate becomes a typed `AnalysisSpec`. Code checks assets, columns, restrictions, analytical method/type fit and validated many-to-one joins. Invalid proposals are dropped with reasons. Duplicate tests are removed.
6. **Prioritize and limit the work.** Rules, or an optional decision model, rank valid candidates. Diversity and configured budgets bound the first round. Recurring investigations can carry forward prior verified claims for retesting.
7. **Test and iterate.** Deterministic analysis skills execute through the query gateway. A hypothesis can become supported, rejected or inconclusive. Bounded follow-up rounds may drill into results; stop rules and iteration limits prevent unlimited exploration.

Example candidates for an SLA question, if the needed columns are available:

| Candidate question | Needed data | Interpretation limit |
|---|---|---|
| Does breach rate vary by reassignment count? | Reviewed breach flag and reassignment count | Association does not establish cause |
| Does resolution time differ by assignment group? | Start/end timestamps and group | Check priority mix and sample size before ranking teams |
| Are high-priority incidents concentrated in a few services? | Priority and service identifier | Validate which priority value means highest severity |
| Has weekly incident volume changed? | Incident timestamp and record grain | Check missing periods, duplicates and time-zone rules |

These are illustrative questions, not new findings or measured results.

## Sequence diagram

The diagram condenses the configurable playbook. Context retrieval and metadata work can run independently before hypothesis generation. The low-autonomy plan gate applies when the run's autonomy level is 1 or 2. Publication is optional and requires its own approval.

```mermaid
sequenceDiagram
    actor User as Analyst / approver
    participant App as Application + workflow
    participant Context as Context + catalog
    participant Investigator as Hypothesis agent
    participant Gateway as Analysis skills + query gateway
    participant Data as Selected data sources
    participant Review as Findings + REV verifier
    User->>App: Question, sources, period, baseline, decision
    App->>App: Resolve permissions; pin run inputs and versions
    opt Autonomy level 1 or 2
        App-->>User: Proposed plan
        User->>App: Approve intended plan
    end
    par Business context
        App->>Context: Retrieve definitions, knowledge, reviewed facts
    and Data understanding
        App->>Gateway: Discover, profile and check selected data
        Gateway->>Data: Authorized read-only queries
        Data-->>Gateway: Results and quality evidence
    end
    App->>Investigator: Objective + context + profiles + quality issues
    Investigator->>Investigator: Domain/generic rules + optional model proposals
    Investigator->>Investigator: Validate, deduplicate, rank and bound candidates
    loop Accepted hypotheses; bounded follow-up rounds
        Investigator->>Gateway: Typed analytical specification
        Gateway->>Data: Validated read-only query
        Data-->>Gateway: Data for statistical test
        Gateway-->>Review: Supported / rejected / inconclusive + evidence
    end
    Review->>Gateway: Reproduction and applicable confirmation checks
    Review-->>App: Findings, current verdicts, strength and caveats
    App-->>User: Evidence brief and saved-evidence conversation
    User->>App: Record owner, action and success measure
    opt External publication requested
        App-->>User: Publication approval request
        User->>App: Approve exact bundle
        App->>App: Verify approval before publishing
    end
```

## What “verified” means

REV applies implemented checks such as method fit, sample adequacy, adjusted significance, effect size, reproducibility, a second method, narrative-to-number consistency, data stability and evidence completeness. Applicable held-out confirmation is recorded separately.

A verified badge means the implemented checks passed. It does not automatically mean the claim is confirmed, causal, free of bias or valid forever. Read the finding's strength, exploratory/confirmation state and caveats. A later data, context or policy change can make old verification stale or void; current standing takes precedence over old narrative text.

## Is all domain knowledge mapped to all data?

**No. Knowledge availability, retrieval relevance and physical data mapping are different things.**

| Layer | How it is used | What it does not establish |
|---|---|---|
| Workspace notes and reviewed brief | Supply definitions, corrections and prohibited interpretations | An unqualified note does not prove which table or column it describes |
| Glossary/document mappings | `mapped_columns` can name concrete schema/table/column references; catalog columns can have glossary links | A reusable pack reference may not resolve to a local physical column |
| Domain packs | Workspace policy can explicitly choose packs; otherwise catalog matching selects analytical packs | Matching a table name does not certify its business semantics |
| Retrieval/compiler | Selects relevant knowledge for the question and purpose within context budgets | It does not insert every document into every prompt |
| Execution scope | Controls the selected assets, columns, admissible joins and gateway queries | Knowledge text cannot grant access or authorize writes |

Important current behavior: an unmapped memory entry is treated as workspace knowledge. It can be visible broadly within that workspace. When a workspace's domain cannot be determined, the vocabulary applicability filter can retain vocabulary from all visible packs. Hypothesis template enablement is a separate selection path. For predictable demonstrations, explicitly choose the intended domain pack and use fully qualified mappings for data-specific definitions.

### Live catalog snapshot

Read-only inspection of the four retained local workspaces on 25 October 2026:

| Workspace | Selected active tables | Columns | Columns with glossary links | Local entries with explicit column mappings | Applicable vocabulary |
|---|---:|---:|---:|---:|---|
| Operations Intelligence Demo | 4 | 51 | 9 | 0 of 5 | ITSM |
| ServiceNow Incident Intelligence (demo) | 3 | 43 | 10 | 0 of 4 | ITSM |
| ServiceNow process mining (demo) | 6 | 96 | 13 | 0 of 2 | ITSM |
| Retail Orders (demo) | 3 | 22 | 3 | 0 of 1 | Sales |

The 35 glossary-linked columns out of 212 selected columns demonstrate partial linking, not certified semantic coverage. Some identifiers and technical columns need no business glossary entry. Visible pack entries also contain mapping references, but the count of such entries does not prove that each reference resolves correctly in the local schema. Brief assertions and semantic metrics are separate stores and are not included in the local-entry counts above. The guided Operations run pins ten reviewed facts from brief version 2; that is not a claim that ten columns are mapped.

## How to add domain knowledge and map it

1. **Choose a domain and an owner.** Start with ITSM/SLA for one pilot team. Confirm incident grain, the intended population and the authoritative source. Set the workspace's pack policy explicitly where needed.
2. **Select and profile the right tables.** In Sources, select the intended data. In Data → Catalog, review business names, descriptions, roles, units, sensitive columns and glossary aliases. Check keys and relationship cardinality; do not join on similar-looking names alone.
3. **Add a reviewed definition.** For a quick addition use the overview's reusable business context or Data → Brief & readiness. Add the definition, correction or prohibited interpretation and review any suggested assertions.
4. **Add richer reference documents.** Use Data → Documents → New document in a writable pack, or Import & export for supported knowledge bundles. Capture policy, examples, exclusions, synonyms, an accountable owner, source reference and effective date. Mark reviewed only after an owner checks it.
5. **Make data mappings explicit.** Use Data → Context & mappings to select real source columns, enter examples and evidence, assign an owner and review applicability. This guided editor does not require metadata JSON. Catalog aliases and document `analystos.mapped_columns` extensions remain available for existing workflows.
6. **Define the metric and joins.** Use Data → Metrics for the exact formula, denominator, filters, time field, time zone and units. Use Definitions to review the semantic model and relationships. Narrative policy alone does not create an executable KPI or a validated join.
7. **Check retrieval and results.** Preview the question's context using the context tooling/API, inspect run context receipts and test a small known dataset. Check both positive and negative examples. Then start a fresh guided investigation with the reviewed brief pinned.

Example contract for a domain owner to approve before mapping:

| Item | Example requiring local confirmation |
|---|---|
| Business term | SLA breach |
| Definition | A record failed the agreed service target under the organization's policy |
| Physical mapping | Exact selected incident table and breach flag; verify true/false polarity |
| Formula | Breached eligible incidents / all eligible incidents |
| Population | Explicit incident classes, exclusions and cancellation rules |
| Time | Defined event timestamp, reporting period, time zone and business calendar |
| Validation | Known breached/non-breached examples; manual reconciliation of a small sample |
| Ownership | Named service-management owner, policy reference and review date |

Do not equate “SLA met = false” with “SLA breached = true” until nulls, cancellations and policy exceptions have been checked.

## What to implement next

| Priority | Improvement | Why it matters |
|---|---|---|
| 1 | Populate and review domain mappings | The coverage screen and guided editor are implemented; domain owners still need to approve the organization's actual definitions and examples |
| 2 | Expand owner-reviewed numerical answer evaluations | Golden grounding checks are implemented; column/clarification checks alone do not establish numerical correctness |
| 3 | Conduct the real analyst pilot | Use actual tasks and measurements to evaluate accuracy, correction effort, time benefit and repeat usage |
| 4 | Measure production capacity and operations | Use pilot workloads to prioritize concurrency, latency, query cost, deployment and support work |

After the pilot demonstrates reliable value, measure concurrency, query latency and provider cost before selecting production scaling work. The architecture has queues, budgets, caching and versioning, but this demo does not establish enterprise capacity or production readiness.

## Suggested executive demo

Show one business question, one tested hypothesis, its evidence and caveats, then the owned action. Show how a reviewed definition changes the next run's context. Finish with the pilot scorecard and explain that real results are pending. Keep infrastructure and individual agent names in the technical appendix.

## Implementation references

The explanation was checked against the current local code and read-only database snapshot; it is not inferred from the reference repositories.

- Lifecycle: `src/analystos/capabilities/builtin/playbooks/investigate.v1.yaml`
- Hypotheses: `src/analystos/agents/investigator.py`, `packs/itsm/templates.yaml`, `skills/hypothesis_templates.py` under `src/analystos/`
- Context: `src/analystos/agents/context_agent.py`, `context/service.py`, `context/compiler.py`, `knowledge/entries.py` under `src/analystos/`
- Applicability: `src/analystos/capabilities/packs.py`; scoped memory: `src/analystos/services/brief.py`
- Tests and verification: `src/analystos/agents/data_scientist.py`, `agents/critic.py` under `src/analystos/`
- Recent value features: [implementation evidence](../60-delivery/evidence/investigation-value-20261025.md)
- Actual trial: [analyst pilot protocol](analyst-pilot-protocol.md)
