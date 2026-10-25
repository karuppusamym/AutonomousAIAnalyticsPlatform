# Context and hypothesis grounding — 2026-10-25

Branch: `codex/investigator-demo`. Scope: GROUND-01 through GROUND-07; software implementation complete. Actual analyst pilot VALUE-05 remains outstanding.

## Delivered behavior

1. Relevant model prompts receive scoped reviewed facts, knowledge versions and receipts. Hypothesis evidence tracks the versions actually used, including reviewed brief dependencies.
2. Resolve business meaning from applicable approved knowledge first, then bounded model assistance. The model must cite known evidence and authorized columns. Conflicts, missing definitions and incomplete measures pause investigation; recheck resumes meaning resolution without approving a plan or publication.
3. Auto hypothesis generation checks requested outcomes and coverage, rather than relying solely on a six-candidate count. Model proposals supplement validated rule candidates.
4. New measures require reviewed formula inputs, denominator, exclusions, grain, unit and time basis. Supported contracts include numeric means, flag rates and elapsed-time measures. Grouping an existing reviewed measure does not automatically create a new measure proposal.
5. Catalog → Context & mappings shows authorized columns, mapped/unmapped/outdated/conflicting knowledge, guided owner review, source/domain applicability, dates and revisioned bindings. Run explanations distinguish available entries, resolved meaning and per-hypothesis evidence.
6. Prose definitions and policies do not become executable calculations. Typed population filters pass existing validation; typed quality assertions support bounded sample checks through QueryGateway. These are not universal SQL security policies.
7. Owner-authored golden questions store expected mappings or expected clarification and repeatable evaluations. Pilot scorecards remain separate.

## Validation

- 197 relevant unit tests passed (`var/grounding-final-unit.log`).
- Six integration tests passed, including grounding API, meaning wait/recheck and existing local end-to-end investigations (`var/grounding-final-integration.log`).
- 50 frontend tests passed (`var/grounding-done-ui-tests.log`).
- Five Chromium browser journeys passed, including mapping lifecycle/reload, accessibility checks, phone layout and dark theme (`var/grounding-done-browser.log`). Final phone screenshot visually inspected.
- Production frontend build passed. Existing large bundle advisory remains; this is not a load/performance certification.
- OpenAPI export freshness, targeted Ruff and whitespace checks passed.
- Browser journeys use the mock backend; the separate live check below uses the running local API and provider.

## Live local check

Workspace `ws_796ff841746f` exposes 50 authorized catalog columns, of which 12 have knowledge links; this is link coverage, not certified semantic correctness.

Question: “Compare SLA breach rates by assignment group using the existing made_sla field.” Live OpenRouter was used; four supporting entries resolved; no clarification was required. This confirms reuse of the existing grounded outcome for this question, not arbitrary-question correctness.

Draft binding `bind_38d314e82f7a` maps `src_src_55781634f7ba.incident.made_sla`. It was deliberately left unapproved for owner review. Gateway query `qry_81b0ff3af2ad` examined 1,000 sampled rows: 1,000 non-null values and two distinct values. This establishes a bounded structural check, not proof of business meaning or whole-table data quality.

Machine-readable local evidence: `var/grounding-release-20261025/live-check.json` and `model-resolution.json` (ignored local artifacts). No provider credentials included.

## Limits and handoff

No real analyst assessment exists yet; no productivity or business-benefit claim is established. Golden evaluations currently check grounding and clarification, not numerical answers. Complex new calculations, unsupported calendar semantics and unresolved operational definitions remain review proposals. Supported explicit calendar-month constraints and grounded typed filters are validated before hypothesis execution; broader date reasoning is not certified.

Existing run approvals and publication gates remain enforced. Existing pinned runs retain their agent versions and budgets; new runs use updated manifests. The optional Python sandbox is outside this validation.

Owner instructions: [Context mapping and domain onboarding](../../30-runbooks/context-mapping-and-domain-onboarding.md).
