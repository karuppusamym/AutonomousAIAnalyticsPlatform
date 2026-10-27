"""SQL Engineer Agent (§13.11): reusable analytical dataset + ad-hoc NL->SQL with gateway repair loop."""
from __future__ import annotations

import re
from typing import Any

import sqlglot
from sqlalchemy import select
from sqlglot import exp
from sqlglot.optimizer.simplify import simplify

from analystos.agents.common import (
    ModelOutcome,
    asset_rows,
    catalog_for_prompt,
    compact_json,
    compile_for,
    defer_compile,
    llm_json,
    sensitive,
    task_output,
)
from analystos.artifacts.registry import link, save_artifact
from analystos.capabilities import packs as pack_registry
from analystos.contracts.analysis import AnalysisSpec, Derivation, Filter
from analystos.contracts.bi import DatasetDef
from analystos.core.errors import AnalystOSError, InvalidInput, ModelUnavailable, NotAnswerable, SQLRejected
from analystos.db.base import session_scope
from analystos.db.models import Hypothesis, Insight, Relationship, SourceAsset, by_code
from analystos.governance.budgets import ASK_PURPOSE, ask_actor, check_ask_budget
from analystos.llm.redaction import QUESTION_MODEL_INSTRUCTION, redact_question, restore_values, tokenize_values
from analystos.runtime.context import RunContext
from analystos.skills.catalog import normalize_type
from analystos.skills.literals import check_literals


def has_sql(data: Any) -> str | None:
    """Escalation check for SQL generation/repair: the answer must carry a statement."""
    return None if isinstance(data, dict) and str(data.get("sql") or "").strip() else "no SQL statement in the answer"


CANNOT_ANSWER_KINDS = ("not_in_data", "write", "ambiguous", "missing_input")
# A question that opens with a data-changing verb: Ask only reads, so it is refused before a model writes a
# SELECT that pretends to have done it. Verbs that also open analytics questions as nouns (change, close,
# add, cancel) are left to the model's `cannot_answer: write`.
WRITE_REQUEST = re.compile(r"^\s*(?:please\s+)?(?:delete|remove|update|insert|mark|set|drop|truncate|alter|reopen|assign|"
                           r"approve|reject|modify|edit|rename|merge|archive|purge)\b", re.I)


def cannot_answer(data: Any) -> dict[str, str] | None:
    """The model's explicit refusal ({"sql": null, "cannot_answer": {kind, reason}}), or None."""
    if not isinstance(data, dict) or str(data.get("sql") or "").strip():
        return None
    ca = data.get("cannot_answer")
    if isinstance(ca, dict) and ca.get("kind") in CANNOT_ANSWER_KINDS:
        return {"kind": str(ca["kind"]), "reason": str(ca.get("reason") or "")[:300]}
    return None


def sql_or_refusal(data: Any) -> str | None:
    """Generation check: a statement, or a well-formed refusal (a refusal is never escalated to a larger model)."""
    return None if cannot_answer(data) is not None else has_sql(data)


def placeholder_problem(sql: str, dialect: str) -> str | None:
    """Why a model-written SELECT is a placeholder rather than an answer, or None. Live runs (P4-V02,
    2026-09-28) showed models answering out-of-scope questions with `NULL AS competitor_share`,
    `'N/A' AS caller_email` or `WHERE 1=0`: well-formed, gateway-clean and wrong. An output column
    computed from no column, row or aggregate, or a filter that is always false, answers nothing."""
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except sqlglot.errors.ParseError:
        return None  # the gateway validator reports unparseable SQL
    if not isinstance(tree, exp.Select):
        return None
    for projection in tree.expressions:
        node = projection.this if isinstance(projection, exp.Alias) else projection
        if isinstance(node, exp.Star) or next(node.find_all(exp.Column, exp.Star, exp.AggFunc, exp.Window, exp.Select), None):
            continue
        return f"output column '{projection.alias_or_name or node.sql(dialect=dialect)}' is a constant, not data"
    for clause in ("where", "having"):
        cond = tree.args.get(clause)
        if cond is None or next(cond.this.find_all(exp.Column, exp.Select), None) is not None:
            continue
        try:
            value = simplify(cond.this.copy())
        except Exception:  # noqa: BLE001 - an expression sqlglot cannot fold is not provably false
            continue
        if isinstance(value, exp.Boolean) and not value.this:
            return f"the {clause.upper()} clause is always false ({cond.this.sql(dialect=dialect)})"
    limit = tree.args.get("limit")
    if limit is not None and isinstance(limit.expression, exp.Literal) and str(limit.expression.this) == "0":
        return "LIMIT 0 returns no rows"
    return None


def slug(text: str, n: int = 40) -> str:
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", text.lower())).strip("_")[:n] or "col"


def derivation_alias(d: Derivation) -> str:
    """The dataset column a derivation becomes; a related table's column is prefixed with the entity it is read
    through (`customer_region`), so it never collides with the base table's own columns."""
    if d.type == "column" and not d.via:
        return d.column
    base = {"column": d.column, "duration_hours": f"{d.column}_to_{d.end_column}_hours", "after_hours": f"{d.column}_after_hours",
            "later_than": f"{d.end_column}_after_{d.column}",
            "bucket": f"{d.column}_bucket", "equals": f"{d.column}_is_{slug(str(d.value), 20)}", "is_true": f"{d.column}_true",
            "date_trunc": f"{d.column}_{d.grain}", "hour_of_day": f"{d.column}_hour", "day_of_week": f"{d.column}_dow"}[d.type]
    if d.via:
        from analystos.skills.lookups import ref_stem

        stem = ref_stem(d.via)
        base = base if base.startswith(f"{stem}_") else f"{stem}_{base}"
    return slug(base, 60)


def primary_asset(ctx: RunContext, verified_specs: list[AnalysisSpec]) -> str:
    counts: dict[str, int] = {}
    for s in verified_specs:
        counts[s.asset] = counts.get(s.asset, 0) + 1
    if counts:
        return max(counts, key=counts.get)
    rows = sorted(asset_rows(ctx), key=lambda ac: -(ac[0].row_count or 0))
    return f"{rows[0][0].schema_name}.{rows[0][0].name}"


