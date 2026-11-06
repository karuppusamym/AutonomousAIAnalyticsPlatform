# Blue Ocean Discovery: Governed autonomous analytics

Date: 2026-10-30. Context: README and the v2 positioning line only; no market research was run.
Competitor ratings are judgment from general knowledge, not checked against current vendor docs. Verify before
external use. Tier sizes are order-of-magnitude guesses.

## Strategy Canvas

Competitor types: **A** warehouse-native NL-to-SQL (Snowflake Cortex Analyst, Databricks Genie) · **B** BI-suite
copilots (Power BI Copilot, Tableau, ThoughtSpot) · **C** chat-with-data apps (ChatGPT data analysis, Julius) ·
**D** notebook/collaboration tools (Hex).

| Factor | A | B | C | D | Industry avg |
|---|---|---|---|---|---|
| Natural-language Q&A | 4 | 4 | 5 | 3 | 4 |
| Visualization polish | 2 | 5 | 3 | 4 | 3.5 |
| Connector breadth / stack neutrality | 1 | 3 | 3 | 3 | 2.5 |
| Semantic / metric layer | 4 | 4 | 1 | 2 | 3 |
| Access governance (RBAC/RLS) | 5 | 4 | 2 | 3 | 3.5 |
| Verifiable evidence behind each number | 2 | 2 | 1 | 3 | 2 |
| Autonomous multi-step investigation | 2 | 2 | 3 | 2 | 2 |
| Proactive monitoring | 2 | 3 | 1 | 2 | 2 |
| Approval-gated side effects (publish/schedule) | 2 | 3 | 1 | 2 | 2 |
| Reusable context for other apps and agents | 3 | 2 | 1 | 2 | 2 |
| Time-to-value | 3 | 3 | 5 | 3 | 3.5 |
| Cost predictability | 2 | 3 | 4 | 3 | 3 |

- **Converged:** "ask in English, get a chart." Answer quality, polish and speed to first answer.
- **Diverged:** governance and connector breadth, mostly because each vendor is tied to its own stack.
- **Industry assumption:** a fast, fluent answer is the product; trust will come from better models.
- **Overlooked (2 or below for everyone):** evidence per number, autonomy, proactive monitoring, approval-bound
  actions, reusable context.

## Non-Customer Analysis

### Tier 1: Soon-to-be non-customers
- **Who:** companies that bought a copilot or Genie-style tool, where analysts still get the same tickets because
  business users don't trust the numbers.
- **Tolerating:** untraceable answers, metric definitions that drift between tools, a human re-check before publishing.
- **Would leave for:** a tool that answers only what it can certify and hands the rest to an analyst with evidence.
- **Substitutes:** the analyst ticket queue, spreadsheets, a second AI tool.
- **Size:** most enterprises piloting AI-BI (estimate).

### Tier 2: Refusing non-customers
- **Who:** regulated or audit-bound organisations (banks, insurers, health, public sector) and air-gapped sites, e.g.
  Nigerian fintechs under regulator scrutiny, German Mittelstand with on-prem data and GDPR constraints, Nordic
  public bodies.
- **Why they refuse:** a model writing SQL and stating a number with no audit trail; data leaving the perimeter;
  lock-in to one cloud's warehouse.
- **Use instead:** analyst teams, manual reporting packs, on-prem BI without AI.
- **Convert with:** the deterministic path (validated spec → gateway → statistics → numbers guard), local or
  air-gapped model options, tenant-bound audit.
- **Size:** smaller in headcount, highest in contract value.

### Tier 3: Unexplored non-customers
- **Who:** mid-market and SMB operators (e.g. India) with data in Postgres, Tally, Zoho or ERP exports and no analyst;
  and **other software agents and apps** that need trusted metrics.
- **Why ignored:** pricing and sales motions assume a data team; "analytics" is assumed to end in a dashboard.
- **What makes it relevant:** certified answers exposed over REST/MCP; a lite profile that runs without a warehouse.
- **Use instead:** nothing, spreadsheets, or agents querying raw databases ungoverned.
- **Size:** unclear, possibly the largest; least validated.

**Shared barriers:** can't trust or audit the number, can't govern it, need a data team to set up and maintain.
**Shared unmet need:** an answer that is provably right or honestly escalated.

## Four Actions (ERRC)

**Eliminate**
- The chart-first product: no proprietary visualization layer; publishing is delegated to Superset.
- Unverified answers: nothing reaches a user without the deterministic path.
- Warehouse lock-in: every source goes through one gateway across database kinds.

