"""A knowledge remote never carries a credential: user:pass@ and a bare token as the https user are refused."""
from __future__ import annotations

import pytest

from analystos.core.errors import InvalidInput
from analystos.knowledge.remote import validate_remote


@pytest.mark.parametrize("url", ["https://user:pass@github.com/o/r.git", "https://ghp_abc123@github.com/o/r.git",
                                 "https://x-access-token@github.com/o/r.git"])
def test_credentials_in_an_https_remote_are_refused(url):
    with pytest.raises(InvalidInput, match="no credentials"):
        validate_remote(url)


@pytest.mark.parametrize("url", ["https://github.com/o/r.git", "ssh://git@github.com/o/r.git", "git@github.com:o/r.git",
                                 "/srv/packs/r.git"])
def test_plain_remotes_pass(url):
    assert validate_remote(url) == url


def test_a_drive_rooted_path_is_a_local_remote_only_on_windows(monkeypatch):
    import os

    monkeypatch.setattr(os, "name", "nt")
    assert validate_remote(r"C:\packs\r.git") == r"C:\packs\r.git"
    assert validate_remote("C:/packs/r.git") == "C:/packs/r.git"
    with pytest.raises(InvalidInput):
        validate_remote(r"C:relative\r.git")
    monkeypatch.setattr(os, "name", "posix")
    with pytest.raises(InvalidInput):
        validate_remote(r"C:\packs\r.git")
