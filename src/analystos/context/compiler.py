"""Context compiler (P4-T03, spec v3 §4.3): the only way run context reaches a model prompt.

Given a purpose, the text the call is about (objective, question) and the caller's mandatory
inputs, it builds the prompt context under the purpose profile's budget
(`platform settings → context.profiles`):

* a **relevant-column digest** of the authorized catalog: columns ranked by the objective's terms,
  by columns that matching knowledge maps to, then by analytical usefulness; SQL purposes keep only
  the tables the question or SQL references;
* **knowledge sections** (glossary, business rules, metrics, prior findings, negative knowledge,
  episodes) scored deterministically against the objective; a section with nothing relevant says
  `NO_MATCH` instead of padding the prompt with unrelated grounding;
* **receipts** for every included item (id, section, source, sha256) so the call record and the UI
  can show which context was used, and an **omitted** list for everything that did not fit;
* it **fails visibly** (`ContextOverBudget`) when the mandatory part alone exceeds the budget; the
  caller then takes the deterministic path and records the refusal. Nothing is cut mid-JSON.

Over the knowledge packs (P4-K05) the candidates are **sections**, not whole documents: the index
ranks them (BM25 + vector, reciprocal-rank fused, one hop over the link graph) and the compiler
fuses that rank with its own term-overlap rank, so each item is a section-level excerpt with a
receipt naming its path, anchor and hashes. Memory — episodes, prior findings, negative knowledge —
and other providers' results (`external`, P4-K09) are *supplementary* sections: filled only after
the primary ones (glossary, business rules, metrics), each capped at `supplementary_share` of the
budget and given back first, so memory can never crowd out a glossary term.

The output is split for prompt caching (P4-T04): `header` is stable per workspace (workspace,
domain packs, dialects); the body keys listed in `stable` (objective, pinned context, catalog,
knowledge) are stable for the run and are rendered as compact sorted text (`render_stable`) into the
cached preamble right after the static system text; the rest of `body` is the volatile part and
goes last. Selection is pure (`compile_context`); `load_knowledge` is the only DB access.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from analystos.contracts.platform import PurposeProfile
from analystos.core.errors import ContextOverBudget
from analystos.security.injection import is_injection

NO_MATCH = "NO_MATCH"

# context_entry.kind -> compiler section. Crawler `table` entries are not listed: the catalog
# section already carries the same structure, ranked, so they would only duplicate it.
KIND_SECTIONS = {"term": "glossary", "definition": "glossary", "note": "glossary", "rule": "business_rules",
                 "metric": "metrics", "episode": "episodes", "negative": "negative_knowledge",
                 "attested_computation": "prior_findings"}
PRIMARY_SECTIONS = ("glossary", "business_rules", "metrics")
SUPPLEMENTARY_SECTIONS = ("external", "prior_findings", "negative_knowledge", "episodes")
KNOWLEDGE_SECTIONS = (*PRIMARY_SECTIONS, *SUPPLEMENTARY_SECTIONS)
RRF_K = 60

_SEMANTIC_PRIORITY = {"boolean": 0, "datetime": 1, "categorical": 2, "numeric": 3, "text": 5, "id": 6}
_STOP = {"the", "and", "for", "with", "that", "this", "from", "what", "which", "into", "over", "are", "was", "were",
         "how", "why", "does", "did", "our", "all", "any", "per", "by", "of", "to", "in", "on", "is", "it", "be",
         "find", "show", "drivers", "driver", "analyse", "analyze", "between", "across", "most", "more", "less"}
_WORD = re.compile(r"[a-z0-9]+")


def terms(text: str | None) -> set[str]:
    """Normalised content words: lower-case, split on non-alphanumerics (so snake_case splits),
    stop words and 1–2 letter tokens dropped, a plural `s` removed."""
    out = set()
    for w in _WORD.findall((text or "").lower()):
        if len(w) < 3 or w in _STOP:
            continue
        if len(w) > 4 and w.endswith("es") and not w.endswith("ses"):
            w = w[:-2]
        elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(w)
    return out


def compact(value: Any) -> str:
    return json.dumps(value, default=str, separators=(",", ":"), ensure_ascii=False)


def _size(value: Any) -> int:
    return len(compact(value)) + 1


def _sha(value: Any) -> str:
    return hashlib.sha256((value if isinstance(value, str) else compact(value)).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class KnowledgeItem:
    id: str
    section: str
    name: str
    text: str
    source: str
    mapped_columns: tuple[str, ...] = ()
    trusted: bool = True
    # pack sections (P4-K05): where the excerpt comes from and how the index ranked it
    document_id: str | None = None
    path: str | None = None
    anchor: str | None = None
    sha256: str | None = None  # the document's sha256 (the receipt); section_sha256 is the excerpted section's
    section_sha256: str | None = None
    retrieval_rank: int | None = None  # 1-based rank in the index's fused result list
    lexical_share: float | None = None
    via: str | None = None  # reached by one hop from this path
    synonyms: tuple[str, ...] = ()  # scored with the text, rendered once as `aka:`
    label: str | None = None  # display name / type shown next to the name (metrics)


@dataclass(frozen=True)
class RankedItem:
    item: KnowledgeItem
    score: float  # term overlap (item_score)
    fused: float  # reciprocal-rank fusion of the overlap rank and the index rank
    passed: bool  # relevant enough to reach a prompt


@dataclass
class CompiledContext:
    purpose: str
    header: dict[str, Any]
    body: dict[str, Any]
    receipts: list[dict[str, Any]] = field(default_factory=list)
    omitted: list[dict[str, Any]] = field(default_factory=list)
    no_match: list[str] = field(default_factory=list)
    budget_chars: int = 0
    mandatory_chars: int = 0
    refused: str | None = None  # set by callers that turn ContextOverBudget into a refusal
    # Keys of `body` that are stable for the run or thread (objective, pinned context, catalog, knowledge):
    # `llm_json` renders them as the cached preamble; the other keys are the volatile part sent last.
    stable: list[str] = field(default_factory=list)

    @property
    def chars(self) -> int:
        return (len(compact(self.header)) if self.header else 0) + len(compact(self.body))

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CompiledContext:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def summary(self) -> dict[str, Any]:
        """What the call record and the UI show as "context used"."""
        return {"purpose": self.purpose, "chars": self.chars, "budget_chars": self.budget_chars,
                "receipts": self.receipts, "omitted": self.omitted, "no_match": self.no_match}


# ------------------------------------------------------------------------------------ scoring
def item_score(item: KnowledgeItem, query: set[str], in_scope_columns: set[str]) -> float:
    """Share of the query terms the item mentions (name counts double), plus 0.5 when it maps to a
    column in scope that the query is about. Deterministic: the same inputs always select the same context."""
    if not query:
        return 0.0
    name_terms = terms(item.name)
    body_terms = terms(item.text) | terms(" ".join(item.synonyms))
    hit = len(query & name_terms) * 2 + len(query & (body_terms - name_terms))
    score = min(1.0, hit / len(query))
    # mapping bonus only for a mapped in-scope column the query is about (a mapping alone is not relevance)
    if in_scope_columns and any(_col_key(c) in in_scope_columns and terms(_col_key(c)) & query for c in item.mapped_columns):
        score = min(1.0, score + 0.5)
    return round(score, 4)


def _col_key(ref: str) -> str:
    """`schema.table.column` or a bare column name -> the bare column name, lower-case."""
    return ref.rsplit(".", 1)[-1].lower()


def _col_ref(ref: str) -> tuple[str | None, str]:
    """`[schema.]table.column` -> (table, column); a bare column -> (None, column). Lower-case."""
    parts = ref.lower().split(".")
    return (parts[-2] if len(parts) >= 2 else None), parts[-1]


def _boosted(table: str, entry: Any, boost: set[tuple[str | None, str]]) -> bool:
    name = _col_name(entry).lower()
    return (table, name) in boost or (None, name) in boost


def _col_name(entry: Any) -> str:
    return str(entry.get("name") if isinstance(entry, dict) else entry)


def _col_relevance(table: str, entry: Any, query: set[str], boost: set[tuple[str | None, str]]) -> int:
    name = _col_name(entry)
    meaning = entry.get("meaning", "") if isinstance(entry, dict) else ""
    return len(terms(f"{name} {meaning}") & query) + (2 if _boosted(table, entry, boost) else 0)


def _col_key_sort(table: str, entry: Any, query: set[str], boost: set[tuple[str | None, str]],
                  ordinal: int) -> tuple[int, int, int]:
    semantic = entry.get("semantic_type") if isinstance(entry, dict) else None
    return -_col_relevance(table, entry, query, boost), _SEMANTIC_PRIORITY.get(semantic or "", 4), ordinal


_STATS = ("distinct", "null_rate", "values", "has_blanks")


def _round(value: Any) -> Any:
    """Profile floats to 4 significant digits: `3.37625000000001` costs tokens and says nothing more."""
    return float(f"{value:.4g}") if isinstance(value, float) else value


def _render_column(entry: Any, detail: str) -> Any:
    """names: the name only · columns: name, semantic type, meaning · stats: + distinct, null
    rate, datetime range, category values (what hypothesis specs need) · profile: every field the
    catalog carries (SQL generation needs the physical type)."""
    if not isinstance(entry, dict):
        return str(entry)
    if detail == "names":
        return entry.get("name")
    if detail == "profile":
        return {k: _round(v) for k, v in entry.items() if v is not None}
    out = {"name": entry.get("name"), "semantic_type": entry.get("semantic_type") or entry.get("type")}
    if entry.get("role"):
        out["role"] = entry["role"]
    if entry.get("true_value"):  # how a Yes/No flag says yes: a comparison needs it
        out["true_value"] = entry["true_value"]
    if entry.get("meaning"):
        out["meaning"] = entry["meaning"]
    if detail == "stats":
        out.update({k: entry[k] for k in _STATS if entry.get(k) is not None})
        if out["semantic_type"] in ("datetime", "numeric"):  # ranges: bucket edges and periods need them
            out.update({k: entry[k] for k in ("min", "max") if entry.get(k) is not None})
    return out


# ------------------------------------------------------------------------------------ prompt text
# The run-stable part of a prompt is rendered as sorted text lines, not JSON: JSON repeats every key
# for every column, and sorted names keep the text byte-identical from call to call (prefix caching).
def _short(value: Any) -> str:
    """A profile value as prompt text: floats to 4 significant digits, timestamps to their date."""
    if isinstance(value, float):
        return f"{value:.4g}"
    text = str(value)
    if len(text) > 10 and text[4:5] == "-" and text[10:11] in ("T", " "):
        return text[:10]
    return text


def _column_line(entry: Any) -> str:
    if not isinstance(entry, dict):
        return f"  {entry}"
    sem = entry.get("semantic_type")
    physical = entry.get("type")
    kind = ", ".join(x for x in (physical if physical and physical != sem else None, sem) if x)
    parts = [f"  {entry.get('name')}" + (f" ({kind})" if kind else "")]
    if entry.get("role") and entry.get("role") != sem:
        parts.append(str(entry["role"]))
    if entry.get("values"):
        parts.append("values=[" + "|".join(_short(v) for v in entry["values"]) + "]")
    if entry.get("has_blanks"):
        parts.append("has_blanks")
    if entry.get("null_rate"):
        parts.append(f"nulls={float(entry['null_rate']):.0%}" if float(entry["null_rate"]) >= 0.01 else "nulls<1%")
    if entry.get("distinct") is not None and sem not in ("boolean", "datetime"):  # a range says more for time
        parts.append(f"distinct={entry['distinct']}")
    if entry.get("min") is not None and entry.get("max") is not None:
        parts.append(f"range={_short(entry['min'])}..{_short(entry['max'])}")
    for k in ("mean", "p50"):
        if entry.get(k) is not None:
            parts.append(f"{k}={_short(entry[k])}")
    if entry.get("patterns"):
        parts.append("patterns=" + "|".join(str(p) for p in list(entry["patterns"])[:3]))
    line = " ".join(parts)
    return line + (f" — {entry['meaning']}" if entry.get("meaning") else "")


def render_catalog(tables: list[dict[str, Any]], omitted: Iterable[dict[str, Any]] = ()) -> str:
    """`TABLE <fq> [<role>, grain: <grain>, rows≈N] — <description>` then one line per column, tables and
    columns sorted by name. Every cap says what it dropped."""
    dropped: dict[str, int] = {}
    not_sent: list[str] = []
    for o in omitted:
        if o.get("section") != "catalog":
            continue
        if o.get("columns"):
            dropped[str(o.get("asset"))] = dropped.get(str(o.get("asset")), 0) + len(o["columns"])
        elif not o.get("detail"):
            not_sent.append(str(o.get("asset")))
    lines = []
    for t in sorted(tables, key=lambda t: str(t.get("asset"))):
        tags = [str(t["role"])] if t.get("role") else []
        if t.get("grain"):
            tags.append(f"grain: {t['grain']}")
        if t.get("row_count") is not None:
            tags.append(f"rows≈{int(t['row_count']):,}")
        about = " — ".join(x for x in (t.get("business_name"), t.get("description")) if x)
        lines.append(f"TABLE {t.get('asset')}" + (f" [{', '.join(tags)}]" if tags else "") + (f" — {about}" if about else ""))
        cols = t.get("columns") or []
        if cols and all(not isinstance(c, dict) for c in cols):
            lines.append("  columns: " + ", ".join(sorted(str(c) for c in cols)))
        else:
            lines.extend(_column_line(c) for c in sorted(cols, key=lambda c: str(c.get("name") if isinstance(c, dict) else c)))
        if dropped.get(str(t.get("asset"))):
            lines.append(f"  … {dropped[str(t.get('asset'))]} more columns not shown")
    if not_sent:
        lines.append(f"… {len(not_sent)} more tables not shown: {', '.join(sorted(set(not_sent)))}")
    return "\n".join(lines)


def _knowledge_line(item: Any) -> str:
    if not isinstance(item, dict):
        return f"- {item}"
    head = str(item.get("name") or "")
    if item.get("label"):
        head += f" ({item['label']})"
    if item.get("aka"):
        head += " aka: " + ", ".join(item["aka"])
    line = f"- {head} — {item.get('text') or ''}"
    if item.get("columns"):
        line += f" [columns: {', '.join(item['columns'])}]"
    if item.get("trusted") is False:
        line += " (unreviewed)"
    return line


def render_stable(header: dict[str, Any] | None, stable: dict[str, Any], omitted: Iterable[dict[str, Any]] = ()) -> str:
    """The cached preamble: workspace header, objective, pinned analysis context, catalog and knowledge
    sections as text; catalog and knowledge (crawled or written text) inside <untrusted_context>."""
    out: list[str] = []
    header = header or {}
    head = [f"Workspace: {header['workspace']}"] if header.get("workspace") else []
    if header.get("domain_packs"):
        head.append("domain packs: " + ", ".join(header["domain_packs"]))
    if header.get("dialects"):
        head.append("SQL dialect: " + ", ".join(header["dialects"]))
    if head:
        out.append(" · ".join(head))
    data: list[str] = []
    for key, value in stable.items():
        if key == "objective":
            out.append(f"OBJECTIVE: {value}")
        elif key == "analysis_context" and isinstance(value, dict):
            ctx = "; ".join(f"{k.replace('_', ' ')}: {', '.join(map(str, v)) if isinstance(v, list) else v}"
                            for k, v in value.items() if v not in (None, "", []))
            out.append(f"ANALYSIS CONTEXT (pinned): {ctx}")
        elif key == "catalog" and isinstance(value, list):
            data.append("CATALOG\n" + render_catalog(value, omitted))
        elif key in KNOWLEDGE_SECTIONS:
            title = key.replace("_", " ").upper()
            data.append(f"{title}: {NO_MATCH}" if value == NO_MATCH or not value else
                        title + "\n" + "\n".join(_knowledge_line(i) for i in value))
        else:
            out.append(f"{key.replace('_', ' ').upper()}: {compact(value)}")
    if data:
        out.append("<untrusted_context>\n" + "\n".join(data) + "\n</untrusted_context>")
    return "\n".join(out)


def excerpt(text: str, limit: int) -> str:
    """Whole sentences (else whole words) up to `limit` characters — an excerpt, marked with …"""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    stop = max(cut.rfind(". "), cut.rfind("; "))
    if stop >= limit // 2:
        return cut[:stop + 1] + " …"
    space = cut.rfind(" ")
    return (cut[:space] if space > 0 else cut) + " …"


_SENTENCE = re.compile(r"(?<=[.!?;])\s+")


def focused_excerpt(text: str, query: set[str], limit: int) -> str:
    """The sentences of a section that mention the query most, in their original order, up to
    `limit` characters; gaps are marked with …. Without a query hit it is the plain excerpt."""
    flat = " ".join((text or "").split())
    if len(flat) <= limit or not query:
        return excerpt(flat, limit)
    sentences = [s for s in _SENTENCE.split(flat) if s]
    hits = [len(terms(s) & query) for s in sentences]
    if not any(hits):
        return excerpt(flat, limit)
    chosen: set[int] = set()
    used = 0
    for i in sorted(range(len(sentences)), key=lambda i: (-hits[i], i)):
        if hits[i] == 0:
            break
        cost = len(sentences[i]) + 3
        if used + cost > limit:
            continue
        chosen.add(i)
        used += cost
    if not chosen:
        return excerpt(sentences[max(range(len(sentences)), key=lambda i: (hits[i], -i))], limit)
    out, last = [], -1
    for i in sorted(chosen):
        if i != last + 1:
            out.append("…")
        out.append(sentences[i])
        last = i
    if last != len(sentences) - 1:
        out.append("…")
    return " ".join(out)


def rank_items(items: Iterable[KnowledgeItem], focus: set[str], in_scope: set[str], min_relevance: float) -> list[RankedItem]:
    """Rank one section's candidates: reciprocal-rank fusion of the compiler's term-overlap rank and
    the index's rank (BM25 + vector + one hop), ties by name then id. An item passes the relevance
    gate on term overlap with the focus terms (generic table-name words excluded), or when it was
    reached by one hop from a section that passed. The index rank orders candidates; it never
    admits one on its own: its lexical match counts table-name words too, and a section admitted on
    one table-name word alone would pull its mapped columns' tables into a SQL prompt (measured, P4-K05)."""
    items = list(items)
    scores = {id(i): item_score(i, focus, in_scope) for i in items}
    by_overlap = sorted(items, key=lambda i: (-scores[id(i)], i.name, i.id))
    overlap_rank = {id(i): n for n, i in enumerate(by_overlap, start=1) if scores[id(i)] > 0}
    fused = {id(i): (1.0 / (RRF_K + overlap_rank[id(i)]) if id(i) in overlap_rank else 0.0)
             + (1.0 / (RRF_K + i.retrieval_rank) if i.retrieval_rank else 0.0) for i in items}

    passed = {id(i) for i in items if scores[id(i)] >= min_relevance and scores[id(i)] > 0}
    passed_paths = {i.path for i in items if id(i) in passed and i.path}
    ranked = [RankedItem(i, scores[id(i)], round(fused[id(i)], 8),
                         id(i) in passed or bool(i.via and i.via in passed_paths)) for i in items]
    ranked.sort(key=lambda r: (-r.fused, -r.score, r.item.name, r.item.id))
    return ranked


