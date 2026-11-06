"""Retrieval benchmark for the context compiler (P4-K05).

Corpus: a labelled workspace pack (tests/fixtures/okf/context-benchmark.json: multi-section documents
with links) installed into a workspace; questions are labelled with the section that answers them.
Configurations, each producing a ranked list per question:

* `index_ts_rank_cd`     P4-K01 retrieval: `ts_rank_cd` + vector, RRF
* `index_bm25`           BM25 + vector, RRF
* `index_bm25_hop`       ... plus one hop over the link graph
* `compiler_t03`         P4-T03 selection: whole documents, term overlap, relevance gate
* `compiler_k05`         P4-K05 selection: index sections fused with term overlap, gate incl. hop

The compiler configurations count only what passes the relevance gate (what could reach a prompt);
T03 sends the first `item_chars` of a whole document, so it is credited with a section only when
that excerpt holds it. Metrics per configuration: document recall@1/@3/@5 and MRR, and section
(path#anchor) MRR and recall@3. Needs the control-plane database (the index is Postgres).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from analystos.knowledge import okf

CONTEXT_SET = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "okf" / "context-benchmark.json"
PARAPHRASE_SET = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "okf" / "paraphrase-benchmark.json"
CONFIGS = ("index_ts_rank_cd", "index_bm25", "index_bm25_hop", "compiler_t03", "compiler_k05")


def load(path: Path = CONTEXT_SET) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def render(doc: dict[str, Any]) -> bytes:
    ext: dict[str, Any] = {"kind": doc["kind"], "origin": "benchmark", "trusted": True}
    if doc.get("synonyms"):
        ext["synonyms"] = doc["synonyms"]
    body = "\n\n".join(f"# {s['heading']}\n\n{s['text']}" for s in doc["sections"])
    if doc.get("links"):
        body += "\n\nSee also: " + ", ".join(f"[{p.rsplit('/', 1)[-1][:-3]}](/{p})" for p in doc["links"])
    fm = {"type": {"term": "Glossary Term", "metric": "Metric", "rule": "Business Rule"}[doc["kind"]], "title": doc["title"],
          "analystos": ext}
    return okf.render_document(fm, body).encode()


def install(session: Session, workspace_id: str, data: dict[str, Any], *, author: str = "process:benchmark") -> str:
    """Commit the benchmark documents into the workspace pack; returns the pack id."""
    from analystos.knowledge import store

    pack = store.workspace_pack(session, workspace_id)
    store.commit(session, pack, {d["path"]: render(d) for d in data["documents"]}, author=author,
                 reason="context retrieval benchmark corpus", origin="benchmark", merge=True)
    return pack.id


def _dedupe(keys: list[str]) -> list[str]:
    return list(dict.fromkeys(keys))


def rankings(session: Session, workspace_id: str, question: str, pack_ids: list[str], *, min_relevance: float = 0.15,
             item_chars: int = 240) -> dict[str, list[str]]:
    """`path#anchor` rankings of every configuration for one question."""
    from sqlalchemy import select

    from analystos.context.compiler import KnowledgeItem, excerpt, item_score, pack_section_items, rank_items, terms
    from analystos.db.models import KnowledgeSection
    from analystos.knowledge.entries import pack_entries
    from analystos.knowledge.index import retrieve

    out: dict[str, list[str]] = {}
    for name, kw in (("index_ts_rank_cd", {"lexical": "ts_rank_cd"}), ("index_bm25", {}), ("index_bm25_hop", {"hop": True})):
        hits = retrieve(session, workspace_id, question, limit=20, pack_ids=pack_ids, **kw)
        out[name] = [f"{h.path}#{h.anchor}" for h in hits]
    q = terms(question)
    # P4-T03: whole documents as entries, term overlap, gate. The prompt carries the first
    # `item_chars` of the document, so a section counts as delivered only when that excerpt holds it.
    docs = [e for e in pack_entries(session, workspace_id) if e.pack_id in pack_ids]
    items = [KnowledgeItem(id=e.id, section="glossary", name=e.name, text=" ".join([e.body, *(f"({s})" for s in e.synonyms)]),
                           source=e.origin, path=e.path) for e in docs]
    scored = sorted(((item_score(i, q, set()), i) for i in items), key=lambda si: (-si[0], si[1].name))
    out["compiler_t03"] = []
    for s, i in scored:
        if s >= min_relevance and s > 0:
            sent = excerpt(i.text, item_chars)
            delivered = [f"{i.path}#{sec.anchor}" for sec in session.scalars(
                select(KnowledgeSection).where(KnowledgeSection.document_id == i.id).order_by(KnowledgeSection.ordinal))
                if " ".join(sec.text.split())[:60] in sent]
            out["compiler_t03"].extend(delivered or [f"{i.path}#-"])
    ranked = rank_items(pack_section_items(session, workspace_id, question, None, pack_ids=pack_ids), q, set(), min_relevance)
    out["compiler_k05"] = [f"{r.item.path}#{r.item.anchor}" for r in ranked if r.passed]
    return out


def _metrics(ranks: list[list[str]], expected: list[str]) -> dict[str, float]:
    n = len(expected)
    doc_r = {1: 0, 3: 0, 5: 0}
    doc_rr = sec_rr = 0.0
    sec_r3 = 0
    for ranking, exp in zip(ranks, expected, strict=True):
        path = exp.split("#", 1)[0]
        docs = _dedupe([r.split("#", 1)[0] for r in ranking])
        if path in docs:
            k = docs.index(path) + 1
            doc_rr += 1.0 / k
            for cut in doc_r:
                doc_r[cut] += k <= cut
        secs = _dedupe(ranking)
        if exp in secs:
            k = secs.index(exp) + 1
            sec_rr += 1.0 / k
            sec_r3 += k <= 3
    return {"doc_recall_at_1": round(doc_r[1] / n, 4), "doc_recall_at_3": round(doc_r[3] / n, 4),
            "doc_recall_at_5": round(doc_r[5] / n, 4), "doc_mrr": round(doc_rr / n, 4),
            "section_recall_at_3": round(sec_r3 / n, 4), "section_mrr": round(sec_rr / n, 4)}


def run(session: Session, workspace_id: str, pack_ids: list[str], questions: list[dict[str, Any]]) -> dict[str, Any]:
    per_q = [rankings(session, workspace_id, q["q"], pack_ids) for q in questions]
    expected = [q["expected"] for q in questions]
    report: dict[str, Any] = {"questions": len(questions), "configs": {}}
    for c in CONFIGS:
        report["configs"][c] = {"all": _metrics([r[c] for r in per_q], expected)}
        for t in sorted({q.get("type", "all") for q in questions}):
            idx = [i for i, q in enumerate(questions) if q.get("type") == t]
            report["configs"][c][t] = _metrics([per_q[i][c] for i in idx], [expected[i] for i in idx])
    report["misses"] = {c: [{"q": q["q"], "expected": q["expected"], "top": r[c][:3]}
                            for q, r in zip(questions, per_q, strict=True)
                            if q["expected"].split("#", 1)[0] not in [x.split("#", 1)[0] for x in r[c][:1]]]
                        for c in ("index_ts_rank_cd", "compiler_t03", "compiler_k05")}
    return report


def paraphrase_run(session: Session, workspace_id: str, platform_pack_id: str, path: Path = PARAPHRASE_SET) -> dict[str, Any]:
    """The P4-K10 paraphrase set over the platform pack through the index (document level): K01's
    `ts_rank_cd` leg against BM25, both fused with the vector leg."""
    from analystos.db.models import KnowledgeDocument
    from analystos.knowledge.index import retrieve

    labelled = json.loads(path.read_text(encoding="utf-8"))["questions"]
    titles = {d.path: d.title for d in session.query(KnowledgeDocument).filter(KnowledgeDocument.pack_id == platform_pack_id)}
    out: dict[str, Any] = {"questions": len(labelled)}
    for name, kw in (("index_ts_rank_cd", {"lexical": "ts_rank_cd"}), ("index_bm25", {})):
        ranks = []
        for q in labelled:
            hits = retrieve(session, workspace_id, q["q"], limit=20, pack_ids=[platform_pack_id], **kw)
            ranks.append([f"{titles.get(h.path, h.title)}#x" for h in hits])
        m = _metrics(ranks, [f"{q['expected']}#x" for q in labelled])
        out[name] = {k: v for k, v in m.items() if k.startswith("doc_")}
    return out


def index_first_savings(session: Session, user: Any, workspace_id: str, objective: str, *, purpose: str = "benchmark",
                        budget_tokens: int = 20_000) -> dict[str, Any]:
    """P2-8: what reading the context index first costs against the flat bundle, for one objective: tokens used,
    items carried and omissions of each. The index-first bundle opens with a digest of what exists and carries each
    item as a short pointer, so it should use fewer tokens for the same receipts."""
    from analystos.context.retrieval import compile_bundle

    out: dict[str, Any] = {"objective": objective}
    for name, first in (("flat", False), ("index_first", True)):
        b = compile_bundle(session, user, workspace_id, purpose=purpose, objective=objective, budget_tokens=budget_tokens,
                           index_first=first)["bundle"]
        out[name] = {"tokens": b["budget_used"]["tokens"], "items": len(b["receipts"]), "omissions": len(b["omissions"])}
    flat, first_tokens = out["flat"]["tokens"], out["index_first"]["tokens"]
    out["tokens_saved"] = flat - first_tokens
    out["saved_ratio"] = round((flat - first_tokens) / flat, 4) if flat else 0.0
    return out
