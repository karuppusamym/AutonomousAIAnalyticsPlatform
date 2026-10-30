"""Resolve business meaning before asking people; reviewed knowledge never grants authority."""
from __future__ import annotations

import json
import re
from datetime import date
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select

from analystos.artifacts.registry import link, save_artifact
from analystos.context.compiler import terms
from analystos.contracts.grounding import BindingIn
from analystos.core.errors import InvalidInput, PreconditionFailed
from analystos.core.ids import new_id, stable_hash
from analystos.db.models import ContextBinding, ContextEntry, Source, SourceAsset, SourceColumn, User, WorkspaceMember
from analystos.events.bus import emit
from analystos.governance.audit import audit
from analystos.governance.policy import load_in_workspace, require_role, resolve_scope, scoped_loader
from analystos.security.injection import is_injection


def allowed_ref(ref: str, scope: Any) -> bool:
    asset, sep, col = str(ref).rpartition(".")
    return bool(sep and asset in scope.assets and col in scope.columns.get(asset, [])
                and ref not in scope.denied_columns and f"*.{col}" not in scope.denied_columns)


def binding_state(body: dict, scope: Any, *, today: date | None = None) -> str:
    today = (today or date.today()).isoformat()
    if body.get("state") == "retired" or (body.get("expires_on") and body["expires_on"] < today):
        return "outdated"
    if body.get("effective_from") and body["effective_from"] > today:
        return "not_yet_effective"
    if body.get("source_ids") and not set(body["source_ids"]) <= set(scope.source_ids):
        return "unavailable"
    for ref in body.get("columns", []):
        asset, _, column = ref.rpartition(".")
        if asset not in scope.assets or ref in scope.denied_columns or f"*.{column}" in scope.denied_columns:
            return "unavailable"
        if column not in scope.columns.get(asset, []):
            return "outdated"
    return "resolved" if body.get("state") == "approved" else "draft"


def scoped_bindings(session, scope) -> list[ContextBinding]:
    rows = list(session.scalars(select(ContextBinding).where(ContextBinding.workspace_id == scope.workspace_id)
                               .order_by(ContextBinding.term, ContextBinding.id)))
    # Never reveal definitions containing references outside the caller's scope, even in the studio.
    return [r for r in rows if binding_state(r.definition, scope) != "unavailable"]


def domains_for(session, scope):
    from analystos.capabilities import packs
    from analystos.governance.policy import get_workspace, load_policy

    return {p.name for p in packs.for_scope(scope, load_policy(session, get_workspace(session, scope.workspace_id)))}


