# Runbook — disaster recovery: point-in-time recovery and Temporal restore (spec S12 §1, §5; S12-T7)

Targets: **RPO ≤ 15 min** for the control plane (managed Postgres 16 with PITR), RTO as measured by the drill.
Logical backups and the record-level checks stay in [06-backup-and-recovery.md](06-backup-and-recovery.md); this
runbook adds continuous WAL archiving (PITR) and Temporal. The drill `scripts/dr_drill.py` rehearses both in
throwaway containers and writes `docs/60-delivery/evidence/<date>-s12-dr-drill.md`.

## What is recovered how

| Store | Mechanism | RPO | Notes |
|---|---|---|---|
| Control-plane Postgres | base backup + WAL archive, `recovery_target_time` | `archive_timeout` (drill: 60 s; managed services: ~5 min) | managed: the provider's PITR (RDS / Cloud SQL / Azure Flexible "restore to a point in time") |
| Analytics Postgres | same as control plane, or re-stage from sources | as above | managed outputs (recipes, pipelines) cannot be re-staged: back them up |
| Temporal (own Postgres) | its database's backup / PITR, or Temporal Cloud | as its database | restore Temporal to the same point as the control plane or later, never earlier |
| Artifacts, uploads | object store versioning + lifecycle | object versioning | |
| Redis | not backed up | - | counters reseed from Postgres; nudges are hints |

## Point-in-time recovery (self-managed Postgres)

Primary settings: `wal_level=replica`, `archive_mode=on`, `archive_command` to durable storage (e.g. `wal-g wal-push`,
pgBackRest, or `cp` to a mounted bucket), `archive_timeout=300` (the RPO bound when writes are quiet). A nightly
`pg_basebackup -Ft -z -X stream` (or the tool's full backup) to the same storage; keep base backups for the
retention window plus one.

Recover to just before an incident (an operator `DELETE`, a bad migration, a compromised account):

1. Stop the writers: scale API, workers and scheduler to 0 (`kubectl scale deploy -l app.kubernetes.io/instance=<rel> --replicas=0`).
   Suspend the CronJobs.
2. Find the target time from the audit trail or the database log: the last good moment, in UTC.
3. Provision a new instance from the latest base backup before the target; put `recovery.signal` in the data
   directory and set `restore_command` (fetching archived WAL), `recovery_target_time = '<UTC timestamp>+00'`,
   `recovery_target_action = 'promote'`. Start it; recovery ends when `SELECT pg_is_in_recovery()` is `f`.
   Managed Postgres: "restore to a point in time" creates the instance; skip to 4.
4. Check before switching: `SELECT version_num FROM alembic_version` (the release head, else `analystos migrate`),
   `analystos ops rls-check` (exit 0), `analystos audit verify` (exit 0), and the record checks of
   `scripts/recovery_drill.py` against the recovered database.
5. Point the Secret's `ANALYSTOS_DATABASE_URL` (and the pooler) at the recovered instance; restore Temporal (below)
   to the same point or later; scale the platform back up; resume the CronJobs.
6. Afterwards: everything written after the target time is gone (the incident and anything legitimate after it). The
   audit export in WORM storage (`ops.auditVerify.exportDir`) still has the audit events of that interval: review
   them and re-apply legitimate changes by hand.

## Temporal restore

Temporal keeps in-flight workflow state; the platform's run state is in the control plane, and an activity that
re-runs is idempotent by task key. Restore order: control plane first, then Temporal at the same point or later.

1. Stop the platform workers and the Temporal services (frontend, history, matching, worker).
2. Restore the `temporal` and `temporal_visibility` databases (PITR as above, or `pg_restore` of a dump).
3. Start Temporal on them (`SKIP_DB_CREATE=true` for auto-setup images; Helm: the schema job is idempotent).
4. Start the platform workers. Workflows that were running continue from their last recorded event; a run whose
   Temporal state was lost is re-driven by `analystos` resume (the local orchestrator) or fails visibly, never
   silently (AT-09: no lost accepted goal).
5. Check: `temporal workflow list --query 'ExecutionStatus="Running"'` matches the runs the control plane has as
   running; no run stays RUNNING without a workflow for longer than its heartbeat window.

Temporal Cloud: the provider operates the history store; the drill there is a namespace failover test, booked with
the provider.

## The drill

```bash
python scripts/dr_drill.py --part all --report auto     # docker; uses pgvector/pgvector:pg16 and temporalio/auto-setup:1.25
```

PITR part: a primary with WAL archiving gets the platform schema (`analystos migrate` + `seed`), a base backup,
phase-A data (a workspace with audit events, sealed into the chain), the target time, then the incident (every
workspace deleted, cascading, plus a row that must not survive). The primary is destroyed; a new container restores
the base backup and replays WAL to the target. Checks: phase A present, deleted rows back (row counts equal at the
target), the incident row absent, Alembic head unchanged, `ops rls-check` and `audit verify` pass. Temporal part:
probe workflows are taken mid-flight (waiting for a signal) by a worker that then stops; both Temporal databases are
dumped, server and database destroyed, restored into a new database and server. Checks: every workflow RUNNING with
its history unchanged; a new worker gets the signal and every workflow completes exactly once.

Run it quarterly and after any change to backup tooling; attach the evidence file to the release.