# ------------------------------------------------------------------------------------ compile
def compile_context(purpose: str, profile: PurposeProfile, *, objective: str, required: dict[str, Any],
                    catalog: list[dict[str, Any]] | None = None, knowledge: Iterable[KnowledgeItem] = (),
                    header: dict[str, Any] | None = None, reference_text: str | None = None,
                    limit_chars: int | None = None, min_relevance: float = 0.15,
                    generic_terms: Iterable[str] = (), knowledge_chars: int | None = None) -> CompiledContext:
    """Select the prompt context for one call. Raises ContextOverBudget when `required` (plus the
    minimal catalog when the profile has one) cannot fit, instead of silently cutting it.
    `knowledge_chars` (the calling agent's `knowledge.budget_chars`, FND-006) caps the characters all
    knowledge sections together may add; items past it are omitted with that reason."""
    budget = min(profile.max_chars, limit_chars) if limit_chars else profile.max_chars
    header = dict(header or {})
    query = terms(objective) | terms(reference_text)
    catalog = [t for t in (catalog or []) if isinstance(t, dict)] if "catalog" in profile.sections else []
    in_scope = {_col_name(c).lower() for t in catalog for c in t.get("columns") or []}
    # Table names occur in every column description and most knowledge of a domain (a table named "orders" in a retail workspace),
    # so they select tables but carry no signal for columns or knowledge.
    generic = set(generic_terms) | {w for t in catalog for w in terms(str(t.get("asset") or "").split(".")[-1])}
    focus = (query - generic) or query

    # knowledge candidates, ranked and gated; passing items' mapped columns boost the column ranking
    scored: dict[str, list[RankedItem]] = {s: [] for s in profile.sections if s in KNOWLEDGE_SECTIONS}
    for r in rank_items((i for i in knowledge if i.section in scored), focus, in_scope, min_relevance):
        if r.passed:
            scored[r.item.section].append(r)
    boost = {_col_ref(c) for items in scored.values() for r in items for c in r.item.mapped_columns}

    receipts: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    no_match: list[str] = []
    body: dict[str, Any] = dict(required)

    # catalog: rank tables and columns, cap per profile; the names-only form is mandatory
    tables = _select_tables(catalog, query, focus, boost, profile, reference_text, omitted, no_match)
    # A SQL purpose whose question names no table gets every authorized table by name only (with its
    # role and size): the model can name the table, or the Ask ladder clarifies. Profiling every
    # table would send the whole catalog for a question about none of it.
    names_only = profile.referenced_only and "catalog" in no_match
    minimal: list[dict[str, Any]] = []
    full: list[dict[str, Any]] = []
    for t, cols in tables:
        # the key and the validated joins are how a model writes a correct join: small, so always sent
        head = {k: t[k] for k in ("asset", "business_name", "role", "row_count", "key", "joins") if t.get(k)}
        more = {k: t[k] for k in ("description", "grain") if t.get(k)}
        minimal.append({**head, "columns": [_render_column(c, "names") for c in cols]})
        full.append({**head, **more, "columns": [_render_column(c, profile.catalog_detail) for c in cols]})
    if tables:
        body["catalog"] = minimal
    mandatory = (_size(header) if header else 0) + _size(body)
    if mandatory > budget:
        raise ContextOverBudget(
            f"mandatory context for {purpose} is {mandatory} chars, above its budget of {budget}",
            details={"purpose": purpose, "mandatory_chars": mandatory, "budget_chars": budget,
                     "required_keys": sorted(required)})

    def used() -> int:
        return (_size(header) if header else 0) + _size(body)

    # upgrade tables to the profile's detail, most relevant first, while they fit
    for i, (t, _) in enumerate(tables):
        if names_only and full[i] != minimal[i]:
            omitted.append({"section": "catalog", "asset": t.get("asset"), "detail": "no table referenced (names only sent)"})
        elif full[i] != minimal[i]:
            body["catalog"][i] = full[i]
            if used() > budget:
                body["catalog"][i] = minimal[i]
                omitted.append({"section": "catalog", "asset": t.get("asset"), "detail": f"{profile.catalog_detail} (names only sent)"})
        entry = body["catalog"][i]
        receipts.append({"id": f"asset:{t.get('asset')}", "section": "catalog", "kind": "table", "source": "catalog",
                         "columns": len(entry["columns"]), "sha256": _sha(entry)})

    # knowledge sections: primary ones in profile order, then the supplementary ones (memory and
    # other providers), each of those capped at its share of the budget: best first, whole items
    order = _fill_order(profile)
    before_knowledge = used()
    for section in order:
        if section not in scored:
            continue
        ranked = scored[section]
        if not ranked:
            body[section] = NO_MATCH
            no_match.append(section)
            continue
        body[section] = []
        cap = (used() + int(budget * profile.supplementary_share)) if section in SUPPLEMENTARY_SECTIONS else budget
        for n, r in enumerate(ranked):
            item = r.item
            rendered = {"id": item.id, "name": item.name, "text": focused_excerpt(item.text, focus, profile.item_chars)}
            if item.label:
                rendered["label"] = item.label
            if item.synonyms:
                rendered["aka"] = list(item.synonyms)[:6]
            if item.mapped_columns:
                rendered["columns"] = list(item.mapped_columns)[:8]
            if not item.trusted:
                rendered["trusted"] = False
            if n >= profile.max_items_per_section:
                omitted.append({"section": section, "id": item.id, "name": item.name, "reason": "section item cap"})
                continue
            if is_injection(rendered["text"]) or is_injection(item.name):
                # P7-10: the last check before a prompt, whatever the item's origin or trust.
                omitted.append({"section": section, "id": item.id, "name": "(withheld)", "reason": "screened: reads like "
                                "an instruction to a model"})
                continue
            body[section].append(rendered)
            over_agent = knowledge_chars is not None and used() - before_knowledge > knowledge_chars
            if used() > min(budget, cap) or over_agent:
                reason = "budget" if used() > budget else "section share" if used() > cap else "agent knowledge budget"
                body[section].pop()
                omitted.append({"section": section, "id": item.id, "name": item.name, "reason": reason})
                continue
            receipts.append(_receipt(section, r, rendered))
        if not body[section]:
            body[section] = NO_MATCH  # candidates existed but none fit: say so, the omitted list says why
            no_match.append(section)
    _reorder_body(body, profile)

    if omitted:
        body["omitted"] = _omitted_for_model(omitted)
        # the omitted note itself must fit: give back the lowest-ranked knowledge items until it does
        while used() > budget and _drop_last_knowledge(body, profile, receipts, omitted):
            body["omitted"] = _omitted_for_model(omitted)
    stable = [k for k in body if k in STABLE_KEYS or k in KNOWLEDGE_SECTIONS]
    body = {**{k: body[k] for k in stable}, **{k: v for k, v in body.items() if k not in stable}}  # stable first
    compiled = CompiledContext(purpose=purpose, header=header, body=body, receipts=receipts, omitted=omitted,
                               no_match=no_match, budget_chars=budget, mandatory_chars=mandatory, stable=stable)
    return compiled


