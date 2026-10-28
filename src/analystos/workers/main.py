"""Entry of an isolated worker process: `analystos worker --queues compute-py` (P7-06).

Order matters: the environment is checked and scrubbed and the egress guard installed *before*
anything else runs, so nothing later can read a credential or open a connection. Two transports:

* Temporal (default): polls `<prefix>-<pool>` for `run_isolated_task` (`workers/temporal.py`);
* `--stdio`: one JSON line in (`{"dispatch": ...}`), event lines and one result line out. This is the
  local subprocess pool the lite profile and the conformance suite use.

The platform `Settings` is never built here (it would read `.env`); the worker reads only its own
allowlisted variables.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from analystos.contracts.worker import ISOLATED_POOLS, TaskDispatch
from analystos.workers.isolation import (
    DEFAULT_ARTIFACT_URL,
    EX_CONFIG,
    WorkerMisconfigured,
    check_and_scrub_environment,
    check_runtime,
    install_egress_guard,
)


def parse_pools(value: str | None) -> tuple[str, ...]:
    pools = tuple(dict.fromkeys(p.strip() for p in (value or "").split(",") if p.strip()))
    bad = [p for p in pools if p not in ISOLATED_POOLS]
    if not pools or bad:
        raise ValueError(f"an isolated worker serves only {', '.join(ISOLATED_POOLS)} (got {value!r})")
    return pools


def _write(stream: Any, body: dict[str, Any]) -> None:
    stream.write(json.dumps(body, default=str) + "\n")
    stream.flush()


def serve_stdio(config: Any, stdin: Any = None, stdout: Any = None) -> None:
    from analystos.workers.runtime import StoreClient, execute

    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    client = StoreClient(config.artifact_url)
    try:
        for line in stdin:
            if not line.strip():
                continue
            try:
                dispatch = TaskDispatch.model_validate(json.loads(line)["dispatch"])
            except Exception as exc:  # noqa: BLE001 - a bad frame is answered, the loop goes on
                _write(stdout, {"error": {"code": "invalid_input", "message": f"bad dispatch: {str(exc)[:300]}"}})
                continue
            result = execute(dispatch, config, emit=lambda e: _write(stdout, {"event": e.model_dump(mode="json")}),
                             client=client)
            _write(stdout, {"result": result.model_dump(mode="json")})
    finally:
        client.close()


def run_isolated(pools: tuple[str, ...], *, stdio: bool = False, conformance: bool = False) -> int:
    import faulthandler
    import signal

    from analystos.workers.runtime import WorkerConfig

    faulthandler.register(signal.SIGUSR1, file=sys.stderr, all_threads=True)  # `kill -USR1` dumps every thread's stack
    try:
        check_and_scrub_environment()
        check_runtime()
    except WorkerMisconfigured as exc:
        sys.stderr.write(json.dumps({"error": exc.to_dict()}) + "\n")
        return EX_CONFIG
    artifact_url = os.environ.get("ANALYSTOS_WORKER_ARTIFACT_URL") or DEFAULT_ARTIFACT_URL
    temporal = os.environ.get("ANALYSTOS_TEMPORAL_ADDRESS", "localhost:7233")
    install_egress_guard([artifact_url] if stdio else [artifact_url, temporal])
    config = WorkerConfig(pools=pools, artifact_url=artifact_url, conformance=conformance)
    # stdout is the protocol stream in stdio mode: everything else (logs, prints) goes to stderr.
    protocol = sys.stdout
    sys.stdout = sys.stderr
    if stdio:
        serve_stdio(config, stdout=protocol)
        return 0
    from analystos.workers.temporal import run_temporal_worker

    run_temporal_worker(config, address=temporal, namespace=os.environ.get("ANALYSTOS_TEMPORAL_NAMESPACE", "default"),
                        prefix=os.environ.get("ANALYSTOS_TEMPORAL_QUEUE_PREFIX", "analystos"))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="analystos-isolated-worker")
    parser.add_argument("--queues", default=os.environ.get("ANALYSTOS_WORKER_QUEUES"))
    parser.add_argument("--stdio", action="store_true", help="serve dispatches on stdin/stdout (local subprocess pool)")
    parser.add_argument("--conformance", action="store_true", help="also serve the conformance probe")
    args = parser.parse_args(argv)
    return run_isolated(parse_pools(args.queues), stdio=args.stdio, conformance=args.conformance)


if __name__ == "__main__":
    sys.exit(main())
