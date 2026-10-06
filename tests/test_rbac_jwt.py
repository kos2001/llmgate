from __future__ import annotations

import time

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from conftest import ADMIN, admin, bearer, idp_token, sso_login

CSRF = {"x-llmgate-csrf": "1"}


def test_role_matrix_for_browser_sessions(sso_gw, idp, mock_factory):
    def call(method, path, **kw):
        return sso_gw.request(method, "/admin/api" + path, headers=CSRF, **kw).status_code

    sso_login(sso_gw, idp, "carol")  # viewer
    assert call("GET", "/overview") == 200 and call("GET", "/providers") == 200
    assert call("POST", "/services", json={"name": "s1"}) == 403
    assert call("GET", "/users") == 403 and call("GET", "/audit") == 403 and call("GET", "/auth/settings") == 403

    sso_login(sso_gw, idp, "bob")  # operator
    assert call("POST", "/services", json={"name": "s1"}) == 201
    assert call("POST", "/models", json={"name": "m1"}) == 201
    assert call("POST", "/providers", json={"name": "p", "base_url": "http://x:1/v1"}) == 403
    assert call("GET", "/users") == 403

    sso_login(sso_gw, idp, "alice")  # admin
    assert call("POST", "/providers", json={"name": "p", "base_url": "http://x:1/v1"}) == 201
    assert call("GET", "/users") == 200 and call("GET", "/auth/settings") == 200


def test_cookie_writes_require_csrf_header(sso_gw, idp):
    sso_login(sso_gw, idp, "alice")
    r = sso_gw.post("/admin/api/services", json={"name": "s"})  # a cross-site form post carries the cookie but no custom header
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf"
    assert sso_gw.get("/admin/api/overview").status_code == 200  # reads are fine
    assert sso_gw.post("/admin/api/services", json={"name": "s"}, headers=CSRF).status_code == 201


def test_idp_jwt_as_admin_bearer(sso_gw, idp):
    r = sso_gw.get("/admin/api/users", headers=bearer(idp_token(idp, "alice")))
    assert r.status_code == 200 and r.json()[0]["email"] == "alice@example.com"  # JIT-provisioned
    viewer = bearer(idp_token(idp, "carol"))
    assert sso_gw.get("/admin/api/overview", headers=viewer).status_code == 200
    assert sso_gw.post("/admin/api/services", json={"name": "s"}, headers=viewer).status_code == 403  # no CSRF header needed for bearer


def _forged() -> dict[str, str]:
    now = int(time.time())
    claims = {"iss": "x", "sub": "u-alice", "aud": "llmgate-api", "exp": now + 600, "roles": ["llm-admins"], "email": "alice@example.com"}
    return {
        "alg=none": jwt.encode(claims, None, algorithm="none"),
        "hs256-guessed-secret": jwt.encode(claims, "guess-guess-guess-guess-guess-guess", algorithm="HS256"),
        "foreign-rsa-key": jwt.encode(claims, rsa.generate_private_key(public_exponent=65537, key_size=2048), algorithm="RS256"),
    }


def test_forged_and_invalid_tokens_are_rejected(sso_gw, idp):
    bad = {**_forged(),
           "wrong-audience": idp_token(idp, "alice", aud="someone-else"),
           "expired": idp_token(idp, "alice", ttl=-120)}
    # a token with the right issuer/kid but signed by a different key
    kid = idp.app.state.keys.kid
    claims = {"iss": idp.root, "sub": "u-alice", "aud": "llmgate-api", "exp": int(time.time()) + 600, "roles": ["llm-admins"]}
    bad["right-kid-wrong-key"] = jwt.encode(claims, rsa.generate_private_key(public_exponent=65537, key_size=2048),
                                            algorithm="RS256", headers={"kid": kid})
    for name, token in bad.items():
        assert sso_gw.get("/admin/api/overview", headers=bearer(token)).status_code == 401, name
        assert sso_gw.get("/v1/models", headers=bearer(token)).status_code == 401, name


