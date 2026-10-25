"""Question literals become typed filters, never free-form model SQL or invented reporting periods."""
from __future__ import annotations

import calendar
import re
from datetime import date


def explicit_period(question):
    """An explicit named month and year has exact Gregorian bounds; no guessed default year."""
    months = {name.lower(): n for n, name in enumerate(calendar.month_name) if name}
    months.update({name.lower(): n for n, name in enumerate(calendar.month_abbr) if name})
    found = re.findall(r"\b(" + "|".join(months) + r")\s+(20\d{2})\b", question, re.I)
    if len(found) != 1:
        return None
    month, year = months[found[0][0].lower()], int(found[0][1])
    return (date(year, month, 1).isoformat(), date(year + (month == 12), month % 12 + 1, 1).isoformat())


def reporting_period(question, known):
    explicit = explicit_period(question)
    if explicit:
        return explicit
    names = [name for name in calendar.month_name if name and re.search(r"\b" + name + r"\b", question, re.I)]
    years = {year for item in known if re.search(r"reporting|year|period", item["term"], re.I)
             for year in re.findall(r"\b20\d{2}\b", item["definition"].split("\nReviewed contract:")[0])}
    return explicit_period(names[0] + " " + next(iter(years))) if len(names) == 1 and len(years) == 1 else None


def period_filters(question, column, period):
    names = "|".join(name for name in [*calendar.month_name, *calendar.month_abbr] if name)
    qualifier = re.search(r"\b(since|before|after|through)\s+(?:the\s+month\s+of\s+)?(?:" + names + r")\b", question, re.I)
    mode = qualifier.group(1).lower() if qualifier else "during"
    if mode == "since":
        return [{"column": column, "op": ">=", "value": period[0]}]
    if mode == "before":
        return [{"column": column, "op": "<", "value": period[0]}]
    if mode == "after":
        return [{"column": column, "op": ">=", "value": period[1]}]
    if mode == "through":
        return [{"column": column, "op": "<", "value": period[1]}]
    return [{"column": column, "op": ">=", "value": period[0]}, {"column": column, "op": "<", "value": period[1]}]


def grounded_filters(question, proposals, known, allowed_ref):
    """Accept source fields cited by evidence and literal values grounded in the question."""
    by_id = {k["id"]: k for k in known}
    period = reporting_period(question, known)
    filters, omissions = [], []
    for item in proposals:
        ref = item.get("column", "")
        ids = item.get("evidence_ids", [])
        cited = {c for i in ids if i in by_id for c in by_id[i]["columns"]}
        name = ref.rsplit(".", 1)[-1]
        named = bool(re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", question, re.I))
        if not allowed_ref(ref) or (ref not in cited and not named):
            omissions.append("A proposed filter lacked an authorized evidence-backed column")
            continue
        values = item["value"] if isinstance(item["value"], list) else [item["value"]]
        def literal(value, field=name, evidence_ids=tuple(ids)):
            text = str(value)
            boundary = r"\d" if isinstance(value, (int, float)) and not isinstance(value, bool) else r"\w"
            direct = bool(re.search(r"(?<!" + boundary + ")" + re.escape(text) + r"(?!" + boundary + ")", question, re.I))
            # Coded values can come from a cited, reviewed dictionary (e.g. P1 -> priority code 100).
            dictionary = any(re.search(r"\b(?:" + re.escape(field) + r"|code|value)\s*(?:=|:|is)\s*['\"]?" + re.escape(text) + r"\b",
                                       by_id[i]["definition"], re.I) for i in evidence_ids if i in by_id)
            policy_date = bool(re.fullmatch(r"20\d{2}-\d{2}-\d{2}", text)) and any(
                re.search(r"reporting|calendar|period", by_id[i]["term"], re.I) and text in by_id[i]["definition"]
                for i in evidence_ids if i in by_id)
            return direct or dictionary or policy_date or bool(period and text in period)
        if not all(literal(v) for v in values):
            omissions.append("A proposed filter value was not stated in the question or an explicit reporting period")
            continue
        filters.append({"column": ref, "op": item["op"], "value": item["value"], "evidence_ids": ids})
    return filters, omissions
