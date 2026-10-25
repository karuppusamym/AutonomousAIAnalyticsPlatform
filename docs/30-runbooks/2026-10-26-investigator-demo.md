# Investigator demo — October 26, 2026

Branch: `codex/investigator-demo`. This increment improves the existing investigation UI;
it uses the existing API, query gateway, verification records and approval controls.

## Start locally

From the repository root:

```powershell
docker compose --profile demo up -d postgres api servicenow-mock
cd web
npm run dev -- --host 127.0.0.1
```

Open http://127.0.0.1:5173. For this local seeded installation, sign in as
`admin@analystos.local` with `ChangeMe123!` (unless the bootstrap password was changed).
The web development server serves this branch's current UI; the prebuilt Docker web image
does not include these changes until rebuilt. The API containers use existing local images.

## Five-minute walkthrough

1. Open **ServiceNow Incident Intelligence (demo)** and its completed investigation.
   Rehearsed local URL:
   http://127.0.0.1:5173/w/ws_8efcdf614840/work/investigations/run_5e3893278298.
   These IDs belong to this machine's existing database, not a fresh installation.
2. Start with the investigation brief: current verification counts, three findings at a glance,
   completed execution steps and the suggested next action. This is existing synthetic demo data.
3. Explain the distinction between **verified** and **needs checking**. In the rehearsed run,
   two findings are currently verified and four have void verification records. The latter must
   not be presented as current verified conclusions, even though their old boolean says verified.
4. Open **Limitations to keep in mind**. Use **Recorded run narrative** only as historical run prose;
   current verification badges take precedence.
5. In **Continue the investigation**, choose **Summarize the findings**, then **Send**. Show the saved-evidence
   answer and its clickable finding references. Refresh to demonstrate that the conversation is retained.
   Ask **What are the limitations?**, **Which findings need checking?**, or the suggested finding check question.
   These questions explain stored evidence; they do not rerun analysis or call a model. New analysis requests
   receive an explicit scope explanation. Select **Deeper analysis** when a fresh investigation is intended;
   that existing path can replan work and invalidate pending approvals.
6. Choose **Open evidence** on a verified finding. Show **How it was checked**, the query evidence,
   citations and lineage. Return to the investigation.
7. Choose **Explore findings** to reach the hypothesis board. Open the plan and tasks for execution
   details. Describe approval controls; only demonstrate a pending approval if one is available and
   the proposed action is intended. The brief's next-step button navigates; it never approves.

Local preview: `var/demo/investigation-brief.png`. Live conversation: `var/demo/conversation-live.png`. Live evidence screen:
`var/demo/evidence-live.png`. These generated images are ignored by Git.

## Scope and follow-up

Built: summary-first brief, current verification counts, direct evidence links, state-aware next
steps, execution progress, deduplicated limitations, responsive light/dark layout and keyboard
focus handoffs. A conversational follow-up panel returns bounded answers from saved evidence, with
references and persisted question/answer history. Existing evidence and approval screens remain the source of detail.

After pulling the conversation increment, rebuild the API with `docker compose up -d --build --no-deps api`.
The API was rebuilt locally during implementation. The later value increment requires migration
`0055_investigation_value`, applied by the rebuilt API locally. No new model server is required.

Design references: [AI Analyst](https://github.com/ai-analyst-lab/ai-analyst) for findings with
context and validation, and [AI DB Investigator](https://github.com/miller-28/skill-ai-db-investigator)
for evidence browsing. No third-party code was copied or new runtime dependency installed.

The value increment below adds investigation templates and reviewed business context. A complete
fresh investigation remains subject to plan approval. Evaluate Pipeline Studio,
PySpur and DeepAnalyze separately against the existing platform contracts; their workflow engines
and model infrastructure are not part of this increment.

The current production build emits a large-bundle advisory. This increment is a local demo UI
improvement, not a production readiness certification or a fresh end-to-end model evaluation.

## Readiness check after cleanup

The four curated demo workspaces remain visible. The API image now pins SQLGlot 30.20.0 to preserve
the tested gateway qualification API. Archived test notifications no longer crowd the bell.
OpenRouter chat connectivity and a model-generated analytical query are live-verified; all four
workspaces answered scoped data questions. The Ask screen rendered a 12-row Operations result.

A fresh Operations investigation is ready to review at
http://127.0.0.1:5173/w/ws_796ff841746f/work/investigations/run_385d980e0a6d.
Its plan requires approval before execution; no plan or publication was approved during readiness
checks. The optional isolated Python sandbox remains unavailable in this local Docker configuration;
use the verified SQL workflows for the demo. See `docs/60-delivery/evidence/demo-readiness-20261025.md`.

## Four value additions

1. Open the Operations workspace overview at http://127.0.0.1:5173/w/ws_796ff841746f.
   Show **Business context we can reuse**. Review a definition or correction before saving;
   these entries reuse the existing versioned brief.
2. Choose Start work / Explain and select a guided template. Show the four choices, period,
   comparison, focus and decision fields. The prepared SLA run is
   http://127.0.0.1:5173/w/ws_796ff841746f/work/investigations/run_a8040c23d416.
   Its card shows template v1, brief v2 and ten reviewed facts. It is waiting for plan approval;
   do not present it as a newly completed analysis.
3. Open the completed Operations run
   http://127.0.0.1:5173/w/ws_796ff841746f/work/investigations/run_66f63d34fc79.
   Show **Turn findings into follow-up** and the saved demo action, its owner and due date.
   Impact is a team assessment, not proven savings. Follow the evidence link before making decisions.
4. Return to the overview pilot scorecard. Explain that rehearsals and real assessments are separate.
   Both live counts start at zero. Use a rehearsal only when real task timings and a review exist;
   do not enter invented pilot results. Use the [pilot protocol](analyst-pilot-protocol.md) to conduct
   the real analyst trial.

Verification and remaining limits: [value increment evidence](../60-delivery/evidence/investigation-value-20261025.md).

## Simple context walkthrough

Open Operations → Catalog & definitions → Context & mappings. Expand the short usage guide. Try “Compare SLA breach rates by assignment group using the existing made_sla field.” Show resolved definitions, then choose **Investigate this question** to show the filled-in question and preflight. Close the dialog unless a person is ready to start and review the plan. On a phone, demonstrate **Choose a data section**. The separate live browser check covers this journey without creating workspaces or approving work.
