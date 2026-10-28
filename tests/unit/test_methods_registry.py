"""Analysis methods as plugins (P4-X04, spec v3 §3.5): the closed vocabulary and everything derived
from it come from the method registry, and a new method is one module plus one manifest."""
from __future__ import annotations

import ast
import io
import re
import tokenize
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from analystos import methods
from analystos.agents.insight import template_text
from analystos.agents.investigator import identity_keys, validate_spec
from analystos.agents.prompts import prompt
from analystos.capabilities import registry as cap_registry
from analystos.contracts.analysis import AnalysisSpec, Derivation
from analystos.methods import registry as method_registry
from analystos.methods.base import AnalysisMethod, AnalysisOutcome, ChartIntent, Method
from analystos.services.changes import claim_key
from analystos.skills import sqlbuild as sb

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "analystos"
PORTED = ["rate_by_segment", "numeric_by_segment", "trend", "pareto", "correlation", "driver_model"]
NEW = ["cohort_retention", "contribution_decomposition"]
PACK = ["seasonality", "level_shift", "forecast_backtest", "distribution_shift"]  # S07-T6 time-series pack
# Files that may name a method although they are outside src/analystos/methods/, and why.
ALLOWED = {"skills/stats.py": "statistical primitives label their result family (StatResult.method); the method "
                              "runner (skills/analysis) stamps the calling method's own name on every result"}


# ------------------------------------------------------------------------------------ the vocabulary
def test_every_builtin_method_is_one_module_plus_one_manifest():
    assert methods.names() == PORTED + NEW + PACK
    for name in methods.names():
        m = methods.current().manifests[name]
        assert m.id == f"method.{name}" and m.kind == "Method" and m.source == "builtin"
        assert (SRC / "methods" / f"{name}.py").is_file() and (SRC / "methods" / f"{name}.yaml").is_file()
        assert m.entry == f"python:analystos.methods.{name}:" + type(methods.get(name)).__name__
        assert isinstance(methods.get(name), Method) and methods.get(name).name == name
        assert m.side_effect == "read_source" and m.certification.status == "certified"
        assert (ROOT / m.certification.evidence).is_file(), m.certification.evidence
        assert "primary" in methods.get(name).purposes and methods.get(name).vocabulary
    analysis = [m for m in cap_registry.load(packs_dir=None, entry_points=False).list("Method")
                if (m.spec or {}).get("family") != "ml"]  # the ML method pack (P5-04) is not analysis vocabulary
    assert {m.id for m in analysis} == {f"method.{n}" for n in PORTED + NEW + PACK}


def test_prompt_schema_validation_and_purposes_are_derived():
    text = prompt("hypothesis_generation.v1")
    assert text.startswith("You are the Investigation Agent") and "{method_vocabulary}" not in text
    for m in methods.all_methods():
        assert f"- {m.name}: {m.vocabulary}" in text
    schema = AnalysisSpec.model_json_schema()["properties"]["method"]
    assert schema["enum"] == methods.names() and all(n in schema["description"] for n in methods.names())
    with pytest.raises(ValidationError, match="unknown analysis method 'regression'"):
        AnalysisSpec(method="regression", asset="s.t")
    assert dict(sb.METHOD_PURPOSES) == {n: methods.get(n).purposes for n in methods.names()}


def test_applicable_matches_the_method_shapes():
    assert methods.get("rate_by_segment").applicable("boolean", "categorical")
    assert not methods.get("rate_by_segment").applicable("numeric", "categorical")
    assert methods.get("numeric_by_segment").applicable("numeric", "categorical")
    assert methods.get("trend").applicable(None, None) and not methods.get("trend").applicable(None, "categorical")
    assert methods.get("cohort_retention").applicable("identifier", None)
    assert methods.get("contribution_decomposition").applicable("boolean", "categorical")
    assert methods.column_type(Derivation(type="bucket", column="x", edges=[0, 1])) == "categorical"
    assert methods.column_type(Derivation(column="x"), "id") == "identifier"


