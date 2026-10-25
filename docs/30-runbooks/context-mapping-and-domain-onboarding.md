# Context mapping and domain onboarding

## Start with the question

Open **Data → Catalog & definitions → Context & mappings**. Enter the business question and leave model assistance enabled when desired. The app reads reviewed workspace/pack knowledge and authorized metadata before asking for missing meaning. Model choices must cite existing evidence IDs and the columns that evidence maps. Model confidence alone cannot authorize a mapping or override a conflict.

Definitions, reporting dates, flag polarity, record grain, population and measurement units should be resolved from existing knowledge when possible. If a material definition is still absent or contradictory, the investigation waits before hypothesis tests. Review the remaining questions, correct the mapping and use **Recheck reviewed context** in the run. This does not approve a plan or publication.

An ordinary breakdown of an existing reviewed measure is a view, not a new metric policy. A new repeat window, eligibility rule, weight or denominator still needs an authoritative definition; the model cannot invent it.

Requested populations and reporting windows become typed constraints, not raw model SQL. Explicit named-month/year windows use inclusive start and exclusive end when a reporting timestamp is resolved. A missing year can come from an unambiguous reviewed reporting-year policy. Requested code values can come from a cited data dictionary. A relative window without a reviewed calendar, or an unsupported comparison that cannot be grounded, remains a clarification instead of silently testing the whole dataset. The question preview and run panel expose the applied constraints.

## Onboard or correct a domain

1. Choose the intended sources and selected tables in Sources; discover and profile them.
2. Use **Suggest business terms** to scan metadata/profile shape and unanswered questions. Suggestions go to the existing review queue. Review, edit, approve or reject them there.
3. Use **Map a business term**, or **Review mapping for …** on available knowledge. Pick the actual catalog columns; no metadata JSON editing is required. Source applicability can be selected or inferred from those mappings.
4. Choose the applicable domain pack or scoped workspace knowledge. Set a review owner, supporting policy/evidence, validation examples, effective date and optional expiry.
5. Save a draft, then use **Check mapped data**. Checks read at most 1,000 sampled records per column, on up to ten columns, through the query gateway. Counts and query IDs are retained in a lineage-linked artifact. These checks do not certify whole-table uniqueness or prove business meaning. Quality predicates can also report matching rows in that sample.
6. After the domain owner has checked the meaning and examples, save it as reviewed and approved. Updates require the current revision. Retire obsolete definitions or update expiry/applicability as appropriate.

Drafts are not facts. An unapproved replacement does not displace an active reviewed meaning. Conflicting current owner mappings remain ambiguous. Expired, retired, future-effective, unavailable-source and wrong-domain mappings cannot silently enter the reviewed context path. The studio shows readable mappings in the caller's authorized scope; restricted definitions are withheld rather than revealed through a coverage report.

## Define measures and rules precisely

**Metric contracts** record outcome columns, numerator, denominator, grain, unit, time basis, exclusions and any flag code. Supported investigation calculations include an existing numeric outcome, a flag rate and elapsed hours between two mapped timestamps. These are converted to the platform's existing analytical vocabulary and validated before execution. They do not turn arbitrary prose or arbitrary LLM SQL into a new method. New or unsupported calculations remain proposals; use the existing Metrics/Definitions review paths for formal semantic KPIs.

**Investigation population filters** require a typed mapped-column comparison. Approved applicable filters constrain hypotheses on that table; they are not security permissions or universal SQL policies. **Quality assertions** can be measured in bounded structural checks. **Policy guidance** remains descriptive and cannot execute an operational action. Access policy and external side-effect approvals continue through their existing governance paths.

## Understand coverage and actual usage

The studio's coverage counts columns whose visible business knowledge resolves to current physical references, including pack references and reviewed mappings. This differs from the older catalog-column glossary-alias count. Neither is a semantic certification; technical columns may need no business glossary entry.

The question preview shows resolved meanings and unresolved requirements. The run's **Why this context and analysis?** panel shows its resolution, question coverage and actual pinned-brief prompt receipts. Each hypothesis can show the definitions referenced by that test. A fact saved in a brief is now explicitly carried into the shared prompt builder; required reviewed facts are refused visibly if the prompt budget cannot contain them, instead of silently truncating them.

Prompt reuse incorporates scope, knowledge versions, pinned inputs and effective date. If mappings change before new hypotheses are generated, the app re-resolves context. Hypothesis provenance and verification dependencies retain the meaning versions used; later changes can invalidate current verification.

The Context and Investigator manifests are now version 1.1.0 with declared model purposes and bounded call budgets. Existing runs retain their capability pins. Start a fresh investigation to use the new manifest behavior; older pins may correctly use deterministic fallback. Platform model modes, workspace policy, provider availability and spend limits still govern whether a model is called.

## Evaluate before relying on it

Add **Golden questions** with independently reviewed expected columns or an expected clarification. Evaluations check up to 30 cases and retain the question, expectations, actual mappings and outcomes. Normal mapping cases require an exact column set, so unrelated additional fields do not count as success.

These are grounding checks, not numerical-answer accuracy or analyst productivity measurements. Continue with the [analyst pilot protocol](analyst-pilot-protocol.md), review computed answers against known results and enter actual timings/corrections in the existing workspace pilot scorecard. Rehearsals stay separate from real observations. No actual pilot outcome has been generated automatically.

## Simplified question handoff

Open the built-in usage guide for the everyday journey and result terminology. After a successful meaning check, **Investigate this question** opens the existing Start work preflight with the question filled in; it does not start analysis automatically. Changing the question, model choice or a saved mapping clears the old preview. Typed rule inputs distinguish text, numbers and booleans. On phones, **Choose a data section** reaches all catalog sections. Owner question evaluations remain available in an expandable section. See [everyday usage](everyday-usage.md).
