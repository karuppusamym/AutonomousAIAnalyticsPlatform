# AnalystOS: everyday usage

## Choose the right task

Use **Ask** for a focused question such as “How many incidents breached SLA?” Use **Start work → Explain** to investigate possible reasons, or **Compare** to test differences between groups. Availability depends on your role, data and workspace settings.

## First visit

1. Open a workspace. Under **Data → Sources**, connect or upload data and select the tables to use.
2. Under **Catalog & definitions → Context & mappings**, review what business terms mean and which columns represent them. Owners can request model suggestions, review them and save mappings without editing JSON.
3. Try a business question. Include the outcome, population and time period, for example: “Compare SLA breach rates by assignment group for March 2026.” The app reads applicable reviewed knowledge and may use the configured model to resolve the question.
4. If meaning remains unclear, review the specific questions and definitions. Do not approve a guessed calculation. A draft is available for review; it does not become an approved executable measure.
5. When ready, choose **Investigate this question**. The question carries into Start work. Review the source, costs, readiness and proposed plan before proceeding. Context is checked again during the investigation; the preview does not freeze or approve the analysis.
6. Read the investigation brief, evidence and limitations. Resolve any request under “What needs you”. If definitions changed, use **Recheck reviewed context** in the investigation. Plan and publishing approvals remain separate.
7. Record follow-up actions with owners and success measures. Use the pilot scorecard for actual analyst observations; rehearsals are separate.

## Understand results

A **hypothesis** is a possible explanation to test. **Supported** means a test found evidence under its assumptions; check verification. **Rejected** means this test did not support the explanation, not that its opposite is universally true. **Inconclusive** means the data or test could not decide. A **verified finding** passed the app’s required checks; assumptions and limitations still matter.

A question may produce several hypotheses. Counts such as three supported and nine rejected describe tested explanations, not twelve independent business conclusions.

## Find your way

The overview and context screen include **New here? How to get an answer you can trust**. Open the top-bar **Guide** for capability descriptions, tours and definitions. On phones, use **Choose a data section** to reach every catalog section.

The context screen keeps owner testing under **For owners: test saved questions and prepare a pilot**. These saved question checks test mappings and clarification, not numerical answer accuracy.

Viewers can read definitions and evidence. Analysts can resolve questions and run analysis. Editors and owners can maintain mappings, subject to server permissions. A role does not bypass data scope or approval requirements.

## Remaining work requiring people or systems

The software supports the real analyst pilot, but actual timings, corrections and owner sign-off must come from participating analysts. Company identity-provider configuration and certification against external warehouse instances require those environments. The optional isolated Python execution service is still unavailable locally. None of these are marked complete by a UI rehearsal.

Detailed owner instructions: [Context mapping and domain onboarding](context-mapping-and-domain-onboarding.md).

## See examples and related records

In the Retail Orders workspace, ask **How many orders are there?**, then **Show 5 sample orders**. The app returns a few example rows. **Show 5 sample orders with related details** includes matching customer and product fields using the reviewed relationships. These prompts use the bounded catalog rule path and the query gateway; no model call is needed.

A follow-up such as “list down few same order” is interpreted as a sample request only when the preceding successful answer identifies the matching table. The answer states that interpretation explicitly. A real duplicate request, a filtered new question or an unclear table keeps the normal generation/clarification path. Prior single-table filters are preserved when readable under current access.

Example rows help explain records and joins. They are ordered examples, not a statistically representative sample. Use a question about the full population when starting an investigation into business causes. Related tables require validated same-source many-to-one or one-to-one relationships; unrelated tables are not joined automatically.
