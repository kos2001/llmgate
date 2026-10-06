from __future__ import annotations

import pytest

from conftest import admin


def provider(gw, name, mock, **extra):
    return admin(gw, "POST", "/providers", json={"name": name, "base_url": mock.base_url, **extra})


def model(gw, name, routes, **extra):
    body = {"name": name, "input_price_per_m": 1.0, "output_price_per_m": 2.0, **extra,
            "routes": [{"upstream_model": "mock-chat", **r} for r in routes]}
    return admin(gw, "POST", "/models", json=body)


def key(gw, service="svc", **opts):
    try:
        svc = admin(gw, "POST", "/services", json={"name": service})
    except AssertionError:
        svc = next(s for s in admin(gw, "GET", "/services") if s["name"] == service)
    return admin(gw, "POST", f"/services/{svc['id']}/keys", json={"name": "k", **opts})


def chat(gw, k, model_name="m", content="hello", **extra):
    body = {"model": model_name, "messages": [{"role": "user", "content": content}], **extra}
    return gw.post("/v1/chat/completions", headers={"Authorization": f"Bearer {k['key']}"}, json=body)


@pytest.fixture
def two(gw, mock_factory):
    """Two healthy providers serving model 'm' in the same priority tier."""
    a, b = mock_factory(), mock_factory()
    pa, pb = provider(gw, "alpha", a), provider(gw, "beta", b)
    model(gw, "m", [{"provider_id": pa["id"]}, {"provider_id": pb["id"]}])
    return gw, key(gw), (a, b), (pa, pb)


def test_order_only_ignore(two):
    gw, k, _, _ = two
    for _ in range(4):
        assert chat(gw, k, provider={"order": ["beta"]}).headers["x-llmgate-provider"] == "beta"
        assert chat(gw, k, provider={"only": ["alpha"]}).headers["x-llmgate-provider"] == "alpha"
        assert chat(gw, k, provider={"ignore": ["alpha"]}).headers["x-llmgate-provider"] == "beta"
    r = chat(gw, k, provider={"only": ["nobody"]})
    assert r.status_code == 404 and r.json()["error"]["code"] == "no_matching_provider"


def test_gateway_fields_are_not_forwarded_upstream(two):
    gw, k, mocks, _ = two
    r = chat(gw, k, provider={"order": ["alpha"]}, models=["m"], transforms=["middle-out"], plugins=[], user="u-1", temperature=0.2)
    assert r.status_code == 200
    sent = mocks[0].app.state.mock.last_payload
    assert sent["model"] == "mock-chat" and sent["user"] == "u-1" and sent["temperature"] == 0.2
    assert not {"provider", "models", "transforms", "plugins", "route"} & sent.keys()


def test_allow_fallbacks_false_stops_at_first_provider(gw, mock_factory):
    bad, good = mock_factory(error_rate=1.0), mock_factory()
    pa, pb = provider(gw, "alpha", bad), provider(gw, "beta", good)
    model(gw, "m", [{"provider_id": pa["id"], "priority": 0}, {"provider_id": pb["id"], "priority": 1}])
    k = key(gw)
    assert chat(gw, k).headers["x-llmgate-provider"] == "beta"  # default: fail over
    admin(gw, "POST", f"/providers/{pa['id']}/check")  # alpha is marked unhealthy → ordered last, so pin it with `order`
    r = chat(gw, k, provider={"order": ["alpha"], "allow_fallbacks": False})
    assert r.status_code == 502 and good.app.state.mock.request_count == 1  # beta never got the second request


def test_sort_price_uses_per_provider_prices_and_bills_them(gw, mock_factory):
    a, b = mock_factory(), mock_factory()
    pa, pb = provider(gw, "alpha", a), provider(gw, "beta", b)
    model(gw, "m", [{"provider_id": pa["id"], "input_price_per_m": 10, "output_price_per_m": 10},
                    {"provider_id": pb["id"], "input_price_per_m": 1, "output_price_per_m": 1}])
    k = key(gw)
    for variant in ({"model": "m:floor"}, {"provider": {"sort": "price"}}):
        r = chat(gw, k, **({"model_name": "m:floor"} if "model" in variant else variant))
        assert r.headers["x-llmgate-provider"] == "beta", variant
    u = r.json()["usage"]
    assert u["cost"] == round((u["prompt_tokens"] + u["completion_tokens"]) * 1 / 1e6, 10)  # beta's price, not the model's
    r = chat(gw, k, provider={"order": ["alpha"]})
    assert r.json()["usage"]["cost"] == round((r.json()["usage"]["total_tokens"]) * 10 / 1e6, 10)


def test_sort_throughput_prefers_the_faster_provider(gw, mock_factory):
    slow, fast = mock_factory(token_delay=0.03), mock_factory(token_delay=0.0)
    pa, pb = provider(gw, "slow", slow), provider(gw, "fast", fast)
    model(gw, "m", [{"provider_id": pa["id"]}, {"provider_id": pb["id"]}])
    k = key(gw)
    for name in ("slow", "fast"):  # one measured sample per provider
        assert chat(gw, k, provider={"order": [name]}, stream=True).status_code == 200
    for _ in range(3):
        assert chat(gw, k, model_name="m:nitro").headers["x-llmgate-provider"] == "fast"
        assert chat(gw, k, provider={"sort": "latency"}).headers["x-llmgate-provider"] == "fast"


