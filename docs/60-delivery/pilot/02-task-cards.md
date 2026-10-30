# Pilot task cards

Twelve tasks an analyst might get in a normal week, on the three demo workspaces. Each card is done twice by the
same analyst: the usual way (**manual**) and with the platform (**assisted**). The coordinator tells you which comes
first. Record both on one row of the [timing sheet](03-timing-sheet.csv), then once in the app's scorecard.

| Card | Workspace | Feature (scorecard "What was assessed") | Task |
|---|---|---|---|
| C01 | ServiceNow Incident Intelligence (demo) | Ask conversation | Incident count by priority |
| C02 | ServiceNow Incident Intelligence (demo) | Ask conversation | SLA compliance by team for one quarter, then narrowed |
| C03 | ServiceNow Incident Intelligence (demo) | Investigation | Explain the SLA change between two quarters |
| C04 | ServiceNow Incident Intelligence (demo) | Dashboard by conversation | Draft and refine a weekly operations dashboard |
| C05 | ServiceNow Incident Intelligence (demo) | Dashboard by conversation | Drill into and filter that dashboard |
| C06 | Retail Orders (demo) | Dashboard by conversation | Turn an answer into a dashboard chart |
| C07 | Retail Orders (demo) | Report by conversation | Monthly delivery and returns report, exported as PDF |
| C08 | Retail Orders (demo) | Report by conversation | Read a dashboard's summary and turn it into a report |
| C09 | Retail Orders (demo) | Scheduled digest | Weekly digest on a schedule |
| C10 | ServiceNow process mining (demo) | Ask conversation | Slowest handover between steps |
| C11 | Retail Orders (demo) | Investigation | Why are orders returned? |
| C12 | ServiceNow Incident Intelligence (demo) | Ask conversation | A question the data cannot answer |

## Timing rules (every card)

* **Start** the clock when you have read the card and begin work. **Stop** when you would hand the result to the
  person who asked: checked, corrected, in the form they asked for. Pause for breaks and interruptions only.
* **Manual minutes:** all your working time with your usual tools (SQL client, spreadsheet, BI tool), including your
  own checking.
* **Assisted minutes:** all your working time in the platform: typing, waiting for answers, reading, checking numbers,
  correcting, re-asking. **Of which checking and correcting** is the part spent checking and fixing; it is inside the
  assisted minutes, not added to them.
* **Times you had to step in:** count every rephrase, answered clarifying question, correction, re-run, or
  request to someone else (an owner recording a definition, the approver).
* Write the start and finish times in the sheet as you go; round minutes to one decimal place.
* Do not look at the other run's result for the same card. Do not look at the answer key.
* Record failures, refusals and abandoned attempts too, with **Accuracy** "Incorrect" or "Not reviewed yet" and a note.
  An abandoned assisted run counts as the time you spent before giving up.

**Accuracy** is set by the reviewer, not by you: leave it at "Not reviewed yet" unless the coordinator says otherwise.
"Correct" means every criterion on the card is met; "Partly correct" means the main answer is right but a criterion is
missed (period, scope, a wrong secondary number); "Incorrect" means the main answer is wrong or a wrong number would
have reached the requester.

---

## C01 · Incident count by priority

**Ask:** "How many incidents do we have at each priority level?"
**Manual:** count incidents per priority from the incident table.
**Assisted:** open **Ask a question**, type the question, read the answer and the table. Check it the way you would check a colleague's work.
**Correct when:** all five priorities are listed; each count matches a direct count; the counts add up to all incidents; nothing is invented.
**Scorecard:** Ask conversation → the conversation you used.

## C02 · SLA compliance by team for a quarter, then narrowed

**Ask:** "What share of incidents met SLA for each assignment group in Q3 2026? Then the same for priority 1 and 2 only."
**Manual:** for incidents opened 1 July – 30 September 2026, the share with "made SLA" per assignment group; repeat for priority 1–2.
**Assisted:** in **Ask a question**, ask the first question; then ask the narrowed version as a follow-up in the same conversation
(for example "Only priority 1 and 2 incidents"). If the follow-up is not understood, rephrase it in full and count a step-in.
**Correct when:** both tables use incidents opened in Q3 2026; the second is limited to priority 1–2; each share matches a
direct calculation; incidents without a group are either shown or said to be left out; an incomplete quarter in the
data is pointed out.
**Scorecard:** Ask conversation.

## C03 · Explain the SLA change between two quarters

**Ask:** "SLA results moved between Q2 and Q3 2026. What changed, and which teams should the service desk lead review first next month?"
**Manual:** compare Q2 and Q3 2026 SLA results by assignment group and priority; say what changed and which teams to review.
**Assisted:** **Investigations → Start work → Guided investigation: Explain an SLA increase.** Period to investigate "Q3 2026 (July to September)",
comparison baseline "Q2 2026 (April to June)", decision this should support "Which teams the service desk lead should review first next month",
data **ServiceNow**. Approve the plan if asked. Read the findings and their evidence.
**Correct when:** the findings compare the two named quarters (not all history); every number matches a direct
calculation for the stated period; the direction of the change is right; an incomplete period in the data is pointed
out; associations are not presented as causes; the decision question is answered.
**Scorecard:** Investigation → the run.

## C04 · Draft and refine a weekly operations dashboard