# Body keys that do not change within a run or Ask thread (with the catalog and knowledge sections).
STABLE_KEYS = ("objective", "analysis_context", "reviewed_business_context", "catalog")


def _select_tables(catalog: list[dict[str, Any]], query: set[str], focus: set[str], boost: set[tuple[str | None, str]],
                   profile: PurposeProfile, reference_text: str | None, omitted: list[dict[str, Any]],
                   no_match: list[str]) -> list[tuple[dict[str, Any], list[Any]]]:
    ref = (reference_text or "").lower()
    ranked = []
    drop = set(profile.drop_semantic_types)
    for pos, t in enumerate(catalog):
        asset = str(t.get("asset") or "")
        table = asset.split(".")[-1].lower()
        cols = [c for c in t.get("columns") or [] if not (isinstance(c, dict) and c.get("semantic_type") in drop)]
        if len(cols) < len(t.get("columns") or []):
            omitted.append({"section": "catalog", "asset": asset, "reason": f"semantic types {sorted(drop)} not used by this purpose",
                            "columns": [_col_name(c) for c in t["columns"] if c not in cols]})
        col_rel = sum(_col_relevance(table, c, focus, boost) for c in cols)
        name_rel = len(terms(f"{asset.split('.', 1)[-1]} {t.get('business_name') or ''}") & query)
        named = bool(asset) and (asset.lower() in ref or f" {asset.split('.', 1)[-1].lower()}" in f" {ref}")
        ranked.append((pos, t, cols, col_rel + 2 * name_rel + (10 if named else 0), named))
    if profile.referenced_only and ranked:
        hits = [r for r in ranked if r[3] > 0]
        if hits:
            # a table a referenced one joins to comes along ("revenue by customer segment" names orders only; the
            # segment lives on customers): one hop along validated joins, nothing further
            named = {r[1].get("asset") for r in hits}
            linked = {str(j.get("references") or "").rsplit(".", 1)[0] for r in hits for j in r[1].get("joins") or []
                      if isinstance(j, dict)}
            hits += [r for r in ranked if r[3] <= 0 and r[1].get("asset") in linked - named]
            for r in ranked:
                if r[3] <= 0 and r[1].get("asset") not in linked:
                    omitted.append({"section": "catalog", "asset": r[1].get("asset"), "reason": "not referenced"})
            ranked = hits
        else:
            no_match.append("catalog")  # nothing named or matched: every authorized table, names first
    ranked.sort(key=lambda r: (-r[3], r[0]))
    out = []
    for _, t, cols, _, _ in ranked:
        table = str(t.get("asset") or "").split(".")[-1].lower()
        order = sorted(range(len(cols)), key=lambda i: _col_key_sort(table, cols[i], focus, boost, i))
        keep = [cols[i] for i in order[:profile.max_columns_per_table]]
        dropped = [_col_name(cols[i]) for i in order[profile.max_columns_per_table:]]
        if dropped:
            omitted.append({"section": "catalog", "asset": t.get("asset"), "columns": dropped, "reason": "column cap"})
        out.append((t, keep))
    return out


