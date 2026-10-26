from types import SimpleNamespace

import pytest
import sqlglot
from sqlglot import exp

from analystos.agents import ask_rules, ask_samples


def column(name, personal=False):
    return ask_rules.Column(name=name, data_type="text", role="identifier" if name.endswith("_id") else "dimension",
                            semantic="categorical", distinct=5, ordinal=0, words=frozenset({name}),
                            name_words=frozenset({name}), glossary=False, personal=personal, label=name)


@pytest.fixture
def context(monkeypatch):
    orders = ask_rules.Table(fq="retail.orders", source_id="src", dialect="postgres", entity="order",
                            entity_words=frozenset({"order"}), columns=[column("order_id"), column("customer_id"), column("status"), column("email", True)])
    customers = ask_rules.Table(fq="retail.customers", source_id="src", dialect="postgres", entity="customer",
                               entity_words=frozenset({"customer"}), columns=[column("customer_id"), column("segment"), column("email", True)])
    orders.joins = [ask_rules.Join(from_column="customer_id", to_column="customer_id", target=customers)]
    monkeypatch.setattr(ask_rules, "load_tables", lambda _: ([orders, customers], None))
    return SimpleNamespace(previous_answer={"id":"previous", "sql":"SELECT COUNT(*) FROM retail.orders WHERE status = 'Open'"})


@pytest.mark.parametrize("question", ["list down few same order", "Show 5 sample orders", "List a few sample records"])
def test_samples_preserve_the_prior_population_and_exclude_personal_fields(context, question):
    plan = ask_samples.sample_plan(context, question)
    assert plan["parent_turn_id"] == "previous"
    assert plan["inherited_filter"]
    assert "'Open'" in plan["sql"] and "email" not in plan["sql"]
    assert plan["limit"] == 5


def test_explicit_new_table_does_not_inherit_order_filters(context):
    plan = ask_samples.sample_plan(context, "Show 5 sample customers")
    assert plan["table"] == "retail.customers"
    assert not plan["inherited_filter"] and plan["parent_turn_id"] is None


def test_related_details_preserve_sample_filter_on_a_followup(context):
    first = ask_samples.sample_plan(context, "Show 5 sample orders")
    context.previous_answer = {"id":"sample", "sql":first["sql"], "rules":{"shape":"sample"}}
    plan = ask_samples.sample_plan(context, "Show 5 sample orders with related details")
    assert plan["inherited_filter"] and plan["joined"] == ["retail.customers"]
    tree = sqlglot.parse_one(plan["sql"])
    assert len(list(tree.find_all(exp.Join))) == 1
    assert "email" not in plan["sql"] and "'Open'" in plan["sql"]


@pytest.mark.parametrize("question", ["Show 100 sample orders", "List few same orders for this customer", "Delete 5 sample orders", "Show 5 duplicate orders", "Show 5 sample orders where status is Closed"])
def test_unsupported_or_qualified_requests_do_not_get_simplified(context, question):
    assert ask_samples.sample_plan(context, question) is None


def test_same_is_not_silently_changed_without_matching_prior_context(context):
    context.previous_answer = None
    assert ask_samples.sample_plan(context, "list down few same order") is None


def test_no_invented_relationship(context, monkeypatch):
    tables, _ = ask_rules.load_tables(context)
    tables[0].joins = []
    assert ask_samples.sample_plan(context, "Show 5 sample orders with related details") is None


def test_changed_access_to_filter_column_requires_the_normal_refusal_path(context, monkeypatch):
    tables, _ = ask_rules.load_tables(context)
    tables[0].columns = [c for c in tables[0].columns if c.name != "status"]
    assert ask_samples.sample_plan(context, "Show 5 sample orders") is None
