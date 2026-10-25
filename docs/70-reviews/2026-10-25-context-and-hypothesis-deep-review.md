# Context and hypothesis quality — implementation review

25 October 2026 · branch `codex/investigator-demo`

Follow-up implementation: the prompt handoff, guided mappings, lifecycle, resolution-before-clarification, question coverage, supported prerequisite metric contracts, typed investigation filters and golden grounding evaluations have now been built. This document retains the original findings as review history. Current operation and limits are described in the [onboarding runbook](../30-runbooks/context-mapping-and-domain-onboarding.md); delivery status is in the tracker.

## Decision

Improve how reviewed meaning is resolved and carried into analysis before increasing model usage. The existing platform already has glossary suggestions, human review, context retrieval, semantic metrics and guarded hypothesis execution. Replacing these systems or enabling models for every step is not the first requirement.

The target is a traceable contract:

**Question → resolved business meaning → mapped data and metric definitions → testable hypotheses → validated execution → evidence.**

This is an implementation review and proposed work sequence. It does not mark any improvement below implemented, and does not replace the delivery tracker.

## What already works

| Area | Existing behavior |
|---|---|
| Knowledge discovery | `knowledge/glossary_scan.py` finds coded fields, abbreviations, recurring nouns and terms in unanswered Ask questions. Model assistance fills bounded unresolved candidate definitions rather than creating arbitrary facts. |
| Model grounding | The glossary model receives screened metadata and profile shape; values require sample permission and non-sensitive columns. Output keys, lengths and synonyms are checked; model confidence is capped at 0.7. |
| Human review | `knowledge/suggestions.py` supports edit, approve, reject and provenance; approved glossary candidates become trusted workspace entries. Owner-authored descriptions are protected. |
| Context selection | `context/compiler.py` combines relevant-column selection, section retrieval, relevance gates, budgets, receipts, omissions and injection screening. This is more than a raw document dump. |
| Business lens | A published `AnalysisContextSpec` pins source scope, purpose, description and relevant metric names. `agents/common.py::compile_for` explicitly adds that lens to model inputs. |
| Relationship safety | Semantic suggestions measure keys and fan-out; hypothesis validation admits only the supported in-scope lookup paths. Name similarity alone is insufficient. |
| Hypotheses | Rules and domain templates run first; optional model proposals supplement them. Typed specs, method/type checks, deduplication, diverse selection and bounded rounds constrain execution. |
| Metrics | `agents/semantic.py` prioritizes approved definitions, prevents silent name/formula replacement, validates candidate expressions by execution, and leaves new workspace definitions for review. |
| Reuse | Context and model-response caching, hypothesis replay and version binding already exist. Reuse depends on the applicable inputs and versions, not question text alone. |

## Findings and priorities

### P0 — Connect the pinned reviewed brief to downstream model inputs

Guided starts copy `reviewed_context` into `run.origin`. `context_agent.load_context` saves its filtered facts in the context package and its user notes. However, the shared `compile_for` path explicitly carries the published analysis context, not the origin brief. The compiler's context-package reader `_run_external` extracts only the `external` provider results. Hypothesis generation does not explicitly add the context package's reviewed facts to its required inputs.

Consequently, saving ten reviewed facts in the artifact is not proof that the hypothesis model consumed those ten pinned facts. Some equivalent knowledge may still be retrieved independently from workspace entries or packs, but that does not guarantee the pinned version or the reviewed brief's corrections were used.

A focused characterization probe confirms that a published lens marker reaches the shared compiler inputs while a marker present only in the origin brief does not. This probe isolates the handoff; it is not an end-to-end live model test.

**Change:** extend the shared compiler input contract with a filtered, versioned reviewed-brief section. Include it in cache identity and prompt receipts. Apply current access checks to pinned content, screen text as context rather than instructions, and refuse visibly if required meaning cannot fit. Do not silently trim a critical exclusion or correction.

**Acceptance:** a unique reviewed correction reaches planning, hypothesis and applicable metric prompts; suggested and out-of-scope facts do not. Changing the pin changes the compiled identity. An over-budget required definition produces a clear refusal rather than an apparently grounded answer.