def _omitted_for_model(omitted: list[dict[str, Any]]) -> dict[str, Any]:
    """Compact form for the prompt: the model needs to know the context is partial, not the ids."""
    note: dict[str, Any] = {}
    for o in omitted:
        if o["section"] == "catalog":
            if o.get("columns"):
                note.setdefault("columns_not_sent", {})[str(o.get("asset"))] = len(o["columns"])
            elif o.get("detail"):
                note.setdefault("tables_names_only", []).append(str(o.get("asset")))
            else:
                note.setdefault("tables_not_sent", []).append(str(o.get("asset")))
        else:
            note[o["section"]] = note.get(o["section"], 0) + 1
    return note


def _fill_order(profile: PurposeProfile) -> list[str]:
    """Primary knowledge sections first (profile order), supplementary ones after them."""
    sections = list(profile.sections)
    return [s for s in sections if s not in SUPPLEMENTARY_SECTIONS] + [s for s in sections if s in SUPPLEMENTARY_SECTIONS]


def _reorder_body(body: dict[str, Any], profile: PurposeProfile) -> None:
    """The prompt keeps the profile's section order whatever order the sections were filled in."""
    listed = [s for s in profile.sections if s in body and s in KNOWLEDGE_SECTIONS]
    values = {s: body.pop(s) for s in listed}
    body.update(values)


