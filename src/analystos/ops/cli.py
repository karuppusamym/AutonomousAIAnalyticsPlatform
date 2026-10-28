"""`analystos ops ...` (spec S12):

  rls-check                         every tenant-owned table has RLS, its policy and fill trigger; the runtime role
                                    cannot bypass RLS. Exit 6 otherwise (the nightly CronJob; a failure pages).
  spend-export --month YYYY-MM      the month's cost ledger to --out DIR as parquet (default) or csv; every tenant,
               [--tenant T] [--format csv]   or one tenant under its row-level security.
  spend [--group-by day,tenant] [--days 30] [--tenant T]   the spend rollup as JSON.
  model-matrix [--mode fake|live] [--record] [--limit 6]   the per-model evaluation matrix (evaluation/model_matrix.py;
                                    a source checkout only). Exit 1 when its gate fails.
  model-matrix-import FILE          store the matrix rows of a gate result (scripts/eval_gates.py --out, the nightly
                                    artifact) or a model-matrix run in model_matrix_result (works from the image).
"""
from __future__ import annotations

import argparse
import json
import sys
from contextlib import nullcontext
from pathlib import Path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="analystos ops", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["rls-check", "spend-export", "spend", "model-matrix", "model-matrix-import"])
    parser.add_argument("file", nargs="?", help="model-matrix-import: the JSON file")
    parser.add_argument("--month")
    parser.add_argument("--out", default=".")
    parser.add_argument("--format", choices=["parquet", "csv"], default="parquet")
    parser.add_argument("--tenant", help="bind to one tenant (row-level security) instead of every tenant")
    parser.add_argument("--group-by", default="day,tenant")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--mode", choices=["fake", "live"], default="fake")
    parser.add_argument("--record", action="store_true")
    parser.add_argument("--limit", type=int, default=6)
    args = parser.parse_args(argv)

    if args.command == "rls-check":
        from analystos.ops.checks import rls_check

        result = rls_check()
        print(json.dumps(result, indent=2, default=str))
        return 0 if result["ok"] else 6
    if args.command == "model-matrix":
        try:
            from evaluation import gates as G
            from evaluation.model_matrix import run
        except ImportError:
            print("model-matrix needs a source checkout (the evaluation package)", file=sys.stderr)
            return 2
        result = run(args.mode, limit=args.limit, record=args.record)
        tier = "model_matrix" if args.mode == "live" else "model_matrix_fake"
        failures = G.check(tier, result["metrics"])
        print(json.dumps({**result, "gate": {"tier": tier, "failures": failures}}, indent=1, default=str))
        return 1 if failures else 0

    if args.command == "model-matrix-import":
        if not args.file:
            parser.error("model-matrix-import needs a FILE")
        return _import(Path(args.file))

    from analystos.db.base import session_scope
    from analystos.ops import cost_ledger
    from analystos.tenancy import tenant_scope

    scope = tenant_scope(args.tenant) if args.tenant else nullcontext()  # the CLI itself runs in system scope
    with scope, session_scope() as s:
        if args.command == "spend":
            result = cost_ledger.spend(s, group_by=args.group_by, days=args.days)
        else:
            if not args.month:
                parser.error("spend-export needs --month YYYY-MM")
            result = cost_ledger.export_file(s, args.month, Path(args.out), args.format)
    print(json.dumps(result, indent=2, default=str))
    return 0


def _import(path: Path) -> int:
    """Rows from a gate result ({"config", "tiers": {"model_matrix": {"metrics": {"rows"}}}}) or a run ({"rows"})."""
    from analystos.db.base import session_scope
    from analystos.ops.model_matrix import record

    doc = json.loads(path.read_text(encoding="utf-8"))
    batches = []
    if "tiers" in doc:
        for name, tier in doc["tiers"].items():
            rows = (tier.get("metrics") or {}).get("rows")
            if rows:
                batches.append((tier["metrics"].get("mode") or ("live" if name == "model_matrix" else "fake"), rows))
        version, sha = int(doc["config"]["version"]), doc["config"].get("sha256")
    else:
        batches.append((doc.get("mode", "live"), doc.get("rows") or []))
        version, sha = int(doc.get("gates_version", 0)), doc.get("gates_sha256")
    stored = []
    with session_scope() as s:
        for mode, rows in batches:
            stored.append({"mode": mode, "rows": len(rows), "batch_id": record(s, rows, suite="ask", mode=mode,
                                                                                gates_version=version, gates_sha256=sha)})
    print(json.dumps({"imported": stored}, indent=2))
    return 0 if stored else 3
