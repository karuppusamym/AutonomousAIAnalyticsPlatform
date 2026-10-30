"""Administrator prompt edits: a drop-in for the built-in template, versioned in platform settings, used by every call."""
from __future__ import annotations

import pytest

from analystos.agents import prompts as P
from analystos.contracts.platform import PlatformSettings
from analystos.core.errors import InvalidInput
from analystos.services import platform_settings


def test_an_edit_must_keep_the_placeholders_and_the_untrusted_rule():
    base = P.PROMPTS["sql_generation.v1"]
    P.validate_override("sql_generation.v1", base.replace("You are", "You are, carefully,", 1))
    with pytest.raises(InvalidInput, match=r"missing \{dialect\}"):
        P.validate_override("sql_generation.v1", base.replace("{dialect}", "postgres"))
    with pytest.raises(InvalidInput, match=r"unknown \{table\}"):
        P.validate_override("sql_generation.v1", base + " {table}")
    with pytest.raises(InvalidInput, match="untrusted-context rule"):
        P.validate_override("sql_generation.v1", base.replace(P.UNTRUSTED_NOTE, ""))
    with pytest.raises(InvalidInput, match="unknown prompt template"):
        P.validate_override("no_such.v1", "text")
    with pytest.raises(InvalidInput, match="empty"):
        P.validate_override("run_summary.v1", "   ")
    with pytest.raises(InvalidInput, match="limit"):
        P.validate_override("run_summary.v1", "x" * (P.MAX_OVERRIDE_CHARS + 1))
    # A template without the rule may be rewritten freely (within the placeholders).
    P.validate_override("run_summary.v1", "Summarise the run in two sentences.")


def test_the_edited_text_is_what_a_call_sends_and_its_version_id_changes(monkeypatch):
    edited = P.PROMPTS["sql_generation.v1"].replace("You are", "You are, carefully,", 1)
    before = P.prompt("sql_generation.v1", dialect="postgres")
    monkeypatch.setattr(P, "overrides", lambda: {"sql_generation.v1": edited})
    after = P.prompt("sql_generation.v1", dialect="postgres")
    assert "You are, carefully," in after and "{dialect}" not in after
    assert P.prompt_version_id("sql_generation.v1", after) != P.prompt_version_id("sql_generation.v1", before)
    assert P.active_text("run_summary.v1") == P.PROMPTS["run_summary.v1"]  # templates without an edit are unchanged


def test_a_settings_read_error_falls_back_to_the_built_in_text(monkeypatch):
    def broken():
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(platform_settings, "get", broken)
    assert P.overrides() == {}
    assert P.active_text("planning.v1") == P.PROMPTS["planning.v1"]


def test_settings_reject_an_invalid_edit_and_replace_the_edits_wholesale():
    bad = PlatformSettings.model_validate({"prompts": {"overrides": {"sql_generation.v1": "no placeholders here"}}})
    with pytest.raises(InvalidInput, match="placeholders"):
        platform_settings._validate_references(bad)
    merged = platform_settings._deep_merge({"prompts": {"overrides": {"a.v1": "x", "b.v1": "y"}}},
                                           {"prompts": {"overrides": {"b.v1": "z"}}})
    assert merged["prompts"]["overrides"] == {"b.v1": "z"}  # a reset (key left out) really removes the edit
