"""Air-gapped posture check (spec S05 §8, FR-056, task S05-T7): `GET /api/admin/models/airgap`.

The egress guard itself is code (ANALYSTOS_AIR_GAPPED: only `egress: internal` providers are routed and the
model transport refuses every other host before connecting). This check tells an operator whether the rest of
the installation matches: a local OpenAI-compatible provider with a URL and declared capabilities, the
`air_gapped` preset applied, JEV off, decision purposes on rules, every chat purpose either served by a local
model or covered by a rules rung, and no public MCP hosts allowed. It reads configuration only; it never
calls a model (use `POST /api/admin/models/providers/{id}/test` for a live ping).
"""
from __future__ import annotations

from typing import Any

from analystos.contracts.platform import PRESETS


def _check(name: str, ok: bool, detail: str, *, severity: str = "error") -> dict[str, Any]:
    return {"name": name, "ok": ok, "detail": detail, "severity": "ok" if ok else severity}


def posture(*, config: Any = None, settings: Any = None, air_gapped: bool | None = None, router: Any = None) -> dict[str, Any]:
    from analystos.core.config import get_settings
    from analystos.llm.config import load_models_config
    from analystos.llm.router import ModelRouter
    from analystos.services.platform_settings import get as platform

    cfg = config or load_models_config()
    plat = settings or platform()
    flag = get_settings().air_gapped if air_gapped is None else air_gapped
    checks = [_check("deployment flag", flag, "ANALYSTOS_AIR_GAPPED is set: only egress: internal providers are routed"
                     if flag else "ANALYSTOS_AIR_GAPPED is not set: public providers stay reachable")]

    internal = {n: p for n, p in cfg.providers.items() if p.egress == "internal" and p.kind != "decision"}
    public = sorted(n for n, p in cfg.providers.items() if p.egress != "internal")
    checks.append(_check("local provider", bool(internal),
                         f"internal chat providers: {', '.join(sorted(internal))}" if internal else
                         "no provider declared egress: internal (use config/models.airgapped.yaml)"))
    for name, p in sorted(internal.items()):
        checks.append(_check(f"{name}: endpoint", bool(p.url()), f"{p.type} at {p.url()}" if p.url() else
                             f"no base URL (set {p.base_url_env or 'base_url'})"))
        caps = p.capabilities
        declared = caps.json_mode is not None and caps.context_window is not None
        checks.append(_check(f"{name}: capabilities", declared,
                             f"json_mode={caps.json_mode}, tools={caps.tools}, context_window={caps.context_window}"
                             if declared else "declare capabilities.json_mode and context_window (a local server "
                                              "cannot be asked)", severity="warning"))
    checks.append(_check("public providers", not public,
                         "none configured" if not public else f"configured but refused under the flag: {', '.join(public)}",
                         severity="warning"))
    checks.append(_check("JEV decisions", not plat.features.jev_decisions,
                         "features.jev_decisions is off" if not plat.features.jev_decisions else
                         "features.jev_decisions is on: apply the air_gapped preset"))

    expected = PRESETS["air_gapped"]
    drift = sorted(p for p, mode in expected.items() if plat.llm.purpose_modes.get(p) != mode)
    checks.append(_check("air_gapped preset", not drift,
                         "applied" if not drift else f"{len(drift)} purpose mode(s) differ from the preset: "
                                                     f"{', '.join(drift[:8])}{' …' if len(drift) > 8 else ''}"))
    hosts = list(plat.outbound.mcp_host_allowlist)
    checks.append(_check("MCP hosts", not hosts, "no outbound MCP hosts allowed" if not hosts else
                         f"outbound MCP hosts allowed: {', '.join(hosts[:5])}", severity="warning"))

    r = router or ModelRouter(cfg, settings_provider=lambda: plat, air_gapped=True, layers_provider=lambda _ws: None)
    purposes: dict[str, Any] = {}
    uncovered = []
    for purpose in cfg.routing:
        ladder = r.ladder(purpose)
        why = r.unavailable(purpose)
        entry = {"mode": r.mode(purpose), "rules_fallback": "rules" in ladder,
                 "served_locally": why is None, "reason": why.message[:200] if why is not None else None}
        if why is not None and why.code not in ("llm_disabled", "no_api_key") and not entry["rules_fallback"]:
            uncovered.append(purpose)
        purposes[purpose] = entry
    local = sorted(p for p, e in purposes.items() if e["served_locally"])
    checks.append(_check("purposes covered", not uncovered,
                         f"{len(local)} purpose(s) answered by a local model; the rest by rules" if not uncovered else
                         f"no local model and no rules rung: {', '.join(uncovered)}"))
    ok = all(c["ok"] for c in checks if c["severity"] == "error")
    return {"air_gapped": flag, "ok": ok, "checks": checks, "purposes": purposes}


__all__ = ["posture"]