def build_dataset(ctx: RunContext) -> dict:
    from analystos.skills.sqlbuild import join_alias, render_derivation, render_filter

    with session_scope() as s:
        specs = [AnalysisSpec.model_validate(h.spec) for h in s.scalars(
            select(Hypothesis).join(Insight, Insight.hypothesis_id == Hypothesis.id)
            .where(Insight.run_id == ctx.run.id, Insight.status == "verified").order_by(*by_code(Hypothesis.code)))]
    asset = primary_asset(ctx, specs)
    source_id = ctx.scope.asset_sources[asset]
    dialect = ctx.scope.source_dialects.get(source_id, "postgres")
    denied = set(ctx.scope.denied_columns)
    raw_cols = [c for c in ctx.scope.columns[asset] if f"{asset}.{c}" not in denied]
    derived: dict[str, Derivation] = {}
    types = {c.name: c.semantic_type for a, cols in asset_rows(ctx) if f"{a.schema_name}.{a.name}" == asset for c in cols}
    spec_joins: dict[str, Any] = {}  # via -> Join: related-table attributes the verified findings are broken down by
    for spec in (sp for sp in specs if sp.asset == asset):
        joined_filters = [Derivation(type="column", column=f.column, via=f.via) for f in spec.filters if f.via]
        for d in (spec.outcome, spec.segment, spec.time, *spec.drivers, *joined_filters):
            if d is None or (d.type == "column" and not d.via):
                continue
            j = spec.join(d.via)
            if d.via and (j is None or j.asset not in ctx.scope.assets or ctx.scope.asset_sources.get(j.asset) != source_id
                          or any(f"{j.asset}.{c}" in denied or f"*.{c}" in denied for c in d.columns())):
                continue
            derived.setdefault(derivation_alias(d), d)
            if j is not None:
                spec_joins.setdefault(d.via, j)
    hints = pack_registry.hints()
    start_words = "|".join(map(re.escape, ("created", "start", *hints.event_start)))
    time_col = next((c for c in raw_cols if types.get(c) == "datetime" and re.search(start_words, c)), None) or \
        next((c for c in raw_cols if types.get(c) == "datetime"), None)
    if time_col:
        for d in (Derivation(type="date_trunc", column=time_col, grain="month"), Derivation(type="day_of_week", column=time_col),
                  Derivation(type="hour_of_day", column=time_col)):
            derived.setdefault(derivation_alias(d), d)
    select_parts = [f't."{c}"' for c in raw_cols]
    select_parts += [f'{render_derivation(d, dialect, table_alias="t")} AS "{alias}"' for alias, d in derived.items()]
    joins, join_parts = [], []
    with session_scope() as s:
        a_row = s.scalar(select(SourceAsset).where(SourceAsset.workspace_id == ctx.workspace.id,
                                                   SourceAsset.schema_name == asset.split(".")[0], SourceAsset.name == asset.split(".")[1]))
        rels = list(s.scalars(select(Relationship).where(Relationship.workspace_id == ctx.workspace.id,
                                                         Relationship.from_asset_id == a_row.id, Relationship.validated.is_(True))
                                  .order_by(Relationship.from_column, Relationship.to_column, Relationship.to_asset_id)))
        targets = {r.to_asset_id: s.get(SourceAsset, r.to_asset_id) for r in rels}
    for i, r in enumerate(rels):
        tgt = targets.get(r.to_asset_id)
        if tgt is None:
            continue
        tfq = f"{tgt.schema_name}.{tgt.name}"
        if tfq not in ctx.scope.assets or ctx.scope.asset_sources.get(tfq) != source_id or f"{r.from_column}_name" in raw_cols:
            continue
        name_col = next((c for c in ctx.scope.columns.get(tfq, []) if c in ("name", "number", *hints.display_columns)
                         and f"{tfq}.{c}" not in denied), None)
        if not name_col:
            continue
        alias = f"j{i}"
        joins.append(f'LEFT JOIN {tfq} {alias} ON t."{r.from_column}" = {alias}."{r.to_column}"')
        join_parts.append(f'{alias}."{name_col}" AS "{slug(r.from_column + "_" + name_col)}"')
    for via, j in spec_joins.items():  # the verified specs' own joins (validated many-to-one), under their spec alias
        joins.append(f'LEFT JOIN {j.asset} "{join_alias(via)}" ON t."{via}" = "{join_alias(via)}"."{j.to_column}"')
    filters = [Filter(column=f["column"], op=f["op"], value=f.get("value"), origin="user_redirect")
               for f in ctx.run.constraints.get("filters", []) if f.get("asset") in (None, asset)]
    where = (" WHERE " + " AND ".join(render_filter(f, dialect, table_alias="t") for f in filters)) if filters else ""
    sql = f"SELECT {', '.join(select_parts + join_parts)} FROM {asset} t {' '.join(joins)}{where}"
    sql = sqlglot.transpile(sql, read=dialect, write=dialect)[0]
    run_sql = ctx.run_sql(source_id)
    count = ctx.tools().invoke("sql.execute", {"purpose": "dataset.validate"},
                               lambda: run_sql(f"SELECT COUNT(*) AS n FROM ({sql}) d", purpose="dataset.validate"))
    sample = run_sql(f"SELECT * FROM ({sql}) d", purpose="dataset.sample", max_rows=5)
    columns = [{"name": c, "semantic_type": ("boolean" if derived.get(c) and derived[c].type in ("equals", "is_true", "after_hours", "later_than")
                                              else "numeric" if derived.get(c) and derived[c].type in ("duration_hours", "hour_of_day", "day_of_week")
                                              else "categorical" if derived.get(c) and derived[c].type in ("bucket", "column")
                                              else "datetime" if derived.get(c) and derived[c].type == "date_trunc" else types.get(c)),
                "derived": c in derived, "label": derived[c].label if c in derived else None} for c in sample.columns]
    name = f"aos_{slug(ctx.run.objective, 30)}_{ctx.run.id[-6:]}"
    ds = DatasetDef(name=name, description=f"Analytical dataset for: {ctx.run.objective[:200]}", sql=sql, columns=columns,
                    time_column=derivation_alias(Derivation(type="date_trunc", column=time_col, grain="month")) if time_col else None,
                    source_assets=list(dict.fromkeys([asset] + [f"{t.schema_name}.{t.name}" for t in targets.values() if t] + [j.asset for j in spec_joins.values()])), row_count=int(count.rows[0][0]))
    with session_scope() as s:
        art = save_artifact(s, workspace_id=ctx.workspace.id, run_id=ctx.run.id, type_="dataset", name=name,
                            content={**ds.model_dump(), "raw_time_column": time_col, "dialect": dialect, "source_id": source_id},
                            creator_agent=ctx.agent.id)
        for a in ds.source_assets:
            link(s, ctx.workspace.id, ("dataset", art.id), "built_from", ("table", a), run_id=ctx.run.id)
        for ins_id in [i.id for i in s.scalars(select(Insight).where(Insight.run_id == ctx.run.id, Insight.status == "verified")
                                                     .order_by(*by_code(Insight.code)))]:
            link(s, ctx.workspace.id, ("insight", ins_id), "reproducible_in", ("dataset", art.id), run_id=ctx.run.id)
    ctx.event("dataset.created", {"name": name, "rows": ds.row_count, "columns": len(columns), "derived": list(derived)})
    ctx.say(f"Built virtual analytical dataset {name}: {ds.row_count:,} rows, {len(columns)} columns "
            f"({len(derived)} derived: {', '.join(derived)}){'; user filters applied' if filters else ''}. No source writes (minimum ETL).")
    return {"artifact_id": art.id, "name": name, "rows": ds.row_count}


