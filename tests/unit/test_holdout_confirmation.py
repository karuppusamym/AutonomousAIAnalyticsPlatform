"""P8-15: discovery on most rows, one locked test on the held-out rows, and a strength label.

Live evidence this pins (the retail journey, `scripts/e2e_full_journey.py` data): order `Status` is random,
yet "unit price differs by status" came back supported (rank-biserial 0.1175 vs the 0.10 minimum); the
planted effect is that Marketplace orders are returned more often. Here the random-status difference must
not come out confirmed (and is weak whenever it is supported), while the Marketplace effect is confirmed
on rows the discovery never read.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tests.unit.test_skills_sqlbuild import SPECS  # noqa: E402

from analystos import methods  # noqa: E402
from analystos.agents.critic import CAUSAL  # noqa: E402
from analystos.contracts.analysis import AnalysisSpec, Derivation, Partition  # noqa: E402
from analystos.contracts.evidence import Validation  # noqa: E402
from analystos.contracts.platform import AnalysisSettings  # noqa: E402
from analystos.contracts.policy import DataScope  # noqa: E402
from analystos.core.errors import InvalidInput  # noqa: E402
from analystos.evidence import confirmation as C  # noqa: E402
from analystos.evidence import holdout as H  # noqa: E402
from analystos.evidence.strength import UNCONFIRMED_NOTE, WEAK_NOTE, grade, qualify  # noqa: E402
from analystos.gateway.validator import validate_sql  # noqa: E402
from analystos.registries.hypotheses import spec_hash  # noqa: E402
from analystos.skills import sqlbuild as sb  # noqa: E402
from analystos.skills.analysis import run_analysis, verify_analysis  # noqa: E402
from analystos.skills.stats import benjamini_hochberg  # noqa: E402
from retail_fixture import ORDER_COLUMNS, retail_duck  # noqa: E402
from skills_fixtures import gateway_scope  # noqa: E402

D = Derivation
ASSET = "retail.orders"
KEY = (["Order ID"], "declared_key")
H3 = AnalysisSpec(method="numeric_by_segment", asset=ASSET, outcome=D(column="Unit Price"), segment=D(column="Status"))
H8 = AnalysisSpec(method="rate_by_segment", asset=ASSET, outcome=D(type="equals", column="Returned", value="Yes"),
                  segment=D(column="Sales Channel"))
FAMILY = {  # one round of plausible hypotheses on the orders table; BH runs over all of them
    "H-3 unit price by status": H3,
    "H-4 unit price by returned": AnalysisSpec(method="numeric_by_segment", asset=ASSET, outcome=D(column="Unit Price"),
                                               segment=D(column="Returned")),
    "H-8 returns by channel": H8,
    "returns by status": AnalysisSpec(method="rate_by_segment", asset=ASSET,
                                      outcome=D(type="equals", column="Returned", value="Yes"), segment=D(column="Status")),
    "quantity by channel": AnalysisSpec(method="numeric_by_segment", asset=ASSET, outcome=D(column="Quantity"),
                                        segment=D(column="Sales Channel")),
    "discount by returned": AnalysisSpec(method="numeric_by_segment", asset=ASSET, outcome=D(column="Discount Pct"),
                                         segment=D(column="Returned")),
}


@pytest.fixture(scope="module")
def duck():
    return retail_duck()


def part(key=KEY, fraction=0.3) -> Partition:
    p, why = H.discovery_partition(fraction=fraction, dialect="duckdb", readable=ORDER_COLUMNS, key=key)
    assert p is not None, why
    return p


def count(duck, p: Partition) -> int:
    sql = sb.to_sql(sb.exp.select(sb.count_star().as_(sb.ident("n"))).from_(sb.table(ASSET)).where(
        sb.partition_predicate(p, "duckdb")), "duckdb")
    return int(duck(sql).records()[0]["n"])


# ------------------------------------------------------------------------------------ partition SQL
@pytest.mark.parametrize("dialect", ["postgres", "tsql", "duckdb"])
@pytest.mark.parametrize("name,spec", SPECS, ids=[n for n, _ in SPECS])
def test_every_method_compiles_the_partition_and_passes_the_gateway(dialect, name, spec):
    scope = gateway_scope(dialect)
    p = Partition(key=["number"], basis="declared_key", fraction=0.3)
    for purpose in sb.METHOD_PURPOSES[spec.method]:
        plain = sb.compile_spec(spec, dialect, purpose=purpose, sample_rows=500)
        cq = sb.compile_spec(spec, dialect, purpose=purpose, sample_rows=500, partition=p)
        assert p.salt not in plain.sql and p.salt in cq.sql
        validate_sql(scope, cq.sql, max_rows=cq.max_rows or 1000)  # the one gateway accepts it


@pytest.mark.parametrize("dialect,marker", [("postgres", "POSITION("), ("duckdb", "STRPOS("), ("tsql", "HASHBYTES('MD5'")])
def test_the_bucket_is_dialect_specific_and_the_split_is_a_threshold(dialect, marker):
    p = Partition(key=["number", "sys_id"], basis="declared_key", fraction=0.3)
    disc = sb.to_sql(sb.partition_predicate(p, dialect), dialect)
    held = sb.to_sql(sb.partition_predicate(p.model_copy(update={"side": "holdout", "claim_locked_at": "t"}), dialect), dialect)
    assert marker in disc and disc.endswith(">= 19661") and held.endswith("< 19661")  # round(0.3 * 65536)
    assert "COALESCE" in disc  # a NULL key value still hashes, so every row lands on one side


def test_sides_are_disjoint_complete_stable_and_about_the_fraction(duck):
    p = part()
    held = p.model_copy(update={"side": "holdout", "claim_locked_at": "t"})
    n_disc, n_held = count(duck, p), count(duck, held)
    assert n_disc + n_held == 6000
    assert 0.27 < n_held / 6000 < 0.33
    assert (count(duck, p), count(duck, held)) == (n_disc, n_held)  # same data -> same partition
    full = part(key=None)
    assert full.basis == "full_row" and full.key == ORDER_COLUMNS
    assert count(duck, full) + count(duck, full.model_copy(update={"side": "holdout", "claim_locked_at": "t"})) == 6000


def test_the_held_out_side_never_compiles_before_a_lock():
    held = part().model_copy(update={"side": "holdout"})
    with pytest.raises(InvalidInput, match="locked"):
        sb.partition_predicate(held, "duckdb")
    with pytest.raises(InvalidInput, match="locked"):
        sb.compile_spec(H8, "duckdb", partition=held)


def test_a_method_that_skips_the_partition_is_refused(monkeypatch):
    method = methods.get("rate_by_segment")
    monkeypatch.setattr(method, "compile", lambda spec, dialect, purpose="primary", sample_rows=50000:
                        sb.CompiledQuery('SELECT 1 AS "n"', {}, dialect=dialect))
    with pytest.raises(InvalidInput, match="without the row partition"):
        sb.compile_spec(H8, "duckdb", partition=part())


def test_no_partition_is_stated_with_a_reason():
    cases = [(dict(fraction=0.0), "off"), (dict(dialect="snowflake"), "no stable row hash"),
             (dict(rows=200, min_rows=100), "too few"), (dict(readable=[]), "no readable column")]
    for over, why in cases:
        kw = dict(fraction=0.3, dialect="duckdb", readable=ORDER_COLUMNS, key=KEY) | over
        p, reason = H.discovery_partition(**kw)
        assert p is None and why in reason, over
    rec = H.partition_record(None, "held-out confirmation is off (analysis.holdout_fraction is 0)")
    assert H.from_record(rec) is None
    assert H.not_evaluated("off").evaluated is False
    ok, why = C.holdout_rule(H.not_evaluated("the table has 200 rows").model_dump(), "x", "higher")
    assert not ok and "not checked on held-out rows: the table has 200 rows" in why


def test_the_key_must_be_readable_or_the_whole_readable_row_is_hashed():
    scope = DataScope(workspace_id="w", user_id="u", role="analyst", assets=[ASSET], columns={ASSET: ORDER_COLUMNS},
                      denied_columns=[f"{ASSET}.Order ID", "*.Customer ID"])
    readable = H.readable_columns(scope, ASSET)
    assert "Order ID" not in readable and "Customer ID" not in readable and "Status" in readable
    p, _ = H.discovery_partition(fraction=0.3, dialect="postgresql", readable=readable, key=KEY)
    assert p is not None and p.basis == "full_row" and p.key == readable


def test_the_fraction_is_a_bounded_platform_setting():
    assert AnalysisSettings().holdout_fraction == 0.3
    AnalysisSettings(holdout_fraction=0)
    with pytest.raises(ValueError):
        AnalysisSettings(holdout_fraction=0.6)


def test_partition_record_round_trips():
    p = part()
    rec = H.partition_record(p, None)
    assert rec["applied"] and rec["description"] == "30% of rows held out by Order ID"
    assert H.from_record(rec) == p


# ------------------------------------------------------------------------------------ lock before read
class Clocked:
    """A RunSQL that notes when each statement ran, to prove no held-out row was read before the lock."""

    def __init__(self, inner):
        self.inner, self.dialect, self.log = inner, inner.dialect, []

    def __call__(self, sql, *, purpose="analysis", max_rows=None):
        self.log.append((H.now_iso(), sql))
        return self.inner(sql, purpose=purpose, max_rows=max_rows)


def test_discovery_reads_never_touch_held_out_rows_and_the_lock_comes_first(duck):
    disc = part()
    clocked = Clocked(duck)
    prim = run_analysis(H8, clocked, partition=disc)
    verify_analysis(H8, clocked, prim.stat, partition=disc)
    held_pred = sb.to_sql(sb.partition_predicate(disc.model_copy(update={"side": "holdout", "claim_locked_at": "t"}),
                                                 "duckdb"), "duckdb")
    disc_pred = sb.to_sql(sb.partition_predicate(disc, "duckdb"), "duckdb")
    assert clocked.log and all(disc_pred in sql and held_pred not in sql for _, sql in clocked.log)
    assert prim.stat.details["partition_side"] == "discovery"
    stat = prim.stat.model_dump()
    top = H.top_of(stat)
    locked = H.lock_claim(spec=H8.model_dump(), stat=stat, top=top, direction=H.direction_of(H8.model_dump(), stat),
                          spec_hash=spec_hash(H8.model_dump()))
    before = len(clocked.log)
    record, out = H.test_on_holdout(H8, clocked, locked, disc, alpha=0.05, min_n=100)
    held_reads = clocked.log[before:]
    assert held_reads and all(ts > locked.locked_at for ts, _ in held_reads)
    assert all(ts < locked.locked_at for ts, _ in clocked.log[:before])
    assert record.claim_locked_at == locked.locked_at < record.partition_accessed_at <= held_reads[0][0]
    assert out.stat.details["partition_side"] == "holdout"
    assert record.claim == locked.claim and record.claim_hash == locked.claim_hash


def test_a_claim_hash_binds_what_was_locked():
    spec = H8.model_dump()
    stat = {"highlights": {"top_segment": "Marketplace", "baseline_segment": "Store"}, "test": "chi_square", "groups": [1, 2, 3]}
    a = H.lock_claim(spec=spec, stat=stat, top="Marketplace", direction="higher", spec_hash="h")
    b = H.LockedClaim({**a.claim, "top": "Web"}, a.locked_at)
    assert a.claim["baseline"] == "Store" and a.claim_hash != b.claim_hash


def test_a_multi_group_claim_is_tested_as_its_own_top_vs_baseline_comparison():
    claim = {"top": "Marketplace", "baseline": "Store", "groups": 3}
    spec, contrast = H.claim_spec(H8, claim)
    assert contrast == ["Marketplace", "Store"] and spec.filters[-1].value == contrast and spec.filters[-1].op == "in"
    for other in ({**claim, "groups": 2}, {**claim, "baseline": "(other)"}, {**claim, "top": "3"}):
        assert H.claim_spec(H8, other) == (H8, None)
    bucketed = H8.model_copy(update={"segment": D(type="bucket", column="Quantity", edges=[1, 2, 3])})
    assert H.claim_spec(bucketed, claim) == (bucketed, None)  # a derived segment cannot be filtered: re-run as is


# ------------------------------------------------------------------------------------ strength
@pytest.mark.parametrize("effect,q,label", [
    (0.1175, 0.0028, "weak"),      # the live H-3: 1.2x the 0.10 minimum
    (0.149, 1e-6, "weak"),         # just under 1.5x
    (0.15, 1e-6, "moderate"),      # 1.5x
    (0.30, 1e-6, "strong"),        # 3x and q < alpha / 100
    (0.30, 0.001, "moderate"),     # 3x but q not under alpha / 100
    (0.30, 0.005, "weak"),         # q within 10x of alpha
    (0.30, 0.0049, "moderate"),
])
def test_strength_boundaries(effect, q, label):
    g = grade({"supported": True, "effect_size": effect, "effect_label": "rank_biserial", "p_adjusted": q}, alpha=0.05)
    assert g is not None and g.label == label and g.threshold == 0.10


def test_strength_of_ratio_measures_is_on_the_log_scale_and_ungraded_without_evidence():
    orr = grade({"supported": True, "effect_size": 1.44, "effect_label": "odds_ratio_top_vs_baseline", "p_value": 1e-9}, alpha=0.05)
    assert orr.label == "moderate" and orr.margin == pytest.approx(2.0, abs=1e-3)
    assert grade({"supported": True, "effect_size": 1 / 1.44, "effect_label": "odds_ratio", "p_value": 1e-9},
                 alpha=0.05).margin == pytest.approx(2.0, abs=1e-3)
    assert grade({"supported": False, "effect_size": 0.9, "effect_label": "cramers_v", "p_value": 1e-9}, alpha=0.05) is None
    gini = grade({"supported": True, "effect_size": 0.9, "effect_label": "gini", "p_value": 1e-9}, alpha=0.05)
    assert gini.label == "moderate" and gini.margin is None  # no minimum effect: graded on q, never strong
    assert grade({"supported": True, "effect_size": 0.9, "effect_label": "gini"}, alpha=0.05) is None
    v = Validation(state="exploratory", label="discovery", strength=gini)
    assert Validation.model_validate(v.model_dump()).strength.label == "moderate"


def test_weak_or_unconfirmed_findings_are_not_worded_as_fact():
    text = "Returns are 11.8% for Sales Channel = Marketplace vs 6.0% for Store."
    both = qualify(text, confirmed=False, strength="weak")
    assert both.startswith(text) and WEAK_NOTE in both and UNCONFIRMED_NOTE in both
    assert qualify(both, confirmed=False, strength="weak") == both  # idempotent
    assert qualify(text, confirmed=True, strength="moderate") == text
    for note in (WEAK_NOTE, UNCONFIRMED_NOTE):
        assert not re.search(r"\d", note) and not CAUSAL.search(note)  # no new numbers, no causal wording


# ------------------------------------------------------------------------------------ end to end, retail data
def investigate(duck, key):
    """The run's statistical path in process: discovery tests -> BH -> strength -> (verified) lock -> held-out
    test once -> the confirmation rule. Returns {name: (discovery stat, strength, holdout record, confirmation)}."""
    disc = part(key=key)
    prim = {n: run_analysis(s, duck, partition=disc).stat.model_dump() for n, s in FAMILY.items()}
    names = [n for n in FAMILY if prim[n].get("p_value") is not None]
    for n, q in zip(names, benjamini_hochberg([prim[n]["p_value"] for n in names]), strict=True):
        prim[n]["p_adjusted"] = float(q)
    out = {}
    for n, spec in FAMILY.items():
        stat = prim[n]
        if not (stat.get("supported") and stat.get("p_adjusted", 1) < 0.05):
            out[n] = (stat, None, None, None)
            continue
        top, direction = H.top_of(stat), H.direction_of(spec.model_dump(), stat)
        locked = H.lock_claim(spec=spec.model_dump(), stat=stat, top=top, direction=direction, spec_hash=spec_hash(spec.model_dump()))
        record, _ = H.test_on_holdout(spec, duck, locked, disc, alpha=0.05, min_n=100)
        conf = C.evaluate(verified=True, origin="agent", top=top, direction=direction, holdout=record.model_dump())
        out[n] = (stat, grade(stat, alpha=0.05), record, conf)
    return out


def test_retail_random_status_is_not_confirmed_and_the_planted_marketplace_effect_is(duck):
    res = investigate(duck, KEY)
    h8_stat, h8_strength, h8_hold, h8_conf = res["H-8 returns by channel"]
    assert h8_stat["highlights"]["top_segment"] == "Marketplace"
    assert h8_conf.passed and h8_conf.rule == "holdout_partition", h8_conf.evaluated
    assert h8_hold.top == "Marketplace" and h8_hold.contrast and h8_hold.contrast[0] == "Marketplace"
    assert h8_hold.claim_locked_at < h8_hold.partition_accessed_at and h8_hold.n >= 100
    assert h8_strength.label in ("moderate", "strong")
    h3_stat, h3_strength, _, h3_conf = res["H-3 unit price by status"]
    assert not (h3_conf and h3_conf.passed)
    assert h3_conf is not None or not (h3_stat.get("supported") and h3_stat.get("p_adjusted", 1) < 0.05)
    for n, (stat, _strength, record, conf) in res.items():  # a confirmation always came from the held-out rule
        if conf is not None and conf.passed:
            assert record.supported and record.top == H.top_of(stat), n
    again = investigate(duck, KEY)  # same data -> same partition -> same result
    for n in res:
        a, b = res[n], again[n]
        assert a[0] == b[0] and (a[3].passed if a[3] else None) == (b[3].passed if b[3] else None), n
        if a[2] is not None:
            assert a[2].model_dump(exclude={"claim_locked_at", "partition_accessed_at", "claim_hash"}) == \
                b[2].model_dump(exclude={"claim_locked_at", "partition_accessed_at", "claim_hash"})


def test_retail_random_status_when_the_whole_row_is_the_key(duck):
    """With no key known the row itself is hashed. The random-status price difference then passes on the
    discovery rows, but only just (weak) and the held-out rows do not support it: it is never confirmed."""
    stat, strength, record, conf = investigate(duck, None)["H-3 unit price by status"]
    assert stat["supported"] and strength.label == "weak"
    assert record.evaluated and not record.supported and not conf.passed
    assert "does not support" in next(e["reason"] for e in conf.evaluated if e["rule"] == "holdout_partition")


# ------------------------------------------------------------------------------------ summary and API standing
def test_the_run_summary_names_leads_and_weak_findings_deterministically():
    from types import SimpleNamespace

    from analystos.agents.supervisor import finding_standing, standing_note
    from analystos.api.serialize import standing

    confirmed = SimpleNamespace(validation="confirmed", evidence_bundle={"validation": {"strength": {"label": "moderate"}}})
    lead = SimpleNamespace(validation="exploratory", evidence_bundle={"validation": {"strength": {"label": "weak"},
                                                                                    "holdout": {"evaluated": True}}})
    facts = [{"code": "I-1", "standing": finding_standing(confirmed)}, {"code": "I-2", "standing": finding_standing(lead)}]
    note = standing_note(facts)
    assert "not established facts: I-2." in note and "Weak evidence" in note and "I-1" not in note
    assert standing_note(facts[:1]) == ""
    assert standing(lead.evidence_bundle) == {"strength": {"label": "weak"}, "holdout": {"evaluated": True}}
    assert standing(None) == {"strength": None, "holdout": None}
