"""A meaning review that converges (PILOT-FIX-04).

The C11 rehearsal took three rounds: every recheck asked about new terms, plain table fields (sales channel,
region, category) went to the owner, and data that does not exist (return reasons) blocked the run. Here:

* every open term is collected in one pass and asked once, as one list;
* a term that names exactly one selected catalog field (not an identifier) resolves from the catalog without asking.
  It means "group by this field as recorded"; it is never a metric, a rule or a flag polarity, and it never satisfies
  a term that needs a reviewed definition;
* data the question needs but no selected field could hold becomes a recorded limitation that the run and its
  findings carry; it does not block;
* a recheck asks again what is still open, the code's own gate questions, and what an answer changed. A term the model
  raises later that no answer caused is recorded as not reviewed, not asked.

Rounds are recorded on the run as versioned artifacts with the binding revisions in force, so what was asked and
which reviewed definitions a run used stay pinned to it. No query runs here and nothing a model says is executed.
"""
from __future__ import annotations

import re

from sqlalchemy import select

from analystos.context.compiler import terms
from analystos.core.ids import stable_hash, utcnow

ROUND_NAME = "Meaning review round"
# Fields whose name alone says nothing a person could group by.
GENERIC_LABELS = {"name", "date", "value", "code", "type", "id", "number", "count", "amount", "flag", "key", "description"}
# Nouns that ask for an explanation the data must record to show: a "why" field.
EXPLANATORY = ("reason", "cause", "motive", "feedback", "complaint", "satisfaction", "sentiment", "survey")
_WORD = re.compile(r"[a-z0-9]+")


