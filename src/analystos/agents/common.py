from __future__ import annotations

import dataclasses
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from analystos.context import cache as context_cache
from analystos.core.errors import AnalystOSError, ContextOverBudget
from analystos.core.logging import get_logger
from analystos.db.base import session_scope
from analystos.db.models import RunTask, SourceAsset, SourceColumn
from analystos.runtime.context import RunContext
from analystos.skills.catalog import screen_for_prompt

log = get_logger(__name__)


def task_output(run_id: str, key: str) -> dict[str, Any]:
    with session_scope() as s:
        task = s.scalar(select(RunTask).where(RunTask.run_id == run_id, RunTask.key == key))
        return dict(task.output or {}) if task else {}


def _memo(ctx: Any, name: str) -> dict:
    """A per-step (RunContext) or per-request (Ask context) memo: the context object is rebuilt for
    every step and request, so nothing memoised here outlives an edit by more than one step."""
    holder = getattr(ctx, "__dict__", None)
    return holder.setdefault(name, {}) if isinstance(holder, dict) else {}


def asset_rows(ctx: RunContext) -> list[tuple[SourceAsset, list[SourceColumn]]]:
    """The scope's tables with their columns, in scope order: two queries for the whole scope (not two
    per table), once per step or request."""
    wanted = [tuple(fq.split(".", 1)) for fq in ctx.scope.assets]
    memo = _memo(ctx, "_aos_asset_rows")
    key = (ctx.workspace.id, tuple(wanted))
    if key in memo:
        return list(memo[key])
    by_name: dict[tuple[str, str], SourceAsset] = {}
    cols: dict[str, list[SourceColumn]] = {}
    with session_scope() as s:
        names = sorted({n for _, n in wanted})
        if names:
            for a in s.scalars(select(SourceAsset).where(SourceAsset.workspace_id == ctx.workspace.id, SourceAsset.name.in_(names))
                               .order_by(SourceAsset.id)):
                by_name.setdefault((a.schema_name, a.name), a)
        ids = [by_name[w].id for w in wanted if w in by_name]
        if ids:
            for c in s.scalars(select(SourceColumn).where(SourceColumn.asset_id.in_(ids))
                               .order_by(SourceColumn.asset_id, SourceColumn.ordinal)):
                cols.setdefault(c.asset_id, []).append(c)
        s.expunge_all()
    out = [(by_name[w], cols.get(by_name[w].id, [])) for w in wanted if w in by_name]
    memo[key] = out
    return list(out)


_SEMANTIC_PRIORITY = {"boolean": 0, "datetime": 1, "categorical": 2, "numeric": 3, "text": 5, "id": 6}


def _relevance(name: str, meaning: str, objective_tokens: set[str]) -> int:
    tokens = set(name.lower().replace("_", " ").split()) | set((meaning or "").lower().split())
    return len(tokens & objective_tokens)


def objective_tokens(text: str | None) -> set[str]:
    return {w.strip(",.?").lower() for w in (text or "").split() if len(w) > 3}


def column_rank(entry: dict[str, Any] | str, tokens: set[str]) -> tuple[int, int]:
    """Sort key, most useful first: relevance to the objective, then analytical usefulness."""
    if not isinstance(entry, dict):  # names-only catalog digest (context compiler)
        entry = {"name": str(entry)}
    return -_relevance(entry.get("name", ""), entry.get("meaning", ""), tokens), _SEMANTIC_PRIORITY.get(entry.get("semantic_type") or "", 4)


def table_relevance(table: dict[str, Any], tokens: set[str]) -> int:
    return sum(_relevance(c.get("name", ""), c.get("meaning", ""), tokens) if isinstance(c, dict) else _relevance(str(c), "", tokens)
               for c in table.get("columns") or [])


def catalog_for_prompt(ctx: RunContext, *, include_values: bool = True, objective: str | None = None,
                       capped: bool = True) -> list[dict[str, Any]]:
    """Schema-level catalog for prompts. Denied columns are removed. Category vocabularies (top
    values) are only included when the workspace policy allows data samples to reach models
    (`send_data_samples_to_models`, default false) and then only for low-cardinality, non-PII columns.

    Token economy (admin: llm.compact_prompts): tables and columns are ranked by relevance to the
    objective and analytical usefulness, then capped; identifiers and free text go last. The context
    compiler asks for the uncapped catalog (`capped=False`) and applies its purpose profile's caps
    itself, so what it leaves out is listed as omitted."""
    import copy

    from analystos.services.platform_settings import get as platform

    llm = platform().llm
    tokens = objective_tokens(objective if objective is not None else (ctx.run.objective if ctx.run else None))
    samples_allowed = bool(getattr(getattr(ctx, "policy", None), "send_data_samples_to_models", False))
    denied = set(ctx.scope.denied_columns)
    memo = _memo(ctx, "_aos_catalog")
    key = (include_values, capped, samples_allowed, tuple(sorted(denied)),
           (tuple(sorted(tokens)), llm.compact_prompts, llm.catalog_max_columns_per_table, llm.catalog_max_tables) if capped else None)
    if key in memo:
        return copy.deepcopy(memo[key])
    catalog = []
    rows = asset_rows(ctx)
    joins = _validated_joins(rows, denied)
    for asset, cols in rows:
        fq = f"{asset.schema_name}.{asset.name}"
        entries = []
        for c in cols:
            if f"{fq}.{c.name}" in denied or f"*.{c.name}" in denied:
                continue
            p = c.profile or {}
            sem = getattr(c, "semantics", None) or {}
            entry: dict[str, Any] = {"name": c.name, "type": c.data_type, "semantic_type": c.semantic_type or p.get("semantic_type")}
            if sem.get("semantic_role"):
                entry["role"] = sem["semantic_role"]
            if sem.get("semantic_role") == "flag" and sem.get("flag_true") and not sensitive(c):
                entry["true_value"] = str(sem["flag_true"])[:20]  # a Yes/No column's encoding, like a boolean type
            # Owner- and user-written text is as untrusted as crawled text here: screened at build (P7-20).
            # A model's unreviewed draft never reaches a prompt (knowledge/suggestions.py).
            draft = not sem.get("reviewed")
            meaning = _meaning(None if draft and getattr(c, "business_name_origin", None) == "model" else c.business_name,
                               None if draft and getattr(c, "description_origin", None) == "model" else c.description)
            if meaning:
                entry["meaning"] = meaning
            if p.get("distinct") is not None:
                entry["distinct"] = p.get("distinct")
            if p.get("null_rate") is not None:
                entry["null_rate"] = round(float(p["null_rate"]), 3)
            if p.get("has_blanks"):
                entry["has_blanks"] = True
            for k in ("min", "max", "mean", "p50"):
                if p.get(k) is not None and entry.get("semantic_type") in ("numeric", "datetime"):
                    entry[k] = p[k]
            if p.get("patterns"):
                entry["patterns"] = list(p["patterns"])[:3]
            values = complete_values(p) if include_values and samples_allowed and not sensitive(c) else None
            if values:
                entry["values"] = values
            entries.append(entry)
        if capped and llm.compact_prompts and len(entries) > llm.catalog_max_columns_per_table:
            entries.sort(key=lambda e: column_rank(e, tokens))
            entries = entries[:llm.catalog_max_columns_per_table]
        key_cols = [c.name for c in cols if getattr(c, "is_key", False) and f"{fq}.{c.name}" not in denied]
        catalog.append({"asset": fq, **table_facts(asset), "row_count": asset.row_count,
                        **({"key": key_cols} if key_cols else {}), **({"joins": joins[aid]} if (aid := getattr(asset, "id", None)) in joins else {}),
                        "columns": entries})
    if capped and llm.compact_prompts and len(catalog) > llm.catalog_max_tables:
        catalog.sort(key=lambda t: -table_relevance(t, tokens))
        catalog = catalog[:llm.catalog_max_tables]
    memo[key] = catalog
    return copy.deepcopy(catalog)