def _receipt(section: str, r: RankedItem, rendered: dict[str, Any]) -> dict[str, Any]:
    item = r.item
    out = {"id": item.id, "section": section, "kind": section, "name": item.name, "source": item.source,
           "score": r.score, "trusted": item.trusted, "sha256": item.sha256 or _sha(item.text),
           "excerpt": len(rendered["text"]) < len(" ".join(item.text.split()))}
    if item.path:
        out.update({"document_id": item.document_id, "path": item.path, "anchor": item.anchor,
                    "section_sha256": item.section_sha256, "rank": item.retrieval_rank, "fused": r.fused})
    if item.via:
        out["via"] = item.via
    return out


def _drop_last_knowledge(body: dict[str, Any], profile: PurposeProfile, receipts: list[dict[str, Any]],
                         omitted: list[dict[str, Any]]) -> bool:
    for section in reversed(_fill_order(profile)):
        items = body.get(section)
        if isinstance(items, list) and items:
            item = items.pop()
            receipts[:] = [r for r in receipts if not (r["id"] == item["id"] and r["section"] == section)]
            omitted.append({"section": section, "id": item["id"], "name": item["name"], "reason": "budget"})
            if not items:
                body[section] = NO_MATCH
            return True
    return False


# ------------------------------------------------------------------------------------ knowledge
def load_knowledge(session: Any, workspace_id: str, sections: Iterable[str], *, run_id: str | None = None,
                   per_kind: int = 300, query: str | None = None, external: Iterable[dict[str, Any]] | None = None,
                   candidates: int = 80, assets: list[str] | None = None, denied_columns=()) -> list[KnowledgeItem]:
    """Candidate items for the requested sections: the workspace's context entries, the knowledge
    packs it sees (the platform pack holds installed domain-pack knowledge; P4-K01), verified
    findings and rejected hypotheses of *other* runs, and other providers' results (`external`:
    given, or the run's context package).

    With a `query` the pack candidates are the index's section hits for it (P4-K05: BM25 + vector,
    fused, one hop over links) — section-level, ranked, with receipts; without one, every pack
    document of the wanted kinds as a whole entry (the P4-T03 behaviour)."""
    from sqlalchemy import select

    from analystos.db.models import Hypothesis, Insight
    from analystos.knowledge.entries import pack_entries, workspace_rows

    wanted = set(sections)
    out: list[KnowledgeItem] = []
    kinds = [k for k, s in KIND_SECTIONS.items() if s in wanted]
    for kind in kinds:
        limit = 20 if kind == "episode" else per_kind
        entries = workspace_rows(session, workspace_id, kinds=[kind])[:limit]
        from analystos.services.context_grounding import filter_entries

        entries = filter_entries(session, workspace_id, entries, assets, denied_columns)
        if not query:
            entries += pack_entries(session, workspace_id, kinds=[kind])[:per_kind]
        for e in entries:
            out.append(KnowledgeItem(id=e.id, section=KIND_SECTIONS[kind], name=e.name, text=e.body or "",
                                     source=e.origin or "user", mapped_columns=tuple(e.mapped_columns),
                                     trusted=bool(e.trusted), document_id=e.pack_id and e.id, path=e.path, sha256=e.sha256,
                                     synonyms=tuple(str(s) for s in e.synonyms or ())))
    if query and kinds:
        from analystos.knowledge.entries import applicable_domain_packs, in_applicable_packs

        out.extend(in_applicable_packs(pack_section_items(session, workspace_id, query, kinds, candidates=candidates),
                                       applicable_domain_packs(session, workspace_id), origin_of=lambda i: i.source))
    if "external" in wanted:
        out.extend(external_items(external if external is not None else _run_external(session, workspace_id, run_id)))
    if "prior_findings" in wanted:
        q = select(Insight).where(Insight.workspace_id == workspace_id, Insight.status == "verified")
        if run_id:
            q = q.where(Insight.run_id != run_id)
        for i in session.scalars(q.order_by(Insight.created_at.desc()).limit(50)):
            out.append(KnowledgeItem(id=f"insight:{i.id}", section="prior_findings", name=i.title, text=i.finding,
                                     source=f"run:{i.run_id}"))
    if "negative_knowledge" in wanted:
        q = select(Hypothesis).where(Hypothesis.workspace_id == workspace_id, Hypothesis.status == "rejected")
        if run_id:
            q = q.where(Hypothesis.run_id != run_id)
        seen: set[str] = set()
        for h in session.scalars(q.order_by(Hypothesis.created_at.desc()).limit(100)):
            if h.statement in seen:
                continue
            seen.add(h.statement)
            spec = h.spec or {}
            cols = tuple(str(spec[k]) for k in ("outcome", "segment") if isinstance(spec.get(k), str))
            out.append(KnowledgeItem(id=f"hypothesis:{h.id}", section="negative_knowledge", name=h.statement[:160],
                                     text=f"{h.statement} — tested, not supported.", source=f"run:{h.run_id}",
                                     mapped_columns=cols))
    return out


