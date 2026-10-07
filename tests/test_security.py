from __future__ import annotations

import time

import pytest

from conftest import ADMIN, admin, bearer
from llmgate.errors import GatewayError
from llmgate.throttle import AuthThrottle
from test_routing_prefs import key, model, provider

BAD = {"Authorization": "Bearer sk-lg-this-is-not-a-real-key"}


# ------------------------------------------------------------------------------------------ headers

def test_dashboard_gets_a_strict_csp_and_framing_is_denied(gw):
    for path in ("/dashboard/", "/dashboard/app.js", "/dashboard/style.css"):
        r = gw.get(path)
        csp = r.headers["content-security-policy"]
        assert "default-src 'self'" in csp and "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "object-src 'none'" in csp, path
        assert "unsafe-inline" not in csp.split("script-src")[1].split(";")[0]  # scripts: never inline
        assert r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "same-origin"


def test_sensitive_json_is_never_cached_and_docs_keep_working(gw, mock_factory):
    p = provider(gw, "p", mock_factory())
    model(gw, "m", [{"provider_id": p["id"]}])
    k = key(gw)
    for path in ("/admin/api/overview", "/admin/api/services", "/metrics", "/auth/me"):
        assert gw.get(path, headers=ADMIN).headers["cache-control"] == "no-store", path
    assert gw.post("/key/generate", headers=ADMIN, json={}).headers["cache-control"] == "no-store"  # a response carrying a new secret
    assert "content-security-policy" not in gw.get("/docs").headers  # Swagger loads its UI from a CDN, so no CSP there
    assert gw.get("/v1/models", headers=bearer(k["key"])).headers["x-content-type-options"] == "nosniff"  # everything gets nosniff
    assert gw.get("/healthz").headers["x-content-type-options"] == "nosniff"


# ----------------------------------------------------------------------------------- failed-auth throttle

def test_unit_sliding_window():
    t = AuthThrottle(limit=3, window_s=0.2)
    for _ in range(3):
        t.check("1.2.3.4")
        t.fail("1.2.3.4")
    with pytest.raises(GatewayError) as exc:
        t.check("1.2.3.4")
    assert exc.value.status_code == 429 and exc.value.code == "too_many_failed_attempts" and int(exc.value.headers["Retry-After"]) >= 1
    t.check("5.6.7.8")  # other addresses are unaffected
    time.sleep(0.25)
    t.check("1.2.3.4")  # the window slid past the old failures
    AuthThrottle(limit=0, window_s=1).fail("x")  # limit 0 = disabled
    AuthThrottle(limit=0, window_s=1).check("x")


def test_wrong_api_keys_are_throttled_then_recover(gw_factory, mock_factory):
    gw = gw_factory(auth_fail_limit=5, auth_fail_window_s=1.5)
    p = provider(gw, "p", mock_factory())
    model(gw, "m", [{"provider_id": p["id"]}])
    good = key(gw)
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    codes = [gw.post("/v1/chat/completions", headers=BAD, json=body).status_code for _ in range(5)]
    assert codes == [401] * 5
    blocked = gw.post("/v1/chat/completions", headers=bearer(good["key"]), json=body)
    assert blocked.status_code == 429 and blocked.json()["error"]["code"] == "too_many_failed_attempts"  # per address, even for a valid key
    assert int(blocked.headers["retry-after"]) >= 1
    time.sleep(1.7)
    assert gw.post("/v1/chat/completions", headers=bearer(good["key"]), json=body).status_code == 200


def test_wrong_admin_tokens_are_throttled(gw_factory):
    gw = gw_factory(auth_fail_limit=4, auth_fail_window_s=60)
    for _ in range(4):
        assert gw.get("/admin/api/overview", headers=bearer("guess")).status_code == 401
    r = gw.get("/admin/api/overview", headers=ADMIN)
    assert r.status_code == 429 and r.json()["error"]["code"] == "too_many_failed_attempts"
    assert gw.get("/healthz").status_code == 200  # unauthenticated endpoints are untouched


def test_only_refused_credentials_count(gw_factory, mock_factory):
    gw = gw_factory(auth_fail_limit=3, auth_fail_window_s=60)
    p = provider(gw, "p", mock_factory())
    model(gw, "m", [{"provider_id": p["id"]}])
    k = key(gw)
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    for _ in range(10):  # no credential at all: not an attack on a key
        assert gw.post("/v1/chat/completions", json=body).json()["error"]["code"] == "missing_api_key"
    admin(gw, "PATCH", f"/keys/{k['id']}", json={"enabled": False})
    for _ in range(10):  # a real but disabled key is refused with 403, which is not guessing either
        assert gw.post("/v1/chat/completions", headers=bearer(k["key"]), json=body).status_code == 403
    assert gw.get("/admin/api/overview").status_code == 401  # no admin credential presented
    assert gw.get("/admin/api/overview", headers=ADMIN).status_code == 200  # still nowhere near the limit


def test_throttle_can_be_switched_off(gw_factory):
    gw = gw_factory(auth_fail_limit=0)
    for _ in range(60):
        assert gw.get("/admin/api/overview", headers=bearer("guess")).status_code == 401
    assert gw.get("/admin/api/overview", headers=ADMIN).status_code == 200
