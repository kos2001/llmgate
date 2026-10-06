from __future__ import annotations

import threading
import time

import pytest

from conftest import admin
from test_routing_prefs import chat, key, model, provider


@pytest.fixture
def one(gw, mock_factory):
    mock = mock_factory(token_delay=0.0)
    p = provider(gw, "alpha", mock)
    model(gw, "m", [{"provider_id": p["id"]}], input_price_per_m=1000, output_price_per_m=1000)
    model(gw, "other", [{"provider_id": p["id"]}])
    return gw, mock


def test_budget_duration_window_resets(one):
    gw, _ = one
    k = key(gw, monthly_budget_usd=0.0001, budget_duration="1h")
    assert chat(gw, k).status_code == 200
    blocked = chat(gw, k)
    assert blocked.status_code == 402 and blocked.json()["error"]["code"] == "budget_exceeded"
    row = admin(gw, "GET", "/services")[0]["keys"][0]
    assert row["budget_duration_s"] == 3600 and row["budget_anchor"] is not None

    time.sleep(0.05)  # coarse clocks (~15 ms on Windows): the new window must begin strictly after the spend above
    gw.app.state.db.execute("UPDATE api_keys SET budget_anchor = ? WHERE id = ?", (time.time() - 2 * 3600, row["id"]))
    assert chat(gw, k).status_code == 200  # two hours later the window has rolled over and spend starts from zero
    assert admin(gw, "GET", "/services")[0]["keys"][0]["budget_anchor"] > time.time() - 60


def test_invalid_duration_is_rejected(one):
    gw, _ = one
    svc = admin(gw, "POST", "/services", json={"name": "s"})
    r = gw.post(f"/admin/api/services/{svc['id']}/keys", headers={"Authorization": "Bearer test-admin"},
                json={"name": "k", "budget_duration": "soon"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_duration"


def test_tpm_limit(one):
    gw, _ = one
    k = key(gw, tpm_limit=10)
    assert chat(gw, k, model_name="other").status_code == 200  # over the limit after one reply (a soft, post-hoc limit)
    r = chat(gw, k, model_name="other")
    assert r.status_code == 429 and r.json()["error"]["code"] == "tpm_limit_exceeded" and "retry-after" in r.headers


def test_service_level_rpm_and_model_access(one):
    gw, _ = one
    svc = admin(gw, "POST", "/services", json={"name": "team", "rpm_limit": 2, "allowed_models": ["m", "other"]})
    k = admin(gw, "POST", f"/services/{svc['id']}/keys", json={"name": "k", "allowed_models": ["m"]})
    assert [chat(gw, k).status_code for _ in range(3)] == [200, 200, 429]
    admin(gw, "PATCH", f"/services/{svc['id']}", json={"rpm_limit": None})
    assert chat(gw, k, model_name="other").json()["error"]["code"] == "model_not_allowed"  # key narrows the service's list
    wide = admin(gw, "POST", f"/services/{svc['id']}/keys", json={"name": "wide"})
    assert chat(gw, wide, model_name="other").status_code == 200  # service list applies to keys without their own
    admin(gw, "PATCH", f"/services/{svc['id']}", json={"allowed_models": ["m"]})
    assert chat(gw, wide, model_name="other").status_code == 403


def test_per_model_budget(one):
    gw, _ = one
    k = key(gw, model_budgets={"m": 0.0001})
    assert chat(gw, k, model_name="m").status_code == 200
    r = chat(gw, k, model_name="m")
    assert r.status_code == 402 and r.json()["error"]["code"] == "model_budget_exceeded"
    assert chat(gw, k, model_name="other").status_code == 200  # only that model is capped
    both = chat(gw, k, model_name="m", models=["other"])  # a capped model is skipped when a fallback exists
    assert both.status_code == 200 and both.json()["model"] == "other"


def test_max_parallel_requests(gw, mock_factory):
    slow = mock_factory(token_delay=0.05)
    p = provider(gw, "alpha", slow)
    model(gw, "m", [{"provider_id": p["id"]}])
    k = key(gw, max_parallel=1)
    first = {}
    t = threading.Thread(target=lambda: first.update(r=chat(gw, k, stream=True)))
    t.start()
    time.sleep(0.5)  # the streamed reply takes ~1.5 s, so the slot is held
    r = chat(gw, k)
    assert r.status_code == 429 and r.json()["error"]["code"] == "max_parallel_requests"
    t.join(timeout=10)
    assert first["r"].status_code == 200
    assert chat(gw, k).status_code == 200  # slot released when the stream finished


def test_end_user_budgets_and_blocking(one):
    gw, _ = one
    k = key(gw)
    admin(gw, "POST", "/customers", json={"user_id": "alice", "max_budget_usd": 0.0001})
    admin(gw, "POST", "/customers", json={"user_id": "mallory", "blocked": True})
    assert chat(gw, k, user="alice").status_code == 200
    r = chat(gw, k, user="alice")
    assert r.status_code == 402 and r.json()["error"]["code"] == "customer_budget_exceeded"
    assert chat(gw, k, user="bob").status_code == 200  # unregistered end-users are unrestricted
    blocked = chat(gw, k, user="mallory")
    assert blocked.status_code == 403 and blocked.json()["error"]["code"] == "customer_blocked"

    listed = {c["user_id"]: c for c in admin(gw, "GET", "/customers")}
    assert listed["alice"]["usage"]["cost_usd"] > 0 and listed["bob"].get("unregistered") is True
    admin(gw, "PATCH", "/customers/alice", json={"max_budget_usd": 100})
    assert chat(gw, k, user="alice").status_code == 200
    gw.delete("/admin/api/customers/mallory", headers={"Authorization": "Bearer test-admin"})
    assert chat(gw, k, user="mallory").status_code == 200


def test_tags_track_spend(one):
    gw, _ = one
    k = key(gw)
    chat(gw, k, tags=["team-a", "prod"])
    chat(gw, k, metadata={"tags": ["team-a"]})
    gw.post("/v1/chat/completions", headers={"Authorization": f"Bearer {k['key']}", "x-llmgate-tags": "team-b"},
            json={"model": "m", "messages": [{"role": "user", "content": "x"}]})
    chat(gw, k)
    tags = {t["name"]: t for t in admin(gw, "GET", "/usage/tags")}
    assert tags["team-a"]["requests"] == 2 and tags["prod"]["requests"] == 1 and tags["team-b"]["requests"] == 1
    assert tags["team-a"]["cost_usd"] > tags["prod"]["cost_usd"]
    bad = chat(gw, k, tags="not-a-list")
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_tags"


def test_rate_limit_headers(one):
    gw, _ = one
    k = key(gw, rpm_limit=5)
    r = chat(gw, k)
    assert r.headers["x-ratelimit-limit-requests"] == "5" and r.headers["x-ratelimit-remaining-requests"] == "4"
    assert float(r.headers["x-llmgate-response-cost"]) > 0