SENSITIVE_TAGS = frozenset({"pii", "sensitive", "restricted", "confidential", "secret"})
MAX_PROMPT_VALUES = 12


def sensitive(column: Any) -> bool:
    """A column whose values never reach a prompt: a sensitive tag, or a PII classification."""
    tags = {str(t).lower() for t in (getattr(column, "tags", None) or [])}
    return bool(tags & SENSITIVE_TAGS) or bool((getattr(column, "semantics", None) or {}).get("pii"))


def complete_values(profile: dict[str, Any]) -> list[Any] | None:
    """The column's full value set when the profile knows it is complete and short (≤ 12): a partial
    top-N list would let a model believe the values it did not see do not exist."""
    values = profile.get("values")
    if isinstance(values, list) and profile.get("values_complete") and 0 < len(values) <= MAX_PROMPT_VALUES:
        return list(values)
    top = [t.get("value") for t in profile.get("top_values") or [] if isinstance(t, dict)]
    distinct = profile.get("distinct")
    if top and isinstance(distinct, int) and distinct <= MAX_PROMPT_VALUES and len(top) >= distinct:
        return top[:MAX_PROMPT_VALUES]
    return None


def _validated_joins(rows: list[tuple[Any, list[Any]]], denied: set[str]) -> dict[str, list[dict[str, str]]]:
    """Per asset id, its validated many-to-one joins to the other tables in `rows` ({column, references}): how a
    model should join them. Rejected or unvalidated measurements and denied columns are left out."""
    from analystos.db.models import Relationship

    fq = {a.id: f"{a.schema_name}.{a.name}" for a, _ in rows if getattr(a, "id", None)}
    workspace_id = next((getattr(a, "workspace_id", None) for a, _ in rows), None)
    if len(fq) < 2 or not workspace_id:
        return {}
    out: dict[str, list[dict[str, str]]] = {}
    with session_scope() as s:
        for r in s.scalars(select(Relationship).where(
                Relationship.workspace_id == workspace_id, Relationship.validated.is_(True),
                Relationship.from_asset_id.in_(list(fq)), Relationship.to_asset_id.in_(list(fq))).order_by(Relationship.id)):
            src, dst = fq[r.from_asset_id], fq[r.to_asset_id]
            if (r.evidence or {}).get("rejected") or f"{src}.{r.from_column}" in denied or f"{dst}.{r.to_column}" in denied:
                continue
            out.setdefault(r.from_asset_id, []).append({"column": r.from_column, "references": f"{dst}.{r.to_column}",
                                                        "cardinality": r.cardinality})
    return out


def table_facts(asset: Any) -> dict[str, Any]:
    """Business name, description, role and grain of a table for prompts. An unreviewed model draft is
    replaced by the crawler's rule text (drafts never reach a prompt)."""
    sem = getattr(asset, "semantics", None) or {}
    draft = not getattr(asset, "reviewed", False)
    name = sem.get("business_name") if draft and getattr(asset, "business_name_origin", None) == "model" \
        else getattr(asset, "business_name", None)
    desc = sem.get("description") if draft and getattr(asset, "description_origin", None) == "model" \
        else getattr(asset, "description", None)
    out = {"business_name": screen_for_prompt(name, max_chars=200) or None,
           "description": screen_for_prompt(desc, max_chars=300) or None}
    for k in ("role", "grain"):
        if sem.get(k):
            out[k] = str(sem[k])
    return out


def _meaning(business_name: str | None, description: str | None) -> str:
    name = screen_for_prompt(business_name, max_chars=200)
    desc = screen_for_prompt(description)
    return " - ".join(x for x in (name, desc) if x)


def model_gate(ctx: RunContext, purpose: str, payload: Any, *, deterministic_ok: bool) -> bool:
    """Should this step call a model? `off` never; `auto` only when the deterministic result is not
    good enough; `always` whenever available. Avoided calls are recorded as tokens saved."""
    from analystos.llm.cache import estimate_tokens

    mode = ctx.router.mode(purpose)
    if mode == "off" or (mode == "auto" and deterministic_ok):
        ctx.router.record_skip(purpose, ctx.call_ctx(), estimated_tokens=estimate_tokens(_prompt_text(payload)) + 500,
                               reason=f"mode={mode}: deterministic result used")
        return False
    return True


