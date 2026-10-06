"""Browser-facing SSO endpoints: OIDC authorization-code flow with PKCE, session cookie, /auth/me."""

from __future__ import annotations

import secrets
from typing import Any
from urllib.parse import quote, urlencode

import jwt
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse

from .errors import GatewayError
from .jwtutil import sign_state, verify_state
from .oidc import OIDCClient, new_pkce
from .rbac import resolve_principal, start_session, upsert_user

router = APIRouter(prefix="/auth", tags=["auth"])
STATE_COOKIE = "llmgate_oidc"
DASHBOARD = "/dashboard/"


def _safe_next(value: str | None) -> str:
    """Only same-site relative paths: blocks open redirects such as //evil.com or https://evil.com."""
    if value and value.startswith("/") and not value.startswith("//") and "\\" not in value:
        return value
    return DASHBOARD


def _redirect_uri(request: Request) -> str:
    return request.app.state.settings.oidc_redirect_uri or f"{str(request.base_url).rstrip('/')}/auth/oidc/callback"


def _fail(message: str, code: str) -> RedirectResponse:
    resp = RedirectResponse(f"{DASHBOARD}?{urlencode({'auth_error': message, 'code': code})}", status_code=302)
    resp.delete_cookie(STATE_COOKIE, path="/auth/oidc")
    return resp


@router.get("/config")
async def auth_config(request: Request) -> dict[str, Any]:
    st = request.app.state
    return {
        "oidc": {"enabled": st.oidc.enabled, "login_url": "/auth/oidc/login" if st.oidc.enabled else None},
        "admin_token": st.settings.admin_token_enabled,
        "dev_autologin": bool(st.settings.dev_autologin and st.oidc.enabled),
    }


@router.get("/oidc/login")
async def oidc_login(request: Request, next: str | None = None, prompt: str | None = None) -> RedirectResponse:
    st = request.app.state
    oidc: OIDCClient = st.oidc
    if not oidc.enabled:
        raise GatewayError(404, "SSO is not configured.", type="not_found_error", code="oidc_disabled")
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    verifier, challenge = new_pkce()
    url = await oidc.authorize_url(_redirect_uri(request), state, nonce, challenge,
                                   prompt if prompt in ('login', 'select_account') else None)
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie(STATE_COOKIE, sign_state(st.jwt_secret, {"state": state, "nonce": nonce, "verifier": verifier,
                                                              "next": _safe_next(next)}),
                    max_age=600, httponly=True, samesite="lax", secure=st.settings.cookie_secure, path="/auth/oidc")
    return resp


@router.get("/oidc/callback")
async def oidc_callback(request: Request, code: str | None = None, state: str | None = None,
                        error: str | None = None, error_description: str | None = None) -> RedirectResponse:
    st = request.app.state
    oidc: OIDCClient = st.oidc
    if not oidc.enabled:
        raise GatewayError(404, "SSO is not configured.", type="not_found_error", code="oidc_disabled")
    if error:
        return _fail(error_description or error, "idp_error")
    try:
        saved = verify_state(st.jwt_secret, request.cookies.get(STATE_COOKIE, ""))
    except jwt.PyJWTError:
        return _fail("Login session expired or missing. Please try again.", "bad_state")
    if not code or not state or not secrets.compare_digest(state, saved["state"]):
        return _fail("State mismatch. Please try again.", "bad_state")  # login-CSRF defence

    try:
        tokens = await oidc.exchange_code(code, _redirect_uri(request), saved["verifier"])
        claims = await oidc.verify(tokens["id_token"], audience=st.settings.oidc_client_id, nonce=saved["nonce"])
        if "email" not in claims and tokens.get("access_token"):  # some IdPs keep email out of the ID token
            claims = {**(await _userinfo(request, tokens["access_token"])), **claims}
        user = upsert_user(st.db, st.settings, oidc.issuer, claims)
    except GatewayError as exc:
        return _fail(exc.message, exc.code or "login_failed")
    except (jwt.PyJWTError, KeyError, ValueError):
        return _fail("The identity provider returned an invalid token.", "invalid_token")

    resp = RedirectResponse(saved["next"], status_code=302)
    resp.delete_cookie(STATE_COOKIE, path="/auth/oidc")
    resp.set_cookie(st.settings.session_cookie, start_session(request, user), max_age=st.settings.session_ttl_s,
                    httponly=True, samesite="lax", secure=st.settings.cookie_secure, path="/")
    return resp


async def _userinfo(request: Request, access_token: str) -> dict[str, Any]:
    st = request.app.state
    endpoint = (await st.oidc.metadata()).get("userinfo_endpoint")
    if not endpoint:
        return {}
    try:
        resp = await st.client.get(endpoint, headers={"authorization": f"Bearer {access_token}"}, timeout=5)
        return resp.json() if resp.status_code == 200 else {}
    except Exception:  # userinfo is best-effort enrichment
        return {}


@router.get("/me")
async def me(request: Request) -> dict[str, Any]:
    principal = await resolve_principal(request)
    if principal is None:
        raise GatewayError(401, "Not signed in.", type="authentication_error", code="unauthenticated")
    return principal.public()


@router.post("/logout")
async def logout(request: Request) -> JSONResponse:
    st = request.app.state
    logout_url = None
    if st.oidc.enabled:
        try:
            end = (await st.oidc.metadata()).get("end_session_endpoint")
            if end:
                logout_url = f"{end}?client_id={quote(st.settings.oidc_client_id)}"
        except GatewayError:
            pass
    resp = JSONResponse({"ok": True, "logout_url": logout_url})
    resp.delete_cookie(st.settings.session_cookie, path="/")
    return resp
