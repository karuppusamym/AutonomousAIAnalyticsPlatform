"""PILOT-FIX-04: the meaning review asks once, resolves plain fields from the catalog, records absent data as a
limitation, and a recheck asks only what an answer changed (rehearsal card C11, 2026-10-30: 3 rounds)."""
from types import SimpleNamespace

import pytest

from analystos.agents import common
from analystos.context.compiler import CompiledContext
from analystos.contracts.grounding import ResolveIn
from analystos.contracts.policy import DataScope
from analystos.services import context_grounding as svc
from analystos.services import meaning_review as mr

C11 = ("Why are some orders returned, and are late deliveries part of the reason? Compare categories, sales channels "
       "and regions; say what the data cannot show.")
COLUMNS = {"r.orders": ["order_id", "customer_id", "product_id", "order_date", "promised_date", "delivered_date",
                        "sales_channel", "returned", "status"],
           "r.customers": ["customer_id", "region"], "r.products": ["product_id", "category"]}
TYPES = {"order_id": "id", "customer_id": "id", "product_id": "id", "order_date": "datetime", "promised_date": "datetime",
         "delivered_date": "datetime", "sales_channel": "categorical", "returned": "categorical", "status": "categorical",
         "region": "categorical", "category": "categorical"}


@pytest.fixture
def scope():
    return DataScope(workspace_id="w", user_id="u", role="analyst", source_ids=["src"], assets=list(COLUMNS), columns=COLUMNS)


def cat(columns=COLUMNS):
    return [{"ref": f"{a}.{c}", "name": c.replace("_", " ").title(), "type": TYPES.get(c, "categorical"), "source_id": "src"}
            for a, cols in columns.items() for c in cols]


def binding(term, definition, columns, *, id_="bind_1", revision=1, state="approved"):
    return SimpleNamespace(id=id_, term=term, state=state, revision=revision, entry_id=None,
                           definition={"definition": definition, "columns": columns, "kind": "definition", "state": state})


def run_resolve(monkeypatch, scope, *, model=None, bindings=(), previous=(), columns=COLUMNS, question=C11, plan=None):
    monkeypatch.setattr(svc, "resolve_scope", lambda *a, **k: scope)
    monkeypatch.setattr(svc, "candidates", lambda *a: [])
    monkeypatch.setattr(svc, "domains_for", lambda *a: set())
    monkeypatch.setattr(svc, "scoped_bindings", lambda *a: list(bindings))
    monkeypatch.setattr(svc, "catalog", lambda *a: cat(columns))
    monkeypatch.setattr(mr, "previous_rounds", lambda session, run_id: list(previous))
    monkeypatch.setattr(common, "compile_for", lambda *a, **k: CompiledContext(purpose="glossary_suggestion", header={}, body={}))
    monkeypatch.setattr(common, "llm_json", lambda *a, **k: (model, "stub-model") if model is not None else (None, "deterministic"))
    router = SimpleNamespace(mode=lambda p: "auto", available=lambda *a: True)
    agent_ctx = SimpleNamespace(run=SimpleNamespace(id="run_c11", plan=plan or {}))
    return svc.resolve(SimpleNamespace(merge=lambda u: u), object(), "w", ResolveIn(question=question), router=router,
                       call_ctx=SimpleNamespace(), agent_ctx=agent_ctx)


def by_term(report):
    return {r["term"]: r for r in report["resolutions"]}


def test_plain_fields_resolve_from_the_catalog_and_missing_reasons_are_a_limitation_without_a_model(monkeypatch, scope):
    report = run_resolve(monkeypatch, scope)
    resolved = by_term(report)
    for term, ref in [("Sales Channel", "r.orders.sales_channel"), ("Region", "r.customers.region"), ("Category", "r.products.category")]:
        assert resolved[term]["by"] == "catalog" and resolved[term]["columns"] == [ref] and resolved[term]["state"] == "resolved"
    assert not report["needs_clarification"] and report["clarification"] == []
    review = report["meaning_review"]
    assert review["round"] == 1
    assert [(lim["kind"], lim["term"]) for lim in review["limitations"]] == [("absent_data", "reasons")]
    assert "No field in the selected data records reasons" in report["limitations"][0]
    assert {"Sales Channel", "Region", "Category"} <= {r["term"] for r in review["resolved_without_asking"]}


def test_round_one_asks_every_open_term_once_and_turns_absent_data_into_a_limitation(monkeypatch, scope):
    model = {"missing": ["Define the sales channel values", "Define region"],
             "open_terms": [
                 {"term": "sales channel", "question": "What is a sales channel?", "availability": "in_data"},
                 {"term": "return reason", "question": "Which field holds the return reason?", "availability": "absent"},
                 {"term": "late delivery", "question": "What counts as a late delivery?", "availability": "derivable",
                  "columns": ["r.orders.promised_date", "r.orders.delivered_date", "secret.salary"]},
                 {"term": "Late deliveries", "question": "Late again?", "availability": "derivable"}]}
    report = run_resolve(monkeypatch, scope, model=model)
    questions = report["meaning_review"]["questions"]
    assert [q["question"] for q in questions] == ["What counts as a late delivery?"]
    assert questions[0]["columns"] == ["r.orders.promised_date", "r.orders.delivered_date"]  # unauthorised ref dropped
    assert report["clarification"] == ["What counts as a late delivery?"] and report["needs_clarification"]
    kinds = {(lim["kind"], lim["term"]) for lim in report["meaning_review"]["limitations"]}
    assert ("absent_data", "return reason") in kinds
    assert not any(lim["term"] == "reasons" for lim in report["meaning_review"]["limitations"])  # the same gap once


