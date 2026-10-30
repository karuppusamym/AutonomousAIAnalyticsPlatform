"""PILOT-FIX-05: an investigation's dataset carries LEFT JOIN lookups (product category, customer region), and its
approved metrics declare a row grain ("order", "record"). The compiler reads such a dataset from its base table alone
(no uncertified join reaches a governed query, ADR-0019; a LEFT JOIN never removes a base row), the fields over the
joined tables are unavailable with their own reason, any other join is still refused, and a grain the base table
guarantees is accepted."""
from __future__ import annotations

import pytest

from analystos.contracts.policy import DataScope
from analystos.contracts.semantic import SemanticQuery
from analystos.core.errors import InvalidInput
from analystos.semantic.compiler import compile_query, planner_catalog

DATASET_SQL = ('SELECT t."order_id", t."sales_channel", t."delivered_date", t."promised_date", j0."category" AS '
               '"product_category" FROM retail.orders t LEFT JOIN retail.products j0 ON t."product_id" = j0."product_id"')


def catalog(source=DATASET_SQL, grain="order"):
    fields = [("order_id", None), ("sales_channel", {"is_time": False}), ("delivered_date", None), ("promised_date", None),
              ("product_category", {"is_time": False})]
    return {"id": "m", "version": 4, "hash": "h", "synonyms": {}, "relationships": [],
            "datasets": [{"name": "aos_orders", "source": source,
                          "fields": [{"name": n, "expressions": [{"expression": n}], "dimension": d} for n, d in fields]}],
            "metrics": {"late_delivery_rate": {"id": "x", "version": 1, "hash": "xh", "definition": {
                "name": "late_delivery_rate", "dataset": "aos_orders", "display_name": "Late Delivery Rate", "grain": grain,
                "expressions": [{"expression": "AVG(CASE WHEN delivered_date > promised_date THEN 1.0 ELSE 0.0 END)"}],
                "dimensions": ["sales_channel", "product_category"]}}}}


SCOPE = DataScope(workspace_id="w", user_id="u", role="analyst", source_ids=["s"], assets=["retail.orders", "retail.products"],
                  asset_sources={"retail.orders": "s", "retail.products": "s"}, source_dialects={"s": "postgres"},
                  columns={"retail.orders": ["order_id", "product_id", "sales_channel", "delivered_date", "promised_date"],
                           "retail.products": ["product_id", "category"]})


def test_a_left_join_lookup_dataset_answers_governed_from_its_base_table():
    compiled = compile_query(SemanticQuery(metrics=["late_delivery_rate"], dimensions=["sales_channel"]), catalog(), SCOPE)
    assert "JOIN" not in compiled.sql.upper() and '"retail"."orders"' in compiled.sql and "products" not in compiled.sql
    assert compiled.provenance["metrics"][0]["name"] == "late_delivery_rate"


def test_fields_over_the_joined_table_are_unavailable_and_say_why():
    visible = planner_catalog(catalog(), SCOPE)
    assert visible["metrics"]["late_delivery_rate"]["definition"]["dimensions"] == ["sales_channel"]
    assert "product_category" not in {f["name"] for f in visible["datasets"][0]["fields"]}
    with pytest.raises(InvalidInput, match="joined table") as refused:
        compile_query(SemanticQuery(metrics=["late_delivery_rate"], dimensions=["product_category"]), catalog(), SCOPE)
    assert refused.value.details["reason"] == "uncertified_join"


@pytest.mark.parametrize("source", [
    DATASET_SQL.replace("LEFT JOIN", "JOIN"),  # an inner join can remove base rows
    DATASET_SQL.replace("LEFT JOIN", "RIGHT JOIN"),
    DATASET_SQL.replace('t."sales_channel"', '"sales_channel"'),  # unqualified next to a join
    DATASET_SQL + ' WHERE j0."category" = \'Home\'',  # the join filters base rows
])
def test_other_joins_are_still_refused(source):
    with pytest.raises(InvalidInput, match="cardinality has not been certified"):
        compile_query(SemanticQuery(metrics=["late_delivery_rate"], dimensions=["sales_channel"]), catalog(source), SCOPE)


@pytest.mark.parametrize(("grain", "ok"), [("order", True), ("orders", True), ("record", True), ("all", True),
                                           ("customer", False), ("order line", False)])
def test_a_grain_the_base_table_guarantees_is_accepted(grain, ok):
    query = SemanticQuery(metrics=["late_delivery_rate"], dimensions=["sales_channel"])
    if ok:
        assert compile_query(query, catalog(grain=grain), SCOPE).sql
    else:
        with pytest.raises(InvalidInput, match="grain"):
            compile_query(query, catalog(grain=grain), SCOPE)