# ------------------------------------------------------------------------------------------- ad hoc
# ask_route options (spec v3 §4.2): the ladder, tool-first. A rule decides; a model may only break a tie.
ASK_ROUTES = {"verified_query": "Answer from a verified query in the registry (no model call)",
              "tool": "Answer with a schema-bound rule: a pack-declared distribution or a simple single-table shape "
                      "(count, distribution, average/median/total, top N, over time) built from the catalog",
              "generate": "Write new SQL with the model, then validate and run it through the gateway",
              "decline": "Do not answer yet: a required input is missing, ask the user for it"}


def _stage(ctx: Any, key: str, text: str, **data: Any) -> None:
    """Report a plain-language step to whoever streams this Ask (the SSE route); a no-op elsewhere."""
    fn = getattr(ctx, "on_stage", None)
    if fn is not None:
        fn(key, text, data)


def _authorize_ask(ctx: Any) -> None:
    """Ask runs model-written SQL as the SQL agent's ``sql.execute`` tool: same gate as a run."""
    from analystos.contracts.policy import ExecutionIdentity
    from analystos.tools.registry import ToolRuntime

    identity = ExecutionIdentity(user_id=ctx.user.id, workspace_id=ctx.workspace.id, agent_id=ctx.agent.id, purpose=ASK_PURPOSE)
    ToolRuntime(user=ctx.user, identity=identity, agent=ctx.agent, scope=getattr(ctx, "scope", None)).authorize(
        "sql.execute", {"purpose": ASK_PURPOSE})


def _check_budget(ctx: Any) -> None:
    with session_scope() as s:
        check_ask_budget(s, ctx.workspace.id, ctx.user.id, ctx.policy)


def _result(result: Any) -> dict[str, Any]:
    return {"query_id": result.query_id, "columns": result.columns, "rows": result.rows[:500], "row_count": result.row_count,
            "truncated": result.truncated, "referenced_assets": list(getattr(result, "referenced_assets", None) or []),
            "cache_hit": bool(getattr(result, "cache_hit", False)), "result_hash": getattr(result, "result_hash", None),
            "duration_ms": getattr(result, "duration_ms", None)}


def decide(ctx: Any, purpose: str, state: dict[str, Any], question: Any, facts: dict[str, Any]) -> dict[str, Any]:
    """One DecisionService decision for Ask, recorded against the Ask turn (`task_id`). A context with
    no model router (tests, embedded callers) gets the purpose's rule directly: the same answer the
    service's `rules` backend gives, not recorded."""
    from analystos.decisions.rules import RULES

    services = getattr(ctx, "services", None)
    if getattr(services, "router", None) is not None and hasattr(ctx, "call_ctx"):
        d = services.decisions.decide(purpose, state, question, facts=facts, ctx=ctx.call_ctx(),
                                      subject=getattr(ctx, "subject", None))
        return {**d.summary(), "authority": d.authority, "probabilities": d.probabilities, "enforced": d.enforced,
                "details": d.details, "note": next((a.get("reason") for a in reversed(d.attempts) if a.get("outcome") == "answered"), None)}
    proposal = RULES[purpose](state, facts, question)
    value = proposal.value if proposal is not None else question.default
    return {"id": None, "purpose": purpose, "backend": "rules" if proposal is not None else "default", "value": value,
            "p": None, "model": None, "fallback_reason": None, "authority": None,
            "probabilities": proposal.probabilities if proposal else {}, "enforced": [], "details": proposal.details if proposal else {},
            "note": proposal.note if proposal else None}


def verified_match_score(score: float) -> float:
    """A registry match (coverage + precision, each past its floor) on the 0..1 scale of `ask_route`:
    the weakest accepted match maps to 0.8, so every match the registry accepts is routed to it by the
    rule, exactly as before the decision existed."""
    from analystos.registries.verified_queries import MIN_COVERAGE, MIN_PRECISION

    floor = MIN_COVERAGE + MIN_PRECISION
    return round(0.8 + 0.2 * max(0.0, min(1.0, (score - floor) / (2.0 - floor))), 4)


def _registry_lookup(ctx: Any, question: str, parameters: dict[str, Any] | None) -> Any:
    """The registry's best compatible match and the closer token matches it rejected (with reasons)."""
    from analystos.registries import verified_queries as vqr

    with session_scope() as s:
        found = vqr.find(s, ctx.workspace.id, question, parameters)
        if found.hit is not None:
            s.expunge(found.hit.entry)
        return found


def _registry_match(ctx: Any, question: str, parameters: dict[str, Any] | None) -> Any:
    return _registry_lookup(ctx, question, parameters).hit


