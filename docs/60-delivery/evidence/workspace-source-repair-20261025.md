# Workspace source repair — 2026-10-25

## Cause

The API container had no persistent mount for `/app/var`. Container replacement removed the three file-source upload folders while the control-plane records and staged PostgreSQL tables survived. The source could remain ready for queries against its snapshot while a refresh failed with “File source path does not exist”. This affected retail files, team targets and derived process-mining files. ServiceNow mock connections remained reachable.

## Repair

- Backed up the control-plane and analytics databases, and preserved the existing container runtime files before replacement. Local ignored evidence/backups: `var/source-repair-20261025/`.
- Recovered process-mining and team-target inputs from authorized staged reads through QueryGateway, with bounded pagination and retained timestamp/boolean types. These are recovered snapshots, not a new extraction from an original external upstream.
- Regenerated retail files from the existing deterministic fixture in `scripts/e2e_full_journey.py` (seed 7). Compared every permitted field and row against staged values before using the files. Restricted fields were not read through a bypass; synthetic fields come from that known fixture.
- Restored six files under their existing workspace upload folders. Kept existing workspace/source/table IDs, selections, owner definitions and access policy.
- Added shared named volume `runtime-data:/app/var` to the API, standard/scale workers and scheduler. Seeded it with the preserved runtime files and recovered uploads before container replacement; UID 10001 ownership retained. Credential-free isolated pools do not mount it.
- Ran discovery and synchronization through the normal source API for all six sources. All current source states are ready and their current `last_error` values are clear. Historical failed crawl records remain audit history.

## Validated

| Active workspace | Sources | Selected tables | Result |
|---|---:|---:|---|
| Operations Intelligence Demo | 1 | 4 | Connection, discovery, synchronization and governed counts pass |
| ServiceNow process mining (demo) | 2 | 6 | Both connections and all checks pass |
| ServiceNow Incident Intelligence (demo) | 2 | 3 | Both connections and all checks pass |
| Retail Orders (demo) | 1 | 3 | File connection and all checks pass |

All 16 table counts equal the pre-repair counts. Retail retains 6,000 orders, 400 customers and 60 products. Process derivations retain 14,170 cases and 75,264 transitions. Each source uses its existing separate staging schema; six distinct schemas, each bound to its owning workspace. A cross-workspace read probe from each of the four workspace scopes was rejected by the gateway.

The API container was recreated twice. After the final recreation it was healthy, all six connector tests passed again and all 16 row counts were unchanged. This proves the recovered files survive container replacement in this local Compose deployment.

Automated configuration checks: 11 `test_compose_static.py` cases pass, including durable/shared runtime storage and no runtime volume in isolated pools. Targeted Ruff and whitespace checks pass. Local machine-readable results: `before.json`, `recovery.json`, `sync-results.json`, `after.json`, `isolation.log`, `after-recreate.log` and `compose-tests.log` in the ignored evidence folder. Recovery query IDs and file hashes are recorded there; no credentials are included in the committed evidence.

## Usage and limits

A fresh database or new schemas were unnecessary: existing separate schemas and workspace reader grants were healthy. No workspace was deleted. The ServiceNow sources are the bundled synthetic mock, not certified real-company ServiceNow connections. Restaging can invalidate earlier verification under the existing evidence lifecycle; recheck affected findings before relying on them.

Back up the runtime volume alongside both databases. Removing named volumes is destructive; see [persistent uploads and artifacts](../../30-runbooks/04-lite-and-profiles.md#persistent-uploads-and-artifacts).
