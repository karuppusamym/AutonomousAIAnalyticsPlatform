# Data and privacy note

## Which data

Only the three demo workspaces: **ServiceNow Incident Intelligence (demo)** (20,000 incidents, 3,000 change requests,
team targets), **Retail Orders (demo)** (6,000 orders, 400 customers, 60 products) and **ServiceNow process mining
(demo)** (task activity log and derived cases and transitions). The data is synthetic. Some columns look personal
(caller names, customer names and emails); the platform treats them as personal and hides them unless a person's
role allows them. No real customer or employee data is used. To pilot on your own data later, register it as a new
workspace and repeat the readiness checks first.

## Who sees what

| Who | Sees |
|---|---|
| Analysts | The workspaces they are members of, through their own login. Their own Ask conversations are private to them. Dashboards, reports, investigations and scorecard entries in a workspace are visible to its members |
| Approver | The approval requests in their workspaces (exports, publishing, scheduled delivery) and what each would release |
| Reviewer, coordinator | The scorecard and the results they review. The answer key is theirs only |
| Owner, technical owner | Everything above, the audit log, and the platform's settings |
| Anyone outside the pilot | Nothing. Exports and deliveries need an approver, and only go to approved destinations |

Every query goes through the platform's one data gateway with the person's own permissions; every export, publication
and delivery is an approval bound to exactly what was approved. The audit log records who did what.

## Model use

* **Low-cost models only.** The platform's routes for this pilot use small open models through OpenRouter (Qwen 3,
  DeepSeek, gpt-oss; see `config/models.yaml`). No Claude or Gemini models. Rules and approved definitions answer first;
  a model is asked only when they cannot, and every model answer still goes through the same checks (query validation,
  the numbers check on text, approvals).
* **What a model receives:** the question, the table and column names in the person's scope, reviewed definitions, and
  for some steps small result summaries. Columns a person may not read are outside their scope and are not sent;
  e-mail addresses, card and ID numbers and keys are masked in anything that leaves the platform
  (`llm/redaction.py`). Raw tables are never sent.
* **Spending:** each model call is metered against the workspace budget; the platform refuses calls over budget rather
  than overspending.
* **Option: rules only.** The technical owner can turn models off. Then only questions the rules and approved
  definitions cover are answered; open questions are refused with a reason (the rehearsal measured this: see the
  rehearsal report). Decide before day one, and do not switch mid-pilot: it changes what is measured.

## Pilot records

Timing sheets and scorecard entries use pseudonyms (A1, A2…), not names. Keep the sheet in the owner's normal document
store. Scorecard entries stay in the platform with the assessment type that was chosen; rehearsals stay separate from
real observations and are never counted as results.