# ------------------------------------------------------------------------------------ no hardcoded names
def _string_hits(path: Path, rx: re.Pattern) -> list[int]:
    if path.suffix != ".py":
        return [i for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1) if rx.search(line)]
    out = []
    for tok in tokenize.generate_tokens(io.StringIO(path.read_text(encoding="utf-8")).readline):
        if (tok.type in (tokenize.STRING, tokenize.COMMENT) or tok.type == getattr(tokenize, "FSTRING_MIDDLE", -1)) \
                and rx.search(tok.string):
            out.append(tok.start[0])
    return out


def _consts(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        return [v for e in node.elts for v in _consts(e)]
    return []


def _method_uses(tree: ast.AST, names: set[str]) -> list[int]:
    """Where code treats a string literal as a method name: compared with something called `method`,
    or given as a `method` key / keyword. (`trend` and `correlation` are also chart intents, so a bare
    literal is not a hit.)"""
    hits = []
    aliases = {t.id for node in ast.walk(tree) if isinstance(node, ast.Assign) and "method" in ast.unparse(node.value)
               for t in node.targets if isinstance(t, ast.Name)}  # m = spec.method / spec.get("method")

    def refers(p: ast.AST) -> bool:
        return "method" in ast.unparse(p) or (isinstance(p, ast.Name) and p.id in aliases)

    for node in ast.walk(tree):
        if isinstance(node, ast.Compare):
            parts = [node.left, *node.comparators]
            if any(refers(p) for p in parts) and any(set(_consts(p)) & names for p in parts):
                hits.append(node.lineno)
        elif isinstance(node, ast.Dict):
            keyed = [v for k, v in zip(node.keys, node.values, strict=False) if k is not None and set(_consts(k)) & names]
            if len(keyed) >= 2 and not all(isinstance(v, ast.Constant) for v in keyed):  # a per-method dispatch table
                hits.append(node.lineno)  # (a map of chart intents to chart types is not one)
            for k, v in zip(node.keys, node.values, strict=False):
                if k is not None and _consts(k) == ["method"] and set(_consts(v)) & names:
                    hits.append(node.lineno)
        elif isinstance(node, ast.keyword) and node.arg == "method" and set(_consts(node.value)) & names:
            hits.append(node.value.lineno)
    return sorted(hits)


def test_no_method_name_is_hardcoded_outside_the_methods_package():
    names = set(methods.names())
    words = re.compile(r"\b(" + "|".join(sorted(n for n in names if "_" in n)) + r")\b")
    hits: dict[str, list[int]] = {}
    for path in sorted(SRC.rglob("*")):
        rel = path.relative_to(SRC).as_posix()
        if not path.is_file() or rel.startswith("methods/") or "__pycache__" in rel or rel in ALLOWED \
                or path.suffix not in (".py", ".yaml", ".yml", ".json", ".md", ".sql", ".j2", ".html"):
            continue
        lines = _string_hits(path, words)
        if path.suffix == ".py":
            lines += _method_uses(ast.parse(path.read_text(encoding="utf-8")), names)
        if lines:
            hits[rel] = sorted(set(lines))
    assert not hits, f"method names hardcoded outside src/analystos/methods/ (derive them from the registry): {hits}"
    assert all((SRC / f).is_file() for f in ALLOWED), "stale allow-list entry"


def test_the_grep_catches_a_hardcoded_branch():
    tree = ast.parse('if spec.method == "trend":\n    pass\nx = {"method": "pareto"}\nf(method="correlation")\n'
                     'y = "trend"  # a chart intent is not a hit\nRUN = {"trend": run_trend, "pareto": run_pareto}\n'
                     'm = spec.get("method")\nif m in ("pareto", "x"):\n    pass\n')
    assert _method_uses(tree, set(PORTED)) == [1, 3, 4, 6, 8]


# ------------------------------------------------------------------------------------ the extension point
class RowShare(AnalysisMethod):
    """A third-party method shipped as an entry point: no platform file is edited to add it."""

    name = "row_share"
    vocabulary = "share of rows where a boolean outcome holds (no segment)."
    outcome_types = frozenset({"boolean"})

    def validate(self, spec, semantic):
        return [] if spec.outcome is not None else ["row_share needs an outcome"]

    def compile(self, spec, dialect, *, purpose="primary", sample_rows=50000):
        from sqlglot import exp

        q = exp.select(exp.Avg(this=sb.derive(spec.outcome, dialect)).as_(sb.ident("share"))).from_(sb.table(spec.asset))
        return sb.CompiledQuery(sb.to_sql(q, dialect), {"share": "share"}, [], purpose, dialect, 1, "aggregate")

    def test(self, rows, spec, *, alpha=0.05):
        from analystos.contracts.analysis import StatResult

        share = rows["primary"][0]["share"]
        return AnalysisOutcome(method=self.name, stat=StatResult(method=self.name, test="share", n=1, effect_size=share,
                                                                 highlights={"share": share}, supported=share > 0.5))

    def template_text(self, spec: Mapping[str, Any], stat: Mapping[str, Any]) -> tuple[str, str] | None:
        return "Most rows qualify", f"{stat['highlights']['share'] * 100:.0f}% of rows qualify."

    def chart_intent(self, spec, stat=None):
        return ChartIntent(intent="kpi", dimension="none")


@pytest.fixture
def plugin_registry():
    manifest = {"apiVersion": "analystos/v1", "kind": "Method", "id": "method.row_share", "version": "0.1.0",
                "summary": "Share of rows where an outcome holds", "entry": "python:tests.unit.test_methods_registry:RowShare",
                "side_effect": "read_source", "spec": {"order": 90}}
    method_registry.reset(method_registry.build(extra=[("entrypoint:acme-methods", manifest)], entry_points=False))
    yield
    method_registry.reset()


def test_a_plugin_method_joins_every_derived_artefact(plugin_registry):
    from tests.skills_fixtures import duck_dataset

    from analystos.skills.analysis import run_analysis

    assert methods.names()[-1] == "row_share" and "- row_share: share of rows" in prompt("hypothesis_generation.v1")
    assert "row_share" in AnalysisSpec.model_json_schema()["properties"]["method"]["enum"]
    spec = AnalysisSpec(method="row_share", asset="itsm.incident", outcome=Derivation(type="is_true", column="sla_breached"))
    scope = SimpleNamespace(assets=["itsm.incident"], columns={"itsm.incident": ["sla_breached"]}, denied_columns=[])
    assert validate_spec(spec, scope, {}) == []
    assert validate_spec(spec.model_copy(update={"outcome": None}), scope, {}) == ["row_share needs an outcome"]
    out = run_analysis(spec, duck_dataset())
    assert out.stat.method == "row_share" and 0 < out.stat.effect_size < 0.5 and len(out.query_ids) == 1
    assert template_text(out.stat.model_dump(), spec.model_dump())[0] == "Most rows qualify"
    assert claim_key(spec.model_dump(), None)[0] == "row_share" and identity_keys(spec)


def test_a_pack_cannot_declare_a_python_method(tmp_path):
    pack = tmp_path / "packs" / "evil"
    pack.mkdir(parents=True)
    (pack / "m.yaml").write_text("apiVersion: analystos/v1\nkind: Method\nid: method.evil\nsummary: s\n"
                                 "entry: python:os:getcwd\nside_effect: none\n", encoding="utf-8")
    snap = cap_registry.load(packs_dir=tmp_path / "packs", entry_points=False, legacy=False, connectors=False, strict=False)
    assert "method.evil" in snap.manifests  # the capability is visible ...
    original = cap_registry.PACKS_DIR
    try:
        cap_registry.PACKS_DIR = tmp_path / "packs"
        reg = method_registry.build(entry_points=False)
    finally:
        cap_registry.PACKS_DIR = original
    assert "evil" not in reg.names() and any("config-only" in p for p in reg.problems)  # ... but never imported


def test_a_broken_plugin_is_reported_not_fatal():
    bad = {"apiVersion": "analystos/v1", "kind": "Method", "id": "method.broken", "summary": "s",
           "entry": "python:analystos.methods.base:first", "side_effect": "read_source"}
    reg = method_registry.build(extra=[("entrypoint:x", bad)], entry_points=False)
    assert "broken" in reg.names() and "trend" in reg.names()
    with pytest.raises(Exception, match="does not implement the Method protocol"):
        reg.get("broken")