@scoped_loader
def save_binding(session, user, workspace_id, body: BindingIn, binding_id=None, revision=None):
    require_role(session, user, workspace_id, "editor")
    scope = resolve_scope(session, session.merge(user), workspace_id, minimum_role="editor")
    if body.state == "approved" and body.domain and body.domain not in domains_for(session, scope):
        raise InvalidInput("Select an applicable domain pack or leave domain empty for scoped workspace knowledge")
    if any(not allowed_ref(c, scope) for c in body.columns):
        raise InvalidInput("Every mapping must name an available, authorized catalog column")
    if body.source_ids and not set(body.source_ids) <= set(scope.source_ids):
        raise InvalidInput("Source applicability exceeds the authorized scope")
    mapped_sources = {scope.asset_sources[c.rsplit(".", 1)[0]] for c in body.columns if c.rsplit(".", 1)[0] in scope.asset_sources}
    if body.source_ids and not mapped_sources <= set(body.source_ids):
        raise InvalidInput("Source applicability must include every mapped column's source")
    if not body.source_ids and body.columns:
        source_ids = sorted({scope.asset_sources[c.rsplit(".", 1)[0]] for c in body.columns
                             if c.rsplit(".", 1)[0] in scope.asset_sources})
        body = body.model_copy(update={"source_ids": source_ids})
    if is_injection(body.definition) or is_injection(body.term):
        raise InvalidInput("Definitions must describe business meaning, not instruct a model")
    if body.owner_id and not session.scalar(select(WorkspaceMember).join(User, User.id == WorkspaceMember.user_id).where(
            WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == body.owner_id, User.active.is_(True))):
        raise InvalidInput("Owner must be an active workspace member")
    if body.kind != "policy" and body.state == "approved" and not body.columns:
        raise InvalidInput("Approved analytical definitions need explicit column mappings")
    if body.metric and (body.metric.outcome_column not in body.columns or (body.metric.time_column and body.metric.time_column not in body.columns)):
        raise InvalidInput("Metric outcome and time columns must be explicitly mapped")
    if body.metric and body.metric.end_column and (body.metric.end_column not in body.columns or
            body.metric.end_column.rsplit(".", 1)[0] != body.metric.outcome_column.rsplit(".", 1)[0]):
        raise InvalidInput("Duration start and end must be mapped columns from the same table")
    if body.predicate and body.predicate.column not in body.columns:
        raise InvalidInput("The rule predicate must use a mapped column")
    if binding_id:
        row = load_in_workspace(session, ContextBinding, binding_id, workspace_id, user=user, label="mapping")
        session.refresh(row, with_for_update=True)
        if row.revision != revision:
            raise PreconditionFailed("This mapping changed; reload it before saving")
        row.revision += 1
    else:
        row = ContextBinding(id=new_id("bind"), workspace_id=workspace_id, revision=1, created_by=user.id)
        session.add(row)
    row.term, row.state, row.definition = body.term, body.state, body.model_dump(mode="json")
    entry = session.get(ContextEntry, row.entry_id) if row.entry_id else None
    if entry is None:
        from analystos.context.service import add_entry
        entry = add_entry(session, workspace_id=workspace_id, kind="metric" if body.kind == "metric" else "rule"
                          if body.kind in {"population_rule", "quality_rule", "policy"} else "definition",
                          name=body.term, body=body.definition, origin="reviewed_mapping", trusted=body.state == "approved")
        row.entry_id = entry.id
    entry.name, entry.mapped_columns, entry.trusted = body.term, body.columns, body.state == "approved"
    # Applicability and rule changes must invalidate the existing knowledge-version digest too.
    entry.body = body.definition + "\nReviewed contract: " + json.dumps(body.model_dump(mode="json"), sort_keys=True)
    from analystos.context.embeddings import embed
    entry.embedding = embed(body.term + " " + body.definition)
    session.flush()
    art = save_artifact(session, workspace_id=workspace_id, run_id=None, type_="context_package",
                        name=f"Reviewed mapping {row.term} v{row.revision}",
                        content={"binding_id": row.id, "revision": row.revision, "definition": row.definition}, creator_agent=None)
    link(session, workspace_id, ("context_binding", row.id), "version", (art.type, art.id))
    emit(workspace_id, "context.binding_saved", {"id": row.id, "revision": row.revision, "state": row.state}, session=session)
    audit(f"user:{user.id}", "context.binding_saved", workspace_id=workspace_id, target=row.id, decision="allow",
          details={"revision": row.revision, "state": row.state})
    return row


def filter_entries(session, workspace_id, entries, assets=None, denied_columns=()):
    if not any(e.origin == "reviewed_mapping" for e in entries):
        return entries
    bindings = {r.entry_id: r for r in session.scalars(select(ContextBinding).where(ContextBinding.workspace_id == workspace_id))}
    if not bindings:
        return entries
    all_assets = list(session.scalars(select(SourceAsset).where(SourceAsset.workspace_id == workspace_id, SourceAsset.selected.is_(True))))
    selected = set(assets if assets is not None else [f"{a.schema_name}.{a.name}" for a in all_assets])
    columns = {f"{a.schema_name}.{a.name}": [c.name for c in session.scalars(select(SourceColumn).where(SourceColumn.asset_id == a.id))]
               for a in all_assets if f"{a.schema_name}.{a.name}" in selected}
    scope = SimpleNamespace(workspace_id=workspace_id, assets=list(selected), columns=columns,
                            source_ids=list({a.source_id for a in all_assets if f"{a.schema_name}.{a.name}" in selected}),
                            denied_columns=denied_columns)
    domains = domains_for(session, scope)
    return [e for e in entries if e.id not in bindings or (binding_state(bindings[e.id].definition, scope) == "resolved"
            and (not bindings[e.id].definition.get("domain") or bindings[e.id].definition["domain"] in domains))]


