"""Live-model benchmark helpers shared by the P4-V01 / P4-V02 / P4-08 scripts.

A live tier measures whatever the platform settings say, and the default (`balanced`) answers most
purposes from rules, so a live run under it says little about the model. `process_preset` applies a
settings preset (contracts/platform.PRESETS, e.g. `max_quality` = model first) to *this process only*:
the benchmark runs in-process (ANALYSTOS_ORCHESTRATOR=local), so its runs see the preset while the
shared control-plane document, other processes and other sessions do not."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any


def preset_patch(preset: str) -> dict[str, Any]:
    """The settings patch `platform_settings.apply_preset` would write for `preset`."""
    from analystos.contracts.platform import PRESET_ESCALATION, PRESET_PATCHES, PRESETS
    from analystos.services.platform_settings import _deep_merge

    if preset not in PRESETS:
        raise ValueError(f"preset must be one of {sorted(PRESETS)}")
    return _deep_merge(PRESET_PATCHES.get(preset, {}), {"llm": {"purpose_modes": PRESETS[preset], "ladders": {},
                                                                "escalation": PRESET_ESCALATION.get(preset, {})}})


@contextmanager
def process_preset(preset: str | None, enable_models: list[str] | None = None) -> Iterator[None]:
    """Overlay `preset` (and re-enable `enable_models`, which the stored settings may have disabled) on the stored
    settings document for this process; None / [] = the stored settings. The stored document is never written."""
    if not preset and not enable_models:
        yield
        return
    from analystos.contracts.platform import PlatformSettings
    from analystos.services import platform_settings as ps

    patch = preset_patch(preset) if preset else {}
    reenable = set(enable_models or ())
    original = ps.current

    def current() -> tuple[int, PlatformSettings]:
        version, settings = original()
        merged = ps._deep_merge(settings.model_dump(), patch)
        if reenable:
            merged["llm"]["disabled_models"] = [m for m in merged["llm"]["disabled_models"] if m not in reenable]
        return version, PlatformSettings.model_validate(merged)

    ps.current = current
    ps.invalidate()
    try:
        yield
    finally:
        ps.current = original
        ps.invalidate()


def model_usage(*, run_ids: list[str] | None = None, workspace_ids: list[str] | None = None,
                since: datetime | None = None) -> dict[str, Any]:
    """Provider calls, tokens and cost recorded (model_call) for the given runs or workspaces, by purpose
    and model. Only rows that reached a provider (ok/error) count as calls; skips and cache hits are
    counted separately so a rules-answered purpose shows as such, not as a free model call."""
    from sqlalchemy import or_, select

    from analystos.db.base import session_scope
    from analystos.db.models import ModelCall

    conds = []
    if run_ids:
        conds.append(ModelCall.run_id.in_(run_ids))
    if workspace_ids:
        conds.append(ModelCall.workspace_id.in_(workspace_ids))
    if not conds:
        return {"calls": 0, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0, "by_purpose": {}, "skipped": 0,
                "approval_required": 0}
    q = select(ModelCall).where(or_(*conds))
    if since is not None:
        q = q.where(ModelCall.created_at >= since)
    out: dict[str, Any] = {"calls": 0, "errors": 0, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0, "skipped": 0,
                           "cache_hits": 0, "approval_required": 0, "by_purpose": {}}
    with session_scope() as s:
        for c in s.scalars(q):
            if c.status in ("ok", "error"):
                row = out["by_purpose"].setdefault(f"{c.purpose} / {c.model}", {"calls": 0, "errors": 0, "cost_usd": 0.0,
                                                                                "input_tokens": 0, "output_tokens": 0})
                for d in (out, row):
                    d["calls"] += 1
                    d["errors"] += c.status == "error"
                    d["cost_usd"] += float(c.cost_usd or 0.0)
                    d["input_tokens"] += c.input_tokens or 0
                    d["output_tokens"] += c.output_tokens or 0
            elif c.status == "cache_hit":
                out["cache_hits"] += 1
            elif c.status == "approval_required":
                out["approval_required"] += 1
            else:
                out["skipped"] += 1
    out["cost_usd"] = round(out["cost_usd"], 6)
    for row in out["by_purpose"].values():
        row["cost_usd"] = round(row["cost_usd"], 6)
    return out
