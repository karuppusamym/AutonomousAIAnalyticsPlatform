"""Connector certification kit (S02-T6) without a live engine: the validator suite per dialect, evidence files,
the 180-day rule, and kinds the kit cannot check live are never certified."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from analystos.connectors import certification as cert
from analystos.connectors import certify_kit as kit


@pytest.mark.parametrize("dialect", ["postgres", "tsql", "snowflake", "bigquery", "duckdb", "mysql"])
def test_validator_suite_refuses_every_hostile_statement(dialect):
    check = kit.validator_suite(dialect)
    assert check.passed, check.detail


def test_a_leaky_validator_fails_the_suite(monkeypatch):
    from analystos.gateway import validator

    monkeypatch.setattr(validator, "validate_sql", lambda *a, **k: None)
    check = kit.validator_suite("postgres")
    assert not check.passed and "not refused" in check.detail


def test_kinds_without_live_checks_are_never_certified():
    report = kit.certify("snowflake", "snowflake://u:p@acct/db", settings=SimpleNamespace())
    assert not report.passed
    assert {c.name for c in report.checks if not c.passed} >= {"read_only", "timeout", "cancel", "metadata"}


def test_evidence_file_round_trips_and_ages_out(tmp_path):
    report = kit.Report(kind="postgres", dialect="postgres", engine="PostgreSQL 16.4 (127.0.0.1)",
                        checks=[kit.Check("validator", True, "ok | fine"), kit.Check("read_only", True, "ok")])
    path = kit.write_evidence(report, tmp_path, commit="abc123")
    day = report.started.date()
    assert path == tmp_path / "docs" / "evidence" / "connectors" / f"postgres-{day.isoformat()}.md"
    folder = path.parent
    assert cert.certification("postgres", folder, today=day)["status"] == "certified"
    assert cert.certification("postgres", folder, today=date.fromordinal(day.toordinal() + 180))["status"] == "certified"
    assert cert.certification("postgres", folder, today=date.fromordinal(day.toordinal() + 181))["status"] == "tested"
    report.checks.append(kit.Check("timeout", False, "too slow"))
    kit.write_evidence(report, tmp_path)  # same day, now failing: the file says fail and certifies nothing
    assert cert.certification("postgres", folder, today=day)["status"] == "tested"
    assert "| timeout | FAIL |" in path.read_text(encoding="utf-8")