def pinned_prompt_context(run, scope):
    pinned = ((getattr(run, "origin", None) or {}).get("reviewed_context") or {}) if run else {}
    if not pinned or scope is None:
        return {}, []
    from analystos.services.investigation_value import visible_facts

    facts = [f for f in visible_facts(pinned.get("facts", []), scope) if not is_injection(json.dumps(f, default=str))]
    receipts = [{"id": f"brief:{scope.workspace_id}:{f.get('key', n)}", "section": "reviewed_business_context",
                 "source": "reviewed_brief", "sha256": stable_hash(f), "version": pinned.get("version")}
                for n, f in enumerate(facts)]
    return {"version": pinned.get("version"), "content_hash": pinned.get("content_hash"), "facts": facts,
            "withheld_count": len(pinned.get("facts", [])) - len(facts)}, receipts


@scoped_loader
def check_binding(session, user, workspace_id, binding_id, *, gateway=None):
    """Bounded structural checks; passing them never approves business meaning or a join."""
    import sqlglot

    from analystos.runtime.context import default_gateway

    require_role(session, user, workspace_id, "analyst")
    binding = load_in_workspace(session, ContextBinding, binding_id, workspace_id, user=user, label="mapping")
    scope = resolve_scope(session, session.merge(user), workspace_id, minimum_role="analyst")
    refs = binding.definition.get("columns", [])
    if not refs or any(not allowed_ref(ref, scope) for ref in refs):
        raise InvalidInput("Choose currently available authorized columns before checking a mapping")
    gateway = gateway or default_gateway()
    checks = []
    def quote(value):
        return '"' + value.replace('"', '""') + '"'
    for ref in refs[:10]:
        asset, column = ref.rsplit(".", 1)
        qualified = ".".join(quote(p) for p in asset.split("."))
        dialect = scope.source_dialects.get(scope.asset_sources.get(asset, ""), "postgres")
        predicate = binding.definition.get("predicate")
        extra = ""
        if predicate and predicate["column"] == ref:
            from analystos.contracts.analysis import Filter
            from analystos.skills.sqlbuild import render_filter

            op = {"not_in": "not in"}.get(predicate["op"], predicate["op"])
            expression = render_filter(Filter(column="v", op=op, value=predicate["value"]), "postgres")
            extra = f", SUM(CASE WHEN {expression} THEN 1 ELSE 0 END) AS matching_rows"
        sql = f"SELECT COUNT(*) AS sampled_rows, COUNT(v) AS nonnull_rows, COUNT(DISTINCT v) AS distinct_values{extra} FROM (SELECT {quote(column)} AS v FROM {qualified} LIMIT 1000) sample"
        sql = sqlglot.transpile(sql, read="postgres", write=dialect)[0]
        result = gateway.execute(scope, sql, actor=f"user:{user.id}", purpose="context.mapping_check", max_rows=1, retain_rows=False)
        checks.append({"column": ref, "query_id": result.query_id, "sampled_rows": result.rows[0][0],
                       "nonnull_rows": result.rows[0][1], "distinct_values": result.rows[0][2]})
        if extra:
            checks[-1]["matching_rows"] = result.rows[0][3]
    content = {"binding_id": binding.id, "binding_revision": binding.revision, "checks": checks, "sample_limit": 1000,
               "note": "Bounded structural sample checks, not proof of business meaning, whole-table uniqueness or example correctness"}
    art = save_artifact(session, workspace_id=workspace_id, type_="context_package", name=f"Mapping check {binding.id} v{binding.revision}",
                        content=content, creator_user=user.id)
    link(session, workspace_id, ("context_binding", binding.id), "checked_by", (art.type, art.id))
    for check in checks:
        link(session, workspace_id, (art.type, art.id), "uses_query", ("query", check["query_id"]))
    emit(workspace_id, "context.binding_validated", {"id": binding.id, "revision": binding.revision}, session=session)
    audit(f"user:{user.id}", "context.binding_validated", workspace_id=workspace_id, target=binding.id, decision="allow")
    return content


