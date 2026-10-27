from __future__ import annotations

import secrets
from urllib.parse import quote

from fastapi import APIRouter, Cookie, Depends, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from analystos.api.deps import admin_user, current_user, db
from analystos.api.serialize import row
from analystos.core.config import get_settings
from analystos.core.errors import AnalystOSError, Conflict, Forbidden, Unauthenticated
from analystos.core.ids import new_id
from analystos.db.models import User
from analystos.governance.audit import audit
from analystos.security import oidc
from analystos.security.auth import hash_password, issue_token, verify_password

router = APIRouter(prefix="/api", tags=["auth"])


class Login(BaseModel):
    email: str
    password: str


class NewUser(BaseModel):
    email: str
    name: str
    password: str
    is_admin: bool = False
    attributes: dict = {}


@router.post("/auth/login")
def login(body: Login, session: Session = Depends(db, scope="function")):
    if not get_settings().password_login:
        raise Forbidden("password sign-in is turned off; use single sign-on")
    from analystos.tenancy.session import bind_session, system_session

    system_session(session)  # the user's tenant is not known until the user is found
    user = session.scalar(select(User).where(User.email == body.email.lower()))
    if user is None or not user.active or not verify_password(body.password, user.password_hash):
        audit(f"anon:{body.email[:80]}", "auth.login_failed", decision="deny", session=session)
        raise Unauthenticated("invalid email or password")
    bind_session(session, user.tenant_id)  # the login is the user's tenant's event (ADR-009)
    audit(f"user:{user.id}", "auth.login", decision="allow", session=session)
    return {"access_token": issue_token(user.id, user.email), "token_type": "bearer",
            "user": row(user, exclude={"password_hash"})}


@router.get("/auth/me")
def me(user: User = Depends(current_user)):
    return row(user, exclude={"password_hash"})


@router.get("/users")
def list_users(_: User = Depends(current_user), session: Session = Depends(db, scope="function")):
    return [{"id": u.id, "email": u.email, "name": u.name, "is_admin": u.is_admin} for u in session.scalars(select(User))]


@router.post("/users")
def create_user(body: NewUser, admin: User = Depends(admin_user), session: Session = Depends(db, scope="function")):
    if session.scalar(select(User).where(User.email == body.email.lower())):
        raise Conflict("user exists")
    user = User(id=new_id("usr"), email=body.email.lower(), name=body.name, password_hash=hash_password(body.password),
                is_admin=body.is_admin, attributes=body.attributes)
    session.add(user)
    audit(f"user:{admin.id}", "user.created", target=user.id, session=session)
    return row(user, exclude={"password_hash"})


# ----------------------------------------------------------------------------- OIDC single sign-on
@router.get("/auth/providers")
def auth_providers():
    """What the login screen offers: password sign-in and/or single sign-on."""
    settings = get_settings()
    return {"password": settings.password_login,
            "oidc": {"enabled": oidc.enabled(), "name": settings.oidc_provider_name,
                     "login_url": "/api/auth/oidc/login" if oidc.enabled() else None}}


@router.get("/auth/oidc/login")
def oidc_login(return_to: str | None = None):
    """Start the authorization-code flow: state, nonce and a PKCE verifier go into a short-lived signed
    HttpOnly cookie; the browser is sent to the IdP with the S256 challenge only."""
    idp = oidc.provider()
    verifier, challenge = oidc.pkce_pair()
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    response = RedirectResponse(idp.authorization_url(state=state, nonce=nonce, challenge=challenge), status_code=302)
    response.set_cookie(oidc.TX_COOKIE, oidc.seal_transaction({"state": state, "nonce": nonce, "verifier": verifier,
                                                              "return_to": oidc.safe_return_path(return_to)}),
                        max_age=oidc.TX_TTL_SECONDS, httponly=True, samesite="lax", path="/api/auth/oidc",
                        secure=idp.redirect_uri.startswith("https://"))
    return response


@router.get("/auth/oidc/callback")
def oidc_callback(request: Request, code: str | None = None, state: str | None = None, error: str | None = None,
                  aos_oidc_tx: str | None = Cookie(default=None), session: Session = Depends(db, scope="function")):
    """Validate the transaction, exchange the code with the verifier, validate the ID token and
    provision the user. The browser returns to the web app with the AnalystOS token in the URL
    fragment (never sent to a server); `Accept: application/json` gets the login response instead."""
    from analystos.tenancy.session import bind_session, system_session

    wants_json = "application/json" in (request.headers.get("accept") or "")
    web = get_settings().web_url.rstrip("/")
    system_session(session)  # identity lookup and provisioning happen before the tenant is known
    try:
        if error:
            raise Unauthenticated(f"identity provider returned {error}")
        tx = oidc.open_transaction(aos_oidc_tx, state)
        if not code:
            raise Unauthenticated("no authorization code")
        idp = oidc.provider()
        tokens = idp.exchange(code, tx["verifier"])
        claims = idp.validate_id_token(tokens["id_token"], nonce=tx["nonce"])
        user = oidc.provision(session, claims, issuer=idp.issuer)
        bind_session(session, user.tenant_id)
    except AnalystOSError as exc:
        audit("anon:sso", "auth.sso_login_failed", decision="deny", details={"error": exc.message[:300]}, session=session)
        session.commit()
        if wants_json:
            raise
        response = RedirectResponse(f"{web}/login#sso_error={quote(exc.message[:200])}", status_code=302)
        response.delete_cookie(oidc.TX_COOKIE, path="/api/auth/oidc")
        return response
    token = issue_token(user.id, user.email)
    if wants_json:
        response = JSONResponse({"access_token": token, "token_type": "bearer", "user": row(user, exclude={"password_hash"}),
                                 "return_to": tx["return_to"]})
    else:
        response = RedirectResponse(f"{web}/login#sso_token={quote(token)}&return_to={quote(tx['return_to'])}", status_code=302)
    response.delete_cookie(oidc.TX_COOKIE, path="/api/auth/oidc")
    return response