**Ask:** "I need a weekly incident operations dashboard: volume, SLA compliance, resolution time by priority, busiest teams.
Let me filter by assignment group."
**Manual:** build the same in your BI tool or spreadsheet (four to six tiles and a filter).
**Assisted:** **Dashboards & reports → Build a dashboard from a request**, paste the ask into "What should the dashboard show?". Then refine by conversation: "add average
resolution time in hours by priority", "add a filter for assignment group", and remove any tile you would not keep.
**Correct when:** every kept tile is about incidents and answers part of the ask; each number matches a direct
calculation; each requested change was applied; nothing unrelated stays on the dashboard without you choosing it.
**Scorecard:** Dashboard by conversation → the dashboard. (C05 uses the same dashboard; give each card its own task text.)

## C05 · Drill into and filter the dashboard

**Ask:** "For the network category only, how do the other tiles look? And how did August 2026 break down by day? Show me the records."
**Manual:** filter your C04 workbook to category = network; break August 2026 down by day; list the records.
**Assisted:** on the C04 dashboard, click the **network** bar to filter, read which tiles changed and which say they were
not filtered; on the per-month chart drill into August 2026, then **Show rows**.
**Correct when:** each filtered number matches a direct calculation for category = network; tiles that were not filtered
say so; the daily counts add up to the August total; the rows shown hide personal columns you are not allowed to see.
**Scorecard:** Dashboard by conversation → the C04 dashboard.

## C06 · Turn an answer into a dashboard chart

**Ask:** "What is our late delivery rate by sales channel? Put it on a dashboard I can share later."
**Manual:** late deliveries ÷ orders per sales channel; make a chart.
**Assisted:** in **Ask a question** (Retail), ask the question, then **Add to dashboard** → a new draft titled "Delivery performance".
**Correct when:** the rate per channel matches the approved "Late Delivery Rate" definition (delivered after the promised date);
the dashboard chart shows the same numbers as the answer.
**Scorecard:** Dashboard by conversation → "Delivery performance".

## C07 · Monthly delivery and returns report, exported as PDF

**Ask:** "A one-page monthly report for the operations manager: late delivery rate, late deliveries by channel, returns by
product category with the main finding, and a two-line summary in your words. As a PDF."
**Manual:** write it in your usual document tool with numbers from your own queries.
**Assisted:** **Dashboards & reports → Reports → Write a report → Start report**, then by asking: "add a KPI for late delivery rate", "include the late delivery
rate by sales channel chart", "add a section on returns by product category", "include the returned concentrates in product
category finding", then set the summary yourself ("set the summary text to: …"). Request a **PDF** export; the approver approves; download it.
**Correct when:** every number in the PDF matches a direct calculation and its scope (period, filter) is stated; the finding's
scope is clear; the PDF was produced only after approval.
**Scorecard:** Report by conversation → the report.

## C08 · Read a dashboard's summary and turn it into a report

**Ask:** "What does the Delivery performance dashboard tell us? Send me that as a short report."
**Manual:** read the dashboard and write three to five true sentences about it.
**Assisted:** open the C06 dashboard's **Summary**, read the sentences, then **Turn this into a report**.
**Correct when:** every sentence is true for the dashboard's numbers; no sentence uses a meaningless statistic (for
example a "share of the total" for a rate); the report keeps the same numbers.
**Scorecard:** Report by conversation → the story report.

## C09 · Weekly digest on a schedule

**Ask:** "Send me the C07 report every Monday morning."
**Manual:** describe how you would do this today (calendar reminder, re-running queries, emailing a file) and time one
Monday's worth of that work.
**Assisted:** **Automations → Schedules → New schedule**, kind report, weekly, named "Monday delivery digest"; in the C07 report say "pin it to the Monday
delivery digest schedule"; on the schedule press **Run now**; open the produced digest under **Dashboards & reports → Reports**.
**Correct when:** the digest's numbers equal the report's; it lists its sources; nothing was sent to a destination that was not
approved. (A dashboard digest can also be attached by the technical owner; there is no screen for it yet.)
**Scorecard:** Scheduled digest → the schedule (it must have run once).

## C10 · Slowest handover between steps

**Ask:** "Where does work wait longest when it is handed from one person to another?"
**Manual:** average waiting hours per step-to-step transition, handovers only; the top one.
**Assisted:** **Ask a question** in ServiceNow process mining.
**Correct when:** the transition named and its average wait match a direct calculation over handover transitions only.
**Scorecard:** Ask conversation.

## C11 · Why are orders returned?

**Ask:** "Why are some orders returned, and are late deliveries part of the reason? Compare categories, channels and regions;
tell me what the data cannot show."
**Manual:** your usual analysis of return rates by category, channel and region, and returns for late versus on-time orders.
**Assisted:** **Investigations → Start work → Custom question** in Retail, with the ask as the question. If it asks you to review business meanings, ask the
workspace owner to record them (each request is a step-in), then **Recheck reviewed context**.
**Correct when:** each finding's numbers match a direct calculation for its stated scope; the late-delivery link is tested
or explicitly said to be untestable; missing data (no return reason) is stated; no cause is claimed from an association.
**Scorecard:** Investigation.

## C12 · A question the data cannot answer

**Ask:** "What was the customer satisfaction score for the Network team last month?"
**Manual:** check whether any table holds satisfaction scores; say what you would tell the requester.
**Assisted:** **Ask a question** in ServiceNow Incident Intelligence.
**Correct when:** the answer says plainly that the data has no satisfaction measure and gives no number. Any number is "Incorrect".
**Scorecard:** Ask conversation.
