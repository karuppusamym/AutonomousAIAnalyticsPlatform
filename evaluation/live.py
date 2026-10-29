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
                "approval_required": 0, "fallbacks": []}
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
    out["fallbacks"] = fallbacks(run_ids=run_ids, workspace_ids=workspace_ids, since=since)
    return out


def fallbacks(*, run_ids: list[str] | None = None, workspace_ids: list[str] | None = None,
              since: datetime | None = None) -> list[dict[str, Any]]:
    """N-14: the purposes these runs answered by rules because no model route was allowed (their `model.fallback`
    events, folded per purpose and cause). A live tier with any is not a measurement of those purposes' model."""
    from sqlalchemy import or_, select

    from analystos.db.base import session_scope
    from analystos.db.models import RunEvent
    from analystos.llm.fallback import summarize

    conds = []
    if run_ids:
        conds.append(RunEvent.run_id.in_(run_ids))
    if workspace_ids:
        conds.append(RunEvent.workspace_id.in_(workspace_ids))
    if not conds:
        return []
    q = select(RunEvent.payload).where(RunEvent.type == "model.fallback", or_(*conds)).order_by(RunEvent.id)
    if since is not None:
        q = q.where(RunEvent.created_at >= since)
    with session_scope() as s:
        return summarize([p for p in s.scalars(q) if isinstance(p, dict)])


def fallback_label(found: list[dict[str, Any]] | None) -> str:
    """The suffix a tier heading carries when the tier fell back ('' when it did not)."""
    if not found:
        return ""
    purposes = sorted({f["purpose"] for f in found})
    return f" — fell back to rules ({len(purposes)} purpose{'s' if len(purposes) != 1 else ''})"


def fallback_lines(found: list[dict[str, Any]] | None) -> list[str]:
    """Report lines that label a tier which fell back: per purpose, the cause and how often ([] when none)."""
    if not found:
        return []
    lines = ["### Fell back to rules (no allowed model route)", "",
             "These purposes wanted a model and were answered by the rules rung instead, so this tier does not measure "
             "their model. The cause is the router's (models disabled by the stored settings, a spend cap or budget, a "
             "missing price, policy, no key); re-run with `--enable-models` / `--preset` (this process only) to measure "
             "the model.", "", "| Purpose | cause | times | rung | detail |", "|---|---|---|---|---|"]
    lines += [f"| {f['purpose']} | {f['cause']} | {f['count']} | {f['rung']} | {str(f['detail']).replace('|', '/')[:200]} |"
              for f in found]
    return lines + [""]
