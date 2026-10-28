"""P4-S05 finding: 50 concurrent runs in one workspace each save a semantic-model version; `max + 1`
collided (`uq_semantic_model_version`) and 29/50 runs failed. Saves are now serialized per workspace."""
from __future__ import annotations

import threading

import pytest
from sqlalchemy import select

pytestmark = pytest.mark.integration


def test_concurrent_saves_get_distinct_versions(control_db):
    from analystos.contracts.semantic import SemanticDataset
    from analystos.db.base import session_scope
    from analystos.db.models import SemanticModel, User
    from analystos.semantic.service import save_model
    from analystos.services.workspaces import create_workspace

    with session_scope() as s:
        admin = s.scalar(select(User).where(User.email == "admin@analystos.local"))
        ws_id = create_workspace(s, admin, name="s05 concurrent semantic", objective="x", autonomy_level=3).id

    n, errors, barrier = 8, [], threading.Barrier(8)

    def save(i: int) -> None:
        try:
            barrier.wait(timeout=10)
            with session_scope() as s:
                save_model(s, ws_id, actor="agent:semantic", origin="agent:semantic",
                           datasets=[SemanticDataset.model_validate({"name": f"ds_{i}", "source": f"src.t{i}"})])
        except Exception as exc:  # recorded, asserted below
            errors.append(exc)

    from analystos.tenancy.background import carry_context  # the savers act for the test's tenant

    threads = [threading.Thread(target=carry_context(save), args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert errors == []
    with session_scope() as s:
        versions = sorted(s.scalars(select(SemanticModel.version).where(SemanticModel.workspace_id == ws_id)))
    assert versions == list(range(1, n + 1))