def test_data_collection_quantization_max_price(gw, mock_factory):
    a, b = mock_factory(), mock_factory()
    pa = provider(gw, "logs-prompts", a, data_collection="allow", quantization="fp8")
    pb = provider(gw, "private", b, data_collection="deny", quantization="bf16")
    model(gw, "m", [{"provider_id": pa["id"], "input_price_per_m": 0.1, "output_price_per_m": 0.1},
                    {"provider_id": pb["id"], "input_price_per_m": 5, "output_price_per_m": 5}])
    k = key(gw)
    assert chat(gw, k, provider={"data_collection": "deny"}).headers["x-llmgate-provider"] == "private"
    assert chat(gw, k, provider={"quantizations": ["fp8"]}).headers["x-llmgate-provider"] == "logs-prompts"
    assert chat(gw, k, provider={"max_price": {"prompt": 1, "completion": 1}}).headers["x-llmgate-provider"] == "logs-prompts"
    assert chat(gw, k, provider={"max_price": {"prompt": 0.01}}).status_code == 404


@pytest.mark.parametrize("bad", [{"sort": "vibes"}, {"only": "alpha"}, {"data_collection": "maybe"}, {"max_price": {"prompt": "x"}}, "alpha"])
def test_invalid_provider_preferences_are_rejected(two, bad):
    gw, k, _, _ = two
    r = chat(gw, k, provider=bad)
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_provider_preferences"


def test_key_allowed_providers_is_a_guardrail(gw, mock_factory):
    a, b = mock_factory(), mock_factory()
    pa, pb = provider(gw, "alpha", a), provider(gw, "beta", b)
    model(gw, "m", [{"provider_id": pa["id"]}, {"provider_id": pb["id"]}])
    k = key(gw, allowed_providers=["beta"])
    assert {chat(gw, k).headers["x-llmgate-provider"] for _ in range(4)} == {"beta"}
    assert chat(gw, k, provider={"only": ["alpha"]}).status_code == 404  # a request cannot widen the key's guardrail


def test_free_variant(gw, mock_factory):
    a = mock_factory()
    pa = provider(gw, "alpha", a)
    model(gw, "paid", [{"provider_id": pa["id"]}])
    model(gw, "gratis", [{"provider_id": pa["id"]}], input_price_per_m=0, output_price_per_m=0)
    k = key(gw)
    assert chat(gw, k, model_name="gratis:free").status_code == 200
    r = chat(gw, k, model_name="paid:free")
    assert r.status_code == 404 and r.json()["error"]["code"] == "no_matching_provider"


# ------------------------------------------------------------------------------- model fallbacks

def test_models_array_falls_back_across_models(gw, mock_factory):
    down, up = mock_factory(error_rate=1.0), mock_factory()
    model(gw, "primary", [{"provider_id": provider(gw, "down", down)["id"]}])
    model(gw, "backup", [{"provider_id": provider(gw, "up", up)["id"]}])
    k = key(gw)
    r = chat(gw, k, model_name="primary", models=["backup"])
    assert r.status_code == 200 and r.json()["model"] == "backup" and r.headers["x-llmgate-model"] == "backup"
    log = admin(gw, "GET", "/usage/logs")["data"][0]
    assert log["model"] == "backup" and log["requested_model"] == "primary" and log["attempts"] == 2

    r = gw.post("/v1/chat/completions", headers={"Authorization": f"Bearer {k['key']}"},  # no primary at all
                json={"models": ["ghost", "backup"], "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200 and r.json()["model"] == "backup"
    assert chat(gw, k, model_name="primary").status_code == 502  # without fallbacks it still fails


def test_fallback_on_upstream_client_error_but_not_for_a_single_model(gw, mock_factory):
    m = mock_factory()
    p = provider(gw, "p", m)
    model(gw, "broken", [{"provider_id": p["id"], "upstream_model": "not-served"}])  # engine answers 404
    model(gw, "fine", [{"provider_id": p["id"]}])
    k = key(gw)
    assert chat(gw, k, model_name="broken").status_code == 404
    assert chat(gw, k, model_name="broken", models=["fine"]).json()["model"] == "fine"


# ------------------------------------------------------------------------------- auto router

def test_auto_router_matches_cost_to_difficulty(gw, mock_factory):
    m = mock_factory()
    p = provider(gw, "p", m)
    model(gw, "small", [{"provider_id": p["id"]}], input_price_per_m=0.05, output_price_per_m=0.05)
    model(gw, "large", [{"provider_id": p["id"]}], input_price_per_m=2, output_price_per_m=4)
    k = key(gw)
    easy = chat(gw, k, model_name="llmgate/auto")
    assert easy.json()["model"] == "small"
    hard = gw.post("/v1/chat/completions", headers={"Authorization": f"Bearer {k['key']}"},
                   json={"model": "llmgate/auto", "messages": [{"role": "user", "content": "Refactor this:\n```python\ndef f(x): return x\n```"}]})
    assert hard.json()["model"] == "large"
    narrowed = chat(gw, k, model_name="llmgate/auto", plugins=[{"id": "auto-router", "allowed_models": ["sma*"]}])
    assert narrowed.json()["model"] == "small"
    only_small = key(gw, service="s2", allowed_models=["small"])
    assert gw.post("/v1/chat/completions", headers={"Authorization": f"Bearer {only_small['key']}"},
                   json={"model": "llmgate/auto", "messages": [{"role": "user", "content": "```code```"}]}).json()["model"] == "small"
