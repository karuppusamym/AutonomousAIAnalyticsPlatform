# S07-T8 number check — live evidence, 2026-10-30

**Setup.** Worktree `pilot-fix-meaning-601196` (base 8b6458b plus the uncommitted S07-T8 change), API run from the
worktree on :8017 against the running compose stack (standard profile: Postgres 16 with strict tenancy, Redis,
Temporal, the ServiceNow mock data staged by the 2026-10-30 e2e run). Web dev server on :5183 proxying to that API.
User: the seeded dev analyst. Workspace `ws_c0f6d56a3ce0` ("ServiceNow Incident Intelligence 20261030-2033"), table
`src_src_56ec843c975e.incident`. Reference numbers read with `psql` on the analytics database, not through Ask.

**Result: 5/5 verdicts as expected** ([`number-check-20261030.json`](number-check-20261030.json)).

| Check | Claimed | Verdict | Computed | Answered by | Model cost |
|---|---|---|---|---|---|
| True total | `20,000` | confirmed (ad_hoc) | 20,000 (= psql) | rules | $0 |
| Wrong total | `20037` | contradicted (ad_hoc), "claimed 20037, computed 20000" | 20,000 | rules | $0 |
| One priority row (`label: 1`) | `775` | confirmed (ad_hoc) | 775 (= psql) | rules | $0 |
| Same breakdown, no row named | `775` | unverifiable: "the answer has 5 rows: name the row the number is about with `label`" | — | rules | $0 |
| Out of scope (payroll) | `12` | unverifiable: Ask asked for more detail | — | — | $0.000012 |

Audit: `number_check.completed` rows in the workspace: confirmed 2, contradicted 1, unverifiable 2.

**Browser (same stack).** Ask screen › "Check a number": question "How many incidents are there in total?", claimed
`21,450`, source "Q3 ops slide" → "Contradicted. You entered 21,450; the data gives 20,000.", difference 1,450 (7.2%),
"generated query (not certified)" with the ad hoc note, table, "Data as of 5 hours ago", the SQL with receipt
`qry_075eff71579f`. "Open the answer" opened the Ask thread. No console errors. At 375 px: no horizontal scroll, the
two-column fields collapse to one.

**Not covered here.** A `certified` verdict live (this workspace has no certified incident-count metric; covered by
unit tests); the MCP `number.check` tool over the HTTP transport (the `mcp` extra is not installed on this host;
the handler is the same `check` function); a model-written SQL answer live (the rules rung answered every question).