def catalog(session, scope):
    out = []
    for a in session.scalars(select(SourceAsset).where(SourceAsset.workspace_id == scope.workspace_id)):
        fq = f"{a.schema_name}.{a.name}"
        if fq not in scope.assets:
            continue
        source = session.get(Source, a.source_id)
        for c in session.scalars(select(SourceColumn).where(SourceColumn.asset_id == a.id)):
            ref = f"{fq}.{c.name}"
            if allowed_ref(ref, scope):
                out.append({"ref": ref, "name": c.business_name or c.name, "type": c.semantic_type or c.data_type,
                            "source_id": a.source_id, "source_name": source.name if source else a.source_id,
                            "description": (c.description or "")[:400] if c.description_origin != "model" or (c.semantics or {}).get("reviewed") else ""})
    return out


def candidates(session, scope):
    from analystos.knowledge.entries import visible_entries
    entries = visible_entries(session, scope.workspace_id, kinds=["term", "definition", "metric", "rule", "note"])
    entries = filter_entries(session, scope.workspace_id, entries, scope.assets, scope.denied_columns)
    out = []
    for e in entries:
        refs = []
        for mapping in e.mapped_columns:
            refs += [f"{a}.{c}" for a, cols in scope.columns.items() for c in cols
                     if (f"{a}.{c}" == mapping or f"{a}.{c}".endswith("." + mapping)) and allowed_ref(f"{a}.{c}", scope)]
        if e.trusted and e.origin not in {"agent", "model"} and not is_injection(e.body) and not is_injection(e.name):
            out.append({"id": e.id, "term": e.name, "definition": e.body[:2000], "columns": sorted(set(refs)),
                        "synonyms": list(e.synonyms), "origin": e.origin, "kind": e.kind})
    return out


def resolve_known(question, known, bindings, scope):
    from analystos.services.meaning_review import focus_terms, norm_terms

    focus = focus_terms(question)  # singular forms too: the owner's "Late delivery" answers "late deliveries"
    results = []
    for item in known:
        names = [item["term"], *item.get("synonyms", [])]
        if not any((terms(n) and (terms(n) <= focus or norm_terms(n) <= focus)) or (n and re.search(r"(?<!\w)" + re.escape(n) + r"(?!\w)", question, re.I)) for n in names):
            continue
        refs = item["columns"]
        state = "resolved" if refs else "missing"
        if any(len([r for r in refs if r.rsplit('.', 1)[-1] == name]) > 1 for name in {r.rsplit('.', 1)[-1] for r in refs}):
            state = "ambiguous"
        results.append({**item, "state": state, "by": "reviewed_knowledge"})
    # Pack vocabulary matched for this question (by its term or a synonym): an owner mapping of the same term
    # applies even when the question uses only the synonym ("revenue" for the pack's "Net revenue").
    matched_known = {" ".join(sorted(terms(r["term"]))) for r in results}
    for row in bindings:
        if ((terms(row.term) and (terms(row.term) <= focus or norm_terms(row.term) <= focus)) or " ".join(sorted(terms(row.term))) in matched_known
                or (row.definition.get("kind") == "population_rule" and row.state == "approved")):
            d = row.definition
            state = binding_state(d, scope)
            active_same_term = any(other.id != row.id and terms(other.term) == terms(row.term)
                                   and binding_state(other.definition, scope) == "resolved" for other in bindings)
            if state != "resolved" and active_same_term:
                continue
            if state == "draft" and any(terms(r["term"]) == terms(row.term) and r["state"] == "resolved" for r in results):
                continue
            results = [r for r in results if r["id"] != row.entry_id]
            results.append({"id": row.id, "term": row.term, "definition": d["definition"], "columns": d["columns"],
                            "state": binding_state(d, scope), "by": "reviewed_mapping", "revision": row.revision,
                            "kind": d["kind"], "metric": d.get("metric"), "predicate": d.get("predicate")})
    # Contradictory current definitions need review; a model cannot overrule an owner.
    # A source-specific owner mapping supersedes generic pack vocabulary for the same term.
    explicit = {" ".join(sorted(terms(r["term"]))) for r in results if r.get("by") == "reviewed_mapping"}
    results = [r for r in results if r.get("by") == "reviewed_mapping" or " ".join(sorted(terms(r["term"]))) not in explicit]
    groups = {}
    for r in results:
        if r["state"] == "resolved":
            groups.setdefault(" ".join(sorted(terms(r["term"]))), []).append(r)
    for group in groups.values():
        if len({meaning_identity(r) for r in group}) > 1:
            for r in group:
                r["state"] = "ambiguous"
    return results


