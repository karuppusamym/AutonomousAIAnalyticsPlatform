"""Optional remote repository for a pack (P4-K01, spec v3 §6.1): customers review knowledge through
pull requests by pointing a workspace pack at a repository and pushing revisions to a branch.

A push writes outside the platform, so it is an approval bound to a hash (CLAUDE.md rule 5): the
payload names the pack, revision, content digest, remote and branch; `verify_for_execution` runs
immediately before the push and the approval is consumed. The pushed tree is the revision's files
exactly (the publish policy must pass). Credentials never live in the URL or the database: the
host's own git credential helpers or SSH configuration authenticate.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from analystos.core.errors import ApprovalRequired, Conflict, InvalidInput, UpstreamUnavailable
from analystos.db.models import Approval, KnowledgePack, User
from analystos.knowledge import bundle, store

APPROVAL_ACTION = "knowledge.push"
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{0,119}$")
_SCP = re.compile(r"^[A-Za-z0-9._\-]+@[A-Za-z0-9.\-]+:[A-Za-z0-9._/\-]+$")


def _windows_absolute(url: str) -> bool:
    """A drive-rooted local path (C:/packs/r.git) on a Windows host: the same kind of remote as /srv/packs/r.git."""
    import os
    from pathlib import PureWindowsPath

    return os.name == "nt" and bool(re.match(r"^[A-Za-z]:[\\/]", url)) and PureWindowsPath(url).is_absolute()


def validate_remote(url: str) -> str:
    url = url.strip()
    if re.match(r"^(https|ssh)://", url):
        # any userinfo in an https URL is a credential (a bare token works as the user name); ssh keeps `git@`
        if re.match(r"^[a-z]+://[^/@]*:[^/@]*@", url) or re.match(r"^https://[^/]*@", url):
            raise InvalidInput("put no credentials in the remote URL; use the host's credential helper or SSH keys")
        return url
    if _SCP.match(url) or url.startswith("file://") or url.startswith("/") or _windows_absolute(url):
        return url
    raise InvalidInput("remote must be an https://, ssh://, user@host:path, file:// or absolute path URL")


def set_remote(session: Session, pack: KnowledgePack, url: str | None, branch: str = "main") -> KnowledgePack:
    if pack.kind == "platform":
        raise InvalidInput("the platform pack is built from the installed domain packs and has no remote")
    if not _BRANCH.match(branch) or ".." in branch:
        raise InvalidInput(f"invalid branch {branch!r}")
    pack.git_remote = validate_remote(url) if url else None
    pack.git_branch = branch
    return pack


def payload(session: Session, pack: KnowledgePack) -> dict[str, Any]:
    rev = store.head(session, pack)
    if rev is None or not pack.git_remote:
        raise Conflict("the pack needs a revision and a remote before it can be pushed")
    return {"pack_id": pack.id, "slug": pack.slug, "revision": rev.number, "content_digest": rev.content_digest,
            "remote": pack.git_remote, "branch": pack.git_branch}


def request_push(session: Session, pack: KnowledgePack, user: User) -> Approval:
    from analystos.db.models import Workspace
    from analystos.governance.approvals import request_approval

    if pack.workspace_id is None:
        raise InvalidInput("only workspace packs can be pushed")
    body = payload(session, pack)
    bundle.export_files(session, pack)  # refuse now rather than after an approval: the publish policy must pass
    ws = session.get(Workspace, pack.workspace_id)
    return request_approval(session, workspace_id=pack.workspace_id, run_id=None, action=APPROVAL_ACTION, payload=body,
                            plan_hash=None, policy_version=ws.policy_version if ws else 1, requested_by=user.id,
                            risk_tier="medium", destination=f"git:{pack.git_remote}#{pack.git_branch}",
                            affected_assets=[f"knowledge_pack:{pack.id}"])


def _run(args: list[str], cwd: Path, timeout: int = 120) -> str:
    env = {"GIT_TERMINAL_PROMPT": "0", "PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(cwd)}
    try:
        out = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout,
                             check=True)
    except FileNotFoundError:
        raise UpstreamUnavailable("git is not installed on this host") from None
    except subprocess.CalledProcessError as exc:
        raise UpstreamUnavailable(f"git {args[0]} failed: {(exc.stderr or exc.stdout or '').strip()[:300]}") from None
    except subprocess.TimeoutExpired:
        raise UpstreamUnavailable(f"git {args[0]} timed out") from None
    return out.stdout.strip()


def push(session: Session, pack: KnowledgePack, user: User, approval_id: str) -> dict[str, Any]:
    """Push the head revision to the pack's remote branch, under a verified, single-use approval."""
    from analystos.governance.approvals import consume, verify_for_execution

    body = payload(session, pack)
    files = bundle.export_files(session, pack)
    apr = session.get(Approval, approval_id, with_for_update=True)
    if apr is None or apr.workspace_id != pack.workspace_id or apr.action != APPROVAL_ACTION:
        raise ApprovalRequired("the approval does not cover a knowledge push of this pack")
    verify_for_execution(session, approval_id, payload=body, plan_hash=None)
    consume(session, apr)  # compare-and-set: consumed before the side effect, never replayable
    session.flush()
    with tempfile.TemporaryDirectory(prefix="aos-knowledge-") as tmp:
        work = Path(tmp) / "repo"
        work.mkdir()
        _run(["init", "-q", "-b", pack.git_branch], work)
        _run(["remote", "add", "origin", pack.git_remote], work)
        try:
            _run(["fetch", "-q", "--depth", "1", "origin", pack.git_branch], work)
            _run(["reset", "-q", "--soft", "FETCH_HEAD"], work)
        except UpstreamUnavailable:
            pass  # a new branch (or an empty remote): the first commit starts it
        bundle.write_dir(files, work)  # the tree becomes exactly the revision: `add -A` stages removals too
        _run(["add", "-A", "."], work)
        message = f"knowledge: {pack.slug} revision {body['revision']}\n\ncontent digest {body['content_digest']}\n"
        author = f"{user.name or user.email} <{user.email}>"
        _run(["-c", "user.name=AnalystOS", "-c", "user.email=analystos@localhost", "commit", "-q", "--allow-empty",
              "--author", author, "-m", message], work)
        commit = _run(["rev-parse", "HEAD"], work)
        _run(["push", "-q", "origin", f"HEAD:refs/heads/{pack.git_branch}"], work)
    rev = store.head(session, pack)
    assert rev is not None
    rev.meta = {**(rev.meta or {}), "remote": {"url": pack.git_remote, "branch": pack.git_branch, "commit": commit,
                                               "approval_id": approval_id}}
    from analystos.events.bus import emit

    emit(pack.workspace_id or "", "knowledge.pushed", {"pack_id": pack.id, "revision": body["revision"], "commit": commit,
                                                       "branch": pack.git_branch}, actor=f"user:{user.id}", session=session)
    return {**body, "commit": commit}
