# S12-T6: the compute queue's process pool on Windows - 2026-10-29

Why 0/20 runs finished on Temporal in the 2026-10-28 load test (`2026-10-28-s12-slo-temporal.md`), and the fix.
Host: Windows 11, 8 cores, CPython 3.12.10 (Microsoft Store build), temporalio 1.34.0, Temporal 1.25 (auto-setup, a
throwaway container, removed afterwards).

## What broke

The compute queue ran `execute_task` (profile, quality, `test:*`) in a bare
`concurrent.futures.ProcessPoolExecutor(spawn, max_tasks_per_child=50)`. Three faults combined:

1. **A dead child takes the whole compute worker down.** When any pool child dies, the stdlib pool is broken for
   good: it terminates the other children and fails every pending future with `BrokenProcessPool`. temporalio treats
   a `concurrent.futures.BrokenExecutor` from an activity as fatal for the activity worker
   (`temporalio/worker/_activity.py`: "For broken executors, we have to fail the entire worker"), so the compute queue
   lost its worker and every run waited on it (`RUNNING` until the deadline).
2. **Children die on Windows.** Under the Temporal worker, pool children intermittently exit inside
   `call_queue.get()` with `OSError: [WinError 6] The handle is invalid` raised by `self._sem.release()`
   (7 children in 4 of ~16 local runs of the pool integration tests and probes, and 2 in the 2026-10-29 load run;
   never reproduced in a pool without a running Temporal worker, with the same manager proxies and heartbeat sender). The child's
   semaphore handle is valid when the child starts (checked in each child at warm-up), so it is invalidated later;
   the source of that is not pinned down. This is what turned one bad child into fault 1.
3. **`max_tasks_per_child` loses capacity** (CPython 3.12): a child that exits after its quota is often not replaced.
   Reproduced with no platform code: `ProcessPoolExecutor(2, spawn, max_tasks_per_child=2)` runs 4 of 8 tasks, then
   has no processes and never finishes (`procs 0`, the manager thread waits forever). With the repo's quota of 50 the
   pool shrinks after ~50 tasks per child and stalls after ~200.

A fourth, smaller one: a freshly spawned child imports the platform (seconds on a loaded Windows host) before its
first task, inside that task's heartbeat window.

## Fix (`src/analystos/workflows/compute_pool.py`, used by `workflows/worker.py`)

`ComputePool`, an `Executor` over generations of stdlib pools:

* a child's death fails only the attempts it took down, with `ComputeChildDied` (a `RuntimeError`, not a
  `BrokenExecutor`): Temporal retries them under `TASK_RETRY` and the worker keeps serving; the next task gets a
  fresh generation, logged (`compute pool: a child process died ...`) and counted (`replaced_broken`);
* recycling by generation instead of `max_tasks_per_child`: after `max_workers x max_tasks_per_child` tasks new
  tasks go to a fresh pool and the old one drains and exits;
* each generation is warmed when created (its children import `analystos.workflows.activities`), and the worker
  waits for that before it polls (`wait_ready`);
* the same isolation as before: spawn only (fork is refused), one task per child, at most `max_workers` children per
  generation, cancel of a queued task cancels it in the pool, picklability of the queue's activities checked at
  start-up (what temporalio checks for a `ProcessPoolExecutor`).

No in-process fallback: compute work never runs in the worker process.

## Tests

* `tests/unit/test_compute_pool.py` (8, real spawned children, no services): the recycling stall shape
  (2 children, quota 2, 12 tasks) completes over >= 3 generations; a child that `os._exit`s fails its attempt with
  `ComputeChildDied` (not a `BrokenExecutor`) and the next task runs; the stdlib pool stays broken after one dead child
  (documents why); warm-up, `wait_ready`, cancel, spawn-only, shutdown, `build_workers` uses `ComputePool`.
* `tests/integration/test_temporal_queues.py` now runs on Windows (the `win32` skip is gone; teardown uses
  `SIGTERM`/TerminateProcess where there is no `SIGKILL`; the probe writes one file per call, because appends from
  several processes to one file are not atomic on Windows). Against the throwaway Temporal: workload queues +
  continue-as-new, hung-statistic detection within the heartbeat window, crawl pool - **3/3 passed, three runs in a
  row** after the fix. Before: the hung-statistic test failed (`Activity worker failed` after `BrokenProcessPool`) and
  the queue test timed out.
* Live: `2026-10-29-s12-slo-temporal.md`: 20 concurrent investigations on API + 2 workers, **20/20 reached the
  approval gate** (p95 245 s; 0/20 on 2026-10-28). One pool child died with `WinError 6` during the run; its 4
  attempts failed with `ComputeChildDied`, Temporal retried them, the worker kept serving.

Open: what invalidates the child's semaphore handle (inside CPython's Windows queue handles, only under a running
Temporal worker). It now costs a retry of the attempts in flight, not the worker.