def test_a_model_cannot_turn_a_field_that_exists_into_a_limitation(monkeypatch, scope):
    model = {"open_terms": [{"term": "delivery promise", "question": "Which date is the delivery promise?", "availability": "absent"}]}
    report = run_resolve(monkeypatch, scope, model=model, question="Are deliveries kept to the delivery promise?")
    assert [q["term"] for q in report["meaning_review"]["questions"]] == ["delivery promise"]
    assert report["meaning_review"]["limitations"] == []


def test_recheck_asks_only_what_an_answer_changed(monkeypatch, scope):
    first = run_resolve(monkeypatch, scope, model={"open_terms": [
        {"term": "late delivery", "question": "What counts as a late delivery?", "availability": "derivable"}]})
    previous = [{"round": 1, "asked": [q["key"] for q in first["meaning_review"]["questions"]], "binding_revisions": {}}]
    answer = binding("Late delivery", "Delivered after the promised date, counted in business days",
                     ["r.orders.delivered_date", "r.orders.promised_date"])
    # round 2: the model now raises two new terms; one comes from the owner's answer, one from nowhere
    model = {"open_terms": [
        {"term": "shipping lead time", "question": "How is shipping lead time measured?", "availability": "derivable"},
        {"term": "business days", "question": "Which calendar defines business days?", "availability": "derivable"},
        {"term": "late delivery", "question": "What counts as a late delivery?", "availability": "derivable"}]}
    second = run_resolve(monkeypatch, scope, model=model, bindings=[answer], previous=previous)
    review = second["meaning_review"]
    assert review["round"] == 2
    assert [q["term"] for q in review["questions"]] == ["business days"]  # caused by the answer's wording
    assert [q["term"] for q in review["not_asked"]] == ["shipping lead time"]
    assert {"kind": "unreviewed", "term": "shipping lead time"}.items() <= next(
        lim for lim in review["limitations"] if lim["term"] == "shipping lead time").items()
    assert review["answered"] == ["Late delivery"]
    assert review["pinned"] == [{"id": "bind_1", "term": "Late delivery", "revision": 1}]
    assert by_term(second)["Late delivery"]["by"] == "reviewed_mapping"


def test_a_question_still_open_from_an_earlier_round_is_asked_again_and_nothing_else(monkeypatch, scope):
    model = {"open_terms": [{"term": "late delivery", "question": "What counts as a late delivery?", "availability": "derivable"},
                            {"term": "return window", "question": "How long is the return window?", "availability": "derivable"}]}
    previous = [{"round": 1, "asked": [mr.key_of("late delivery")], "binding_revisions": {}}]
    report = run_resolve(monkeypatch, scope, model=model, previous=previous)
    assert [q["term"] for q in report["meaning_review"]["questions"]] == ["late delivery"]
    assert report["needs_clarification"]


def test_a_conflict_an_answer_creates_is_asked_on_recheck(monkeypatch, scope):
    previous = [{"round": 1, "asked": [], "binding_revisions": {"bind_old": 1}}]
    old = binding("Region", "The customer's region", ["r.customers.region"], id_="bind_old")
    new = binding("Region", "The warehouse region", ["r.customers.region"], id_="bind_new")
    report = run_resolve(monkeypatch, scope, bindings=[old, new], previous=previous, question="Compare returns by region")
    asked = report["meaning_review"]["questions"]
    assert [(q["term"], q["state"]) for q in asked] == [("Region", "ambiguous")]  # two meanings, one question
    assert report["needs_clarification"] and report["meaning_review"]["answered"] == ["Region"]


def test_same_field_name_in_two_tables_is_one_question(monkeypatch, scope):
    columns = {**COLUMNS, "r.orders": [*COLUMNS["r.orders"], "region"]}
    wide = DataScope(**{**scope.model_dump(), "columns": columns})
    report = run_resolve(monkeypatch, wide, columns=columns, question="Compare returns by region")
    asked = report["meaning_review"]["questions"]
    assert len(asked) == 1 and asked[0]["columns"] == ["r.customers.region", "r.orders.region"]
    assert asked[0]["question"].startswith("More than one table has a “region” field")


