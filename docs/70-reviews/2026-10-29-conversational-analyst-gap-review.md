# 2026-10-29 — Conversational analyst gap review: dynamic dashboards and reports

**Question.** What does the platform still lack, compared with current AI-analyst products, to act as an autonomous
analyst that builds dashboards and reports through conversation? This review compares the platform as built on
`develop` (2026-10-29, after PRs #5–#10) with public descriptions of comparable products. It then names the gaps
that matter and turns them into tracker rows (CONV-01…CONV-06).

## 1. What the platform already does (not gaps)

| Capability | Where |
|---|---|
| Governed Ask threads (route, clarify, registry or generated SQL, gateway, numbers guard) | `services/ask.py`, `agents/sql_agent.py` |
| Autonomous investigations with plans, budgets, replans and approval gates | `autonomy/`, `runtime/engine.py` (S06) |
| Dashboard DSL and typed patch ops, utterance → ops (`dashboard_design` ladder), versions, revert, fork-on-edit | `dashboards/` (S08-T1/T2) |
| Builder chat panel over a dashboard grid | `web/src/components/DashboardBuilder.tsx` (S08-T3) |
| Publish to Superset with per-workspace isolation, guest-token embedding, reconcile, approvals | `publishing/`, `dashboards/access.py` |
| Publish a chosen dashboard collection on its own | `dashboards/collections.py` (BI-VIEWS-02) |
| Chart-type rules by intent and data shape | `skills/viz.py` |
| Reports from verified evidence: PDF, HTML, DOCX, PPTX, XLSX; schedules; deliveries | `reports/`, `services/reports.py`, `services/schedules.py` |
| Monitors, triggers, the request desk, what-if, playbooks (editor, simulator, signing) | S09, S13, N-9, S06-T6 |
| Conversation memory, typed corrections promoted to reviewed context | `conversations/` (S04) |

## 2. What comparable products offer

The products were chosen as the ones a buyer compares against. This is not an exhaustive survey. Claims come from
vendor documentation and third-party write-ups as of October 2026.

* **Databricks AI/BI and Genie.**
  - Agentic dashboard authoring: an agent creates and maintains a dashboard end to end. It writes reusable
    semantic definitions that feed several visuals, not one-off queries.
  - Import of Power BI and Tableau files into native dashboards.
  - Saved and shared conversations.
* **Power BI Copilot.** Generates report pages from a prompt over a semantic model, writes narrative summaries of
  a page, and answers chat-style questions.
* **Tableau Pulse / Amazon Q in QuickSight.**
  - Metric digests that explain what changed and why.
  - "Data stories": a narrative document built from dashboard visuals and refreshed with the data.
* **WrenAI (open source, AGPL).** Semantic layer (MDL), text-to-SQL and text-to-chart. Recent versions add agents
  that build dashboards and save reusable "skills".
* **Microsoft LIDA (research, open source).** Goal exploration from a dataset summary, visualization generation
  with self-repair, and chart editing by instruction.
* **DeepAnalyze / DataMind (research agents, 2026).** Autonomous analysis that ends in a written report.

## 3. Gaps that matter

| ID | Gap | Why it matters | Comparable |
|---|---|---|---|
| CONV-01 | **A new chart from a question inside the builder.** "Add resolution time by priority" cannot create a chart with data: builder previews come only from a run or a result handle (S08 §8 "Not built"). | Without it, conversation can only rearrange what an investigation already produced. | Genie authoring, Copilot, WrenAI |
| CONV-02 | **A whole dashboard from a prompt.** "Build an incident operations dashboard" starts an empty dashboard. A draft (KPIs, charts, filters, layout) is produced only by a full investigation. | First value in seconds; refine after. | Genie, Copilot report pages |
| CONV-03 | **From an Ask answer to a draft dashboard.** An answer can be published to Superset as a table, but not added as a recommended chart to a draft that you keep editing. | The natural path: ask, like the answer, keep it. | Genie "add to dashboard", Metabase |
| CONV-04 | **Conversational report authoring.** Reports are generated from a run's evidence; there is no report document a person can shape by conversation ("add a section on EMEA backlog", "include the trend chart", "shorter summary"). | Business readers want the document, not the dashboard. | Copilot, data stories, DeepAnalyze |
| CONV-05 | **Data stories and dashboard digests.** A dashboard has no narrative that says what it shows and what changed since the last refresh. | The autonomous analyst explains, it does not only draw. | Tableau Pulse, QuickSight stories |
| CONV-06 | **Interactive drill-down and cross-filtering in the in-platform preview,** each re-query governed. | Exploration without leaving the platform or exporting to Superset. | All BI tools |

Considered and not taken now:
* **Importing Power BI or Tableau files.** Valuable for migration, but no customer need is recorded yet. The
  existing-dashboard mode covers Superset.
* **Saved "skills" or agents from a conversation.** The playbook editor (S06-T6) and the S10 tool registry already
  give a governed form of this.

## 4. Rules every gap keeps (unchanged)

* Models propose, code decides. A question becomes an `AnalysisSpec` or `SemanticQuery`, then the semantic
  compiler, `skills/sqlbuild`, `gateway/validator` and `QueryGateway.execute`.
* Every chart carries provenance. Numbers in text pass the numbers guard.
* Publishing and exports stay hash-bound approvals.
* Rules first: each new model use gets a purpose in `config/models.yaml` with a rules rung, so `off` mode still
  produces a useful (if plainer) result.
* Business view by default: new screens use plain words, and engineering detail goes behind Show expert tools.

## 5. Plan

* **Wave 1** (parallel, separate files):
  - (a) CONV-01 + CONV-02 + CONV-03: conversational dashboard generation.
  - (b) CONV-04 + CONV-05: conversational reports and data stories.
* Parallel investigation branches (S06 §3.6) are already built (tracker S06-PAR), so they are not a gap.
* **Wave 2:** CONV-06 (drill-down and cross-filter), after wave 1 lands, because it touches the same builder files
  as (a).

Sources: [Databricks AI/BI February 2026 roundup](https://www.databricks.com/blog/whats-new-aibi-february-2026-roundup),
[AI/BI and Genie 2026 release notes](https://learn.microsoft.com/en-us/Azure/databricks/ai-bi/release-notes/2026),
[WrenAI](https://www.getwren.ai/),
[AI dashboard generators 2026 (third-party)](https://www.toolworthy.ai/blog/best-ai-dashboard-generators),
[DeepAnalyze](https://gittrend.io/repo/ruc-datalab/DeepAnalyze), [DataMind](https://gittrend.io/repo/zjunlp/DataMind).