_GENERIC_HEADINGS = {"", "definition", "rule", "summary", "preamble"}


def pack_section_items(session: Any, workspace_id: str, query: str, kinds: Iterable[str] | None, *,
                       candidates: int = 80, pack_ids: Iterable[str] | None = None) -> list[KnowledgeItem]:
    """The index's section hits for `query` over the packs this workspace sees, as compiler items."""
    from sqlalchemy import select

    from analystos.db.models import KnowledgeDocument
    from analystos.knowledge.index import retrieve

    hits = retrieve(session, workspace_id, query, limit=candidates, candidates=candidates,
                    kinds=list(kinds) if kinds is not None else None, hop=True, pack_ids=pack_ids)
    ext: dict[str, dict[str, Any]] = {}
    ids = sorted({h.document_id for h in hits})
    if ids:
        for d in session.scalars(select(KnowledgeDocument).where(KnowledgeDocument.id.in_(ids))):
            e = (d.frontmatter or {}).get("analystos")
            ext[d.id] = e if isinstance(e, dict) else {}
    out = []
    for rank, h in enumerate(hits, start=1):
        x = ext.get(h.document_id, {})
        synonyms = [str(s) for s in x.get("synonyms") or [] if isinstance(s, str)]
        name = h.title if h.heading.strip().lower() in _GENERIC_HEADINGS else f"{h.title} § {h.heading}"
        trusted = x["trusted"] if isinstance(x.get("trusted"), bool) else h.status == "stable"
        origin = str(x.get("origin") or (f"pack:{x['domain_pack']}" if x.get("domain_pack") else f"okf:{h.pack_slug}"))
        out.append(KnowledgeItem(
            id=f"{h.document_id}#{h.anchor}", section=KIND_SECTIONS.get(h.kind, "glossary"), name=name[:300],
            text=h.text or "", synonyms=tuple(synonyms), label=str(x.get("display_name") or "") or None,
            source=origin, mapped_columns=tuple(str(c) for c in x.get("mapped_columns") or [] if isinstance(c, str)),
            trusted=bool(trusted), document_id=h.document_id, path=h.path, anchor=h.anchor, sha256=h.document_sha256,
            section_sha256=h.section_sha256, retrieval_rank=rank, lexical_share=h.lexical_share, via=h.via))
    return out


