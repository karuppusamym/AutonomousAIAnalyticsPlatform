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
