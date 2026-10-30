"""Playbook editor API (S06-T6, spec §3.7): YAML drafts, simulation, signing.

Drafts are ordinary workspace `playbook` definitions (versions, one draft per key, If-Match revisions:
`api/routers/definitions.py`); this router takes and returns YAML, simulates without saving, records a
passing simulation on a draft, and asks for its signature. Publishing (= enabling) stays
`POST /api/workspaces/{workspace_id}/definitions/{definition_id}/publish`, which now signs the version after
verifying the approved, hash-bound sign-off.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, Response
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from analystos.api.deps import current_user, db
from analystos.api.http import expected_revision, set_etag
from analystos.api.serialize import row
from analystos.autonomy import playbook_editor as editor
from analystos.autonomy import playbook_signing, simulator
from analystos.contracts.definition import DefinitionDraftIn, DefinitionPatch
from analystos.core.errors import InvalidInput
from analystos.db.models import Definition, User
from analystos.governance.policy import load_in_workspace, require_role, scoped_loader
from analystos.services import definitions as defs

router = APIRouter(prefix="/api", tags=["playbook-editor"])


class PlaybookYamlIn(BaseModel):
    yaml: str = Field(max_length=editor.MAX_YAML_BYTES)
    title: str | None = Field(default=None, max_length=300)


class PlaybookSimulateIn(BaseModel):
    yaml: str | None = Field(default=None, max_length=editor.MAX_YAML_BYTES)
    definition_id: str | None = None
    fixture: str | dict[str, Any] | None = None  # a built-in fixture name or a custom fixture


class PlaybookDraftSimulateIn(BaseModel):
    fixtures: list[str | dict[str, Any]] | None = None  # default: supervised


@scoped_loader
def _load(session: Session, user: User, workspace_id: str, definition_id: str, minimum: str = "viewer") -> Definition:
    d = load_in_workspace(session, Definition, definition_id, workspace_id, user=user, minimum=minimum, label="definition",
                          for_update=minimum != "viewer")
    if d.kind != "playbook":
        raise InvalidInput(f"definition {definition_id} is a {d.kind}, not a playbook")
    return d


def _out(d: Definition) -> dict[str, Any]:
    return {**defs.out(d), "yaml": editor.to_yaml(d.spec)}


@router.get("/workspaces/{workspace_id}/playbook-editor")
def editor_options(workspace_id: str, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Agents and behaviours, check names, replan reasons, fixture workspaces, the contract schema and a template."""
    require_role(session, user, workspace_id, "viewer")
    return editor.options(session, workspace_id)


@router.post("/workspaces/{workspace_id}/playbook-editor/validate")
def validate(workspace_id: str, body: PlaybookYamlIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Parse and validate against the playbook contract and the capability registry; nothing is saved."""
    require_role(session, user, workspace_id, "viewer")
    return editor.check(session, workspace_id, body.yaml)


@router.post("/workspaces/{workspace_id}/playbook-editor/simulate")
def simulate(workspace_id: str, body: PlaybookSimulateIn, user: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    """Dry-run YAML (or a stored version) against a fixture workspace: steps, budget and approval gates it would
    hit. Never executes SQL or side effects and records nothing."""
    require_role(session, user, workspace_id, "editor")
    if (body.yaml is None) == (body.definition_id is None):
        raise InvalidInput("simulate either yaml or a definition_id")
    if body.definition_id is not None:
        spec = dict(_load(session, user, workspace_id, body.definition_id).spec)
    else:
        raw = editor.parse_yaml(body.yaml or "")
        spec = defs._validate(session, workspace_id, "playbook", str(raw.get("id") or ""), raw)
    return simulator.simulate(spec, body.fixture, session=session, workspace_id=workspace_id, workspace_source=True)


@router.post("/workspaces/{workspace_id}/playbook-editor/drafts", status_code=201)
def create_draft(workspace_id: str, body: PlaybookYamlIn, response: Response, user: User = Depends(current_user),
                 session: Session = Depends(db, scope="function")):
    """A new draft version from YAML (the key is the manifest id); 409 when the key already has a draft."""
    spec = editor.parse_yaml(body.yaml)
    try:  # a manifest id that is no valid key is the caller's mistake (422), not a server error
        draft = DefinitionDraftIn(kind="playbook", key=str(spec.get("id") or ""), title=body.title, spec=spec)
    except ValidationError as exc:
        raise InvalidInput("the playbook YAML is not a valid draft", details={
            "problems": [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]}) from None
    d = defs.create_draft(session, session.merge(user), workspace_id, draft)
    set_etag(response, d.revision)
    return _out(d)


@router.get("/workspaces/{workspace_id}/playbook-editor/drafts/{definition_id}")
def get_draft(workspace_id: str, definition_id: str, response: Response, user: User = Depends(current_user),
              session: Session = Depends(db, scope="function")):
    """Any version as YAML, with its signing state and the diff against the newest published version."""
    d = _load(session, user, workspace_id, definition_id)
    published = defs.latest_published(session, workspace_id, "playbook", d.key)
    set_etag(response, d.revision)
    return {**_out(d), "diff": {"against": f"v{published.version}" if published is not None and published.id != d.id else None,
                                "changes": defs.diff(published.spec, d.spec) if published is not None and published.id != d.id
                                else []}}


@router.put("/workspaces/{workspace_id}/playbook-editor/drafts/{definition_id}")
def update_draft(workspace_id: str, definition_id: str, body: PlaybookYamlIn, response: Response,
                 user: User = Depends(current_user), session: Session = Depends(db, scope="function"),
                 if_match: str | None = Header(default=None)):
    """Replace the draft's YAML. A changed playbook drops its passing simulation and voids a requested signature."""
    d = _load(session, user, workspace_id, definition_id, "editor")
    spec = editor.parse_yaml(body.yaml)
    if spec.get("id") != d.key:
        raise InvalidInput(f"the manifest id ({spec.get('id')}) must stay {d.key}; a new key is a new draft")
    d = defs.update_draft(session, session.merge(user), d, DefinitionPatch(title=body.title, spec=spec),
                          expected_revision(if_match, required=True))
    set_etag(response, d.revision)
    return _out(d)


@router.post("/workspaces/{workspace_id}/playbook-editor/drafts/{definition_id}/simulate")
def simulate_draft(workspace_id: str, definition_id: str, body: PlaybookDraftSimulateIn, response: Response,
                   user: User = Depends(current_user), session: Session = Depends(db, scope="function"),
                   if_match: str | None = Header(default=None)):
    """Simulate the draft against the fixtures (default: supervised). Passing makes it `tested`, with the
    simulation digests bound to its content hash: what the signature request then binds."""
    d = _load(session, user, workspace_id, definition_id, "editor")
    d = defs.test(session, session.merge(user), d, expected_revision(if_match, required=True),
                  arguments={"fixtures": body.fixtures} if body.fixtures else None)
    set_etag(response, d.revision)
    return _out(d)


@router.post("/workspaces/{workspace_id}/playbook-editor/drafts/{definition_id}/request-signature", status_code=201)
def request_signature(workspace_id: str, definition_id: str, response: Response, user: User = Depends(current_user),
                      session: Session = Depends(db, scope="function"), if_match: str | None = Header(default=None)):
    """Ask an approver (not the requester) to sign off this simulated draft; the approval binds its content hash."""
    d = _load(session, user, workspace_id, definition_id, "editor")
    approval = playbook_signing.request_signature(session, session.merge(user), d, expected_revision(if_match, required=True))
    set_etag(response, d.revision)
    return {"definition": _out(d), "approval": row(approval, exclude={"payload"})}
