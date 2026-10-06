"""A mock OpenID Connect provider (think: a tiny Keycloak) for developing and testing SSO.

Implements discovery, JWKS (RS256), the authorization-code flow with PKCE (S256), client
credentials, userinfo and a user-picker login page. It is deliberately **not** secure: anyone
can sign in as any listed user.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlencode

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

DEFAULT_USERS: dict[str, dict[str, Any]] = {
    "alice": {"sub": "u-alice", "email": "alice@example.com", "name": "Alice Admin", "roles": ["llm-admins"]},
    "bob": {"sub": "u-bob", "email": "bob@example.com", "name": "Bob Operator", "roles": ["llm-operators"]},
    "carol": {"sub": "u-carol", "email": "carol@example.com", "name": "Carol Viewer", "roles": ["llm-viewers"]},
    "dave": {"sub": "u-dave", "email": "dave@example.com", "name": "Dave (no groups)", "roles": []},
    "eve": {"sub": "u-eve", "email": "eve@evil.test", "name": "Eve (other domain)", "roles": ["llm-admins"]},
}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class Keys:
    def __init__(self) -> None:
        self.rotate()

    def rotate(self) -> None:
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "mock-" + secrets.token_hex(4)

    def jwk(self) -> dict[str, str]:
        n = self.private.public_key().public_numbers()
        enc = lambda i: _b64(i.to_bytes((i.bit_length() + 7) // 8, "big"))  # noqa: E731
        return {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": self.kid, "n": enc(n.n), "e": enc(n.e)}


def create_app(
    issuer: str,
    clients: dict[str, dict[str, Any]] | None = None,
    users: dict[str, dict[str, Any]] | None = None,
    api_audience: str = "llmgate-api",
    auto_login: str | None = None,
) -> FastAPI:
    app = FastAPI(title="mock-oidc")
    keys = Keys()
    users = users or DEFAULT_USERS
    clients = clients or {
        "llmgate-dashboard": {"secret": "dashboard-secret", "redirect_prefixes": ["http://127.0.0.1", "http://localhost"]},
        "ci-pipeline": {"secret": "ci-secret", "service": "chatbot-web", "models": ["Qwen/Qwen2.5-7B-Instruct"]},
    }
    codes: dict[str, dict[str, Any]] = {}
    app.state.keys, app.state.issuer, app.state.users = keys, issuer, users

    def sign(claims: dict[str, Any], ttl: int = 3600) -> str:
        now = int(time.time())
        body = {"iss": issuer, "iat": now, "nbf": now, "exp": now + ttl, "jti": secrets.token_hex(8), **claims}
        return jwt.encode(body, keys.private, algorithm="RS256", headers={"kid": keys.kid})

    def user_claims(u: dict[str, Any]) -> dict[str, Any]:
        return {"sub": u["sub"], "email": u["email"], "email_verified": True, "name": u["name"], "roles": u["roles"]}

    def issue_user_token(login: str, aud: str, ttl: int = 3600, **extra: Any) -> str:
        return sign({**user_claims(users[login]), "aud": aud, **extra}, ttl)

    app.state.issue_user_token = issue_user_token
    app.state.sign = sign

    @app.get("/.well-known/openid-configuration")
    async def discovery() -> dict[str, Any]:
        return {
            "issuer": issuer, "authorization_endpoint": f"{issuer}/authorize", "token_endpoint": f"{issuer}/token",
            "jwks_uri": f"{issuer}/jwks", "userinfo_endpoint": f"{issuer}/userinfo", "end_session_endpoint": f"{issuer}/logout",
            "response_types_supported": ["code"], "grant_types_supported": ["authorization_code", "client_credentials"],
            "subject_types_supported": ["public"], "id_token_signing_alg_values_supported": ["RS256"],
            "code_challenge_methods_supported": ["S256"], "scopes_supported": ["openid", "profile", "email"],
        }

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:  # the sign-in page is opened in a browser tab; do not log a 404 for it
        return Response(status_code=204)

    @app.get("/jwks")
    async def jwks() -> dict[str, Any]:
        return {"keys": [keys.jwk()]}

    @app.get("/authorize", response_class=HTMLResponse)
    async def authorize(request: Request, client_id: str, redirect_uri: str, response_type: str = "code",
                        login_as: str | None = None) -> Any:
        client = clients.get(client_id)
        if not client or response_type != "code":
            return JSONResponse({"error": "invalid_client"}, status_code=400)
        if not any(redirect_uri.startswith(p) for p in client.get("redirect_prefixes", [])):
            return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)
        params = dict(request.query_params)
        if login_as in users:  # automation shortcut
            return await confirm(request, login_as)
        if auto_login in users and "prompt" not in params:  # dev convenience: behave like an IdP with a live session
            return await confirm(request, auto_login)  # (a client sending prompt=select_account still gets the picker)
        links = "".join(
            f'<li><a href="/authorize/confirm?{html.escape(urlencode({**params, "user": k}))}">'
            f"{html.escape(u['name'])} &lt;{html.escape(u['email'])}&gt; — {html.escape(', '.join(u['roles']) or 'no groups')}</a></li>"
            for k, u in users.items())
        return HTMLResponse(f"<!doctype html><meta charset=utf-8><title>Mock IdP</title>"
                            f"<h2>Mock OIDC provider</h2><p>Sign in as:</p><ul>{links}</ul>")

    @app.get("/authorize/confirm")
    async def confirm(request: Request, user: str) -> Any:
        q = request.query_params
        client = clients.get(q.get("client_id", ""))
        if not client or user not in users:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
        code = secrets.token_urlsafe(24)
        codes[code] = {"user": user, "client_id": q["client_id"], "redirect_uri": q["redirect_uri"], "nonce": q.get("nonce"),
                       "challenge": q.get("code_challenge"), "exp": time.time() + 60}
        back = {"code": code, **({"state": q["state"]} if "state" in q else {})}
        sep = "&" if "?" in q["redirect_uri"] else "?"
        return RedirectResponse(f"{q['redirect_uri']}{sep}{urlencode(back)}", status_code=302)

    def authenticate_client(request: Request, client_id: str | None, secret: str | None) -> tuple[str, dict[str, Any]] | None:
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("basic "):
            client_id, _, secret = base64.b64decode(auth[6:]).decode().partition(":")
        client = clients.get(client_id or "")
        if client and secrets.compare_digest(client.get("secret", ""), secret or ""):
            return client_id, client  # type: ignore[return-value]
        return None

    @app.post("/token")
    async def token(request: Request) -> JSONResponse:
        # application/x-www-form-urlencoded, parsed by hand so the mock needs no python-multipart
        form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
        grant_type, code, redirect_uri = form.get("grant_type", ""), form.get("code"), form.get("redirect_uri")
        client_id, client_secret, code_verifier = form.get("client_id"), form.get("client_secret"), form.get("code_verifier")
        found = authenticate_client(request, client_id, client_secret)
        if not found:
            return JSONResponse({"error": "invalid_client"}, status_code=401)
        cid, client = found
        if grant_type == "client_credentials":
            claims: dict[str, Any] = {"sub": cid, "azp": cid, "aud": api_audience}
            if client.get("service"):
                claims["llmgate_service"] = client["service"]
            if client.get("models"):
                claims["llmgate_models"] = client["models"]
            return JSONResponse({"access_token": sign(claims), "token_type": "Bearer", "expires_in": 3600})
        if grant_type != "authorization_code":
            return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
        grant = codes.pop(code or "", None)  # single use
        if not grant or grant["exp"] < time.time() or grant["client_id"] != cid or grant["redirect_uri"] != redirect_uri:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        if grant["challenge"]:
            expect = _b64(hashlib.sha256((code_verifier or "").encode()).digest())
            if not secrets.compare_digest(expect, grant["challenge"]):
                return JSONResponse({"error": "invalid_grant", "error_description": "PKCE verification failed"}, status_code=400)
        u = users[grant["user"]]
        id_claims = {**user_claims(u), "aud": cid, "azp": cid, **({"nonce": grant["nonce"]} if grant["nonce"] else {})}
        return JSONResponse({
            "id_token": sign(id_claims), "access_token": sign({**user_claims(u), "aud": api_audience, "azp": cid}),
            "token_type": "Bearer", "expires_in": 3600,
        })

    @app.get("/userinfo")
    async def userinfo(request: Request) -> JSONResponse:
        try:
            claims = jwt.decode(request.headers.get("authorization", "")[7:], keys.private.public_key(), algorithms=["RS256"],
                                audience=api_audience, issuer=issuer)
        except jwt.PyJWTError:
            return JSONResponse({"error": "invalid_token"}, status_code=401)
        return JSONResponse({k: claims[k] for k in ("sub", "email", "name", "roles") if k in claims})

    @app.get("/logout", response_class=HTMLResponse)
    async def logout() -> str:
        return "<!doctype html><meta charset=utf-8><p>Signed out of the mock IdP.</p>"

    return app


def run() -> None:
    import uvicorn

    p = argparse.ArgumentParser(description="Mock OIDC provider")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8200)
    p.add_argument("--auto-login", default=None, help="approve every sign-in as this user (alice, bob, ...) without showing the picker")
    a = p.parse_args()
    uvicorn.run(create_app(f"http://{a.host}:{a.port}", auto_login=a.auto_login), host=a.host, port=a.port, log_level="warning")


if __name__ == "__main__":
    run()