def test_signing_key_rotation(sso_gw, idp):
    old = idp_token(idp, "alice")
    assert sso_gw.get("/admin/api/overview", headers=bearer(old)).status_code == 200
    idp.app.state.keys.rotate()
    new = idp_token(idp, "alice")  # unknown kid → the gateway re-fetches the JWKS once
    assert sso_gw.get("/admin/api/overview", headers=bearer(new)).status_code == 200
    assert sso_gw.get("/admin/api/overview", headers=bearer(old)).status_code == 401


def test_admin_token_can_be_disabled(idp):
    from fastapi.testclient import TestClient

    from conftest import sso_settings
    from llmgate.main import create_app

    with TestClient(create_app(sso_settings(idp, admin_token_enabled=False)), base_url="http://127.0.0.1") as gw:
        assert gw.get("/admin/api/overview", headers=ADMIN).status_code == 401
        assert gw.get("/admin/api/overview", headers=bearer(idp_token(idp, "alice"))).status_code == 200


# ------------------------------------------------------------------------------- inference with JWT

def _inference_setup(gw, vllm):
    p = admin(gw, "POST", "/providers", json={"name": "v", "base_url": vllm.base_url})
    for name in ("Qwen/Qwen2.5-7B-Instruct", "other-model"):
        admin(gw, "POST", "/models", json={"name": name, "input_price_per_m": 1, "output_price_per_m": 1,
                                           "routes": [{"provider_id": p["id"], "upstream_model": "mock-chat"}]})
    admin(gw, "POST", "/services", json={"name": "chatbot-web"})


def _m2m(idp) -> str:
    resp = httpx.post(idp.root + "/token", data={"grant_type": "client_credentials"}, auth=("ci-pipeline", "ci-secret"))
    return resp.json()["access_token"]


def _chat(gw, token, model):
    return gw.post("/v1/chat/completions", headers=bearer(token),
                   json={"model": model, "messages": [{"role": "user", "content": "hi"}]})


def test_jwt_authenticates_inference_and_binds_to_service(sso_gw, idp, mock_factory):
    _inference_setup(sso_gw, mock_factory())
    token = _m2m(idp)
    ok = _chat(sso_gw, token, "Qwen/Qwen2.5-7B-Instruct")
    assert ok.status_code == 200, ok.text
    denied = _chat(sso_gw, token, "other-model")  # the token's models claim narrows access
    assert denied.status_code == 403 and denied.json()["error"]["code"] == "model_not_allowed"

    keys = admin(sso_gw, "GET", "/services")[0]["keys"]
    assert len(keys) == 1 and keys[0]["kind"] == "jwt" and keys[0]["name"] == "ci-pipeline"
    logs = admin(sso_gw, "GET", "/usage/logs")["data"]
    assert any(l["service_name"] == "chatbot-web" and l["key_name"] == "ci-pipeline" and l["success"] for l in logs)
    assert sso_gw.get("/v1/key", headers=bearer(token)).json()["data"]["service"] == "chatbot-web"

    admin(sso_gw, "PATCH", f"/keys/{keys[0]['id']}", json={"enabled": False})  # revoke that identity
    assert _chat(sso_gw, token, "Qwen/Qwen2.5-7B-Instruct").status_code == 403


def test_jwt_inference_needs_a_known_service_claim(sso_gw, idp, mock_factory):
    _inference_setup(sso_gw, mock_factory())
    user_token = idp_token(idp, "alice")  # a human token: valid, but names no service
    assert _chat(sso_gw, user_token, "other-model").json()["error"]["code"] == "no_service_claim"
    sign = idp.app.state.sign
    unknown = sign({"sub": "x", "aud": "llmgate-api", "llmgate_service": "ghost"})
    assert _chat(sso_gw, unknown, "other-model").json()["error"]["code"] == "unknown_service"
    admin(sso_gw, "PATCH", "/services/1", json={"enabled": False})
    assert _chat(sso_gw, _m2m(idp), "other-model").json()["error"]["code"] == "service_disabled"


def test_session_jwt_is_not_an_inference_credential(sso_gw, idp, mock_factory):
    _inference_setup(sso_gw, mock_factory())
    sso_login(sso_gw, idp, "alice")
    session = sso_gw.cookies.get("llmgate_session")
    assert _chat(sso_gw, session, "other-model").status_code == 401
    assert sso_gw.get("/admin/api/overview", headers=bearer(session)).status_code == 200  # but it is an admin bearer


