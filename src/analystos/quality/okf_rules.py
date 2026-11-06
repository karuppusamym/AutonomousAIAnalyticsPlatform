"""Quality rules as code (P1-7): an OKF v0.2 pack of a workspace's active rules (`knowledge/okf.py`), and its import.

Export: one document per active rule, type `Quality Rule`, the rule under `analystos.quality_rule` (exactly the
validated `QualityRule`, no database ids), plus `index.md` and `log.md`. Deterministic: paths derive from the asset
and the rule's content hash, YAML keys are sorted, no timestamps. So export -> import -> approve -> export gives the
same bytes. Import: every document that parses as a rule lands as a `quality_rule` review draft
(`origin=import:okf`); nothing runs until an owner approves it (CLAUDE.md rule 7: imported text is data).
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from analystos.contracts.quality import QualityRule
from analystos.core.errors import InvalidInput
from analystos.core.ids import stable_hash
from analystos.knowledge import okf

TYPE = "Quality Rule"
EXTENSION_KEY = "quality_rule"
DIRECTORY = "quality-rules"
ORIGIN = "import:okf"
MAX_DOCUMENTS = 2000


def _slug(text: str) -> str:
    from analystos.knowledge.entries import slugify

    return slugify(text, 60)


def document(rule: QualityRule) -> tuple[str, str]:
    """(path, text) of one rule."""
    fm = {"type": TYPE, "title": rule.key(), "status": "stable", "tags": ["quality_rule", str(rule.dimension)],
          "analystos": {EXTENSION_KEY: rule.model_dump(mode="json")}}
    if rule.description:
        fm["description"] = rule.description
    body = (f"# Quality rule\n\n{rule.description or rule.key()}\n\n"
            f"Dimension: {rule.dimension}. Severity: {rule.severity}. Pass threshold: {rule.threshold:g}.")
    return f"{DIRECTORY}/{_slug(rule.asset)}-{rule.content_hash()[:12]}.md", okf.render_document(fm, body)


def export_files(rules: list[QualityRule]) -> dict[str, bytes]:
    files: dict[str, str] = {}
    for rule in rules:
        path, text = document(rule)
        files[path] = text
    lines = [f"- [{p.rsplit('/', 1)[-1][:-3]}]({p})" for p in sorted(files)]
    files["index.md"] = "---\nokf_version: '0.2'\n---\n\n# Quality rules\n\n" + ("\n".join(lines) or "(empty)") + "\n"
    files["log.md"] = f"# Export log\n\nQuality rules exported: {len(rules)}.\n"
    data = {p: t.encode("utf-8") for p, t in sorted(files.items())}
    problems = okf.check_publish_policy(data)
    if problems:  # pragma: no cover - a renderer bug, never the caller's input
        raise InvalidInput("the export is not a valid OKF pack", details={"problems": [p.as_dict() for p in problems]})
    return data


def parse_files(files: dict[str, bytes]) -> tuple[list[tuple[str, QualityRule]], list[dict[str, str]]]:
    """(rules with their paths, problems). Pure. A document that is not a quality rule is reported, not guessed."""
    problems = okf.check_conformance(files)
    if problems:
        raise InvalidInput("the pack is not a conformant OKF v0.2 bundle",
                           details={"problems": [p.as_dict() for p in problems[:50]]})
    docs = sorted(p for p in files if okf.is_markdown(p) and p.rsplit("/", 1)[-1] not in okf.RESERVED)
    if len(docs) > MAX_DOCUMENTS:
        raise InvalidInput(f"a rules pack may hold at most {MAX_DOCUMENTS} documents")
    rules: list[tuple[str, QualityRule]] = []
    errors: list[dict[str, str]] = []
    for path in docs:
        doc = okf.parse_document(path, files[path])
        raw = doc.extension.get(EXTENSION_KEY)
        if doc.type != TYPE or not isinstance(raw, dict):
            errors.append({"path": path, "error": "not_a_quality_rule"})
            continue
        try:
            rules.append((path, QualityRule.model_validate(raw)))
        except Exception as exc:  # noqa: BLE001 - a bad document is reported, the rest still import
            errors.append({"path": path, "error": "invalid_rule", "detail": str(exc).splitlines()[0][:300]})
    return rules, errors


def export_pack(session: Session, user: Any, workspace_id: str) -> dict[str, Any]:
    from analystos.governance.policy import require_role
    from analystos.quality.service import list_rules

    require_role(session, user, workspace_id, "viewer")
    rules = [QualityRule.model_validate(r.definition) for r in list_rules(session, workspace_id, status="active")]
    files = export_files(rules)
    return {"files": files, "documents": len(rules),
            "digest": okf.content_digest({p: stable_hash(t.decode("utf-8")) for p, t in files.items()})}


def import_pack(session: Session, user: Any, workspace_id: str, data: bytes | dict[str, bytes]) -> dict[str, Any]:
    """Queue every rule of a pack as a review draft. Needs editor; never activates a rule."""
    from analystos.governance.audit import audit
    from analystos.governance.policy import require_role
    from analystos.knowledge import bundle
    from analystos.knowledge.suggestions import field, propose

    require_role(session, user, workspace_id, "editor")
    if isinstance(data, dict):
        files = dict(data)
    else:
        try:
            files, _ = bundle.read_zip(data)
        except Exception as exc:  # noqa: BLE001 - any unreadable archive is the caller's input error
            raise InvalidInput(f"not a readable OKF pack: {exc}") from None
    rules, errors = parse_files(files)
    digest = okf.content_digest({p: stable_hash(files[p].decode("utf-8", "replace")) for p in sorted(files)})
    queued, skipped = [], 0
    for path, rule in rules:
        prov = {"source": ORIGIN, "path": path, "pack": digest[:16]}
        text = rule.description or f"{rule.key()} (dimension {rule.dimension}, threshold {rule.threshold:g})"
        row = propose(session, workspace_id, kind="quality_rule", subject=f"quality:okf:{rule.content_hash()[:32]}",
                      title=f"Quality rule: {rule.key()}"[:300],
                      fields={"statement": field(text, 1.0, **prov), "rule": field(rule.model_dump(mode="json"), 1.0, **prov)},
                      origin=ORIGIN, proposed_by=f"user:{user.id}", batch=digest[:80])
        if row is None:
            skipped += 1
        else:
            queued.append(row.id)
    audit(f"user:{user.id}", "quality_rule.imported", workspace_id=workspace_id, target=digest[:40], decision="allow",
          details={"documents": len(rules) + len(errors), "queued": len(queued), "skipped": skipped,
                   "errors": len(errors)}, session=session)
    return {"documents": len(rules) + len(errors), "suggestions": queued, "skipped": skipped, "errors": errors,
            "pack_digest": digest, "note": "imported rules are review drafts until an owner approves them"}