### P0 — Resolve meaning and ambiguity before running business-sensitive tests

`context_agent.py` currently resolves terms using suffix matching against in-scope columns and reports unmapped objective words. Its `ambiguous_terms` list is primarily a word heuristic; this step records ambiguity but does not itself pause the run. A valid timestamp or flag is not proof of the intended reporting meaning.

**Change:** resolve the outcome, population, entity grain, period, baseline, time column, units, flag polarity and joins into explicit bindings. Prefer exact source/asset/column identities and reviewed definitions over suffix matches. When multiple candidates change the answer materially, request a choice. Broader Ask clarification already exists; reuse its patterns without assuming it covers the investigation path.

For example, “resolution time” might mean elapsed time, business hours or time excluding paused states. The model can propose those alternatives; an owner or authoritative definition must choose.

**Acceptance:** `created_at` versus `resolved_at`, opposite breach flags, two same-named columns from different sources, priority polarity and business-hour calculations produce a correct binding or a clarification. A schema-valid query with unresolved critical meaning does not proceed as a verified business answer.

### P1 — Decide model exploration by question coverage, not candidate count alone

`investigator.generate_hypotheses` considers the deterministic path sufficient when carried plus valid rule proposals reach the configured count (six locally). That is a quantity threshold. Validity, method diversity and outcome diversity are useful, but they do not establish that a specific requested comparison or outcome was covered. Rule generation currently inspects the largest selected tables first and returns the first usable proposal set.

**Change:** evaluate coverage of the requested outcome, baseline, population, plausible alternatives and necessary tables. If gaps remain, send the model the existing valid candidates and the uncovered requirements. Ask it only for additional grounded tests. Retain budgets and the existing protected rule/model selection shares. Do not use an LLM's self-reported confidence as the correctness gate.

**Acceptance:** six generic candidates do not suppress exploration for an uncovered named outcome; sufficiently relevant candidates avoid the call. Adding a model does not displace the rule tests that existing benchmark evidence requires.

### P1 — Bring metric planning ahead of hypotheses when a new measure is required

The current playbook runs the semantic KPI stage after finding verification and dataset construction. The semantic agent can propose new metrics, but that later stage does not make an arbitrary new business metric executable by an earlier hypothesis. Structured investigations remain limited to the registered methods and derivations.

**Change:** before hypothesis selection, identify approved measures that answer the question. If none applies, propose a metric contract through the existing semantic review system: formula, numerator, denominator, grain, population, exclusions, unit, time basis, source columns and examples. Execute bounded checks and request review where the definition establishes new business policy. Bind hypotheses to the approved or explicitly scoped experimental version. Keep experimental labels visible; do not silently promote them into business KPIs.

**Acceptance:** a newly requested “repeat-incident rate” requires an agreed repeat definition and denominator. Execution success alone does not approve it. Existing approved metrics retain their meanings. Unsupported calculations produce an explicit gap rather than invented SQL or a made-up KPI.

### P1 — Make knowledge mappings and applicability visible

An unmapped memory item is broadly visible as workspace knowledge. Vocabulary applicability can retain all visible domains when the workspace domain is unknown. Pack references and catalog glossary links are different representations; their presence does not certify physical resolution. The prior read-only snapshot found 35 glossary-linked columns among 212 selected columns, not complete semantic coverage.

**Change:** extend the existing context preview and receipt UI with term → definition → source → table → column → metric → join resolution. Show mapped, ambiguous, unavailable and stale states. Show why an item was included or omitted. Ask the owner to classify broad business rules as general guidance or scoped definitions. Prefer explicit domain selection when it is known.

**Acceptance:** an analyst can explain which definition and exact field drove a hypothesis, which reviewed items were omitted, and whether a mapping resolves in the current selected data. Coverage distinguishes required business concepts from technical columns that need no glossary entry.

### P2 — Distinguish proposed business rules from executable tests and operational policy

The review queue supports rule documents and negative knowledge. That does not make every approved prose rule an executable analytic constraint, nor does it authorize an operational action.

