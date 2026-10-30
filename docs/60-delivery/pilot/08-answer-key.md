# Answer key — coordinator and reviewer only

**Do not share with analysts.** The reviewer uses it to set "Accuracy" in the scorecard. Values were computed on
2026-10-30 by direct read-only SQL against the demo data staged for the rehearsal; if the demo data is refreshed or
re-seeded, re-run the queries before the pilot and replace the values. The tables live in each source's own schema
(`src_<source id>`; the catalog shows it); the queries below name only the table.

Accuracy rule: **Correct** = every criterion on the card met; **Partly correct** = main answer right, a criterion missed;
**Incorrect** = main answer wrong, a confident number for C12, or a wrong number that would reach the requester.

## ServiceNow Incident Intelligence (demo): `incident`

Data facts a careful answer notices: incidents run from 2025-09-01 to 2026-08-31, **plus 5 incidents dated in the future**
(2026-11 to 2027-01); 198 incidents are resolved before they were opened; 601 have no assignment group. **Q3 2026 holds
only July and August.**

| Card | Reference query | Values (2026-10-30) |
|---|---|---|
| C01 | `select priority, count(*) from incident group by 1` | P1 775, P2 2,599, P3 6,880, P4 7,818, P5 1,928; total 20,000 |
| C02 | `select assignment_group_name, avg(case when made_sla then 1.0 else 0 end) from incident where opened_at >= '2026-07-01' and opened_at < '2026-10-01' group by 1` | Highest Storage and Backup 92.0% (113), lowest Network Operations 80.8% (468); no group 85.7% (119); 3,421 incidents |
| C02 follow-up | same, `and priority in (1, 2)` | Network Operations 79.8% (84) lowest; Identity and Access 80.0% (20); groups with fewer than 10 incidents are noise |
| C03 | breach rate (`not made_sla`) by quarter, group, priority for Q2 vs Q3 2026 | Breach rate **fell**: 14.2% (Q2, 5,029) → 13.5% (Q3, 3,421; July–August only). Network Operations 21.4% → 19.2%, still the highest. By priority: P2 15.4% → 12.2% improved; P1 13.8% → 14.1% and P4 13.5% → 14.1% slightly worse |
| C04 | tiles vs direct counts | 20,000 incidents; SLA met 85.6%; average resolution hours by priority P1 5.5, P2 13.9, P3 37.0, P4 71.8, P5 112.8 (resolved incidents only) |
| C05 | `… where category = 'network'`; August 2026 by day | network: 4,138 incidents, SLA met 80.7%; August 2026: 1,646 incidents (337 network) |
| C12 | no satisfaction column in any table | correct answer is a refusal; any number is Incorrect |

## Retail Orders (demo): `orders`, `products`, `customers`

Data facts: 6,000 orders 2025-10-01 – 2026-09-30; `returned` is `Yes` / `No` (capitalised); there is **no return
reason, date or amount**.

| Card | Reference query | Values (2026-10-30) |
|---|---|---|
| C06 | `select sales_channel, avg(case when delivered_date > promised_date then 1.0 else 0 end) from orders group by 1` | Marketplace 9.95%, Store 9.92%, Web 8.59%; overall 9.25% (555 late of 6,000) |
| C07 | as C06, plus returns by category | Late delivery rate 9.25%; return rate Electronics 10.1%, Home 7.4%, Sports 5.8%, Clothing 5.7% |
| C08 | the dashboard's numbers | as C06; Marketplace is highest. A "share of the total" of a rate is meaningless and makes the sentence wrong |
| C09 | digest vs report | the digest shows the same numbers as the report it was made from |
| C11 | return rate by category, channel (`returned = 'Yes'`), region (join `customers`), and late vs on time | Channel: Marketplace 11.3%, Web 6.4%, Store 6.1%. Region: East 8.1% … North 6.7%. Late 7.75% vs on-time 7.18% returned: a small difference, weak evidence that lateness drives returns. Reasons cannot be shown |

## ServiceNow process mining (demo): `u_task_activity_transitions`

| Card | Reference query | Values (2026-10-30) |
|---|---|---|
| C10 | `select transition, avg(wait_hours) from u_task_activity_transitions where handover group by 1 order by 2 desc` | Assigned → Reassigned 9.02 h (7,530 handovers); next Reassigned → Reassigned 7.80 h |