def test_a_field_never_stands_in_for_a_term_that_needs_a_business_definition(monkeypatch, scope):
    columns = {"r.orders": ["order_id", "backlog"]}
    narrow = DataScope(workspace_id="w", user_id="u", role="analyst", source_ids=["src"], assets=["r.orders"], columns=columns)
    report = run_resolve(monkeypatch, narrow, columns=columns, question="Why is the backlog growing?")
    assert report["needs_clarification"]
    assert any(q["term"] == "backlog" for q in report["meaning_review"]["questions"])
    assert not any(r.get("by") == "catalog" for r in report["resolutions"])


def test_plan_questions_are_collected_up_front(monkeypatch, scope):
    report = run_resolve(monkeypatch, scope, question="Why are orders returned?", plan={"questions": ["Is the sales channel related?"]})
    assert by_term(report)["Sales Channel"]["by"] == "catalog"


def test_finding_caveats_carry_only_absent_data(monkeypatch):
    review = {"limitations": [{"kind": "absent_data", "statement": "No field in the selected data records reasons."},
                              {"kind": "unreviewed", "statement": "Not reviewed by an owner: x."}]}
    outputs = {"hypotheses": {}, "context": {"grounding": {"meaning_review": review}}}
    asked = []

    def scalar(stmt):
        key = next(k for k in outputs if f"'{k}'" in str(stmt.compile(compile_kwargs={"literal_binds": True})))
        asked.append(key)
        return outputs[key]
    assert mr.run_limitations(SimpleNamespace(scalar=scalar), "run_1") == [
        "Data limitation: No field in the selected data records reasons."]
    assert asked == ["hypotheses", "context"]


@pytest.mark.parametrize("term,head", [("reasons for returns", "reason"), ("return reason", "reason"),
                                       ("customer satisfaction", "satisfaction"), ("late delivery", "delivery")])
def test_head_noun(term, head):
    assert mr._head(term) == head


def test_a_glossary_term_without_a_mapping_resolves_to_the_one_field_of_its_name(monkeypatch, scope):
    """The retail pack defines "Sales channel" but maps nothing here: the C11 owner had to define it by hand."""
    monkeypatch.setattr(svc, "candidates", lambda *a: [
        {"id": "ke_sc", "term": "Sales channel", "definition": "Where the order was placed", "columns": [], "synonyms": [], "kind": "term"},
        {"id": "ke_rr", "term": "Return rate", "definition": "Share of orders returned", "columns": [], "synonyms": [], "kind": "metric"}])
    monkeypatch.setattr(svc, "resolve_scope", lambda *a, **k: scope)
    monkeypatch.setattr(svc, "domains_for", lambda *a: set())
    monkeypatch.setattr(svc, "scoped_bindings", lambda *a: [])
    monkeypatch.setattr(svc, "catalog", lambda *a: cat())
    report = svc.resolve(SimpleNamespace(merge=lambda u: u), object(), "w",
                         ResolveIn(question="Compare the return rate by sales channel", use_model=False))
    terms_ = by_term(report)
    assert (terms_["Sales channel"]["state"], terms_["Sales channel"]["columns"], terms_["Sales channel"]["by"]) == (
        "resolved", ["r.orders.sales_channel"], "glossary_and_catalog")
    assert terms_["Return rate"]["state"] == "missing"  # a metric's formula is never taken from a name match
    assert [q["term"] for q in report["meaning_review"]["questions"]] == ["Return rate"]


def test_an_owner_definition_answers_the_plural_the_question_uses(scope):
    owner = binding("Late delivery", "Delivered after the promised date", ["r.orders.delivered_date", "r.orders.promised_date"])
    category = binding("Category", "Product category", ["r.products.category"], id_="bind_2")
    resolved = svc.resolve_known(C11, [], [owner, category], scope)
    assert {r["term"] for r in resolved if r["state"] == "resolved"} == {"Late delivery", "Category"}


def test_a_proposed_metric_for_a_field_nobody_records_is_a_limitation_not_a_question(monkeypatch, scope):
    """C11 round 2 proposed a "return reason" metric with no columns and asked the owner to review its contract."""
    model = {"metric_proposals": [{"term": "return reason", "definition": "Why an order was returned", "columns": [],
                                   "missing_contract_fields": ["return_reason"]}],
             "missing": ["Explicit reasons for returns (no 'return_reason' column in catalog)"]}
    report = run_resolve(monkeypatch, scope, model=model, question="Compare returned orders by sales channel")
    assert report["meaning_review"]["questions"] == [] and not report["needs_clarification"]
    assert [(lim["kind"], lim["term"]) for lim in report["meaning_review"]["limitations"]] == [("absent_data", "return reason")]


def test_a_rule_question_is_the_gate_itself_and_is_asked_on_any_round(monkeypatch, scope):
    previous = [{"round": 1, "asked": [], "binding_revisions": {}}]
    report = run_resolve(monkeypatch, scope, previous=previous, question="Why is the backlog of returned orders growing?")
    assert report["meaning_review"]["round"] == 2
    assert any(q["kind"] == "rule" and q["term"] == "backlog" for q in report["meaning_review"]["questions"])
    assert report["needs_clarification"]