**Change:** label suggestions as descriptive definitions, analytic eligibility/filter rules, data-quality assertions or operational policy. For executable rules, require typed predicates and measurable examples. Resolve conflicts and applicability before execution. Preserve existing review and side-effect approval boundaries.

**Acceptance:** “exclude cancelled incidents” becomes a versioned, verified population filter if approved; “escalate after 48 hours” remains a policy suggestion until an owner confirms its threshold and any action is separately authorized. The model cannot invent an SLA target or authorize writes.

### P2 — Preserve the reason for each selected analysis and learn from corrections

Hypothesis prompts request rationale, and ranking computes provenance, but the main hypothesis persistence path stores question, statement, spec, priority and origin without an explicit complete evidence chain for the model rationale and every mapping choice. Negative knowledge and prior findings already exist and should be extended rather than replaced.

**Change:** retain the objective clause addressed, accepted definition/version, chosen columns, rejected alternative mappings, method rationale and proposed evidence source. When a user corrects a result, distinguish wrong mapping, wrong formula, data quality, unsupported method and statistical rejection. A rejected statistical association is not a rejected business definition.

**Acceptance:** “Why did you test this?” can be answered from stored provenance without inventing a retrospective explanation. A mapping correction is reviewed and affects later relevant runs; it does not train a general rule from one unsupported association.

## Suggested example flow

Question: “Why did repeat incidents increase in September?”

1. Retrieve the relevant incident schema, relationships and reviewed policies.
2. Identify missing meaning: what counts as repeat, which entity is matched, the repeat window and which date defines September?
3. Ask only the unresolved questions; reuse approved answers already in scope.
4. Resolve or propose the repeat-rate metric contract. Do not invent a seven-day window.
5. Show the chosen numerator, denominator, columns, exclusions and baseline to the analyst.
6. Generate tests of the approved measure by service, team and priority; check volume and case mix as alternative explanations where the registered methods support them.
7. Run the typed tests, retain unsupported and inconclusive outcomes, verify findings and show caveats.
8. Review any newly useful metric or rule before making it reusable policy; assign follow-up work without claiming causation or automatic savings.

## Delivery sequence and proof

1. Fix pinned-context transport and add prompt-path regression coverage.
2. Add resolved meaning and clarification for critical ambiguities.
3. Add outcome/requirement coverage to hypothesis model gating.
4. Introduce prerequisite metric planning using existing semantic services.
5. Extend context preview with mapping and applicability explanations.
6. Add typed rule proposals and structured correction provenance.

For a controlled release, choose one domain and 20–30 owner-reviewed questions. Include wrong date fields, missing mappings, opposite flag polarity, duplicate-producing joins, insufficient data, conflicting definitions, schema changes and repeated questions. Require correct bindings or explicit abstention, stable approved metric definitions, safe execution and retained evidence. Then compare model-off, Auto and model-assisted configurations on accuracy, clarification burden, relevant hypothesis coverage, token cost and analyst correction time.

Do not optimize for the percentage of hypotheses supported: a well-designed investigation can correctly reject many plausible alternatives. Optimize for answering the actual question accurately, with evidence and an explicit account of uncertainty.

## Review evidence and limits

Inspected implementation: `agents/{common,context_agent,investigator,semantic,supervisor,prompts}.py`, `context/{compiler,service,preview}.py`, `knowledge/{glossary_scan,suggestions,entries}.py`, `semantic/suggest.py`, `services/relationship_safety.py`, analysis/context/semantic contracts and `investigate.v1.yaml` (all under `src/analystos/`, except the domain packs).

Focused checks: glossary scan, context compiler, model-plus-rule hypothesis behavior, semantic review rules and a temporary pinned-brief handoff probe. Results are recorded in `var/context-deep-review-tests-20261025.log`. These are focused local checks, not a full regression, new live model benchmark or production scalability assessment.

Previous mapping snapshot: [domain mapping audit](../60-delivery/evidence/domain-mapping-audit-20261025.json). Product overview: [team and executive guide](../30-runbooks/product-team-executive-guide-20261025.md).