**Reduce**
- Model dependence: rules rung first, model only proposes; lowers and stabilises per-question cost.
- Visualization investment: "good enough" via Superset.
- Analyst time on repeat requests: certified answers serve them instantly.

**Raise**
- Evidence per number (2 → 5).
- Governance: tenant-bound, ABAC, audit (3.5 → 5).
- Stack neutrality (2.5 → 4).

**Create**
- Answer-or-escalate: the Analyst Request Desk (S13) hands unanswerable questions to people with evidence.
- Meaning review: analysts own definitions; approved definitions accumulate into a certified library.
- Context as a product: versioned context other apps and agents query via MCP/REST/OKF (S03, S10).
- Autonomous investigation within budgets, plus proactive monitors.
- Approval-bound side effects: publish, schedule and notify only after human approval, bound to hashes.
- Value metrics: analyst hours saved.

## New Value Curve

| Factor | Industry avg | Blue ocean |
|---|---|---|
| Visualization polish | 3.5 | 2 (reduced, delegated) |
| Natural-language Q&A | 4 | 4 (held, certified-first) |
| Connector breadth / neutrality | 2.5 | 4 |
| Semantic / metric layer | 3 | 4 |
| Access governance | 3.5 | 5 |
| Verifiable evidence | 2 | 5 |
| Autonomous investigation | 2 | 5 |
| Proactive monitoring | 2 | 4 |
| Approval-gated side effects | 2 | 5 |
| Reusable context for agents | 2 | 5 |
| Time-to-value | 3.5 | 3 |
| Cost predictability | 3 | 4 |

## Opportunity Summary

**The opportunity:** compete on trust instead of fluency. The red ocean is "ask a question, get a chart"; the open
water is "an answer you can defend, or a clear handoff to a person."

**Primary customers:** Tier 1 companies that bought AI-BI and aren't getting trusted answers; regulated Tier 2
organisations as the beachhead.

**Key differentiator:** the number is verified and the action is approved. Models propose, code decides.

**Cost impact:** no proprietary chart layer, no warehouse tie, rules rung before the model.

**Value impact:** analysts move from answering tickets to owning meaning; other agents get a governed source of numbers.

**Risks to test**
- Incumbents can add evidence and approval features; the lead depends on how deep the deterministic path goes.
- Reducing visualization polish may block Tier 1 buyers who judge on demos.
- Tier 3 sizing is a hypothesis until a few operators and agent builders are interviewed.

**Implications for next steps**
- PRFAQ headline: "every number comes with its evidence, and what it can't prove goes to a person."
- Closed World: certified answers, the context ledger and the tool registry carry the differentiation.
- Persona: run JTBD on the Tier 1 analyst-team lead, who is both buyer and the person whose tickets get cut.

## Competitors for the number check (web check, 2026-10-30)

| Group | Players | What they verify | Gap the number check fills |
|---|---|---|---|
| Trusted answers inside AI BI | Snowflake Cortex Analyst → Cortex Agents (verified queries in semantic views; Aug 2026 note recommends moving to Agents), Databricks Genie (trusted assets, benchmarks; Genie One/Agents from Jun 2026), ThoughtSpot Spotter (semantic-layer tokens, "verifiable", MCP integration), Power BI Copilot (verified answers on the semantic model) | Their own answers, inside their own product | A number from *another* tool, slide or agent; verdict decided by code |
| Semantic / metric layers | dbt Semantic Layer, Cube, AtScale, LookML | One definition | Does not judge a quoted number |
| Data quality / diff | Monte Carlo, Bigeye, Anomalo, Datafold | Data health, table diffs | Not business claims |
| AI-claim checking | Count (claim labels + runnable SQL, vendor claim), factfence (open source, numbers vs cited web pages), Aletheia (ACM research prototype), LLM guardrails (Patronus, Galileo) | Text vs sources, or their own answers | Not against a governed, scoped warehouse answer with a receipt |

No product found that checks an arbitrary external claim against a governed warehouse answer as a service (API + MCP).
This is a gap, not a moat: group 1 can add it. Vendor accuracy claims (e.g. Genie "32% → 90%") are self-reported.

## Built from this

Most Raise/Create factors already existed (Desk escalation S13, meaning review, context ledger + MCP S03/S10,
approval-bound side effects, REV and the numbers guard, value metrics). The gap was the Tier 1 wedge: checking a
number another tool produced without replacing that tool. Built as **S07-T8 number check** (2026-10-30): API, MCP
tool `number.check`, and a "Check a number" panel on the Ask screen.
