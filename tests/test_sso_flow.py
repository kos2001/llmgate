from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import MockServer, admin, bearer, sso_login, sso_settings
from llmgate.config import Settings
from llmgate.main import create_app

CSRF = {"x-llmgate-csrf": "1"}


def test_config_and_login_redirect(sso_gw, idp):
    assert sso_gw.get("/auth/config").json()["oidc"] == {"enabled": True, "login_url": "/auth/oidc/login"}
    r = sso_gw.get("/auth/oidc/login", follow_redirects=False)
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert r.headers["location"].startswith(idp.root + "/authorize")
    assert q["code_challenge_method"] == ["S256"] and q["code_challenge"] and q["state"] and q["nonce"]
    assert q["scope"] == ["openid profile email"]
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie


@pytest.mark.parametrize("user,role", [("alice", "admin"), ("bob", "operator"), ("carol", "viewer"), ("dave", "viewer")])
def test_login_maps_idp_roles(sso_gw, idp, user, role):
    cb = sso_login(sso_gw, idp, user)
    assert cb.status_code == 302 and cb.headers["location"] == "/dashboard/"
    session = [c for c in cb.headers.get_list("set-cookie") if c.startswith("llmgate_session=")][0].lower()
    assert "httponly" in session and "samesite=lax" in session
    me = sso_gw.get("/auth/me").json()
    assert me["role"] == role and me["email"] == f"{user}@example.com" and me["kind"] == "session"
    assert me["permissions"] == ["viewer", "operator", "admin"][: ["viewer", "operator", "admin"].index(role) + 1]


def test_login_denials(idp):
    with TestClient(create_app(sso_settings(idp, oidc_default_role="none")), base_url="http://127.0.0.1") as gw:
        denied = sso_login(gw, idp, "dave")  # authenticated at the IdP, but no mapped group
        assert "code=no_role" in denied.headers["location"] and "llmgate_session" not in denied.headers.get("set-cookie", "")
        assert gw.get("/auth/me").status_code == 401
        assert "code=domain_not_allowed" in sso_login(gw, idp, "eve").headers["location"]  # evil.test
        assert "llmgate_session" in sso_login(gw, idp, "alice").headers["set-cookie"]


def test_state_and_pkce_are_enforced(sso_gw, idp):
    sso_gw.cookies.clear()
    r = sso_gw.get("/auth/oidc/login", follow_redirects=False)
    code_url = httpx.get(r.headers["location"] + "&login_as=alice", follow_redirects=False).headers["location"]
    q = parse_qs(urlparse(code_url).query)
    forged = sso_gw.get(f"/auth/oidc/callback?code={q['code'][0]}&state=forged", follow_redirects=False)
    assert "code=bad_state" in forged.headers["location"]
    sso_gw.cookies.clear()  # no state cookie at all (e.g. login CSRF from another browser)
    assert "code=bad_state" in sso_gw.get(code_url, follow_redirects=False).headers["location"]
    sso_gw.cookies.clear()
    sso_gw.get("/auth/oidc/login", follow_redirects=False)  # a new login attempt has its own state + verifier
    replay = sso_gw.get(code_url, follow_redirects=False)  # ...so the earlier code/state pair is rejected
    assert "code=bad_state" in replay.headers["location"]
    assert sso_gw.get("/auth/me").status_code == 401


def test_next_param_blocks_open_redirects(sso_gw, idp):
    assert sso_login(sso_gw, idp, "alice", next="//evil.example").headers["location"] == "/dashboard/"
    assert sso_login(sso_gw, idp, "alice", next="https://evil.example").headers["location"] == "/dashboard/"
    assert sso_login(sso_gw, idp, "alice", next="/dashboard/%23models").headers["location"] == "/dashboard/#models"


def test_logout_clears_session_and_points_to_idp(sso_gw, idp):
    sso_login(sso_gw, idp, "alice")
    out = sso_gw.post("/auth/logout")
    assert out.json()["logout_url"].startswith(idp.root + "/logout")
    sso_gw.cookies.clear()
    assert sso_gw.get("/auth/me").status_code == 401


def test_sso_disabled_by_default(gw):
    assert gw.get("/auth/config").json()["oidc"]["enabled"] is False
    assert gw.get("/auth/oidc/login", follow_redirects=False).status_code == 404
    assert gw.get("/auth/me").status_code == 401
    assert gw.get("/auth/me", headers=bearer("test-admin")).json()["kind"] == "admin_token"


def test_default_is_to_deny_signed_in_users_without_a_mapped_group(idp):
    from conftest import sso_settings

    settings = sso_settings(idp)
    settings = settings.model_copy(update={"oidc_default_role": Settings().oidc_default_role})  # the shipped default
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as gw:
        assert "code=no_role" in sso_login(gw, idp, "dave").headers["location"]  # authenticated, but nobody granted a role
        assert sso_login(gw, idp, "bob").headers["location"] == "/dashboard/"