def meaning_identity(item):
    return stable_hash({"definition": item["definition"], "columns": sorted(item["columns"]),
                        "metric": item.get("metric"), "predicate": item.get("predicate")})


def existing_metric_view(proposal, resolutions):
    """A grouped view of an existing rate is not permission to invent a new metric formula."""
    if proposal.get("missing_contract_fields") or not proposal.get("columns"):
        return False
    text = (proposal.get("term", "") + " " + proposal.get("definition", "")).lower()
    if re.search(r"\b(weighted|adjusted|score|risk|rolling|calendar|window|eligibility|eligible|excluding|exclude|threshold|growth|margin|delta|ratio)\b", text):
        return False
    resolved = [r for r in resolutions if r.get("state") == "resolved"]
    available_refs = {c for r in resolved for c in r.get("columns", [])}
    if not set(proposal["columns"]) <= available_refs:
        return False
    for meaning in resolved:
        if not terms(meaning["term"]) <= terms(proposal["term"]):
            continue
        permitted = terms("rate share proportion breakdown by across") | {t for r in resolved for t in terms(r["term"])}
        if not terms(proposal["term"]) <= permitted:
            continue
        if meaning.get("kind") == "metric":
            if terms(proposal["term"]) == terms(meaning["term"]) or re.search(r"\b(by|across|breakdown)\b", proposal["term"], re.I):
                return True
            continue
        if re.search(r"\b(rate|proportion|share)\b", text):
            for ref in meaning.get("columns", []):
                column = re.escape(ref.rsplit(".", 1)[-1])
                if re.search(r"\b" + column + r"\s*=\s*(false|true)\b", meaning["definition"], re.I):
                    return True
    return False


