"""Publishing a workspace playbook over the API the way S06-T6 requires: simulate the draft, request a signature,
a second person (the platform admin, made an approver of the workspace) approves, then publish."""
from __future__ import annotations

from typing import Any


def publish_playbook(api: Any, ws: str, draft: dict, author: dict, approver: dict,
                     approver_email: str = "admin@analystos.local") -> Any:
    base = f"/api/workspaces/{ws}"
    api.post(f"{base}/members", headers=author, json={"email": approver_email, "role": "approver"})  # may already be one
    r = api.post(f"{base}/playbook-editor/drafts/{draft['id']}/simulate", headers={**author, "If-Match": f'"{draft["revision"]}"'},
                 json={})
    assert r.status_code == 200 and r.json()["status"] == "tested", r.text
    r = api.post(f"{base}/playbook-editor/drafts/{draft['id']}/request-signature", headers={**author, "If-Match": r.headers["ETag"]})
    assert r.status_code == 201, r.text
    etag = r.headers["ETag"]
    approval = r.json()["approval"]["id"]
    assert api.post(f"/api/approvals/{approval}/approve", headers=author).status_code == 403  # never the requester
    a = api.post(f"/api/approvals/{approval}/approve", headers=approver)
    assert a.status_code == 200, a.text
    return api.post(f"{base}/definitions/{draft['id']}/publish", headers={**author, "If-Match": etag})
