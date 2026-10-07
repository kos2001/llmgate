from __future__ import annotations


from conftest import admin
from test_routing_prefs import chat, key, model, provider

H = {"Authorization": "Bearer test-admin"}


def two_providers(gw, mock_factory, **model_extra):
    bad, good = mock_factory(), mock_factory()
    pa, pb = provider(gw, "flaky", bad), provider(gw, "steady", good)
    model(gw, "m", [{"provider_id": pa["id"], "priority": 0}, {"provider_id": pb["id"], "priority": 1}], **model_extra)
    return bad, good, pa, pb


def last_attempts(gw):
    return admin(gw, "GET", "/usage/logs")["data"][0]["attempts"]


def test_cooldown_outlasts_a_misleading_health_probe(gw_factory, mock_factory):
    """/health can say OK while inference keeps failing; cooldown stops every request from paying for that."""
    gw = gw_factory(cooldown_after_fails=2, cooldown_s=30)
    bad, good, pa, _ = two_providers(gw, mock_factory)
    bad.app.state.mock.error_rate = 1.0  # inference fails, /health stays fine
    k = key(gw)
    for _ in range(2):
        admin(gw, "POST", f"/providers/{pa['id']}/check")  # the probe flips it back to healthy each time
        assert chat(gw, k).status_code == 200 and last_attempts(gw) == 2  # flaky tried first, then steady
    assert gw.app.state.router.cooldown_remaining(pa["id"]) > 0

    admin(gw, "POST", f"/providers/{pa['id']}/check")
    assert [p["status"] for p in admin(gw, "GET", "/providers") if p["name"] == "flaky"] == ["healthy"]
    seen = bad.app.state.mock.request_count
    assert chat(gw, k).status_code == 200 and last_attempts(gw) == 1  # flaky is cooling down: skipped outright
    assert chat(gw, k, provider={"order": ["flaky"]}).status_code == 200 and last_attempts(gw) == 1  # even if preferred
    assert bad.app.state.mock.request_count == seen


def test_cooldown_is_ignored_when_every_provider_cools(gw_factory, mock_factory):
    gw = gw_factory(cooldown_after_fails=1, cooldown_s=30)
    srv = mock_factory()
    p = provider(gw, "only", srv)
    model(gw, "m", [{"provider_id": p["id"]}])
    k = key(gw)
    srv.app.state.mock.fail_next = 1
    assert chat(gw, k).status_code == 502  # one failure puts the only provider into cooldown
    assert gw.app.state.router.cooldown_remaining(p["id"]) > 0
    assert chat(gw, k).status_code == 200  # still tried: nothing else to use, and it has recovered


def test_retries_on_the_same_provider(gw_factory, mock_factory):
    gw = gw_factory(retry_backoff_s=0.01)
    srv = mock_factory()
    p = provider(gw, "only", srv)
    model(gw, "m", [{"provider_id": p["id"]}])
    k = key(gw)
    srv.app.state.mock.fail_next = 1
    assert chat(gw, k).status_code == 502  # default: no retries
    srv.app.state.mock.fail_next = 1
    r = chat(gw, k, num_retries=2)
    assert r.status_code == 200 and last_attempts(gw) == 2
    assert "num_retries" not in srv.app.state.mock.last_payload  # gateway-only field is not forwarded


def test_cost_strategy_prefers_the_cheapest_route(gw, mock_factory):
    a, b = mock_factory(), mock_factory()
    pa, pb = provider(gw, "pricey", a), provider(gw, "cheap", b)
    model(gw, "m", [{"provider_id": pa["id"], "input_price_per_m": 9, "output_price_per_m": 9},
                    {"provider_id": pb["id"], "input_price_per_m": 1, "output_price_per_m": 1}], routing_strategy="cost")
    k = key(gw)
    assert {chat(gw, k).headers["x-llmgate-provider"] for _ in range(5)} == {"cheap"}


def test_least_busy_and_usage_strategies_run(gw, mock_factory):
    a, b = mock_factory(), mock_factory()
    pa, pb = provider(gw, "a", a), provider(gw, "b", b)
    for strategy in ("least-busy", "usage", "latency"):
        model(gw, f"m-{strategy}", [{"provider_id": pa["id"]}, {"provider_id": pb["id"]}], routing_strategy=strategy)
    k = key(gw)
    for strategy in ("least-busy", "usage", "latency"):
        seen = {chat(gw, k, model_name=f"m-{strategy}").headers["x-llmgate-provider"] for _ in range(6)}
        assert seen <= {"a", "b"}
    # `usage` balances by recent tokens: after many requests neither provider is left idle
    used = {chat(gw, k, model_name="m-usage").headers["x-llmgate-provider"] for _ in range(6)}
    assert used == {"a", "b"}


def test_route_level_rpm_limit_diverts_traffic(gw, mock_factory):
    a, b = mock_factory(), mock_factory()
    pa, pb = provider(gw, "limited", a), provider(gw, "overflow", b)
    model(gw, "m", [{"provider_id": pa["id"], "priority": 0, "rpm_limit": 2}, {"provider_id": pb["id"], "priority": 1}])
    k = key(gw)
    assert [chat(gw, k).headers["x-llmgate-provider"] for _ in range(4)] == ["limited", "limited", "overflow", "overflow"]


