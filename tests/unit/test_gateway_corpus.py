"""P7-15: Atlas's adversarial SQL corpus as a gateway fixture, plus the platform-function boundary.

The corpus (106 cases, one JSON file per dialect) is copied unchanged from
``AIDataAnalyst@8b48fd9cf1d5ff1fcf4c05968f11b973b1cf9fdb:tests/fixtures/adversarial_sql_corpus``
(ADR-0018: donors supply corpora and tests, not code). Every case runs through the real
``gateway.validator.validate_sql`` and must match ``expectations.json``: the outcome, and for a
rejection the reason code the gateway's message maps to, so a case cannot pass for the wrong reason.

Two deliberate differences from Atlas's "zero accepted" rule, both recorded in the expectations file:
``SELECT *`` is accepted by policy because the gateway expands it to the authorized, non-denied
columns (checked below), and the Oracle cases are refused because Oracle is not a gateway dialect.
The scope authorizes one table with every column the corpus names, so a rejection comes from a
guard, never from an unknown column.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

from analystos.contracts.policy import DataScope
from analystos.core.errors import SQLRejected
from analystos.gateway.dialects import PROFILES
from analystos.gateway.validator import validate_sql

CORPUS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "adversarial_sql_corpus"
DIALECT_FILES = ("bigquery", "oracle", "postgres", "snowflake", "tsql")
COLUMNS = ["customer_id", "state_code", "email_address", "opened_at", "note", "loid", "pid"]

# Rejection message -> stable reason code (Atlas's code names where the meaning is the same).
_CODES: tuple[tuple[str, str], ...] = (
    (r"is not supported by the gateway", "UNSUPPORTED_DIALECT"),
    (r"Exactly one statement", "EXACTLY_ONE_STATEMENT_REQUIRED"),
    (r"SELECT \.\.\. INTO", "SELECT_INTO_FORBIDDEN"),
    (r"Row locking clauses|Table hints", "LOCKING_READ_FORBIDDEN"),
    (r"Unconditioned joins", "CROSS_OR_UNBOUNDED_JOIN_FORBIDDEN"),
    (r"statements are not allowed\. Only read-only", "READ_ONLY_QUERY_REQUIRED"),
    (r"not in the authorized scope|Unknown table", "UNKNOWN_OR_UNAUTHORIZED_TABLE"),
    (r"not a known built-in|Schema-qualified function|Namespaced function", "UNAUTHORIZED_FUNCTION"),
    (r"Function \S+\(\) is not allowed", "FORBIDDEN_FUNCTION"),
    (r"Table-valued functions", "TABLE_VALUED_SOURCE_FORBIDDEN"),
    (r"restricted by policy", "DENIED_COLUMN"),
)


def reason_code(message: str) -> str:
    for pattern, code in _CODES:
        if re.search(pattern, message):
            return code
    return "OTHER"


def corpus_scope(dialect: str, denied: list[str] | None = None) -> DataScope:
    return DataScope(workspace_id="w", user_id="u", role="analyst", source_ids=["s"], assets=["retail.customer"],
                     asset_sources={"retail.customer": "s"}, columns={"retail.customer": COLUMNS},
                     source_dialects={"s": dialect}, denied_columns=denied or [])


def run_case(dialect: str, sql: str) -> tuple[str, str | None]:
    try:
        validate_sql(corpus_scope(dialect), sql, max_rows=100)
    except SQLRejected as exc:
        return "rejected", reason_code(str(exc))
    return "accepted", None


def load_corpus() -> list[tuple[str, dict]]:
    pairs = []
    for name in DIALECT_FILES:
        payload = json.loads((CORPUS_DIR / f"{name}.json").read_text(encoding="utf-8"))
        assert payload["dialect"] == name
        pairs += [(name, case) for case in payload["cases"]]
    return pairs


CORPUS = load_corpus()
EXPECTATIONS = json.loads((CORPUS_DIR / "expectations.json").read_text(encoding="utf-8"))


def test_corpus_and_expectations_cover_each_other() -> None:
    keys = [f"{d}:{c['id']}" for d, c in CORPUS]
    assert len(keys) == len(set(keys)) == 106
    assert set(keys) == set(EXPECTATIONS["cases"])


@pytest.mark.parametrize("dialect,case", CORPUS, ids=[f"{d}:{c['id']}" for d, c in CORPUS])
def test_corpus_case_matches_expectation(dialect: str, case: dict) -> None:
    want = EXPECTATIONS["cases"][f"{dialect}:{case['id']}"]
    outcome, code = run_case(dialect, case["sql"])
    assert (outcome, code) == (want["outcome"], want.get("code")), case["sql"]


def test_only_star_projection_is_accepted() -> None:
    accepted = {k for k, v in EXPECTATIONS["cases"].items() if v["outcome"] == "accepted"}
    assert accepted and all(k.endswith("wildcard_projection-star") for k in accepted)
    assert all(EXPECTATIONS["cases"][k].get("policy") for k in accepted)


@pytest.mark.parametrize("dialect", ["postgres", "tsql", "snowflake", "bigquery"])
def test_star_is_expanded_and_denied_columns_still_refused(dialect: str) -> None:
    v = validate_sql(corpus_scope(dialect), "SELECT * FROM retail.customer", max_rows=10)
    tree = sqlglot.parse_one(v.executable_sql, read=dialect)
    assert not [s for s in tree.find_all(exp.Star)]
    assert len(v.referenced_columns) == len(COLUMNS)
    with pytest.raises(SQLRejected, match="restricted by policy"):
        validate_sql(corpus_scope(dialect, ["retail.customer.email_address"]), "SELECT * FROM retail.customer",
                     max_rows=10)


# ------------------------------------------------------------------ gaps beyond the corpus (P7-15)
@pytest.mark.parametrize("dialect", ["postgres", "tsql"])
@pytest.mark.parametrize("sql", [
    "SELECT fn_send_mail(customer_id) FROM retail.customer",
    "SELECT fn_send_mail(c.customer_id) AS sent FROM retail.customer AS c",
    "SELECT c.customer_id FROM retail.customer c WHERE fn_audit(c.note) = 1",
])
def test_unknown_function_refused(dialect: str, sql: str) -> None:
    assert run_case(dialect, sql) == ("rejected", "UNAUTHORIZED_FUNCTION")


@pytest.mark.parametrize("hint", ["NOLOCK", "UPDLOCK, HOLDLOCK", "TABLOCKX", "READPAST", "XLOCK"])
def test_tsql_table_hints_refused(hint: str) -> None:
    assert run_case("tsql", f"SELECT c.customer_id FROM retail.customer c WITH ({hint})") == \
        ("rejected", "LOCKING_READ_FORBIDDEN")


@pytest.mark.parametrize("dialect", ["postgres", "tsql", "snowflake"])
@pytest.mark.parametrize("on", [
    "TRUE", "1 = 1", "TRUE OR a.customer_id = b.customer_id", "b.customer_id = b.customer_id",
    "a.customer_id = a.customer_id", "b.customer_id IS NOT NULL", "a.customer_id = 5",
    "(a.customer_id = b.customer_id OR 1 = 1)",
])
def test_unkeyed_join_refused(dialect: str, on: str) -> None:
    sql = f"SELECT a.customer_id FROM retail.customer a JOIN retail.customer b ON {on}"
    if dialect == "tsql":
        sql = sql.replace("TRUE", "1 = 1")
    assert run_case(dialect, sql) == ("rejected", "CROSS_OR_UNBOUNDED_JOIN_FORBIDDEN")


@pytest.mark.parametrize("dialect", ["postgres", "tsql", "snowflake"])
@pytest.mark.parametrize("on", [
    "ON a.customer_id = b.customer_id",
    "ON b.customer_id = a.customer_id AND b.note IS NOT NULL",
    "ON (a.customer_id = b.customer_id OR a.pid = b.pid)",
    "ON a.customer_id <> b.customer_id",
    "USING (customer_id)",
])
def test_keyed_join_accepted(dialect: str, on: str) -> None:
    assert run_case(dialect, f"SELECT a.customer_id FROM retail.customer a JOIN retail.customer b {on}") == \
        ("accepted", None)


# ------------------------------------------------------------------ platform functions stay allowed
def _anonymous_names(sql: str, dialect: str) -> set[str]:
    return {n.name.lower() for n in sqlglot.parse_one(sql, read=dialect).find_all(exp.Anonymous)}


@pytest.mark.parametrize("dialect", ["postgres", "tsql"])
def test_every_function_the_compiler_emits_is_allowlisted(dialect: str) -> None:
    """Strict postgres/tsql must not refuse the platform's own SQL: every unmodelled function that
    ``skills/sqlbuild`` (and so Ask, methods, profiling and the benchmarks) emits is allowlisted."""
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from tests.unit.test_skills_sqlbuild import SPECS

    from analystos.skills import sqlbuild as sb

    seen: set[str] = set()
    for _name, spec in SPECS:
        for purpose in sb.METHOD_PURPOSES[spec.method]:
            seen |= _anonymous_names(sb.compile_spec(spec, dialect, purpose=purpose, sample_rows=500).sql, dialect)
    for grain in ("day", "week", "month", "quarter"):
        seen |= _anonymous_names(sb.to_sql(sb.trunc_expr(sb.col("t"), grain, dialect), dialect), dialect)
    assert seen <= PROFILES[dialect].allowed_anonymous, seen - PROFILES[dialect].allowed_anonymous


@pytest.mark.parametrize("sql", [
    "SELECT DATE_TRUNC('month', c.opened_at) AS m, EXTRACT(YEAR FROM c.opened_at) AS y, AGE(c.opened_at) AS a, "
    "DATE_PART('dow', c.opened_at) AS d, TO_CHAR(c.opened_at, 'YYYY') AS s, COALESCE(c.note, '') AS n, "
    "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY c.pid) AS p FROM retail.customer c GROUP BY 1, 2, 3, 4, 5, 6",
    "SELECT CAST(c.opened_at AS DATE) AS d, c.opened_at::date AS e, NOW() AS n, CURRENT_DATE AS t FROM retail.customer c",
])
def test_common_postgres_builtins_pass(sql: str) -> None:
    assert run_case("postgres", sql) == ("accepted", None)


@pytest.mark.parametrize("sql", [
    "SELECT DATEADD(DAY, 1, c.opened_at) AS a, DATEDIFF(DAY, c.opened_at, GETDATE()) AS b, "
    "DATEPART(HOUR, c.opened_at) AS h, DATEDIFF_BIG(SECOND, c.opened_at, c.opened_at) AS s, "
    "EOMONTH(c.opened_at) AS e, YEAR(c.opened_at) AS y, ISNULL(c.note, '') AS n, "
    "CONVERT(DATE, c.opened_at) AS d, CAST(c.opened_at AS DATE) AS d2, CONCAT(c.note, 'x') AS c2, "
    "HASHBYTES('MD5', c.note) AS hb, DATALENGTH(c.note) AS dl FROM retail.customer c",
])
def test_common_tsql_builtins_pass(sql: str) -> None:
    assert run_case("tsql", sql) == ("accepted", None)


def test_cross_join_only_to_a_provably_single_row_derived_table() -> None:
    """Owner decision 2026-09-26: percent-of-total (CROSS JOIN to an ungrouped aggregate or LIMIT 1) is
    allowed; comma joins, ON TRUE and CROSS JOIN to a table or a grouped subquery stay refused."""
    from analystos.contracts.policy import DataScope
    from analystos.core.errors import SQLRejected
    from analystos.gateway.validator import validate_sql

    scope = DataScope(workspace_id="w", user_id="u", role="a", source_ids=["s"],
                      assets=["retail.customer", "retail.account"],
                      asset_sources={"retail.customer": "s", "retail.account": "s"},
                      columns={"retail.customer": ["customer_id", "state_code"], "retail.account": ["account_id", "customer_id"]},
                      source_dialects={"s": "postgres"})
    allowed = [
        "SELECT c.state_code, COUNT(*) * 1.0 / t.total AS share FROM retail.customer c "
        "CROSS JOIN (SELECT COUNT(*) AS total FROM retail.customer) t GROUP BY c.state_code, t.total",
        "SELECT c.customer_id, x.account_id FROM retail.customer c "
        "CROSS JOIN (SELECT a.account_id FROM retail.account a ORDER BY a.account_id LIMIT 1) x",
    ]
    for sql in allowed:
        assert validate_sql(scope, sql, max_rows=10).executable_sql
    refused = [
        "SELECT c.customer_id FROM retail.customer c CROSS JOIN retail.account a",
        "SELECT c.customer_id FROM retail.customer c, (SELECT COUNT(*) AS n FROM retail.account) t",
        "SELECT c.customer_id FROM retail.customer c CROSS JOIN "
        "(SELECT a.customer_id, COUNT(*) AS n FROM retail.account a GROUP BY a.customer_id) t",
        "SELECT c.customer_id FROM retail.customer c JOIN retail.account a ON TRUE",
    ]
    for sql in refused:
        with pytest.raises(SQLRejected):
            validate_sql(scope, sql, max_rows=10)