def external_items(results: Iterable[dict[str, Any]]) -> list[KnowledgeItem]:
    """Other providers' results (P4-K09 `ProviderResult.as_dict()`) as untrusted `external` items,
    ranked in the provider's own order; each keeps the provider's receipt (path, anchor, sha256)."""
    out = []
    for res in results or []:
        if not isinstance(res, dict) or res.get("status") != "MATCHED":
            continue
        provider = str(res.get("provider") or "external")
        for rank, it in enumerate(res.get("items") or [], start=1):
            if not isinstance(it, dict) or not str(it.get("text") or "").strip():
                continue
            title = str(it.get("title") or it.get("path") or "")
            heading = str(it.get("heading") or "")
            out.append(KnowledgeItem(
                id=f"{provider}:{it.get('path')}#{it.get('anchor')}", section="external",
                name=(title if heading.strip().lower() in _GENERIC_HEADINGS else f"{title} § {heading}")[:300],
                text=str(it["text"]), source=f"{provider}:{it.get('path')}", trusted=False, path=str(it.get("path") or ""),
                anchor=str(it.get("anchor") or ""), sha256=str(it.get("sha256") or "") or None, retrieval_rank=rank))
    return out


def _run_external(session: Any, workspace_id: str, run_id: str | None) -> list[dict[str, Any]]:
    """The `external` results the run's context agent already fetched: prompts reuse them rather
    than calling a provider (an MCP tool, with its budget and screening) once per model call."""
    if not run_id:
        return []
    from sqlalchemy import select

    from analystos.db.models import Artifact

    art = session.scalar(select(Artifact).where(Artifact.workspace_id == workspace_id, Artifact.run_id == run_id,
                                                Artifact.type == "context_package").order_by(Artifact.created_at.desc()).limit(1))
    ext = (art.content or {}).get("external") if art is not None else None
    return ext if isinstance(ext, list) else []


__all__ = ["KIND_SECTIONS", "NO_MATCH", "PRIMARY_SECTIONS", "SUPPLEMENTARY_SECTIONS", "CompiledContext", "KnowledgeItem",
           "RankedItem", "compile_context", "excerpt", "external_items", "focused_excerpt", "item_score", "load_knowledge",
           "pack_section_items", "rank_items", "render_catalog", "render_stable", "terms"]