def test_model_aliases(gw, mock_factory):
    srv = mock_factory()
    p = provider(gw, "p", srv)
    model(gw, "llama-3.1-70b", [{"provider_id": p["id"]}])
    k = key(gw)
    admin(gw, "POST", "/aliases", json={"alias": "gpt-4", "model_name": "llama-3.1-70b"})
    r = chat(gw, k, model_name="gpt-4")
    assert r.status_code == 200 and r.json()["model"] == "llama-3.1-70b"  # callers can keep their old model ids
    assert chat(gw, k, model_name="gpt-4:floor").status_code == 200  # variants still work through an alias
    assert gw.post("/admin/api/aliases", headers=H, json={"alias": "llama-3.1-70b", "model_name": "llama-3.1-70b"}).status_code == 409
    assert gw.post("/admin/api/aliases", headers=H, json={"alias": "x", "model_name": "ghost"}).status_code == 404
    assert gw.delete("/admin/api/aliases/gpt-4", headers=H).status_code == 204
    assert chat(gw, k, model_name="gpt-4").status_code == 404


def test_configured_fallbacks_and_context_window_fallbacks(gw, mock_factory):
    down, up = mock_factory(error_rate=1.0), mock_factory()
    small = mock_factory(max_model_len=40)  # rejects long prompts with a context-length error
    big = mock_factory(max_model_len=8192)
    pd, pu, ps, pb = provider(gw, "down", down), provider(gw, "up", up), provider(gw, "small", small), provider(gw, "big", big)
    model(gw, "backup", [{"provider_id": pu["id"]}])
    model(gw, "primary", [{"provider_id": pd["id"]}], fallbacks=["backup"])
    model(gw, "large-ctx", [{"provider_id": pb["id"]}])
    model(gw, "short-ctx", [{"provider_id": ps["id"]}], context_window_fallbacks=["large-ctx"])
    k = key(gw)
    r = chat(gw, k, model_name="primary")
    assert r.status_code == 200 and r.json()["model"] == "backup"  # no `models` in the request: the model's own fallbacks apply
    assert admin(gw, "GET", "/usage/logs")["data"][0]["requested_model"] == "primary"

    r = chat(gw, k, model_name="short-ctx", content="word " * 200)
    assert r.status_code == 200 and r.json()["model"] == "large-ctx"  # context error → bigger-context model
    assert chat(gw, k, model_name="short-ctx", content="short").json()["model"] == "short-ctx"  # primary works: no fallback


# ------------------------------------------------------------------------------------------ cache

def test_response_cache(gw_factory, mock_factory):
    gw = gw_factory(cache_ttl_s=60)
    srv = mock_factory()
    p = provider(gw, "p", srv)
    model(gw, "m", [{"provider_id": p["id"]}], input_price_per_m=10, output_price_per_m=10)
    k, other = key(gw), key(gw, service="other")
    first = chat(gw, k, content="cache me")
    assert first.headers["x-llmgate-cache"] == "miss" and first.json()["usage"]["cost"] > 0
    hit = chat(gw, k, content="cache me")
    assert hit.headers["x-llmgate-cache"] == "hit" and srv.app.state.mock.request_count == 1
    assert hit.json()["usage"]["cost"] == 0 and hit.json()["id"] != first.json()["id"]  # fresh gen id, zero cost
    assert hit.json()["choices"] == first.json()["choices"]
    log = admin(gw, "GET", "/usage/logs")["data"][0]
    assert log["cache_hit"] == 1 and log["cost_usd"] == 0 and log["provider_id"] is None

    assert chat(gw, k, content="cache me", user="someone", tags=["x"]).headers["x-llmgate-cache"] == "hit"  # ignored fields
    assert chat(gw, k, content="different").headers["x-llmgate-cache"] == "miss"
    assert chat(gw, other, content="cache me").headers["x-llmgate-cache"] == "miss"  # entries are per service
    assert chat(gw, k, content="cache me", cache={"no-cache": True}).headers["x-llmgate-cache"] == "miss"  # forced refresh
    assert "x-llmgate-cache" not in chat(gw, k, content="cache me", stream=True).headers  # streams bypass the cache
    gw.post("/v1/chat/completions", headers={"Authorization": f"Bearer {k['key']}", "Cache-Control": "no-store"},
            json={"model": "m", "messages": [{"role": "user", "content": "never stored"}]})
    assert chat(gw, k, content="never stored").headers["x-llmgate-cache"] == "miss"

    stats = admin(gw, "GET", "/cache")
    assert stats["hits"] >= 2 and stats["entries"] >= 2 and stats["ttl_s"] == 60
    assert admin(gw, "POST", "/cache/flush")["flushed"] >= 2
    assert chat(gw, k, content="cache me").headers["x-llmgate-cache"] == "miss"


def test_cache_hits_do_not_consume_budget(gw_factory, mock_factory):
    gw = gw_factory(cache_ttl_s=60)
    p = provider(gw, "p", mock_factory())
    model(gw, "m", [{"provider_id": p["id"]}], input_price_per_m=100, output_price_per_m=100)  # ~$0.004 per call
    k = key(gw, monthly_budget_usd=0.01)
    assert chat(gw, k, content="same").status_code == 200
    spent = admin(gw, "GET", "/services")[0]["keys"][0]["usage_month"]["cost_usd"]
    for _ in range(5):
        assert chat(gw, k, content="same").headers["x-llmgate-cache"] == "hit"
    assert admin(gw, "GET", "/services")[0]["keys"][0]["usage_month"]["cost_usd"] == spent


def test_cache_is_opt_in_per_request_when_globally_off(gw, mock_factory):
    srv = mock_factory()
    p = provider(gw, "p", srv)
    model(gw, "m", [{"provider_id": p["id"]}])
    k = key(gw)
    assert "x-llmgate-cache" not in chat(gw, k).headers
    assert chat(gw, k, cache={"ttl": 30}).headers["x-llmgate-cache"] == "miss"
    assert chat(gw, k, cache={"ttl": 30}).headers["x-llmgate-cache"] == "hit"
    assert chat(gw, k, cache="yes").status_code == 400