def resolve(session, user, workspace_id, body, *, router=None, call_ctx=None, reviewed_context=None, agent_ctx=None):
    scope = resolve_scope(session, session.merge(user), workspace_id, source_ids=body.source_ids, minimum_role="viewer")
    known = candidates(session, scope)
    from analystos.services.investigation_value import visible_facts
    for n, fact in enumerate(visible_facts((reviewed_context or {}).get("facts", []), scope)):
        text = str(fact.get("value", ""))
        if is_injection(text):
            continue
        refs = [f"{a}.{c}" for a, cols in scope.columns.items() for c in cols
                if allowed_ref(f"{a}.{c}", scope) and re.search(r"(?<!\w)" + re.escape(c) + r"(?!\w)", text, re.I)]
        known.append({"id": f"brief:{scope.workspace_id}:{fact.get('key', n)}", "term": str(fact.get("subject") or fact.get("field")),
                      "definition": text[:2000], "columns": refs, "synonyms": [], "origin": "reviewed_brief"})
    domains = domains_for(session, scope)
    bindings = [b for b in scoped_bindings(session, scope) if not b.definition.get("domain") or b.definition["domain"] in domains]
    results = resolve_known(body.question, known, bindings, scope)
    from analystos.services import meaning_review as mr

    plan_text = _plan_text(agent_ctx)
    cat = catalog(session, scope)
    mr.map_glossary_to_catalog(results, cat)
    # Plain table fields named by the question or plan resolve from the catalog, never by asking (PILOT-FIX-04).
    fields = mr.catalog_fields(body.question + "\n" + plan_text, cat, results)
    model_status, missing, proposals, proposed_filters, open_terms = "not_requested", [], [], [], []
    if body.use_model:
        from analystos.runtime.context import default_router, workspace_call_ctx
        router = router or default_router()
        call_ctx = call_ctx or workspace_call_ctx(workspace_id, agent_id="catalog_steward", prompt_version="context_resolution.v1")
        if router.mode("glossary_suggestion") != "off" and router.available("glossary_suggestion", call_ctx):
            from analystos.core.errors import AnalystOSError
            try:
                payload = {"question": body.question, "plan_questions": plan_text[:2000], "known": known[:100],
                           "catalog": cat[:300], "already_resolved": results + [f for f in fields if f["state"] == "resolved"]}
                if agent_ctx is not None:
                    from analystos.agents.common import compile_for, llm_json

                    compiled = compile_for(agent_ctx, "glossary_suggestion", payload, catalog=[])
                    data, model = llm_json(agent_ctx, "glossary_suggestion", "context_resolution.v1", compiled, max_tokens=2500)
                    answer = SimpleNamespace(data=data)
                    if data is None:
                        model_status = str(model)
                else:
                    from analystos.agents.prompts import prompt, prompt_version_id

                    system = prompt("context_resolution.v1")
                    call_ctx.prompt_version = prompt_version_id("context_resolution.v1", system)
                    answer = router.complete_json("glossary_suggestion", system, json.dumps(payload), ctx=call_ctx, max_tokens=2500)
                from pydantic import ValidationError

                from analystos.contracts.grounding import ResolutionAnswer

                try:
                    data = ResolutionAnswer.model_validate(answer.data).model_dump()
                except ValidationError:
                    data = {}
                    if model_status == "not_requested":
                        model_status = "invalid_output"
                by_id = {k["id"]: k for k in known}
                proposed_filters = data.get("filters", [])
                for item in data.get("resolutions", [])[:25]:
                    if not isinstance(item, dict):
                        continue
                    ids, refs = item.get("evidence_ids") or [], item.get("columns") or []
                    supported = {r for i in ids if i in by_id for r in by_id[i]["columns"]}
                    if not ids or any(i not in by_id for i in ids) or not refs or not set(refs) <= supported:
                        continue
                    if is_injection(str(item.get("reason", ""))) or is_injection(str(item.get("term", ""))):
                        continue
                    # Preserve conflicts/expiry/drafts; an LLM cannot erase an unresolved owner definition.
                    if any(r["state"] != "resolved" and terms(r["term"]) & terms(str(item.get("term", ""))) for r in results):
                        continue
                    contracts = [b for b in bindings if b.entry_id in ids and binding_state(b.definition, scope) == "resolved"]
                    if len(contracts) > 1:
                        continue
                    contract = contracts[0] if contracts else None
                    resolved_item = {"id": contract.id if contract else "model:" + stable_hash(item)[:16], "term": str(item.get("term", ""))[:200],
                                    "columns": refs, "definition": str(item.get("reason", ""))[:1000], "state": "resolved",
                                    "by": "model_with_reviewed_evidence", "evidence_ids": ids}
                    if contract:
                        resolved_item.update(kind=contract.definition["kind"], metric=contract.definition.get("metric"),
                                             predicate=contract.definition.get("predicate"), revision=contract.revision)
                    if not any(r["id"] == resolved_item["id"] and r["state"] == "resolved" for r in results):
                        results.append(resolved_item)
                missing = [str(x)[:300] for x in data.get("missing", [])[:10] if isinstance(x, str) and not is_injection(x)]
                for t in data.get("open_terms", [])[:25]:
                    if isinstance(t, dict) and not is_injection(str(t.get("term", "")) + " " + str(t.get("question", ""))):
                        open_terms.append({"term": str(t.get("term", ""))[:200], "question": str(t.get("question", ""))[:500],
                                           "availability": str(t.get("availability", "unknown")).lower(),
                                           "columns": [c for c in t.get("columns") or [] if allowed_ref(str(c), scope)]})
                for p in data.get("metric_proposals", [])[:5]:
                    if isinstance(p, dict) and all(allowed_ref(str(c), scope) for c in p.get("columns", [])) and not is_injection(str(p)):
                        proposals.append({"term": str(p.get("term", ""))[:200], "definition": str(p.get("definition", ""))[:2000],
                                          "columns": p.get("columns", []), "missing_contract_fields": p.get("missing_contract_fields", []),
                                          "state": "proposed", "executable": False})
                if model_status == "not_requested":
                    model_status = "used"
            except AnalystOSError as exc:
                model_status = exc.code
        else:
            model_status = "unavailable_or_off"
    model_missing = set(missing)
    unresolved = [r for r in results if r["state"] != "resolved"]
    # Familiar words that require a business definition, not merely a column-name guess.
    for concept in ("repeat", "recurring", "backlog", "business hours"):
        if terms(concept) <= terms(body.question) and not any(terms(concept) & terms(r["term"]) and r["state"] == "resolved" for r in results):
            missing.append(f"Define {concept} for this population, including eligibility and any time window")
    reused_views = [p for p in proposals if existing_metric_view(p, results)]
    proposals = [p for p in proposals if not existing_metric_view(p, results)]
    missing = list(dict.fromkeys(missing))
    for proposal in proposals:
        if not any(r.get("kind") == "metric" and r.get("state") == "resolved" and terms(r["term"]) == terms(proposal["term"]) for r in results):
            missing.append(f"Review the proposed metric contract for {proposal['term']} before testing it")
    from analystos.services.question_constraints import (
        explicit_period,
        grounded_filters,
        period_filters,
        reporting_period,
    )
    analysis_filters, filter_omissions = grounded_filters(body.question, proposed_filters, known, lambda c: allowed_ref(c, scope))
    period = reporting_period(body.question, known)
    binding_ids = {b.entry_id: b.id for b in bindings}
    period_evidence_ids = [binding_ids.get(k["id"], k["id"]) for k in known
                           if period and not explicit_period(body.question) and re.search(r"reporting|year|period", k["term"], re.I)]
    if period:
        metadata = {c["ref"]: c for c in cat}
        def timestamp(ref):
            return str(metadata.get(ref, {}).get("type", "")).lower().startswith(("date", "timestamp"))
        analysis_filters = [f for f in analysis_filters if f["value"] not in period or timestamp(f["column"])]
        chosen = {f["column"] for f in analysis_filters if f["value"] in period and not isinstance(f["value"], list) and timestamp(f["column"])}
        chosen |= {r["metric"]["time_column"] for r in results if r.get("metric") and r["metric"].get("time_column") and timestamp(r["metric"]["time_column"])}
        if not chosen:
            chosen = {c for r in results if r.get("state") == "resolved" for c in r["columns"]
                      if metadata.get(c, {}).get("type") in {"datetime", "timestamp", "date", "timestamptz"}}
        if len(chosen) == 1:
            time_column = next(iter(chosen))
            analysis_filters = [f for f in analysis_filters if f["column"] != time_column]
            analysis_filters += period_filters(body.question, time_column, period)
        else:
            missing.append("The period is known; resolve which reporting timestamp applies from reviewed context")
    priority = re.search(r"\bP([1-5])\b", body.question, re.I)
    priority_refs = {c for r in results if "priority" in terms(r["term"]) for c in r["columns"]}
    if priority and not any(f["value"] == int(priority.group(1)) and f["column"] in priority_refs for f in analysis_filters):
        missing.append("Resolve the requested priority code from the reviewed data dictionary before testing")
    date_lower = {f["column"] for f in analysis_filters if f["op"] in {">", ">="} and isinstance(f["value"], str) and re.fullmatch(r"20\d{2}-\d{2}-\d{2}", f["value"])}
    date_upper = {f["column"] for f in analysis_filters if f["op"] in {"<", "<="} and isinstance(f["value"], str) and re.fullmatch(r"20\d{2}-\d{2}-\d{2}", f["value"])}
    date_constrained = bool(date_lower & date_upper) or bool(re.search(r"\b(since|after)\s+(last|previous)\b", body.question, re.I) and date_lower) or bool(re.search(r"\b(before|through)\s+(last|previous)\b", body.question, re.I) and date_upper)
    if not date_constrained and re.search(r"\b(last month|previous month|last quarter|previous quarter|last week)\b", body.question, re.I):
        missing.append("Resolve the relative reporting window from a reviewed reporting-period policy before testing")
    if not period and not date_constrained and re.search(r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+20\d{2}\b", body.question, re.I):
        missing.append("Resolve supported date constraints for the requested comparison periods before testing")
    # Ask once (PILOT-FIX-04): every open term in one list; absent data becomes a limitation; a recheck asks only
    # what is still open and what an answer changed.
    questions = [mr.review_question(r) for r in unresolved] + [mr.review_question(f) for f in fields if f["state"] != "resolved"]
    for text in dict.fromkeys(missing):
        if text in model_missing:
            questions.append(mr.model_question(text))
        elif text.startswith("Review the proposed metric contract for "):
            term = text.removeprefix("Review the proposed metric contract for ").removesuffix(" before testing it")
            questions.append({**mr.rule_question(text, term), "kind": "metric"})
        else:
            concept = re.match(r"Define (.+?) for this population", text)
            questions.append(mr.rule_question(text, concept.group(1) if concept else None))
    # A field named like a term that needs a business definition ("backlog") is that question, not a resolution.
    needs_definition = [set(mr.words(q["term"])) for q in questions if q["kind"] in {"rule", "metric"} and q["term"] != q["question"]]
    fields = [f for f in fields if not any(set(mr.words(f["term"])) & w for w in needs_definition)]
    run_id = getattr(getattr(agent_ctx, "run", None), "id", None)
    previous = mr.previous_rounds(session, run_id) if isinstance(run_id, str) else []
    from analystos.contracts.grounding import MeaningReview

    review = MeaningReview.model_validate(mr.build(
        question=body.question, plan_text=plan_text, results=results, fields=fields, questions=questions,
        open_terms=open_terms, cat=cat, bindings=bindings, previous=previous)).model_dump()
    asked = review["questions"]
    resolutions = results + [f for f in fields if f["state"] == "resolved"]
    return {"question": body.question, "resolutions": resolutions, "missing": [q["question"] for q in asked if q["kind"] != "review"],
            "metric_proposals": proposals,
            "reused_metric_views": reused_views,
            "analysis_filters": analysis_filters, "filter_omissions": filter_omissions, "reporting_period": period,
            "period_evidence_ids": period_evidence_ids, "knowledge_version": getattr(call_ctx, "knowledge_version", None),
            "needs_clarification": bool(asked), "model_status": model_status,
            "scope_hash": scope.scope_hash(), "context_hash": stable_hash(resolutions),
            "binding_version": binding_version(bindings, scope),
            "available_count": len(known), "used_count": len([r for r in resolutions if r["state"] == "resolved"]),
            "clarification": [q["question"] for q in asked],
            "meaning_review": review, "limitations": [lim["statement"] for lim in review["limitations"]]}


def _plan_text(agent_ctx) -> str:
    """The plan's questions and focus, when resolving for a run: terms the plan depends on are collected too."""
    plan = getattr(getattr(agent_ctx, "run", None), "plan", None)
    if not isinstance(plan, dict):
        return ""
    parts = []
    for q in plan.get("questions") or []:
        parts.append(str(q.get("question") or q.get("text") or "") if isinstance(q, dict) else str(q))
    focus = plan.get("focus")
    if focus:
        parts.append(" ".join(map(str, focus)) if isinstance(focus, list) else str(focus))
    return "\n".join(p for p in parts if p and not is_injection(p))[:4000]


def binding_version(bindings, scope):
    return stable_hash([(r.id, r.revision, binding_state(r.definition, scope)) for r in bindings])


def current_report(ctx, report):
    """Re-resolve automatically if review, expiry or scope changed since the context step."""
    if not report or not report.get("binding_version"):
        return report
    from analystos.contracts.grounding import ResolveIn
    from analystos.db.base import session_scope

    with session_scope() as session:
        domains = domains_for(session, ctx.scope)
        bindings = [b for b in scoped_bindings(session, ctx.scope) if not b.definition.get("domain") or b.definition["domain"] in domains]
        knowledge_unchanged = not report.get("knowledge_version") or report["knowledge_version"] == getattr(ctx, "knowledge_version", None)
        if knowledge_unchanged and report["binding_version"] == binding_version(bindings, ctx.scope) and report.get("scope_hash") == ctx.scope.scope_hash():
            return report
        return resolve(session, ctx.user, ctx.workspace.id,
                       ResolveIn(question=report["question"], source_ids=ctx.scope.source_ids, use_model=True),
                       router=ctx.router, call_ctx=ctx.call_ctx(), reviewed_context=(ctx.run.origin or {}).get("reviewed_context"), agent_ctx=ctx)
