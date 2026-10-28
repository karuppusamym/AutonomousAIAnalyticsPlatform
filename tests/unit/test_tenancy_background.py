"""S01-T4: background work runs as the tenant that owns it; Temporal carries the tenant in a header."""
from __future__ import annotations

import pickle
import threading

import pytest

from analystos.tenancy import background as bg
from analystos.tenancy import current_tenant, is_system, system_scope, tenant_scope


@pytest.fixture
def owners(monkeypatch):
    rows = {("analysis_run", "run_a"): "tnt_a", ("analysis_run", "run_b"): "tnt_b", ("run_event", "evt_p"): None}
    monkeypatch.setattr(bg, "_enabled", lambda: True)
    monkeypatch.setattr(bg, "owner_tenant", lambda t, i: rows.get((t, i), bg._MISSING))
    return rows


def test_platform_loop_narrows_to_the_owner(owners):
    with system_scope(), bg.as_owner_of("analysis_run", "run_a") as owner:
        assert owner == "tnt_a" and current_tenant() == "tnt_a" and not is_system()


def test_unbound_work_binds_the_owner(owners):
    with bg.as_owner_of("analysis_run", "run_b"):
        assert current_tenant() == "tnt_b"
    assert current_tenant() is None


def test_another_tenants_row_is_refused(owners):
    with tenant_scope("tnt_a"), pytest.raises(bg.TenantMismatch), bg.as_owner_of("analysis_run", "run_b"):
        pass


def test_platform_row_runs_in_system_scope_only_when_no_tenant_is_bound(owners):
    with bg.as_owner_of("run_event", "evt_p"):
        assert is_system()
    with tenant_scope("tnt_a"), pytest.raises(bg.TenantMismatch), bg.as_owner_of("run_event", "evt_p"):
        pass


def test_missing_row_changes_nothing(owners):
    with tenant_scope("tnt_a"), bg.as_owner_of("analysis_run", "nope"):
        assert current_tenant() == "tnt_a"


def test_owned_by_reads_the_named_argument(owners):
    @bg.owned_by("analysis_run", "run_id")
    def step(run_id: str, key: str = "x") -> str | None:
        return current_tenant()

    assert step("run_a") == "tnt_a"
    assert step(key="y", run_id="run_b") == "tnt_b"


def test_platform_loop_decorator():
    @bg.platform_loop
    def loop() -> bool:
        return is_system()

    with tenant_scope("tnt_a"):
        assert loop() is True
        assert current_tenant() == "tnt_a"


def test_carry_context_reaches_a_new_thread():
    seen: list[str | None] = []
    with tenant_scope("tnt_a"):
        fn = bg.carry_context(lambda: seen.append(current_tenant()))
    t = threading.Thread(target=fn)
    t.start()
    t.join()
    assert seen == ["tnt_a"]


def test_off_mode_skips_the_lookup(monkeypatch):
    monkeypatch.setattr(bg, "_enabled", lambda: False)
    monkeypatch.setattr(bg, "owner_tenant", lambda t, i: pytest.fail("no lookup when tenancy is off"))
    with bg.as_owner_of("analysis_run", "run_a"):
        assert current_tenant() is None


def test_owner_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(bg, "_MAX_ENTRIES", 3)
    bg.forget()
    for i in range(10):
        with bg._lock:
            bg._cache[("t", str(i))] = "x"
            if len(bg._cache) > bg._MAX_ENTRIES:
                bg._cache.popitem(last=False)
    assert len(bg._cache) == 3
    bg.forget()


# ------------------------------------------------------------------ Temporal header
def test_header_round_trip():
    from analystos.workflows.tenant_propagation import HEADER, read_tenant, with_tenant

    headers = with_tenant({}, "tnt_a")
    assert set(headers) == {HEADER} and read_tenant(headers) == "tnt_a"
    assert with_tenant({}, None) == {} and read_tenant({}) is None and read_tenant(None) is None


def _probe(x: int) -> tuple[int, str | None]:
    return x, current_tenant()


def test_tenant_bound_activity_is_picklable_and_scoped():
    from analystos.workflows.tenant_propagation import TenantBound

    fn = pickle.loads(pickle.dumps(TenantBound(_probe, "tnt_a")))  # crosses into compute pool processes
    assert fn(7) == (7, "tnt_a")
    assert current_tenant() is None


def test_client_interceptor_stamps_the_bound_tenant():
    import asyncio

    from analystos.workflows.tenant_propagation import _ClientOutbound, read_tenant

    class Next:
        async def start_workflow(self, input):  # noqa: A002
            return input.headers

    class Input:
        headers: dict = {}

    async def go():
        with tenant_scope("tnt_a"):
            return await _ClientOutbound(Next()).start_workflow(Input())

    assert read_tenant(asyncio.run(go())) == "tnt_a"


def test_orchestrator_submit_carries_the_callers_tenant():
    from analystos.workflows.orchestrator import _submit

    async def where() -> tuple[str | None, bool]:
        return current_tenant(), is_system()

    with tenant_scope("tnt_a"):
        assert _submit(where()).result(timeout=5) == ("tnt_a", False)
    with system_scope():
        assert _submit(where()).result(timeout=5) == (None, True)


def test_cli_maintenance_commands_run_in_system_scope(monkeypatch):
    from analystos import cli

    seen: list[tuple[str, bool]] = []
    monkeypatch.setattr(cli, "_main", lambda argv: seen.append((argv[0], is_system())) or 0)
    cli.main(["calibrate"])
    cli.main(["worker"])
    assert seen == [("calibrate", True), ("worker", False)]


def test_strict_is_the_default():
    from analystos.core.config import Settings

    assert Settings.model_fields["tenancy_mode"].default == "strict"


def test_owned_by_refuses_a_function_without_its_parameter():
    """A decorator left on the wrong function (a helper inserted above the one it guarded) fails at import."""
    import pytest

    from analystos.tenancy.background import owned_by

    with pytest.raises(TypeError, match="no parameter 'run_id'"):
        @owned_by("analysis_run", "run_id")
        def helper(session, run, outcome):  # noqa: ARG001
            return None


def test_engine_entry_points_run_as_the_run_owner():
    """The local orchestrator calls these from a driver thread with no tenant bound; each binds its own."""
    from analystos.runtime import engine

    for fn in (engine.plan_run, engine.get_state, engine.execute_task, engine.finish_run):
        assert getattr(fn, "__owned_by__", None) == ("analysis_run", "run_id"), fn.__name__
