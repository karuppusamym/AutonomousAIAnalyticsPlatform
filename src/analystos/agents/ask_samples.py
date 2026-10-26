"""Small example-record requests, grounded in current scope and a prior single-table answer."""
from __future__ import annotations

import re
from typing import Any

import sqlglot
from sqlglot import exp

from analystos.agents import ask_rules

_REQUEST = re.compile(
    r"^(?:please )?(?:list(?: down)?|show(?: me)?|give(?: me)?) "
    r"(?:(?:a )?few|(?P<count>\d{1,3})) (?:(?P<kind>sample|example|same) )?"
    r"(?P<subject>[a-z_ ]+?)(?P<related> with (?:related|customer and product) details| across related tables)?[.!?]*$"
)


def sample_plan(ctx: Any, question: str) -> dict[str, Any] | None:
    match = _REQUEST.fullmatch(" ".join(question.lower().split()))
    if match is None:
        return None
    count = int(match["count"] or 5)
    if not 1 <= count <= 20:
        return None
    tables, lex = ask_rules.load_tables(ctx)
    previous = getattr(ctx, "previous_answer", None) or {}
    prior, prior_table = None, None
    if previous.get("sql"):
        try:
            tree = sqlglot.parse_one(previous["sql"])
            if (previous.get("rules") or {}).get("shape") == "sample":
                sample_base = tree.find(exp.Subquery)
                if sample_base is not None:
                    tree = sample_base.this
            refs = list(tree.find_all(exp.Table))
            if isinstance(tree, exp.Select) and len(refs) == 1 and not tree.args.get("joins") and not tree.args.get("having"):
                fq = f"{refs[0].db}.{refs[0].name}"
                prior_table = next((t for t in tables if t.fq == fq), None)
                prior = tree if prior_table else None
        except sqlglot.errors.ParseError:
            pass
    subject = match["subject"].strip()
    table = prior_table if subject in ("rows", "records", "examples", "these orders", "these sample orders") else ask_rules._subject_table(subject, tables, lex)
    if table is None or (match["kind"] == "same" and (prior_table is None or prior_table.fq != table.fq)):
        return None
    # A named new table must not silently inherit the previous table's population.
    prior = prior if prior_table and prior_table.fq == table.fq else None
    columns = [c for c in table.columns if not c.personal]
    if not columns:
        return None
    key = next((c for c in columns if c.role == "identifier"), columns[0])
    schema, name = table.fq.split(".", 1)
    base = exp.select(*[exp.column(c.name, quoted=True) for c in columns]).from_(exp.table_(name, db=schema, quoted=True))
    if prior is not None and prior.args.get("where"):
        condition = prior.args["where"].this.copy()
        allowed = {c.name for c in columns}
        if any(c.name not in allowed for c in condition.find_all(exp.Column)) or condition.find(exp.Subquery):
            return None
        for column in condition.find_all(exp.Column):
            column.set("table", None)
        base = base.where(condition)
    base = base.order_by(exp.column(key.name, quoted=True)).limit(count)
    projected = [exp.column(c.name, table="b", quoted=True) for c in columns[:12]]
    query = exp.select(*projected).from_(base.subquery("b"))
    joined = []
    if match["related"]:
        for i, join in enumerate(table.joins[:3]):
            target_columns = [c for c in join.target.columns if not c.personal and c.name != join.to_column][:4]
            if join.from_column not in {c.name for c in columns} or not target_columns:
                continue
            alias = f"r{i}"
            target_schema, target_name = join.target.fq.split(".", 1)
            target = exp.table_(target_name, db=target_schema, quoted=True).as_(alias)
            query = query.join(target, on=exp.column(join.from_column, table="b", quoted=True).eq(
                exp.column(join.to_column, table=alias, quoted=True)), join_type="LEFT")
            query = query.select(*[exp.column(c.name, table=alias, quoted=True).as_(f"{target_name}_{c.name}", quoted=True)
                                   for c in target_columns], append=True)
            joined.append(join.target.fq)
        if not joined:
            return None  # normal model/refusal path explains missing reviewed relationships
    query = query.order_by(exp.column(key.name, table="b", quoted=True)).limit(count)
    return {"sql": query.sql(dialect=table.dialect), "table": table.fq, "limit": count, "joined": joined,
            "parent_turn_id": previous.get("id") if prior is not None else None,
            "inherited_filter": bool(prior is not None and prior.args.get("where")), "related_available": bool(table.joins),
            "interpreted_same_as_sample": match["kind"] == "same"}


def sample_answer(ctx: Any, question: str) -> dict[str, Any] | None:
    plan = sample_plan(ctx, question)
    if plan is None:
        return None
    from analystos.agents.sql_agent import ASK_PURPOSE, _record_skip, _result, _stage, ask_actor

    _stage(ctx, "execute", "Reading a few example records within your current data access")
    result = ctx.services.gateway.execute(ctx.scope, plan["sql"], actor=ask_actor(ctx.user.id), purpose=ASK_PURPOSE)
    _record_skip(ctx, "Bounded example records from current catalog", 500, rung="rules")
    explanation = f"Up to {plan['limit']} example records from {plan['table']}, ordered by a visible record field. These are not a random or representative sample."
    if plan["interpreted_same_as_sample"]:
        explanation += " Interpreted ‘few same’ as a request for sample records from the preceding answer; no duplicate-order test was performed."
    if plan["inherited_filter"]:
        explanation += " The preceding answer’s filters were retained."
    if plan["joined"]:
        explanation += " Related details use validated many-to-one or one-to-one relationships."
    return {"status": "answered", "answered_by": "rules", "route": "tool", "sql": plan["sql"],
            "explanation": explanation, "result": _result(result), "model": None, "attempts": [], "decisions": [],
            "parent_turn_id": plan["parent_turn_id"], "rules": {"shape": "sample", **plan},
            "suggestions": [f"Show 5 sample {plan['table'].split('.')[-1]} with related details"] if plan["related_available"] else []}