def _record_skip(ctx: Any, reason: str, avoided: int, rung: str = "registry") -> None:
    router = getattr(ctx, "router", None)
    if router is not None:
        router.record_skip("sql_generation", ctx.call_ctx(), estimated_tokens=avoided, reason=reason, rung=rung)


def _registry_answer(ctx: Any, question: str, hit: Any) -> dict[str, Any]:
    """Ladder rung L1 (P4-T05): answer from a verified query with no model call, or decline and ask
    for a missing required parameter."""
    from analystos.llm.cache import estimate_tokens
    from analystos.registries import verified_queries as vqr

    entry = {"id": hit.entry.id, "name": hit.entry.name, "pattern": hit.pattern, "score": round(hit.score, 3)}
    template, params, dialect = hit.entry.sql_template, list(hit.entry.parameters or []), hit.entry.dialect
    avoided = estimate_tokens(compact_json({"question": question, "catalog": catalog_for_prompt(ctx)})) + 500
    if hit.missing:
        _record_skip(ctx, f"L1 registry: declined, {entry['name']} needs {', '.join(p['name'] for p in hit.missing)}", avoided)
        missing = [{k: p.get(k) for k in ("name", "type", "column", "values") if p.get(k) is not None} for p in hit.missing]
        return {"status": "needs_input", "answered_by": "registry", "verified_query": entry, "parameters": hit.values,
                "missing": missing, "sql": None, "model": None, "attempts": [], "result": None,
                "explanation": f"This matches the verified query '{entry['name']}', which needs "
                               f"{', '.join(p['name'] for p in hit.missing)}. Say which value to use; nothing was guessed."}
    _stage(ctx, "registry", f"Found a verified answer: '{entry['name']}'", verified_query=entry["id"])
    values = {p["name"]: vqr.coerce(p, hit.values[p["name"]]) for p in params}  # vocabulary spelling, typed
    sql = vqr.render(template, params, values, dialect)
    _stage(ctx, "execute", "Running it through the query gateway (read-only, within your access)")
    result = ctx.services.gateway.execute(ctx.scope, sql, actor=ask_actor(ctx.user.id), purpose=ASK_PURPOSE, run_id=None, task_id=None)
    _record_skip(ctx, f"L1 registry: verified query {entry['name']}", avoided)
    with session_scope() as s:
        vqr.record_hit(s, entry["id"])
    return {"status": "answered", "answered_by": "registry", "verified_query": entry, "parameters": values, "sql": sql,
            "explanation": f"Answered by the verified query '{entry['name']}' (no model call).", "chart": None, "model": None,
            "attempts": [], "result": _result(result)}