def compact_json(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"), ensure_ascii=False)


def _prompt_text(payload: Any) -> str:
    from analystos.context.compiler import CompiledContext

    if isinstance(payload, DeferredContext):
        return payload.estimate_text()
    if isinstance(payload, CompiledContext):
        return compact_json(payload.header) + compact_json(payload.body)
    return compact_json(payload)


_SCOPE_CATALOG: Any = object()


@dataclass
class DeferredContext:
    """A context compiled only when a model call will actually happen (Stream B): `model_gate` and the
    refusal paths account for an avoided call with `estimate_text()` (the mandatory inputs plus the
    scope's column names, no DB or retrieval); `llm_json` compiles it after the mode, agent-budget and
    route checks passed."""

    ctx: Any
    purpose: str
    required: dict[str, Any]
    kwargs: dict[str, Any] = field(default_factory=dict)

    def estimate_text(self) -> str:
        scope = getattr(self.ctx, "scope", None)
        denied = set(getattr(scope, "denied_columns", None) or ())
        names = {a: [c for c in cols if f"{a}.{c}" not in denied]
                 for a, cols in (getattr(scope, "columns", None) or {}).items()}
        return compact_json({**self.required, "catalog": names})

    def resolve(self) -> Any:
        return compile_for(self.ctx, self.purpose, self.required, **self.kwargs)


def defer_compile(ctx: Any, purpose: str, required: dict[str, Any], **kwargs: Any) -> DeferredContext:
    """`compile_for`, deferred until `llm_json` knows the call will be sent (compile after the gate)."""
    return DeferredContext(ctx=ctx, purpose=purpose, required=required, kwargs=kwargs)


def knowledge_version_for(ctx: Any) -> str | None:
    """The workspace knowledge version (P4-T06), once per step or request: RunContext memoises its own
    (scope packs); any other context memoises the policy-pack version on itself."""
    if isinstance(getattr(type(ctx), "knowledge_version", None), property):
        return ctx.knowledge_version
    memo = _memo(ctx, "_aos_kv")
    if "v" not in memo:
        from analystos.context import version

        workspace_id = getattr(getattr(ctx, "workspace", None), "id", None)
        memo["v"] = version.workspace_knowledge_version(workspace_id, policy=getattr(ctx, "policy", None)) \
            if workspace_id else None
    return memo["v"]


def context_header(ctx: Any) -> dict[str, Any]:
    """Workspace header (P4-T04): stable per workspace and scope, so it sits in the cached prompt
    prefix right after the static system text. Nothing objective- or run-specific goes here."""
    from datetime import date

    header: dict[str, Any] = {"knowledge_effective_date": date.today().isoformat()}
    workspace = getattr(ctx, "workspace", None)
    if getattr(workspace, "name", None):
        header["workspace"] = workspace.name
    scope = getattr(ctx, "scope", None)
    if scope is not None:
        try:
            from analystos.capabilities import packs

            refs = sorted(p.ref for p in packs.for_scope(scope, getattr(ctx, "policy", None)))
            if refs:
                header["domain_packs"] = refs
        except Exception as exc:  # the header is a convenience; a pack registry error must not block a call
            log.warning("context header: domain packs unavailable: %s", exc)
        dialects = sorted(set((getattr(scope, "source_dialects", None) or {}).values()))
        if dialects:
            header["dialects"] = dialects
    return header


def compile_for(ctx: Any, purpose: str, required: dict[str, Any], *, objective: str | None = None,
                catalog: Any = _SCOPE_CATALOG, reference_text: str | None = None) -> Any:
    """Compile the prompt context for one model call (P4-T03) from the caller's mandatory inputs,
    the authorized catalog and the workspace knowledge, under the purpose profile's budget.

    Returns a CompiledContext for `llm_json`. When the mandatory part alone is over budget the
    context comes back `refused` (never cut): `llm_json` then records the refusal and the caller
    takes its deterministic path."""
    from analystos.context.compiler import PINNED_SECTION, CompiledContext
    from analystos.contracts.platform import PurposeProfile
    from analystos.services.platform_settings import get as platform

    settings = platform()
    profile = settings.context.profiles.get(purpose) or PurposeProfile(max_chars=1_500_000)
    profile, knowledge_chars = knowledge_scope(ctx, profile)
    run = getattr(ctx, "run", None)
    context = ((getattr(run, "capabilities", None) or {}).get("analysis_context") or {}) if run else {}
    from analystos.services.context_grounding import pinned_prompt_context

    # The reviewed brief is the compiler's own mandatory section (S03-T3): receipted, hashed into the cache and
    # identity, refused (never trimmed) when it does not fit.
    pinned, pinned_receipts = pinned_prompt_context(run, getattr(ctx, "scope", None))
    from analystos.core.ids import stable_hash
    meaning_receipts: list[dict[str, Any]] = []
    for meaning in (required.get("resolved_meaning") or {}).get("resolutions", []):
        if meaning.get("state") == "resolved":
            meaning_receipts.append({"id": meaning["id"], "section": "resolved_meaning", "source": meaning.get("by"),
                                    "revision": meaning.get("revision"), "columns": meaning.get("columns", []),
                                    "sha256": stable_hash(meaning)})
    if context:
        spec = context.get("spec") or {}
        # The reviewed, pinned business lens travels with every model purpose; it never changes
        # the authorized catalog or substitutes for the run's own question.
        required = {**required, "analysis_context": {
            "purpose": spec.get("purpose"), "business_description": spec.get("business_description"),
            "relevant_metrics": spec.get("metric_names"),
            "version": (context.get("definition") or {}).get("version")}}
    if objective is None:
        objective = getattr(run, "objective", None) or str(required.get("objective") or required.get("question") or "")
    if catalog is _SCOPE_CATALOG:
        catalog = (catalog_for_prompt(ctx, include_values=profile.catalog_detail == "profile", objective=objective, capped=False)
                   if "catalog" in profile.sections else None)
    if not settings.context.compiler_enabled:  # admin kill switch: the increment-3 payload, fit_payload only
        return CompiledContext(purpose=purpose, header={}, body={**required, **({PINNED_SECTION: pinned} if pinned else {}),
                                                                 **({"catalog": catalog} if catalog else {})},
                               receipts=[*pinned_receipts, *meaning_receipts])
    header = context_header(ctx)
    limit = int(settings.llm.max_prompt_tokens * 3.6) - _SYSTEM_RESERVE_CHARS
    key = _context_key(ctx, purpose, profile, settings, objective=objective, required=required, catalog=catalog,
                       reference_text=reference_text, header=header, limit=limit, knowledge_chars=knowledge_chars,
                       pinned=pinned) if settings.context.cache_enabled else None
    workspace_id = getattr(getattr(ctx, "workspace", None), "id", None)
    key_scope = context_cache.scope_of(ctx)
    reused = _COMPILED.get(key, purpose, workspace_id=workspace_id, scope=key_scope) if key else None
    if reused is not None:
        return reused
    compiled, complete = _compile(ctx, purpose, profile, settings, objective=objective, required=required, catalog=catalog,
                                  reference_text=reference_text, header=header, limit=limit, knowledge_chars=knowledge_chars,
                                  pinned=pinned, pinned_receipts=pinned_receipts)
    compiled.receipts.extend(meaning_receipts)
    if key and complete:  # a context compiled without its knowledge (load failed) is not kept
        _COMPILED.put(key, purpose, compiled, ttl=settings.context.cache_ttl_seconds, workspace_id=workspace_id,
                      scope=key_scope)
    return compiled


def knowledge_scope(ctx: Any, profile: Any) -> tuple[Any, int | None]:
    """The agent's knowledge contract (FND-006) applied to a purpose profile: knowledge sections the
    agent does not declare are removed (so they are neither loaded nor compiled), and its
    `budget_chars` caps what the knowledge sections may take. Callers without an agent contract
    (feedback, the knowledge preview) keep the profile as configured."""
    from analystos.context.compiler import KNOWLEDGE_SECTIONS
    from analystos.contracts.registry import AgentSpec

    agent = getattr(ctx, "agent", None)
    if not isinstance(agent, AgentSpec):
        return profile, None
    allowed = set(agent.knowledge_sections)
    sections = [s for s in profile.sections if s not in KNOWLEDGE_SECTIONS or s in allowed]
    if sections != list(profile.sections):
        profile = profile.model_copy(update={"sections": sections})
    return profile, (agent.knowledge.budget_chars if agent.knowledge is not None else None)


def _compile(ctx: Any, purpose: str, profile: Any, settings: Any, *, objective: str, required: dict[str, Any], catalog: Any,
             reference_text: str | None, header: dict[str, Any], limit: int,
             knowledge_chars: int | None = None, pinned: dict[str, Any] | None = None,
             pinned_receipts: list[dict[str, Any]] | None = None) -> tuple[Any, bool]:
    from analystos.context.compiler import (
        KNOWLEDGE_SECTIONS,
        PINNED_SECTION,
        CompiledContext,
        compile_context,
        load_knowledge,
        terms,
    )

    run = getattr(ctx, "run", None)
    knowledge: list = []
    complete = True
    sections = [x for x in profile.sections if x in KNOWLEDGE_SECTIONS]
    workspace_id = getattr(getattr(ctx, "workspace", None), "id", None)
    if sections and workspace_id:
        query = _retrieval_query(objective, reference_text)
        cache_key = _retrieval_key(ctx, sections, query) if settings.context.cache_enabled else None
        cached = context_cache.get("retrieval", purpose, cache_key) if cache_key else None
        if cached is not None:
            knowledge = [_knowledge_item(d) for d in cached]
        else:
            try:
                with session_scope() as s:
                    knowledge = load_knowledge(s, workspace_id, sections, run_id=getattr(run, "id", None), query=query,
                                               assets=getattr(getattr(ctx, "scope", None), "assets", None),
                                               denied_columns=getattr(getattr(ctx, "scope", None), "denied_columns", ()))
                if cache_key:
                    rows = [dataclasses.asdict(k) for k in knowledge]
                    context_cache.put(cache_key, rows, chars=len(compact_json(rows)), ttl=settings.context.cache_ttl_seconds)
            except Exception as exc:  # knowledge is optional context; its sections then say NO_MATCH
                log.warning("context compiler: knowledge unavailable for %s: %s", purpose, exc)
                complete = False
    generic = {w for a in (getattr(getattr(ctx, "scope", None), "assets", None) or []) for w in terms(a.split(".")[-1])}
    try:
        return compile_context(purpose, profile, objective=objective, required=required, catalog=catalog,
                               knowledge=knowledge, header=header, reference_text=reference_text,
                               limit_chars=max(2_000, limit), min_relevance=settings.context.min_relevance,
                               generic_terms=generic, knowledge_chars=knowledge_chars, pinned=pinned,
                               pinned_receipts=pinned_receipts or ()), complete
    except ContextOverBudget as exc:
        body = {**required, **({PINNED_SECTION: pinned} if pinned else {})}
        return CompiledContext(purpose=purpose, header={}, body=body, refused=exc.message,
                               budget_chars=int(exc.details.get("budget_chars") or 0),
                               mandatory_chars=int(exc.details.get("mandatory_chars") or 0)), complete


_SYSTEM_RESERVE_CHARS = 8_000  # room for the static system text inside max_prompt_tokens (fit_payload re-checks exactly)


# ------------------------------------------------------------------------------ compiled-context reuse (CTX-005)
COMPILED_CONTEXT_TTL_SECONDS = 900
COMPILED_CONTEXT_MAX_ENTRIES = 256


class CompiledContextCache:
    """Compiled contexts reused within one run or Ask thread (CTX-005).

    Key: (purpose, workspace knowledge version, scope hash, inputs hash), and the run or thread it
    was compiled for. The knowledge version covers the context entries, the knowledge packs the
    workspace sees, the enabled domain packs and the platform settings (so an edit to any of them
    compiles afresh); the scope hash covers what the caller may see; the inputs hash covers the
    mandatory inputs, the objective and reference text, the catalog as sent, the purpose profile and
    the budget. Knowledge that changes without a version (verified findings of *other* runs, which a
    prompt may cite) is bounded by the run/thread and a TTL. A context without a run or thread, or
    whose knowledge version cannot be computed, is never cached. Hits return a copy."""

    def __init__(self, ttl: float = COMPILED_CONTEXT_TTL_SECONDS, max_entries: int = COMPILED_CONTEXT_MAX_ENTRIES,
                 store: Any = None) -> None:
        import threading

        self.ttl, self.max_entries = ttl, max_entries
        self._store = store
        self._lock = threading.Lock()
        self.stats: dict[str, dict[str, int]] = {}

    @property
    def store(self) -> Any:
        """The shared context store (Redis when configured, else an in-process LRU honouring the TTL)."""
        return self._store if self._store is not None else context_cache.store()

    def _count(self, purpose: str, what: str, chars: int = 0, workspace_id: str | None = None) -> None:
        with self._lock:
            row = self.stats.setdefault(purpose, {"compiled": 0, "reused": 0, "chars_reused": 0})
            row[what] += 1
            row["chars_reused"] += chars
        context_cache.note("compiled", purpose, "hits" if what == "reused" else "misses", chars=chars,
                           workspace_id=workspace_id)

    @staticmethod
    def _key(key: str, workspace_id: str | None, scope: dict[str, Any] | None = None) -> str | None:
        """Under the tenant/scope/policy prefix and the workspace's context-cache prefix (S01-T7), so an entry is
        only found by the same tenant and scope, and one workspace's entries can be counted and cleared."""
        return context_cache.entry_key("compiled", workspace_id, key, **(scope or {}))

    def get(self, key: str, purpose: str, *, workspace_id: str | None = None,
            scope: dict[str, Any] | None = None) -> Any | None:
        from analystos.context.compiler import CompiledContext

        entry = self._key(key, workspace_id, scope)
        if entry is None:
            return None
        try:
            hit = self.store.get(entry)
        except Exception:
            hit = None
        if not isinstance(hit, dict):
            return None
        compiled = CompiledContext.from_dict(hit)  # a JSON round trip: every hit is a fresh copy
        self._count(purpose, "reused", compiled.chars, workspace_id)
        return compiled

    def put(self, key: str, purpose: str, compiled: Any, *, ttl: float | None = None, workspace_id: str | None = None,
            scope: dict[str, Any] | None = None) -> None:
        self._count(purpose, "compiled", workspace_id=workspace_id)
        entry = self._key(key, workspace_id, scope)
        if entry is None:
            return
        try:
            self.store.set(entry, compiled.to_dict(), self.ttl if ttl is None else min(ttl, self.ttl))
        except Exception as exc:  # pragma: no cover - reuse is an optimisation
            log.warning("compiled context not stored: %s", exc)

    def clear(self) -> None:
        with self._lock:
            self.stats = {}
        self.store.clear_local()


_COMPILED = CompiledContextCache()


def compiled_context_stats() -> dict[str, dict[str, int]]:
    """Per purpose, this process: contexts compiled (misses, stored) and reused (hits), with the
    characters reused. `context_cache.stats()` has the shared counters (compiled, retrieval)."""
    return {p: dict(v) for p, v in _COMPILED.stats.items()}


def clear_context_caches() -> None:
    """Measurement and test reset: compiled contexts, retrieval results and their counters."""
    _COMPILED.clear()
    context_cache.clear()


def _session_of(ctx: Any) -> str | None:
    run = getattr(ctx, "run", None)
    return f"run:{run.id}" if getattr(run, "id", None) else (
        f"thread:{ctx.thread_id}" if getattr(ctx, "thread_id", None) else None)


def _scope_hash(ctx: Any) -> str:
    from analystos.core.ids import stable_hash

    scope = getattr(ctx, "scope", None)
    return scope.scope_hash() if hasattr(scope, "scope_hash") else stable_hash(compact_json(scope))


# Sections whose content changes without a knowledge version (other runs' findings, episodes, the
# run's external results): their retrieval is shared only within the run or thread.
_SESSION_SECTIONS = {"prior_findings", "negative_knowledge", "external", "episodes"}


def _retrieval_key(ctx: Any, sections: list[str], query: str | None) -> str | None:
    """Knowledge retrieval (BM25 + vector over the packs) for the same query and sections is done once:
    shared by every purpose, step, worker and Ask request with the same workspace knowledge version."""
    workspace_id = getattr(getattr(ctx, "workspace", None), "id", None)
    knowledge = knowledge_version_for(ctx)
    if workspace_id is None or knowledge is None:
        return None
    session = _session_of(ctx) if set(sections) & _SESSION_SECTIONS else None
    if set(sections) & _SESSION_SECTIONS and session is None:
        return None
    run = getattr(ctx, "run", None)
    from datetime import date

    # The caller's scope is part of the prefix (S01-T7): load_knowledge filters by the scope's assets and denied
    # columns, so a narrower scope never reads a wider one's retrieval.
    return context_cache.key("retrieval", workspace_id, {"knowledge": knowledge, "effective_date": date.today().isoformat(), "sections": sorted(sections), "query": query,
                                                         "session": session, "run": getattr(run, "id", None)},
                             **context_cache.scope_of(ctx))


def _retrieval_query(objective: str | None, reference_text: str | None) -> str | None:
    return " ".join(x for x in (objective, reference_text) if x) or None


def knowledge_retrieval_key(ctx: Any, purpose: str, *, objective: str | None, reference_text: str | None = None) -> str | None:
    """The shared-cache key `compile_for` looks up for this call's knowledge retrieval (None when the purpose has
    no knowledge sections, the cache is off, or the entry cannot be shared). The context preview reads it to say
    whether a call would reuse retrieval work."""
    from analystos.context.compiler import KNOWLEDGE_SECTIONS
    from analystos.contracts.platform import PurposeProfile
    from analystos.services.platform_settings import get as platform

    settings = platform()
    if not settings.context.cache_enabled or not getattr(getattr(ctx, "workspace", None), "id", None):
        return None
    profile, _ = knowledge_scope(ctx, settings.context.profiles.get(purpose) or PurposeProfile(max_chars=1_500_000))
    sections = [x for x in profile.sections if x in KNOWLEDGE_SECTIONS]
    return _retrieval_key(ctx, sections, _retrieval_query(objective, reference_text)) if sections else None


def _knowledge_item(data: dict[str, Any]) -> Any:
    from analystos.context.compiler import KnowledgeItem

    fields = KnowledgeItem.__dataclass_fields__
    values = {k: v for k, v in data.items() if k in fields}
    for k in ("mapped_columns", "synonyms"):
        values[k] = tuple(values.get(k) or ())
    return KnowledgeItem(**values)


def _context_key(ctx: Any, purpose: str, profile: Any, settings: Any, *, objective: str, required: dict[str, Any],
                 catalog: Any, reference_text: str | None, header: dict[str, Any], limit: int,
                 knowledge_chars: int | None = None, pinned: dict[str, Any] | None = None) -> str | None:
    """(purpose, knowledge version, scope hash, inputs hash, run/thread). The inputs hash covers the
    catalog as sent, so a catalog edit (curation, crawl) compiles afresh: that is the catalog version; and the
    pinned brief, so a re-pinned brief does too."""
    from analystos.core.ids import stable_hash

    session = _session_of(ctx)
    workspace_id = getattr(getattr(ctx, "workspace", None), "id", None)
    if session is None or workspace_id is None:
        return None
    knowledge = knowledge_version_for(ctx)
    if knowledge is None:
        return None
    try:
        scope_hash = _scope_hash(ctx)
        inputs = stable_hash({"required": required, "objective": objective, "reference_text": reference_text,
                              "catalog": catalog, "header": header, "profile": profile.model_dump(mode="json"),
                              "limit": limit, "min_relevance": settings.context.min_relevance,
                              "knowledge_chars": knowledge_chars, "pinned": pinned})
    except (TypeError, ValueError):  # an input that cannot be hashed canonically is simply not cached
        return None
    return stable_hash({"purpose": purpose, "knowledge": knowledge, "scope": scope_hash, "inputs": inputs, "session": session})


def _size(value: Any) -> int:
    return len(compact_json(value)) + 1  # + separator


def fit_payload(payload: dict[str, Any], *, max_chars: int, objective: str | None = None) -> dict[str, Any]:
    """Shrink a prompt payload to `max_chars` of JSON by dropping whole units, never characters.

    Order: whole catalog tables (least relevant to the objective first, at least one kept), then
    whole columns (least useful first, at least one per table kept), then trailing items of other
    top-level lists (largest list first). Everything dropped is listed under `omitted`, so the
    model knows the context is partial. Interim until the context compiler (P4-T03)."""
    if len(compact_json(payload)) <= max_chars:
        return payload
    tokens = objective_tokens(objective if objective is not None else str(payload.get("objective") or payload.get("question") or ""))
    out = dict(payload)
    omitted: dict[str, Any] = {"reason": f"prompt budget {max_chars} chars"}
    if isinstance(out.get("omitted"), dict):  # the context compiler's own omissions are kept
        omitted.update({k: v for k, v in out["omitted"].items() if k != "reason"})
    out["omitted"] = omitted

    def exact() -> int:
        return len(compact_json(out))

    size = exact()
    catalog = out.get("catalog")
    if isinstance(catalog, list) and all(isinstance(t, dict) for t in catalog):
        catalog = [{**t, "columns": list(t.get("columns") or [])} for t in catalog]
        out["catalog"] = catalog
        # 1. whole tables, least relevant (then latest-listed) first
        order = sorted(range(len(catalog)), key=lambda i: (table_relevance(catalog[i], tokens), -i))
        drop: set[int] = set()
        dropped_tables = omitted.setdefault("catalog_tables", [])
        size = exact()
        for i in order:
            if size <= max_chars or len(drop) >= len(catalog) - 1:
                break
            drop.add(i)
            name = str(catalog[i].get("asset"))
            dropped_tables.append(name)
            size += _size(name) - _size(catalog[i])  # running estimate; re-synced when it says "fits"
            if size <= max_chars:
                out["catalog"] = [t for j, t in enumerate(catalog) if j not in drop]
                size = exact()
        catalog = [t for j, t in enumerate(catalog) if j not in drop]
        out["catalog"] = catalog
        # 2. whole columns, least useful first across the remaining tables
        dropped_cols: dict[str, list[str]] = omitted.setdefault("catalog_columns", {})
        size = exact()
        if size > max_chars:
            ranked = sorted(((ti, c) for ti, t in enumerate(catalog) for c in t["columns"]),
                            key=lambda tc: column_rank(tc[1], tokens), reverse=True)
            for ti, col in ranked:
                if size <= max_chars:
                    break
                table = catalog[ti]
                if len(table["columns"]) <= 1:
                    continue
                table["columns"].remove(col)
                asset = str(table.get("asset"))
                name = col.get("name") if isinstance(col, dict) else col
                size += _size(name) - _size(col) + (0 if asset in dropped_cols else _size(asset) + 3)
                dropped_cols.setdefault(asset, []).append(name)
                if size <= max_chars:
                    size = exact()
            size = exact()
        if not omitted.get("catalog_tables"):
            omitted.pop("catalog_tables", None)
        if not omitted.get("catalog_columns"):
            omitted.pop("catalog_columns", None)
        size = exact()
    # 3. trailing items of other lists, largest list first
    while size > max_chars:
        lists = [(k, v) for k, v in out.items() if k not in ("catalog", "omitted") and isinstance(v, list) and len(v) > 1]
        if not lists:
            break
        key, items = max(lists, key=lambda kv: _size(kv[1]))
        out[key] = items[:-1]
        omitted[f"{key}_items_dropped"] = omitted.get(f"{key}_items_dropped", 0) + 1
        size = exact()
    return out


@dataclass
class PromptLayout:
    """What `llm_json` sends after the system text: the cached preamble (workspace header and the run-stable
    part as text) and the volatile per-call inputs, after the final `fit_payload` guard."""

    header: dict[str, Any]
    stable: dict[str, Any]
    volatile: dict[str, Any]
    preamble: str
    fitted: dict[str, Any]
    trimmed: bool

    @property
    def volatile_text(self) -> str:
        return compact_json(self.volatile)


def prompt_layout(ctx: Any, payload: Any, *, system: str, prompt_vars: dict[str, str] | None = None,
                  stable: dict[str, Any] | None = None) -> PromptLayout:
    """The prompt messages for `payload` (a CompiledContext or a plain dict) after the system text `system`: one
    function for the model call and for the context preview (context/preview.py), so the preview cannot drift."""
    from analystos.context.compiler import CompiledContext, render_stable
    from analystos.services.platform_settings import get as platform

    compiled = payload if isinstance(payload, CompiledContext) else None
    body: dict[str, Any] = compiled.body if compiled is not None else payload
    llm = platform().llm
    header = dict(compiled.header) if compiled is not None else {}
    if prompt_vars and "dialect" in prompt_vars:
        header.pop("dialects", None)  # the system text already names the dialect: say it once
    stable_part = {k: body[k] for k in compiled.stable if k in body} if compiled is not None else dict(stable or {})
    merged = {**stable_part, **{k: v for k, v in body.items() if k not in stable_part}}
    budget = int(llm.max_prompt_tokens * 3.6) - len(system) - len(compact_json(header)) - 200  # inverse of estimate_tokens
    run = getattr(ctx, "run", None)
    fitted = fit_payload(merged, max_chars=max(2_000, budget), objective=run.objective if run else None)
    kept_stable = {k: fitted[k] for k in stable_part if k in fitted}
    volatile = {k: v for k, v in fitted.items() if k not in kept_stable}
    if compiled is not None:
        preamble = render_stable(header, kept_stable, compiled.omitted) if (header or kept_stable) else ""
    else:
        preamble = compact_json(kept_stable) if kept_stable else ""
    return PromptLayout(header=header, stable=kept_stable, volatile=volatile, preamble=preamble, fitted=fitted,
                        trimmed=fitted is not merged)


def _agent_refusal(ctx: Any, purpose: str) -> tuple[str, str] | None:
    """Agent manifest enforcement (P4-X03): only the agent's declared model purposes are routed, and
    only while its per-step budget (`llm_calls`, `usd`) lasts. Contexts without a manifest (Ask,
    monitors) are governed by the workspace policy alone."""
    manifest = getattr(ctx, "manifest", None)
    if manifest is None or manifest.kind != "Agent":
        return None
    from analystos.capabilities.agents import body

    if purpose not in body(manifest).purposes:
        return "purpose_not_declared", (f"Model purpose {purpose} is not declared by {manifest.ref}; "
                                        "using the deterministic path.")
    for kind in ("llm_calls", "usd"):
        if not ctx.budget_left(kind):
            return "agent_budget_exhausted", (f"{manifest.ref} {kind} budget ({getattr(ctx.budget, kind)}) is used up "
                                              f"for this step; {purpose} uses the deterministic path.")
    return None


def _record_context_refusal(ctx: Any, purpose: str, call: Any, reason: str, estimated: int) -> None:
    """A compiler refusal is a model call that did not happen: record it like the router's own
    oversize refusal (status `refused`), so it shows in the call log and the savings ledger."""
    sink = getattr(ctx.router, "sink", None)
    if sink is not None:
        sink.record(ctx=call, purpose=purpose, profile="-", provider="context_compiler", model="-", status="refused",
                    attempt=0, latency_ms=0, input_tokens=0, output_tokens=0, cost_usd=0.0, request_hash=None,
                    error=reason[:500], tokens_saved=estimated)


class ModelOutcome(str):
    """Why `llm_json` returned no data: a reason code (the string itself, so callers that log or
    compare the old second element keep working) plus the message and details of the one cause.

    Codes: mode_off, no_api_key, provider_cooldown (details.retry_in_s), policy_blocked,
    residency_blocked, approval_required, budget_exceeded, cap_reached, context_over_budget,
    invalid_output, purpose_not_declared, agent_budget_exhausted, upstream_unavailable, no_route."""

    message: str
    details: dict[str, Any]

    def __new__(cls, code: str, message: str = "", **details: Any) -> ModelOutcome:
        out = super().__new__(cls, code)
        out.message = message or code
        out.details = details
        return out

    @property
    def code(self) -> str:
        return str.__str__(self)


_OUTCOME_BY_ERROR = {"llm_disabled": "mode_off", "provider_quota_exhausted": "provider_cooldown",
                     "spend_cap_reached": "cap_reached", "spend_counters_unavailable": "cap_reached",
                     "model_route_unavailable": "no_route", "egress_blocked": "policy_blocked"}


def model_outcome(exc: AnalystOSError) -> ModelOutcome:
    """The reason code of a router error (see `ModelOutcome`), keeping its message and details."""
    code = _OUTCOME_BY_ERROR.get(exc.code, exc.code)
    if code == "mode_off" and exc.details.get("oversize"):
        code = "context_over_budget"
    elif code == "budget_exceeded" and exc.details.get("cap"):
        code = "cap_reached"
    elif exc.code == "upstream_unavailable":
        code = "upstream_unavailable"
    return ModelOutcome(code, exc.message, **exc.details)


def note_fallback(ctx: Any, purpose: str, outcome: Any, *, say: bool) -> Any:
    """N-14: a purpose that wanted a model had no allowed route (models disabled by the settings, a spend cap or
    budget, a missing price, policy, no key...), so the rules rung answers. Emit the structured reason as a
    `model.fallback` event (once per purpose and cause in a step) and, with `say`, the plain sentence as a run
    message. Returns the ModelFallback, or None when `outcome` is not a route fallback."""
    from analystos.llm.fallback import fallback_for

    fallback = fallback_for(purpose, outcome)
    if fallback is None:
        return None
    seen = _memo(ctx, "_aos_fallbacks")
    if (purpose, fallback.cause) in seen:
        return fallback
    seen[(purpose, fallback.cause)] = True
    payload = fallback.to_dict()
    if say:
        ctx.say(fallback.sentence, kind="decision", data={"purpose": purpose, "model_fallback": payload})
    event = getattr(ctx, "event", None)
    if callable(event):
        task = getattr(getattr(ctx, "task", None), "key", None)
        try:
            event("model.fallback", {**payload, **({"task": task} if task else {})})
        except Exception as exc:  # the event is a report; losing it must not fail the rules answer
            log.warning("model.fallback event for %s not recorded: %s", purpose, exc)
    return fallback


def llm_json(ctx: RunContext, purpose: str, prompt_name: str, payload: Any, *,
             exclude_families: list[str] | None = None, max_tokens: int | None = None,
             prompt_vars: dict[str, str] | None = None, validate: Callable[[Any], str | None] | None = None,
             escalate: str | None = None, escalated_from: str | None = None,
             stable: dict[str, Any] | None = None) -> tuple[Any | None, str | None]:
    """Call a chat model for JSON. Returns (data, model) or (None, ModelOutcome) — callers degrade
    visibly, and can say which of the causes in `ModelOutcome` stopped the call.

    Cheap first, escalate: `validate(data)` is the caller's deterministic check (None = usable, else
    why not). A small-tier answer that fails it - or is not JSON - is re-asked once of the large tier
    when the purpose's escalation policy allows (router.complete). `escalate` (with the failing model
    as `escalated_from`) asks the large tier directly after a check further downstream failed; an
    answer that still fails validation is returned all the same and the caller's own checks decide.

    `payload` is a CompiledContext (`compile_for`, P4-T03), a DeferredContext (`defer_compile`:
    compiled here, only once the call will be sent) or, for purposes without run context (narrative,
    verification, summary), a plain dict; `stable` is a plain payload's run-stable part.

    Prompt layout for provider prompt caching (P4-T04, Stream B): message 1 the static system text
    (with the method vocabulary), message 2 the workspace + run preamble (header, objective, pinned
    context, catalog and knowledge as sorted text — identical for every call that shares them),
    both marked as the stable prefix; message 3 the purpose's volatile inputs only.

    The call context carries the workspace policy (provider list, residency, approval threshold),
    `prompt_version = <name>@<hash of the exact system text>`, the compiler's receipts and the
    workspace knowledge version (L0 cache key, P4-T06); the prompt is fitted to the admin prompt
    limit by dropping whole entries (fit_payload, the final guard), never by cutting JSON."""
    from analystos.agents.prompts import prompt, prompt_version_id
    from analystos.context.compiler import CompiledContext
    from analystos.llm.cache import estimate_tokens
    from analystos.services.platform_settings import get as platform

    if ctx.router.mode(purpose) == "off":
        model_gate(ctx, purpose, payload, deterministic_ok=True)  # records the avoided call
        return None, ModelOutcome("mode_off", f"model use for '{purpose}' is turned off by the administrator", purpose=purpose)
    refused = _agent_refusal(ctx, purpose)
    if refused:
        ctx.say(refused[1], kind="decision")
        ctx.router.record_skip(purpose, ctx.call_ctx(), estimated_tokens=estimate_tokens(_prompt_text(payload)) + 500,
                               reason=refused[1][:200])
        return None, ModelOutcome(refused[0], refused[1], purpose=purpose)
    system = prompt(prompt_name, **(prompt_vars or {}))
    call = dataclasses.replace(ctx.call_ctx(exclude_families=exclude_families), prompt_version=prompt_version_id(prompt_name, system))
    call.with_policy(getattr(ctx, "policy", None))
    if not ctx.router.available(purpose, call):  # before compiling: an unavailable route needs no context
        why = getattr(ctx.router, "unavailable", lambda *_: None)(purpose, call)
        outcome = model_outcome(why) if why is not None else ModelOutcome("no_route", f"no model route for {purpose}")
        fallback = note_fallback(ctx, purpose, outcome, say=True)
        if fallback is not None:  # the avoided call, accounted like any other rules answer
            ctx.router.record_skip(purpose, call, estimated_tokens=estimate_tokens(_prompt_text(payload)) + 500,
                                   reason=f"fallback {fallback.cause}: {fallback.detail}"[:200], rung=fallback.rung)
        return None, outcome
    if isinstance(payload, DeferredContext):
        payload = payload.resolve()
    compiled = payload if isinstance(payload, CompiledContext) else None
    body: dict[str, Any] = compiled.body if compiled is not None else payload
    if compiled is not None and compiled.refused:
        message = (f"Context for {purpose} not sent: {compiled.refused} (mandatory {compiled.mandatory_chars} chars > "
                   f"budget {compiled.budget_chars}); using the deterministic path.")
        ctx.say(message, kind="decision", data={"purpose": purpose, "mandatory_chars": compiled.mandatory_chars,
                                                "budget_chars": compiled.budget_chars})
        _record_context_refusal(ctx, purpose, call, message, estimate_tokens(compact_json(body)) + len(system) // 4)
        return None, ModelOutcome("context_over_budget", message, purpose=purpose, mandatory_chars=compiled.mandatory_chars,
                                  budget_chars=compiled.budget_chars)
    llm = platform().llm
    if call.knowledge_version is None and call.workspace_id and llm.cache_enabled and purpose in llm.cacheable_purposes:
        call.knowledge_version = knowledge_version_for(ctx)
    layout = prompt_layout(ctx, compiled if compiled is not None else body, system=system, prompt_vars=prompt_vars,
                           stable=stable)
    if layout.trimmed:
        fitted = layout.fitted
        ctx.say(f"Prompt for {purpose} trimmed to fit the model budget (~{estimate_tokens(compact_json(fitted))} tokens); "
                f"omitted: {compact_json({k: v for k, v in fitted['omitted'].items() if k != 'reason'})[:300]}", kind="decision")
    if compiled is not None:
        call.context_receipts = compiled.receipts
    messages: list[dict[str, Any]] = [{"role": "system", "content": system, "cache": True}]
    if layout.preamble:
        messages.append({"role": "user", "content": layout.preamble, "cache": True})
    messages.append({"role": "user", "content": layout.volatile_text})
    spend = getattr(ctx, "spend", None)
    if spend is not None:
        spend("llm_calls")
    check = (lambda r: validate(r.data)) if validate is not None else None
    try:
        response = ctx.router.complete(purpose, messages, ctx=call, json_output=True, max_tokens=max_tokens,
                                       validate=check, escalate=escalate, escalated_from=escalated_from)
        if spend is not None:
            spend("usd", response.cost_usd)
        if response.escalated_from:
            ctx.say(f"{purpose}: the answer of {response.escalated_from} failed validation ({response.escalation_reason}); "
                    f"escalated to {response.model}.", kind="decision",
                    data={"purpose": purpose, "escalated_from": response.escalated_from, "model": response.model,
                          "reason": response.escalation_reason})
        return response.data, response.model
    except AnalystOSError as exc:
        log.warning("llm %s failed: %s", purpose, exc)
        if exc.code == "escalation_unavailable":  # the caller's own checks already decided; nothing else was lost
            ctx.say(f"{purpose}: no larger model to escalate to ({exc.message}).", kind="decision")
            return None, model_outcome(exc)
        remedy = exc.details.get("remedy") if isinstance(exc.details, dict) else None
        outcome = model_outcome(exc)
        fallback = note_fallback(ctx, purpose, outcome, say=False)
        ctx.say(f"Model call for {purpose} unavailable ({exc.code}); using deterministic fallback."
                + (f" {remedy}" if remedy else ""), kind="decision",
                data={"purpose": purpose, "code": exc.code, **({"remedy": remedy} if remedy else {}),
                      **({"model_fallback": fallback.to_dict()} if fallback is not None else {})})
        return None, outcome


def llm_json_batch(ctx: RunContext, purpose: str, prompt_name: str, items: list[dict[str, Any]], *, list_key: str,
                   max_tokens: int | None = None, exclude_families: list[str] | None = None,
                   validate: Callable[[Any], str | None] | None = None) -> tuple[dict[str, dict] | None, Any]:
    """One model call for a run's per-item prompts (Stream B: narratives, reviews) instead of one each,
    so the static system text is sent once. Every item carries an `id`; the answer is
    `{list_key: [{"id", ...}]}`. Returns ({id: answer}, model), or (None, ModelOutcome) when no call
    was made, or (None, model name) when the answer is malformed — the caller then asks per item.
    Each answer is still validated by the caller, item by item."""
    def malformed(data: Any) -> str | None:
        if not isinstance(data, dict) or not isinstance(data.get(list_key), list):
            return f"answer is not an object with a {list_key} list"
        return validate(data) if validate is not None else None

    data, model = llm_json(ctx, purpose, prompt_name, {list_key: items}, max_tokens=max_tokens,
                           exclude_families=exclude_families, validate=malformed)
    if data is None:
        return None, model
    if not isinstance(data, dict) or not isinstance(data.get(list_key), list):
        return None, str(model)
    wanted = {str(i.get("id")) for i in items}
    return {str(a["id"]): a for a in data[list_key] if isinstance(a, dict) and str(a.get("id")) in wanted}, model