def test_jwt_inference_is_off_without_sso(gw):
    token = jwt.encode({"sub": "x"}, "k" * 40, algorithm="HS256")
    assert gw.get("/v1/models", headers=bearer(token)).status_code == 401


# --------------------------------------------------------------------------------- user management

def test_manual_role_overrides_claims_until_released(sso_gw, idp):
    sso_login(sso_gw, idp, "bob")
    bob = admin(sso_gw, "GET", "/users")[0]
    assert bob["role"] == "operator" and bob["role_source"] == "claims"

    admin(sso_gw, "PATCH", f"/users/{bob['id']}", json={"role": "viewer"})
    assert sso_gw.get("/auth/me").json()["role"] == "viewer"  # takes effect on the live session
    sso_login(sso_gw, idp, "bob")
    assert sso_gw.get("/auth/me").json()["role"] == "viewer"  # IdP says operator; the pinned role wins

    admin(sso_gw, "PATCH", f"/users/{bob['id']}", json={"release_role": True})
    sso_login(sso_gw, idp, "bob")
    assert sso_gw.get("/auth/me").json()["role"] == "operator"


def test_disabling_a_user_kills_sessions_and_logins(sso_gw, idp):
    sso_login(sso_gw, idp, "carol")
    carol = admin(sso_gw, "GET", "/users")[0]
    admin(sso_gw, "PATCH", f"/users/{carol['id']}", json={"enabled": False})
    assert sso_gw.get("/auth/me").status_code == 401
    assert "code=user_disabled" in sso_login(sso_gw, idp, "carol").headers["location"]
    admin(sso_gw, "DELETE", f"/users/{carol['id']}")
    assert sso_login(sso_gw, idp, "carol").headers["location"] == "/dashboard/"  # re-provisioned from claims


def test_pre_provisioned_role_is_adopted_on_first_login(sso_gw, idp):
    pending = admin(sso_gw, "POST", "/users", json={"email": "Dave@Example.com", "role": "operator"})
    assert pending["pending"] is True
    sso_login(sso_gw, idp, "dave")  # IdP grants no groups; the admin-assigned role applies
    users = admin(sso_gw, "GET", "/users")
    assert len(users) == 1 and users[0]["role"] == "operator" and users[0]["pending"] is False
    assert sso_gw.get("/auth/me").json()["role"] == "operator"


def test_cannot_lock_yourself_out(sso_gw, idp):
    sso_login(sso_gw, idp, "alice")
    me = sso_gw.get("/auth/me").json()
    for body in ({"role": "viewer"}, {"enabled": False}):
        r = sso_gw.patch(f"/admin/api/users/{me['user_id']}", json=body, headers=CSRF)
        assert r.status_code == 400 and r.json()["error"]["code"] == "self_lockout"
    assert sso_gw.delete(f"/admin/api/users/{me['user_id']}", headers=CSRF).status_code == 400


def test_audit_log_records_who_changed_what(sso_gw, idp):
    sso_login(sso_gw, idp, "bob")
    sso_gw.post("/admin/api/services", json={"name": "audited"}, headers=CSRF)
    sso_gw.post("/admin/api/providers", json={"name": "p", "base_url": "http://x:1/v1"}, headers=CSRF)  # forbidden
    sso_gw.get("/admin/api/overview")  # reads are not audited
    rows = admin(sso_gw, "GET", "/audit")["data"]
    assert [(r["actor"], r["role"], r["method"], r["path"], r["status"]) for r in rows][:2] == [
        ("bob@example.com", "operator", "POST", "/admin/api/providers", 403),
        ("bob@example.com", "operator", "POST", "/admin/api/services", 201),
    ]
    assert not any(r["method"] == "GET" for r in rows)


def test_auth_settings_reports_effective_config(sso_gw, idp):
    s = admin(sso_gw, "GET", "/auth/settings")
    assert s["oidc"]["enabled"] and s["oidc"]["issuer"] == idp.root and s["oidc"]["role_map"]["llm-admins"] == "admin"
    assert "dashboard-secret" not in str(s) and "test-admin" not in str(s)  # no credentials in the settings view
    assert [r["name"] for r in s["roles"]] == ["viewer", "operator", "admin"]