def ask_registry(ctx: Any, question: str, parameters: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """The registry rung alone; None is a miss (the model path may answer)."""
    hit = _registry_match(ctx, question, parameters)
    return None if hit is None else _registry_answer(ctx, question, hit)


def _distribution_specs() -> tuple[dict[str, Any], ...]:
    """Count distributions the installed domain packs declare (`ask_distributions` hints): core
    names no table or column; the pack says which entity and dimensions a rule may count."""
    from analystos.capabilities import packs

    return packs.hints().ask_distributions


def _distribution_pattern(spec: dict[str, Any]) -> re.Pattern[str]:
    words = [w for d in spec.get("dimensions") or [] for w in d.get("words") or []] + list(spec.get("ambiguous") or [])
    alt = lambda xs: "|".join(re.escape(str(x).lower()) for x in sorted(xs, key=len, reverse=True))  # noqa: E731
    entity = re.escape(str(spec["entity"]).lower())
    return re.compile(rf"(?:show(?: me)? |give me (?:the )?|what is (?:the )?)?(?:distribution|breakdown) (?:of )?"
                      rf"(?:{alt(spec.get('nouns') or [spec['entity']])}) (?:based on|by|per|across) "
                      rf"(?:{entity} )?({alt(words)})")


def _distribution_plan(ctx: Any, question: str, parameters: dict[str, Any] | None) -> dict[str, Any] | None:
    """Only a complete, unfiltered distribution question for a pack-declared entity is covered.

    Identifiers come from the caller's scope; an ambiguous word (e.g. "type") is never silently
    mapped to a field. Other questions keep the verified-query/model route and its refusals.
    """
    plain = re.sub(r"[^a-z0-9]+", " ", question.lower()).strip()
    for spec in _distribution_specs():
        match = _distribution_pattern(spec).fullmatch(plain)
        if match is None:
            continue
        entity = str(spec["entity"]).lower()
        assets = [fq for fq in ctx.scope.assets if fq.rpartition(".")[2].lower() == entity]
        if len(assets) != 1:
            return None
        asset = assets[0]
        columns = set(getattr(ctx.scope, "columns", {}).get(asset, []))
        denied = set(getattr(ctx.scope, "denied_columns", []))
        dims = spec.get("dimensions") or []
        available = {d["label"]: d["column"] for d in dims
                     if d["column"] in columns and f"{asset}.{d['column']}" not in denied}
        if not available:
            return None
        requested = match.group(1)
        param = f"{entity}_type"
        named = next((d["label"] for d in dims if requested in [str(w).lower() for w in d.get("words") or []]), None)
        if named is not None and named not in available:
            return None
        label = named or str((parameters or {}).get(param) or "")
        return {"asset": asset, "entity": entity, "param": param, "available": available,
                "label": label if label in available else None}
    return None


def _distribution_answer(ctx: Any, question: str, plan: dict[str, Any], parameters: dict[str, Any] | None) -> dict[str, Any]:
    label = plan["label"]
    if label is None:
        choices = list(plan["available"])
        _stage(ctx, "clarify", f"Checking which {plan['entity']} type you mean")
        return {"status": "needs_input", "answered_by": "rules", "model": None, "sql": None, "attempts": [],
                "result": None, "parameters": parameters or {},
                "missing": [{"name": plan["param"], "values": choices}],
                "explanation": f"‘Type’ could mean {' or '.join(choices)}. Choose the field to group by; nothing was guessed."}
    column = plan["available"][label]
    count = f"{plan['entity']}_count"
    sql = (f"SELECT {column}, COUNT(*) AS {count} FROM {plan['asset']} "
           f"GROUP BY {column} ORDER BY {count} DESC")
    _stage(ctx, "execute", f"Counting {plan['entity']} records by {label.lower()} through the read-only query gateway")
    result = ctx.services.gateway.execute(ctx.scope, sql, actor=ask_actor(ctx.user.id), purpose=ASK_PURPOSE,
                                          run_id=None, task_id=None)
    if getattr(ctx, "router", None) is not None:
        from analystos.llm.cache import estimate_tokens

        ctx.router.record_skip("sql_generation", ctx.call_ctx(), estimated_tokens=estimate_tokens(question) + 500,
                               reason=f"L2 rule: {plan['entity']} distribution by {column}", rung="rules")
    return {"status": "answered", "answered_by": "rules", "model": None, "sql": sql, "attempts": [],
            "result": _result(result), "chart": {"type": "bar", "x": column, "y": count},
            "explanation": f"Count of {plan['entity']} records grouped by {label.lower()} (schema-bound rule; no model call)."}


def _rules_answer(ctx: Any, question: str, plan: Any) -> dict[str, Any]:
    """Ladder rung L2 (rules): run the plan `agents/ask_rules` resolved from the catalog, or ask which
    column was meant when a phrase names several. The SQL comes from skills/sqlbuild and runs through
    the gateway; the avoided generation call is recorded as a skip."""
    from analystos.agents import ask_rules
    from analystos.llm.cache import estimate_tokens

    table = plan.table
    avoided = estimate_tokens(compact_json({"question": question, "tables": [
        {"asset": table.fq, "columns": [c.name for c in table.columns]}] if table else []})) + 500
    if plan.status == "clarify":
        (phrase, options), = plan.ambiguous.items()
        _stage(ctx, "clarify", f"'{phrase}' matches more than one column: {', '.join(options)}")
        _record_skip(ctx, f"L2 rules: clarify '{phrase}' ({', '.join(options)})", avoided, rung="rules")
        example = f" For example: \"{plan.suggestions[0]}\"." if plan.suggestions else ""
        return {"status": "clarify", "answered_by": "rules", "sql": None, "model": None, "attempts": [], "result": None,
                "missing": [{"name": phrase, "values": options}], "suggestions": plan.suggestions,
                "explanation": f"'{phrase}' could mean {' or '.join(options)}. Say which one; nothing was guessed.{example}"}
    sql = ask_rules.sql_for(plan)
    _stage(ctx, "execute", "Running it through the query gateway (read-only, within your access)")
    result = ctx.services.gateway.execute(ctx.scope, sql, actor=ask_actor(ctx.user.id), purpose=ASK_PURPOSE, run_id=None, task_id=None)
    _record_skip(ctx, f"L2 rules: {plan.measure[1]} over {table.fq} by {', '.join(a for a, _, _ in plan.dims) or 'nothing'}",
                 avoided, rung="rules")
    return {"status": "answered", "answered_by": "rules", "sql": sql, "explanation": ask_rules.explain(plan),
            "chart": ask_rules.chart_for(plan), "model": None, "attempts": [], "result": _result(result),
            "suggestions": plan.suggestions, "rules": plan.summary()}


def route(ctx: Any, question: str, hit: Any, rejected: list[dict[str, Any]] | None = None,
          *, tool_match: float = 0.0) -> dict[str, Any]:
    """`ask_route` (authority `route`): verified query > tool > generate, decline when a required input
    is missing. The facts are deterministic; the rule answers every case but a close tie. A verified
    query whose words matched but whose SQL does not answer the question (`rejected`) scores 0: it is
    never a candidate, and the decision records why it was passed over."""
    from analystos.decisions.types import Question

    facts = {"verified_match": verified_match_score(hit.score) if hit is not None else 0.0,
             "tool_match": tool_match,
             "missing_inputs": [p["name"] for p in hit.missing] if hit is not None else []}
    if rejected:
        facts["verified_rejected"] = [{k: r[k] for k in ("name", "score", "reasons")} for r in rejected]
    return decide(ctx, "ask_route", {"question": question},
                  Question.choice("How should this analytics question be answered?", ASK_ROUTES, default="generate"), facts)


def clarify(ctx: Any, question: str) -> dict[str, Any]:
    """`clarify_needed` (authority `escalate_only`): ask the user when the question names nothing to
    measure; a model may only add a question, never remove one."""
    from analystos.decisions.types import Question
    from analystos.registries.verified_queries import tokens

    missing = [] if tokens(question) else ["what to measure"]
    q = Question.escalation("Is this analytics question too ambiguous to answer without asking the user?",
                            levels=["answer", "clarify"], baseline="answer", escalate_to="clarify", escalate_at=0.7)
    return {**decide(ctx, "clarify_needed", {"question": question}, q, {"missing_inputs": missing}), "missing_inputs": missing}

def ask(ctx: RunContext, question: str, *, max_repairs: int = 2, parameters: dict[str, Any] | None = None,
        use_registry: bool = True) -> dict[str, Any]:
    out = _ask(ctx, question, max_repairs=max_repairs, parameters=parameters, use_registry=use_registry)
    return label(out)


def label(out: dict[str, Any]) -> dict[str, Any]:
    """Every answer, and the chart it draws, says whether it was compiled from an approved definition
    (`governed`, with the model and compiler versions) or improvised (`ad_hoc`), ADR-0019."""
    out = {"governance": "ad_hoc", **out}
    tag: dict[str, Any] = {"governance": out["governance"]}
    if out["governance"] == "governed":
        semantic = out.get("semantic") or {}
        tag |= {"semantic_model_version": semantic.get("semantic_model_version", semantic.get("model_version")),
                "compiler_version": semantic.get("compiler_version")}
    if isinstance(out.get("chart"), dict):
        out["chart"] = {**out["chart"], **tag}
    return out


SEMANTIC_PURPOSE = "semantic_query"


def _semantic_words(catalog: dict[str, Any]) -> set[str]:
    """Tokens of every metric name, display name, synonym and dimension the planner may use."""
    from analystos.registries.verified_queries import tokens

    words: set[str] = set()
    for name, entry in catalog["metrics"].items():
        d = entry["definition"]
        for text in [name, d.get("display_name") or "", *(d.get("dimensions") or [])]:
            words |= tokens(text.replace(".", " "))
    for phrase in catalog.get("synonyms") or {}:
        words |= tokens(phrase)
    return words


def _model_semantic_query(ctx: Any, question: str, catalog: dict[str, Any]) -> Any:
    """Model rung of `semantic_query` (P7-02): a small model *chooses* a SemanticQuery from the planner
    catalog (names only). Only when the rules rung found nothing and the question names a metric word;
    otherwise the avoided call is recorded. None = not expressible (the ad-hoc path answers)."""
    from pydantic import ValidationError

    from analystos.agents.common import model_gate
    from analystos.contracts.semantic import SemanticQuery
    from analystos.registries.verified_queries import tokens

    if getattr(ctx, "router", None) is None or not catalog["metrics"]:
        return None
    related = bool(tokens(question) & _semantic_words(catalog))
    payload = {"question": question, "metrics": {n: {k: e["definition"].get(k) for k in ("display_name", "description",
                                                                                          "dimensions", "format")}
                                                 for n, e in sorted(catalog["metrics"].items())},
               "time_dimensions": sorted({f"{d['name']}.{f['name']}" for d in catalog["datasets"] for f in d.get("fields", [])
                                          if isinstance(f.get("dimension"), dict) and f["dimension"].get("is_time")})}
    if not model_gate(ctx, SEMANTIC_PURPOSE, payload, deterministic_ok=not related):
        return None

    def check(data: Any) -> str | None:
        if not isinstance(data, dict) or "semantic_query" not in data:
            return "answer must be {\"semantic_query\": {...} | null}"
        if data["semantic_query"] is None:
            return None
        try:
            chosen = SemanticQuery.model_validate(data["semantic_query"])
        except ValidationError as exc:
            return f"not a SemanticQuery: {exc.errors()[0]['msg']}"
        unknown = [m for m in chosen.metrics if m not in catalog["metrics"]]
        return f"unknown metrics {unknown}" if unknown else None

    _stage(ctx, "semantic", "Matching the question to the approved metrics")
    data, _model = llm_json(ctx, SEMANTIC_PURPOSE, "semantic_query.v1", payload, validate=check)
    if data is None or check(data) is not None or data.get("semantic_query") is None:
        return None
    return SemanticQuery.model_validate(data["semantic_query"])


def _semantic_answer(ctx: Any, question: str, parameters: dict[str, Any] | None) -> dict[str, Any] | None:
    """Governed rung (ADR-0019): an explicit `semantic_query`, the rules rung (exact metric / synonym
    match) or the `semantic_query` model's choice is compiled against the approved model version.
    None means not expressible here: the caller's ad-hoc path answers, labelled `ad_hoc`."""
    from pydantic import ValidationError

    from analystos.contracts.semantic import SemanticQuery
    from analystos.core.ids import stable_hash
    from analystos.semantic.compiler import compile_query, match_question, planner_catalog

    catalog = getattr(ctx, "semantic_catalog", None)
    explicit = (parameters or {}).get("semantic_query")
    if explicit is not None and set(parameters) != {"semantic_query"}:
        raise InvalidInput("Put all metric query options inside semantic_query.")
    if not catalog:
        if explicit is not None:
            raise InvalidInput("An approved semantic model is required for a metric query.")
        return None
    if parameters and explicit is None:
        return None
    visible = planner_catalog(catalog, ctx.scope)  # masked fields and out-of-scope datasets are absent
    chosen_by = "explicit" if explicit is not None else "rules"
    try:
        query = SemanticQuery.model_validate(explicit) if explicit is not None else match_question(question, visible)
    except ValidationError as exc:
        raise InvalidInput("The metric query contains invalid fields or values.") from exc
    if query is None:
        query, chosen_by = _model_semantic_query(ctx, question, visible), "model"
        if query is None:
            return None
    _stage(ctx, "semantic", "Compiling the approved metric definition" + (" (no model call)" if chosen_by != "model" else ""))
    try:
        compiled = compile_query(query, catalog, ctx.scope)
    except InvalidInput as exc:
        # A model's choice the compiler cannot use (an unknown name) is not an answer: the ad-hoc path
        # answers. A governance refusal (fan-out, unvalidated join) is the answer, whoever chose the query.
        if chosen_by == "model" and "edge" not in exc.details:
            _stage(ctx, "semantic", f"The metric query could not be compiled ({exc.message[:120]}); answering ad hoc")
            return None
        raise
    compiled.provenance["policy_hash"] = stable_hash(ctx.policy.model_dump(mode="json"))
    compiled.provenance["chosen_by"] = chosen_by
    _stage(ctx, "execute", "Running the approved calculation through the query gateway")
    result = ctx.services.gateway.execute(ctx.scope, compiled.sql, actor=ask_actor(ctx.user.id),
                                          purpose=ASK_PURPOSE, run_id=None, task_id=getattr(ctx, "turn_id", None))
    _record_skip(ctx, "Approved metric compiled deterministically", 500, rung="rules")
    if chosen_by == "rules":
        router = getattr(ctx, "router", None)
        if router is not None:
            router.record_skip(SEMANTIC_PURPOSE, ctx.call_ctx(), estimated_tokens=500, reason="rules: exact metric match",
                               rung="rules")
    chart = _semantic_chart(query)
    return {"status": "answered", "answered_by": "semantic", "governance": "governed", "semantic": compiled.provenance,
            "sql": compiled.sql, "explanation": "Calculated from the approved metric definition: " + ", ".join(query.metrics),
            "chart": chart, "model": None, "attempts": [], "decisions": [], "route": "semantic", "result": _result(result)}


def _semantic_chart(query: Any) -> dict[str, Any] | None:
    """A presentation hint for a governed answer, labelled like the answer it draws."""
    if len(query.metrics) != 1 or len(query.dimensions) + (1 if query.time and query.time.grain else 0) > 1:
        return None
    if query.time and query.time.grain:
        return {"type": "line", "x": query.time.dimension, "y": query.metrics[0]}
    if query.dimensions:
        return {"type": "bar", "x": query.dimensions[0], "y": query.metrics[0]}
    return {"type": "kpi", "x": None, "y": query.metrics[0]}


def ask_dialect(ctx: Any) -> str:
    return next(iter(ctx.scope.source_dialects.values()), "postgres")


def generation_context(ctx: Any, asked_text: str, *, redacted: bool = False) -> tuple[Any, dict[str, Any], list[dict[str, Any]]]:
    """(compiled context, required inputs, catalog) of Ask's SQL generation for an already-redacted question: the
    one path for the model call and for the context preview (context/preview.py)."""
    catalog = catalog_for_prompt(ctx, objective=asked_text, capped=False)
    required: dict[str, Any] = {"question": asked_text}  # the dialect is named once, in the system text
    memory = getattr(ctx, "conversation_memory", None)
    if memory and memory.get("clarifications"):
        # S04: the user's own corrections (session, personal, approved team), as labelled data, never instructions
        required["conversation_clarifications"] = memory["clarifications"]
    if redacted:
        required["redacted_values"] = QUESTION_MODEL_INSTRUCTION
    compiled = compile_for(ctx, "sql_generation", required, objective=asked_text, catalog=catalog, reference_text=asked_text)
    return compiled, required, catalog


def _ask(ctx: RunContext, question: str, *, max_repairs: int = 2, parameters: dict[str, Any] | None = None,
        use_registry: bool = True) -> dict[str, Any]:
    """NL question -> governed SQL -> result. Tool-first: `ask_route` sends a registry match to the
    verified query (or declines for a missing parameter) before any model is asked; `clarify_needed`
    may stop an ambiguous question before generation. The decisions choose a path only: the SQL
    still goes through the gateway validator and scope. Repairs use the gateway's rejection message.
    Gate and budget are checked before any model call, and the budget again before every attempt."""
    _stage(ctx, "scope", "Checking what you are allowed to see")
    _authorize_ask(ctx)
    _check_budget(ctx)
    from analystos.agents.ask_samples import sample_answer

    sample = sample_answer(ctx, question)
    if sample is not None:
        return sample
    semantic = _semantic_answer(ctx, question, parameters)
    if semantic is not None:
        return semantic
    hit, rejected = None, []
    if use_registry:
        _stage(ctx, "registry", "Looking for a verified answer to this question")
        found = _registry_lookup(ctx, question, parameters)
        hit, rejected = found.hit, found.rejected
        if rejected and hit is None:
            _stage(ctx, "registry", f"A verified query is close but does not answer this: {rejected[0]['reasons'][0]}",
                   rejected=[r["name"] for r in rejected])
    plan = _distribution_plan(ctx, question, parameters)
    rules = None
    if plan is None and hit is None:
        from analystos.agents.ask_rules import plan_for

        rules = plan_for(ctx, question)
    chosen = route(ctx, question, hit, rejected, tool_match=1.0 if (plan or rules) is not None and hit is None else 0.0)
    decisions = [chosen]
    path = chosen["value"]
    if (path == "tool" and plan is None and rules is None) or (path in ("verified_query", "decline") and hit is None):
        path = "verified_query" if hit is not None else "generate"  # nothing to run on that rung: the next one
    _stage(ctx, "route", {"verified_query": "Answering from the verified query registry",
                          "decline": "A required input is missing",
                          "tool": "Answering with a schema-bound rule built from the catalog (no model)",
                          "generate": "No verified answer fits: writing new SQL"}[path], route=chosen["value"])
    if path in ("verified_query", "decline"):
        out = _registry_answer(ctx, question, hit)
        return {**out, "route": chosen["value"], "decisions": decisions}
    if path == "tool":
        out = _distribution_answer(ctx, question, plan, parameters) if plan is not None else _rules_answer(ctx, question, rules)
        return {**out, "route": chosen["value"], "decisions": decisions}
    if WRITE_REQUEST.search(question):
        _stage(ctx, "scope", "Ask only reads data: this asks for a change")
        raise NotAnswerable("Ask only reads data; this question asks to change it. Nothing was changed or guessed.",
                            details={"reason": "write", "by": "rules"})
    asked = clarify(ctx, question)
    decisions.append(asked)
    if asked["value"] == "clarify":
        _stage(ctx, "clarify", "The question needs more detail before it can be answered")
        return {"status": "clarify", "answered_by": None, "route": chosen["value"], "decisions": decisions, "sql": None,
                "model": None, "attempts": [], "result": None,
                "missing": [{"name": n} for n in asked["missing_inputs"]],
                "explanation": "This question is too open to answer safely. Say what to measure (a count, a rate, an "
                               "average), over which records and period, and how to group it."}
    # P7-10: account/customer numbers, cards, SSNs, IBANs and e-mails typed into the question leave as
    # tokens; the model writes the token and the real value is restored here, before the gateway.
    rq = redact_question(question)
    asked_text = rq.text
    _stage(ctx, "context", "Finding the tables that answer this")
    generation, required, catalog = generation_context(ctx, asked_text, redacted=bool(rq.redacted))
    dialect = ask_dialect(ctx)
    _stage(ctx, "generate", "Writing the SQL")
    data, model = llm_json(ctx, "sql_generation", "sql_generation.v1", generation, prompt_vars={"dialect": dialect},
                           validate=sql_or_refusal)
    refused = cannot_answer(data)
    if refused is not None:  # a model may withhold an answer (never widen one): ask back, or decline with its reason
        return _model_refusal(ctx, refused, chosen, decisions, model)
    if has_sql(data):  # no data, or an answer without a SQL statement: say which cause stopped it
        raise _generation_unavailable(model if data is None else ModelOutcome(
            "invalid_output", "the model answered without a SQL statement", purpose="sql_generation"))
    attempts = []
    sql = restore_values(str(data["sql"]), rq.values)
    escalated = False
    attempt = 0
    known = _known_values(ctx)
    while True:
        if attempt:
            _check_budget(ctx)  # every attempt counts; over budget ends the loop (no repair)
        problem = placeholder_problem(sql, dialect)
        if problem:  # not repairable: a repair would only dress the placeholder differently
            _stage(ctx, "check", f"The written SQL does not answer from data: {problem}")
            raise NotAnswerable(f"The data in scope does not answer this question ({problem}). Nothing was guessed.",
                                details={"reason": "placeholder", "by": "rules"})
        try:
            checked = check_literals(sql, dialect, known)  # 'yes' where the data says 'Yes': fixed here, or repaired
            if checked.rewrites:
                sql = checked.sql
                decisions.append({"kind": "literal_case", "value": checked.rewrites})
                _stage(ctx, "check", "Matched the letter case of compared values to the data: " + "; ".join(checked.rewrites))
            _stage(ctx, "execute", "Running it through the query gateway (read-only, within your access)")
            result = ctx.services.gateway.execute(ctx.scope, sql, actor=ask_actor(ctx.user.id), purpose=ASK_PURPOSE,
                                                  run_id=None, task_id=None)
            explanation = data.get("explanation")
            if isinstance(explanation, str):
                explanation = restore_values(explanation, rq.values)
            return {"status": "answered", "answered_by": "model", "sql": sql, "explanation": explanation,
                    "chart": data.get("chart"), "model": model, "attempts": attempts, "result": _result(result),
                    "route": chosen["value"], "decisions": decisions}
        except (SQLRejected, AnalystOSError) as exc:
            attempts.append({"sql": sql, "error": exc.message})
            if attempt >= max_repairs:
                if escalated or not isinstance(exc, SQLRejected):
                    raise
                # Cheap first, escalate: the gateway still rejects the small tier's SQL after its repairs,
                # so the large tier writes it once more. The gateway validates that answer like any other.
                escalated = True
                better, better_model = llm_json(ctx, "sql_generation", "sql_generation.v1", generation,
                                                prompt_vars={"dialect": dialect}, validate=has_sql,
                                                escalate=f"sql_rejected after {attempt} repairs: {exc.message}"[:200],
                                                escalated_from=model)
                if has_sql(better):
                    raise
                _stage(ctx, "escalate", f"The gateway still refused the SQL ({exc.message[:120]}); a stronger model rewrites it",
                       model=better_model)
                data, model, sql = better, better_model, restore_values(str(better["sql"]), rq.values)
                attempt += 1
                continue
            _stage(ctx, "repair", f"The gateway refused the SQL ({exc.message[:120]}); repairing it")
            sent_sql, sent_error = tokenize_values(sql, rq.values), tokenize_values(exc.message, rq.values)
            fix, _ = llm_json(ctx, "sql_repair", "sql_repair.v1",
                              defer_compile(ctx, "sql_repair", {**required, "sql": sent_sql, "error": sent_error},
                                            objective=asked_text, catalog=catalog,
                                            reference_text=f"{sent_sql}\n{sent_error}"), validate=has_sql)
            if has_sql(fix):
                raise
            sql = restore_values(str(fix["sql"]), rq.values)
            attempt += 1


def _model_refusal(ctx: Any, refused: dict[str, str], chosen: dict[str, Any], decisions: list[dict[str, Any]],
                   model: Any) -> dict[str, Any]:
    """The model said it cannot answer: ambiguous -> clarify, missing_input -> needs_input, otherwise a
    `not_answerable` refusal. Every branch withholds the answer, so the model cannot grant anything here."""
    kind, reason = refused["kind"], refused["reason"]
    base = {"answered_by": None, "route": chosen["value"], "decisions": decisions, "sql": None, "model": model,
            "attempts": [], "result": None}
    if kind == "ambiguous":
        _stage(ctx, "clarify", "The question needs more detail before it can be answered")
        return {**base, "status": "clarify", "missing": [{"name": "what to measure"}],
                "explanation": reason or "This question is too open to answer safely. Say what to measure (a count, a "
                                         "rate, an average), over which records and period, and how to group it."}
    if kind == "missing_input":
        _stage(ctx, "clarify", "The question needs a specific value")
        return {**base, "status": "needs_input", "missing": [{"name": reason or "a specific value"}],
                "explanation": f"This question needs a specific value ({reason or 'say which one'}). Nothing was guessed."}
    raise NotAnswerable(f"The data in scope cannot answer this: {reason or kind}. Nothing was guessed.",
                        details={"reason": kind, "by": "model"})


def _known_values(ctx: Any) -> dict[str, set[str]]:
    """Column name -> the complete set of text values the profile measured, over the caller's tables (never a
    sensitive or denied column): what `skills/literals.check_literals` holds model-written comparisons to."""
    denied = set(getattr(ctx.scope, "denied_columns", None) or [])
    out: dict[str, set[str]] = {}
    try:
        rows = asset_rows(ctx)
    except Exception:  # noqa: BLE001 - no catalog: nothing to check against
        return {}
    for a, cols in rows:
        fq = f"{a.schema_name}.{a.name}"
        for c in cols:
            p = c.profile or {}
            if f"{fq}.{c.name}" in denied or sensitive(c) or not p.get("values_complete") or not p.get("values")                     or normalize_type(c.data_type) != "text":
                continue
            out.setdefault(c.name.lower(), set()).update(str(v) for v in p["values"])
    return out


def _generation_unavailable(outcome: Any) -> ModelUnavailable:
    """The one reason generation got no SQL (ModelOutcome from llm_json), as the Ask refusal carries it."""
    reason = getattr(outcome, "code", None) or str(outcome or "no_route")
    message = getattr(outcome, "message", None) or f"SQL generation unavailable ({reason})"
    return ModelUnavailable(f"SQL generation unavailable: {message}",
                            details={**(getattr(outcome, "details", None) or {}), "reason": reason})


def dataset_def(run_id: str) -> tuple[DatasetDef, dict]:
    content = None
    with session_scope() as s:
        from analystos.db.models import Artifact

        art = s.scalar(select(Artifact).where(Artifact.run_id == run_id, Artifact.type == "dataset").order_by(Artifact.created_at.desc()))
        if art is None:
            raise InvalidInput("no dataset artifact for this run")
        content = dict(art.content)
        content["artifact_id"] = art.id
    return DatasetDef.model_validate({k: content[k] for k in DatasetDef.model_fields if k in content}), content


__all__ = ["ask", "build_dataset", "dataset_def", "derivation_alias", "task_output"]