def _norm(word: str) -> str:
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("ches", "shes", "xes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def words(text: str | None) -> list[str]:
    """Lower-case words in order, a plural ending removed ("categories" -> "category")."""
    return [_norm(w) for w in _WORD.findall((text or "").lower().replace("_", " "))]


def key_of(term: str) -> str:
    """One key per term however it is written: "Late deliveries" and "late delivery" are one question."""
    return " ".join(sorted(terms(" ".join(words(term))))) or (term or "").strip().lower()


def norm_terms(text: str) -> set[str]:
    return terms(" ".join(words(text)))


def focus_terms(text: str) -> set[str]:
    """`terms` of a question plus its singular forms, so an owner's "Category" matches "categories"."""
    return terms(text) | norm_terms(text)


def _contains(seq: list[str], phrase: list[str]) -> bool:
    n = len(phrase)
    return any(seq[i:i + n] == phrase for i in range(len(seq) - n + 1))


def _matches(token: str, candidate: str) -> bool:
    return token == candidate or (len(token) >= 5 and len(candidate) >= 5 and token[:5] == candidate[:5])


def field_tokens(cat: list[dict]) -> set[str]:
    out: set[str] = set()
    for c in cat:
        out.update(words(c["ref"].rsplit(".", 1)[-1]))
        out.update(words(c.get("name")))
    return out


def in_catalog(token: str, tokens: set[str]) -> bool:
    return any(_matches(_norm(token), t) for t in tokens)


# ------------------------------------------------------------------------------------ resolve from the catalog
def catalog_fields(text: str, cat: list[dict], resolutions: list[dict]) -> list[dict]:
    """Terms naming selected catalog fields. One field: resolved by the catalog. Several fields with the same
    name in different tables: one question. Terms a reviewed definition already covers are left to it."""
    seq = words(text)
    covered = [set(words(r["term"])) for r in resolutions]
    by_label: dict[tuple[str, ...], list[dict]] = {}
    for c in cat:
        if str(c.get("type") or "").lower() in {"id", "identifier"}:
            continue
        column = c["ref"].rsplit(".", 1)[-1]
        for label in {tuple(words(c.get("name"))), tuple(words(column))}:
            if not label or (len(label) == 1 and label[0] in GENERIC_LABELS):
                continue
            if _contains(seq, list(label)):
                by_label.setdefault(label, [])
                if c not in by_label[label]:
                    by_label[label].append(c)
    # "sales channel" wins over "channel" when both match the same field
    labels = [lab for lab in by_label if not any(lab != other and _contains(list(other), list(lab))
                                                 and {c["ref"] for c in by_label[lab]} <= {c["ref"] for c in by_label[other]}
                                                 for other in by_label)]
    out = []
    for label in labels:
        if any(set(label) <= cov for cov in covered):
            continue
        cols = by_label[label]
        term = " ".join(label)
        if len(cols) == 1:
            c = cols[0]
            out.append({"id": "catalog:" + c["ref"], "term": c.get("name") or term, "columns": [c["ref"]],
                        "definition": f"The {c.get('name') or term} field as recorded in the data, used to group results.",
                        "state": "resolved", "by": "catalog", "kind": "field"})
        else:
            out.append({"id": "catalog:" + stable_hash(sorted(c["ref"] for c in cols))[:16], "term": term,
                        "columns": sorted(c["ref"] for c in cols), "definition": "", "state": "ambiguous",
                        "by": "catalog", "kind": "field"})
    return out


def map_glossary_to_catalog(results: list[dict], cat: list[dict]) -> None:
    """A glossary term with a meaning but no field mapped here ("Sales channel" from a domain pack) resolves to the
    one selected field of the same name. Metrics and rules keep their review: a name match is not a formula."""
    for r in results:
        if r.get("state") != "missing" or r.get("by") != "reviewed_knowledge" or r.get("kind") in {"metric", "rule"}:
            continue
        wanted = words(r["term"])
        hits = [c for c in cat if str(c.get("type") or "").lower() not in {"id", "identifier"}
                and wanted in (words(c.get("name")), words(c["ref"].rsplit(".", 1)[-1]))]
        if len(hits) == 1:
            r.update(state="resolved", columns=[hits[0]["ref"]], by="glossary_and_catalog")


# ------------------------------------------------------------------------------------ data that does not exist
_SMALL = {"the", "a", "an", "and", "or", "in", "on", "by", "per", "to", "is", "are", "what", "which", "data", "this"}


def _head(term: str) -> str | None:
    """The noun a term is about: "reasons for returns" -> reason, "return reason" -> reason."""
    ws = _WORD.findall((term or "").lower())
    for joiner in ("for", "of", "behind"):
        if joiner in ws and ws.index(joiner) > 0:
            return _norm(ws[ws.index(joiner) - 1])
    content = [w for w in ws if len(w) >= 3 and w not in _SMALL]
    return _norm(content[-1]) if content else None


def absent_limitation(term: str, cat_tokens: set[str], *, model_says_absent: bool) -> dict | None:
    """A limitation when no selected field could hold the term. The rule needs a "why" noun (reason, cause …);
    a model's claim that data is absent is accepted only when the catalog agrees."""
    head = _head(term)
    if not head or in_catalog(head, cat_tokens):
        return None
    if not (model_says_absent or any(_matches(head, e) for e in EXPLANATORY)):
        return None
    phrase = term.strip().rstrip("?.").lower()
    return {"key": key_of(term), "term": term.strip(), "kind": "absent_data",
            "statement": f"No field in the selected data records {phrase}. Findings can show what goes with the "
                         f"outcome, not {phrase} themselves."}


def noun_limitation(noun: str) -> dict:
    return {"key": noun, "term": noun + "s", "kind": "absent_data",
            "statement": f"No field in the selected data records {noun}s. Findings can show what goes with the outcome, "
                         f"not the {noun}s themselves."}


def explanatory_terms(text: str) -> list[str]:
    seq = words(text)
    return [e for e in EXPLANATORY if e in seq]


# ------------------------------------------------------------------------------------ questions, once
REVIEW_QUESTION = {
    "missing": "What does “{t}” mean in this data? Map it to the fields that hold it.",
    "draft": "“{t}” has a draft definition. Approve it or correct it.",
    "ambiguous": "“{t}” has more than one meaning here. Keep the one that applies.",
    "outdated": "The definition of “{t}” no longer matches the data. Update it.",
    "not_yet_effective": "The definition of “{t}” is not in effect yet. Say which definition applies now.",
}


def review_question(r: dict) -> dict:
    text = REVIEW_QUESTION.get(r["state"], "Review “{t}”.").format(t=r["term"])
    if r.get("by") == "catalog" and r["state"] == "ambiguous":
        text = f"More than one table has a “{r['term']}” field. Which one does the question mean?"
    return {"key": key_of(r["term"]), "term": r["term"], "question": text, "kind": "review",
            "state": r["state"], "columns": list(r.get("columns") or []),
            "binding_id": r["id"] if str(r.get("id", "")).startswith("bind_") else None}


def rule_question(text: str, term: str | None = None) -> dict:
    return {"key": key_of(term or text), "term": term or text, "question": text, "kind": "rule", "columns": []}


def model_question(text: str, term: str | None = None, columns: list[str] | None = None) -> dict:
    return {"key": key_of(term or text), "term": term or text, "question": text, "kind": "model", "columns": columns or []}


def _dedupe(questions: list[dict]) -> list[dict]:
    seen, out = set(), []
    for q in questions:
        if q["key"] in seen:
            continue
        seen.add(q["key"])
        out.append(q)
    return out


def _covered(q: dict, resolved: list[dict]) -> bool:
    """A model question about a term already resolved (reviewed or from the catalog) is not asked."""
    if q["kind"] != "model":
        return False
    qwords = set(words(q["term"])) - FILLER
    for r in resolved:
        rwords = set(words(r["term"]))
        # a catalog field covers only its own name: "returned" does not answer "return rate"
        if rwords and (rwords == qwords if r.get("by") == "catalog" else rwords <= qwords):
            return True
    return False


# Words a model wraps around a term when it asks about it ("Define the sales channel values").
FILLER = {"define", "definition", "clarify", "confirm", "mean", "meaning", "value", "label", "the", "a", "an", "of",
          "for", "what", "which", "is", "are", "does", "do", "this", "in", "data", "field", "column", "please", "it",
          "possible", "list", "each", "and", "its"}


# ------------------------------------------------------------------------------------ rounds on a run
def binding_revisions(bindings) -> dict[str, int]:
    return {b.id: b.revision for b in bindings}


def changed_since(previous: list[dict], bindings) -> list:
    """Bindings created or revised after the last recorded round: the owner's answers."""
    if not previous:
        return []
    before = previous[-1].get("binding_revisions") or {}
    return [b for b in bindings if before.get(b.id) != b.revision]


def caused_by_answer(q: dict, changed) -> bool:
    """Code-raised questions (a reviewed definition's state, a rule such as the reporting period) are the gate itself:
    they follow from reviewed state, so a new one means that state moved, and they are always asked. A model's new
    term is asked only when an answer brought it up."""
    if q["kind"] in {"review", "rule"}:
        return True
    qwords = set(words(q["term"]))
    for b in changed:
        bwords = set(words(b.term))
        if bwords and (bwords & qwords):
            return True
        if qwords and qwords <= set(words((b.definition or {}).get("definition", ""))):
            return True
    return False


def build(*, question: str, plan_text: str, results: list[dict], fields: list[dict], questions: list[dict],
          open_terms: list[dict], cat: list[dict], bindings, previous: list[dict]) -> dict:
    """The one list to ask, what resolved without asking, the limitations, and what a recheck did not ask."""
    text = question + "\n" + plan_text
    tokens = field_tokens(cat)
    resolved = [r for r in results + fields if r.get("state") == "resolved"]
    # a limitation found in an earlier round stays with the run, whatever a later model call says
    limitations: list[dict] = [lim for p in previous for lim in p.get("limitations") or [] if lim.get("kind") == "absent_data"]
    asks = list(questions)
    for t in open_terms:
        term = str(t.get("term") or "").strip()
        if not term:
            continue
        lim = absent_limitation(term, tokens, model_says_absent=t.get("availability") == "absent")
        if lim:
            limitations.append(lim)
            continue
        asks.append(model_question(str(t.get("question") or f"What does “{term}” mean in this data?"), term,
                                   list(t.get("columns") or [])))
    # a proposed metric or a model question about a "why" field no table holds ("return reason") is a limitation
    for q in sorted(asks, key=lambda q: q["kind"] != "metric"):
        if q["kind"] in {"model", "metric"} and not q["columns"]:
            lim = absent_limitation(q["term"], tokens, model_says_absent=False)
            if lim:
                asks.remove(q)
                if q["term"] == q["question"]:  # free text: the limitation names the noun, not the model's sentence
                    lim = noun_limitation(_head(q["term"]) or lim["term"])
                if not any(set(words(x["term"])) & set(words(lim["term"])) for x in limitations):
                    limitations.append(lim)
    for noun in explanatory_terms(text):
        lim = absent_limitation(noun, tokens, model_says_absent=False)
        if lim and not any(noun in words(x["term"]) for x in limitations):
            limitations.append(noun_limitation(noun))
    # a question about a term the data cannot hold is the limitation, not a question
    lim_words = [set(words(lim["term"])) for lim in limitations]
    asks = [q for q in asks if not (q["kind"] == "model" and any(lw and lw <= set(words(q["question"] + " " + q["term"]))
                                                                  for lw in lim_words))]
    asks = _dedupe([q for q in asks if not _covered(q, resolved)])
    round_no = len(previous) + 1
    asked_before = {k for p in previous for k in p.get("asked") or []}
    changed = changed_since(previous, bindings)
    not_asked = []
    if previous:
        kept = []
        for q in asks:
            if q["key"] in asked_before or caused_by_answer(q, changed):
                kept.append(q)
            else:
                not_asked.append(q | {"reason": "raised after the first round and not caused by an answer"})
        asks = kept
    for q in not_asked:
        limitations.append({"key": q["key"], "term": q["term"], "kind": "unreviewed",
                            "statement": f"Not reviewed by an owner: {q['term']}. The analysis uses the data as recorded."})
    limitations = list({lim["key"]: lim for lim in limitations}.values())
    return {"round": round_no, "questions": asks, "limitations": limitations, "not_asked": not_asked,
            "resolved_without_asking": [{"term": r["term"], "by": r.get("by"), "columns": r.get("columns", [])}
                                        for r in resolved if r.get("by") in {"catalog", "reviewed_knowledge", "glossary_and_catalog",
                                                                              "reviewed_brief", "model_with_reviewed_evidence"}
                                        or (r.get("by") == "reviewed_mapping" and not any(
                                            b.id == r["id"] for b in changed))],
            "answered": sorted({b.term for b in changed}),
            "pinned": [{"id": r["id"], "term": r["term"], "revision": r.get("revision")}
                       for r in resolved if r.get("by") == "reviewed_mapping"],
            "binding_revisions": binding_revisions(bindings)}


def previous_rounds(session, run_id: str | None) -> list[dict]:
    if not run_id:
        return []
    from analystos.db.models import Artifact

    rows = session.scalars(select(Artifact).where(Artifact.run_id == run_id, Artifact.type == "context_package",
                                                  Artifact.name.like(ROUND_NAME + " %")).order_by(Artifact.created_at))
    return sorted((dict(a.content or {}) for a in rows), key=lambda c: c.get("round", 0))


def record_round(session, run, grounding: dict, *, user_id: str, asked_at) -> dict | None:
    """Called when the analyst rechecks: the round that was waiting becomes history the next round reads."""
    from analystos.artifacts.registry import link, save_artifact

    review = (grounding or {}).get("meaning_review") or {}
    done = previous_rounds(session, run.id)
    round_no = len(done) + 1
    now = utcnow()
    minutes = round((now - asked_at).total_seconds() / 60, 1) if asked_at else None
    content = {"run_id": run.id, "round": round_no,
               "asked": [q["key"] for q in review.get("questions") or []],
               "questions": [{"term": q["term"], "question": q["question"]} for q in review.get("questions") or []],
               "limitations": review.get("limitations") or [],
               "binding_revisions": review.get("binding_revisions") or {},
               "asked_at": asked_at.isoformat() if asked_at else None, "rechecked_at": now.isoformat(),
               "owner_minutes": minutes}
    art = save_artifact(session, workspace_id=run.workspace_id, run_id=run.id, type_="context_package",
                        name=f"{ROUND_NAME} {round_no}", content=content, creator_user=user_id)
    link(session, run.workspace_id, ("run", run.id), "meaning_review", (art.type, art.id), run_id=run.id)
    return content


def run_limitations(session, run_id: str) -> list[str]:
    """What the data cannot show, as finding caveats: the limitations the run's context step recorded."""
    from analystos.db.models import RunTask

    out: list[str] = []
    for key in ("hypotheses", "context"):
        output = session.scalar(select(RunTask.output).where(RunTask.run_id == run_id, RunTask.key == key)) or {}
        review = ((output.get("grounding") or {}).get("meaning_review")) or {}
        if review:
            out = [f"Data limitation: {lim['statement']}" for lim in review.get("limitations") or []
                   if lim.get("kind") == "absent_data"]
            break
    return out


def finding_caveats(run_id: str) -> list[str]:
    from analystos.db.base import session_scope

    with session_scope() as s:
        return run_limitations(s, run_id)
