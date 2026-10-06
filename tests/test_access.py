from __future__ import annotations

from conftest import ADMIN, admin, make_stack


def test_auth_errors(gw, mock_factory):
    st = make_stack(gw, [mock_factory().base_url])
    body = {"model": "chat-model", "messages": [{"role": "user", "content": "x"}]}
    assert gw.post("/v1/chat/completions", json=body).status_code == 401
    bad = gw.post("/v1/chat/completions", json=body, headers={"Authorization": "Bearer sk-lg-wrong"})
    assert bad.status_code == 401 and bad.json()["error"]["code"] == "invalid_api_key"
    admin(gw, "PATCH", f"/keys/{st.key_id}", json={"enabled": False})
    assert st.chat().status_code == 403
    admin(gw, "PATCH", f"/keys/{st.key_id}", json={"enabled": True})
    admin(gw, "PATCH", f"/services/{st.service_id}", json={"enabled": False})
    assert st.chat().json()["error"]["code"] == "service_disabled"


def test_admin_requires_token(gw):
    assert gw.get("/admin/api/providers").status_code == 401
    assert gw.get("/admin/api/providers", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert gw.get("/admin/api/providers", headers=ADMIN).status_code == 200


def test_key_is_stored_hashed_and_rotation_invalidates_old(gw, mock_factory):
    st = make_stack(gw, [mock_factory().base_url])
    listed = admin(gw, "GET", "/services")[0]["keys"][0]
    assert "key" not in listed and "key_hash" not in listed and st.key.startswith(listed["key_prefix"])
    assert st.chat().status_code == 200
    new = admin(gw, "POST", f"/keys/{st.key_id}/rotate")["key"]
    assert st.chat().status_code == 401
    ok = gw.post("/v1/chat/completions", headers={"Authorization": f"Bearer {new}"},
                 json={"model": "chat-model", "messages": [{"role": "user", "content": "x"}]})
    assert ok.status_code == 200


def test_allowed_models_restrict_key(gw, mock_factory):
    st = make_stack(gw, [mock_factory().base_url], key_opts={"allowed_models": ["something-else"]})
    r = st.chat()
    assert r.status_code == 403 and r.json()["error"]["code"] == "model_not_allowed"
    assert gw.get("/v1/models", headers=st.headers()).json()["data"] == []
    assert gw.post("/v1/chat/completions", headers=st.headers(),
                   json={"model": "ghost", "messages": []}).json()["error"]["code"] == "model_not_found"


def test_rpm_limit(gw, mock_factory):
    st = make_stack(gw, [mock_factory().base_url], key_opts={"rpm_limit": 2})
    assert [st.chat().status_code for _ in range(3)] == [200, 200, 429]
    assert int(st.chat().headers["retry-after"]) >= 1


def test_monthly_token_limit_on_key_and_service(gw, mock_factory):
    st = make_stack(gw, [mock_factory().base_url], key_opts={"monthly_token_limit": 30})
    assert st.chat().status_code == 200
    r = st.chat()
    assert r.status_code == 402 and r.json()["error"]["code"] == "token_limit_exceeded"

    admin(gw, "PATCH", f"/keys/{st.key_id}", json={"monthly_token_limit": None})
    assert st.chat().status_code == 200
    admin(gw, "PATCH", f"/services/{st.service_id}", json={"monthly_budget_usd": 0.00001})
    assert st.chat().json()["error"]["code"] == "budget_exceeded"


def test_budget_exceeded_requests_are_logged(gw, mock_factory):
    st = make_stack(gw, [mock_factory().base_url], key_opts={"monthly_budget_usd": 0.0001}, in_price=100, out_price=100)
    st.chat()
    assert st.chat().status_code == 402
    logs = admin(gw, "GET", "/usage/logs", params={"only_errors": True})
    assert logs["total"] == 1 and logs["data"][0]["status_code"] == 402


def test_model_catalog_and_key_info(gw, mock_factory):
    st = make_stack(gw, [mock_factory().base_url], in_price=0.5, out_price=1.5, key_opts={"rpm_limit": 60})
    public = gw.get("/api/v1/models").json()["data"][0]
    assert public["id"] == "chat-model" and public["context_length"] == 8192
    assert float(public["pricing"]["prompt"]) == 0.5 / 1e6 and public["status"] == "available"
    st.chat()
    info = gw.get("/v1/key", headers=st.headers()).json()["data"]
    assert info["label"] == "k1" and info["usage"]["tokens"] > 0 and info["rate_limit"]["requests"] == 60


def test_usage_aggregation_endpoints(gw, mock_factory):
    st = make_stack(gw, [mock_factory().base_url])
    for _ in range(3):
        st.chat("count me")
    by_model = admin(gw, "GET", "/usage/summary", params={"group_by": "model"})
    assert by_model[0]["name"] == "chat-model" and by_model[0]["requests"] == 3
    by_service = admin(gw, "GET", "/usage/summary", params={"group_by": "service"})
    assert by_service[0]["name"] == "svc" and by_service[0]["total_tokens"] == by_model[0]["total_tokens"]
    series = admin(gw, "GET", "/usage/timeseries", params={"bucket": "hour", "split": "model"})
    assert sum(r["requests"] for r in series) == 3
    ov = admin(gw, "GET", "/overview")
    assert ov["totals"]["requests"] == 3 and ov["counts"]["models"] == 1


def test_provider_check_and_sync_models(gw, mock_factory):
    mock = mock_factory(["alpha", "beta"])
    p = admin(gw, "POST", "/providers", json={"name": "v", "base_url": mock.base_url})
    checked = admin(gw, "POST", f"/providers/{p['id']}/check")
    assert checked["status"] == "healthy" and checked["engine_stats"]["requests_running"] == 0
    synced = admin(gw, "POST", f"/providers/{p['id']}/sync-models", params={"register": True})
    assert sorted(synced["registered"]) == ["alpha", "beta"]
    assert {m["name"] for m in admin(gw, "GET", "/models")} == {"alpha", "beta"}
    assert admin(gw, "POST", f"/providers/{p['id']}/sync-models", params={"register": True})["registered"] == []

    mock.stop()
    assert admin(gw, "POST", f"/providers/{p['id']}/check")["status"] == "unhealthy"


def test_duplicate_names_conflict(gw):
    admin(gw, "POST", "/services", json={"name": "dup"})
    assert gw.post("/admin/api/services", json={"name": "dup"}, headers=ADMIN).status_code == 409
