from __future__ import annotations

import json
import time

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from conftest import ADMIN, MockServer, admin, bearer, sso_login
from llmgate.config import Settings
from llmgate.main import create_app
from test_routing_prefs import chat, key, model, provider


@pytest.fixture
def stack(gw, mock_factory):
    mock = mock_factory(["mock-chat", "mock-rerank"])
    p = provider(gw, "alpha", mock)
    model(gw, "m", [{"provider_id": p["id"]}])
    return gw, key(gw), mock, p


def auth(k):
    return {"Authorization": f"Bearer {k['key']}"}


def events(text: str) -> list[dict]:
    return [json.loads(l[5:]) for l in text.splitlines() if l.startswith("data:") and "[DONE]" not in l]


# ----------------------------------------------------------------------------- ids and attribution

def test_generation_id_and_lookup(stack):
    gw, k, _, _ = stack
    r = chat(gw, k)
    gen_id = r.json()["id"]
    assert gen_id.startswith("gen-") and r.headers["x-request-id"] == gen_id
    for base in ("/v1", "/api/v1"):
        g = gw.get(f"{base}/generation", params={"id": gen_id}, headers=auth(k)).json()["data"]
        u = r.json()["usage"]
        assert (g["tokens_prompt"], g["tokens_completion"]) == (u["prompt_tokens"], u["completion_tokens"])
        assert g["total_cost"] == u["cost"] and g["model"] == "m" and g["provider_name"] == "alpha" and not g["streamed"]

    s = chat(gw, k, stream=True)
    ids = {c["id"] for c in events(s.text)}
    assert ids == {s.headers["x-request-id"]}  # every SSE chunk carries the same gen-… id
    g = gw.get("/api/v1/generation", params={"id": s.headers["x-request-id"]}, headers=auth(k)).json()["data"]
    assert g["streamed"] and g["finish_reason"] == "stop"

    other = key(gw, service="other")  # generations are private to the service that made them
    assert gw.get("/api/v1/generation", params={"id": gen_id}, headers=auth(other)).status_code == 404


def test_app_attribution_and_end_user_tracking(stack):
    gw, k, _, _ = stack
    h = {**auth(k), "HTTP-Referer": "https://app.example.com", "X-Title": "My Cool App"}
    for user in ("u-1", "u-1", "u-2"):
        assert gw.post("/v1/chat/completions", headers=h, json={"model": "m", "user": user,
                                                                  "messages": [{"role": "user", "content": "hi"}]}).status_code == 200
    chat(gw, k)  # a request with no attribution
    by_app = {r["name"]: r for r in admin(gw, "GET", "/usage/summary", params={"group_by": "app"})}
    assert by_app["My Cool App"]["requests"] == 3 and by_app["(none)"]["requests"] == 1
    by_user = {r["name"]: r["requests"] for r in admin(gw, "GET", "/usage/summary", params={"group_by": "user"})}
    assert by_user["u-1"] == 2 and by_user["u-2"] == 1
    assert admin(gw, "GET", "/usage/logs")["data"][-1]["referer"] == "https://app.example.com"


def test_openrouter_base_path_works_for_inference(stack):
    gw, k, _, _ = stack
    r = gw.post("/api/v1/chat/completions", headers=auth(k), json={"model": "m", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 200 and gw.get("/api/v1/key", headers=auth(k)).json()["data"]["label"] == "k"


# ----------------------------------------------------------------------------- catalog

def test_catalog_metadata_filters_and_capability_checks(gw, mock_factory):
    mock = mock_factory()
    p = provider(gw, "alpha", mock)
    model(gw, "tools-vision", [{"provider_id": p["id"]}], supported_parameters=["tools", "response_format"],
          input_modalities=["text", "image"], max_completion_tokens=4096, context_length=32768)
    model(gw, "plain", [{"provider_id": p["id"]}], supported_parameters=["stop"])
    k = key(gw)
    ids = lambda q: sorted(m["id"] for m in gw.get("/api/v1/models", params=q).json()["data"])  # noqa: E731
    assert ids({}) == ["plain", "tools-vision"]
    assert ids({"supported_parameters": "tools"}) == ["tools-vision"]
    assert ids({"supported_parameters": "tools,response_format"}) == ["tools-vision"]
    assert ids({"input_modalities": "image"}) == ["tools-vision"]
    entry = next(m for m in gw.get("/api/v1/models").json()["data"] if m["id"] == "tools-vision")
    assert entry["top_provider"]["max_completion_tokens"] == 4096 and entry["architecture"]["input_modalities"] == ["text", "image"]
    assert gw.get("/api/v1/models/count").json()["data"]["count"] == 2

    tools = [{"type": "function", "function": {"name": "f", "parameters": {}}}]
    strict = chat(gw, k, model_name="plain", tools=tools, provider={"require_parameters": True})
    assert strict.status_code == 400 and strict.json()["error"]["code"] == "unsupported_parameters"
    assert chat(gw, k, model_name="plain", tools=tools).status_code == 200  # without require_parameters: passthrough
    image = [{"role": "user", "content": [{"type": "text", "text": "?"}, {"type": "image_url", "image_url": {"url": "http://x/y.png"}}]}]
    r = gw.post("/v1/chat/completions", headers=auth(k), json={"model": "plain", "messages": image})
    assert r.status_code == 400 and r.json()["error"]["code"] == "unsupported_modality"
    assert gw.post("/v1/chat/completions", headers=auth(k), json={"model": "tools-vision", "messages": image}).status_code == 200


def test_model_endpoints_report_provider_stats(gw, mock_factory):
    up, down = mock_factory(), mock_factory(error_rate=1.0)
    pa, pb = provider(gw, "alpha", up), provider(gw, "beta", down)
    model(gw, "m", [{"provider_id": pa["id"], "priority": 0, "input_price_per_m": 3}, {"provider_id": pb["id"], "priority": 1}])
    k = key(gw)
    for _ in range(3):
        chat(gw, k, provider={"order": ["alpha"]})
    chat(gw, k, provider={"order": ["beta"], "allow_fallbacks": False})  # a failure on beta
    data = gw.get("/api/v1/models/m/endpoints").json()["data"]
    by = {e["provider_name"]: e for e in data["endpoints"]}
    assert data["id"] == "m" and set(by) == {"alpha", "beta"}
    assert by["alpha"]["uptime_last_30m"] == 100.0 and by["beta"]["uptime_last_30m"] == 0.0
    assert by["alpha"]["pricing"]["prompt"] == "0.000003" and by["alpha"]["latency_last_30m"]["p50"] is not None
    assert gw.get("/api/v1/models/nope/endpoints").status_code == 404
    names = {p["name"] for p in gw.get("/api/v1/providers").json()["data"]}
    assert names == {"alpha", "beta"}


# ----------------------------------------------------------------------------- other endpoints

def test_responses_api(stack):
    gw, k, _, _ = stack
    r = gw.post("/v1/responses", headers=auth(k), json={"model": "m", "input": "hello responses"})
    assert r.status_code == 200 and r.json()["model"] == "m"
    u = r.json()["usage"]
    assert u["input_tokens"] > 0 and u["output_tokens"] > 0 and u["cost"] > 0  # native field names + gateway cost

    s = gw.post("/v1/responses", headers=auth(k), json={"model": "m", "input": "stream it", "stream": True})
    assert "response.output_text.delta" in s.text and "response.completed" in s.text
    assert all(e.get("response", {}).get("model", "m") == "m" for e in events(s.text))
    rows = admin(gw, "GET", "/usage/logs")["data"]
    assert [r["endpoint"] for r in rows] == ["responses", "responses"]
    assert all(r["usage_estimated"] == 0 and r["prompt_tokens"] > 0 and r["completion_tokens"] > 0 for r in rows)


def test_anthropic_messages_api_accounts_split_streaming_usage(stack):
    gw, k, mock, _ = stack
    body = {"model": "m", "max_tokens": 64, "messages": [{"role": "user", "content": "hello claude-style"}]}
    r = gw.post("/v1/messages", headers=auth(k), json=body)
    assert r.json()["content"][0]["type"] == "text" and r.json()["usage"]["input_tokens"] > 0
    s = gw.post("/v1/messages", headers=auth(k), json={**body, "stream": True})
    assert "message_start" in s.text and "message_stop" in s.text
    start = next(e for e in events(s.text) if e["type"] == "message_start")
    assert start["message"]["model"] == "m"
    streamed, plain = admin(gw, "GET", "/usage/logs")["data"]
    assert streamed["usage_estimated"] == 0 and streamed["stream"] == 1
    # input_tokens arrive in message_start, output_tokens in message_delta: both must be counted
    assert (streamed["prompt_tokens"], streamed["completion_tokens"]) == (plain["prompt_tokens"], plain["completion_tokens"])
    assert streamed["finish_reason"] == "end_turn"


def test_rerank(gw, mock_factory):
    mock = mock_factory(["mock-rerank"])
    p = provider(gw, "alpha", mock)
    model(gw, "ranker", [{"provider_id": p["id"], "upstream_model": "mock-rerank"}], kind="rerank", input_price_per_m=0.5)
    k = key(gw)
    r = gw.post("/v1/rerank", headers=auth(k), json={"model": "ranker", "query": "red apple", "top_n": 1,
                                                    "documents": ["green pear", "a red apple pie"]})
    assert r.status_code == 200 and r.json()["results"][0]["index"] == 1 and r.json()["usage"]["cost"] > 0
    assert "model" in r.json() and r.json()["model"] == "ranker"
    log = admin(gw, "GET", "/usage/logs")["data"][0]
    assert log["endpoint"] == "rerank" and log["prompt_tokens"] == r.json()["usage"]["total_tokens"] and log["cost_usd"] > 0
    wrong = chat(gw, k, model_name="ranker")
    assert wrong.status_code == 400 and wrong.json()["error"]["code"] == "wrong_model_kind"


def test_middle_out_trims_long_conversations(gw, mock_factory):
    mock = mock_factory()
    p = provider(gw, "alpha", mock)
    model(gw, "small-ctx", [{"provider_id": p["id"]}], context_length=400)
    k = key(gw)
    history = [{"role": "system", "content": "be brief"}] + [
        {"role": "user" if i % 2 == 0 else "assistant", "content": f"turn {i} " + "lorem ipsum " * 20} for i in range(30)]
    history.append({"role": "user", "content": "FINAL QUESTION"})
    body = {"model": "small-ctx", "messages": history, "max_tokens": 50}

    assert gw.post("/v1/chat/completions", headers=auth(k), json=body).status_code == 200
    assert len(mock.app.state.mock.last_payload["messages"]) == len(history)  # untouched by default
    assert gw.post("/v1/chat/completions", headers=auth(k), json={**body, "transforms": ["middle-out"]}).status_code == 200
    sent = mock.app.state.mock.last_payload["messages"]
    assert 2 < len(sent) < len(history) and sent[0]["role"] == "system" and sent[-1]["content"] == "FINAL QUESTION"
    bad = gw.post("/v1/chat/completions", headers=auth(k), json={**body, "transforms": ["telepathy"]})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_transforms"


# ----------------------------------------------------------------------------- account & keys

def test_credits_reflect_the_tightest_budget(gw, mock_factory):
    mock = mock_factory()
    p = provider(gw, "alpha", mock)
    model(gw, "m", [{"provider_id": p["id"]}], input_price_per_m=100, output_price_per_m=100)
    k = key(gw, monthly_budget_usd=5.0)
    unlimited = key(gw, service="free")
    chat(gw, k)
    c = gw.get("/api/v1/credits", headers=auth(k)).json()["data"]
    assert c["total_credits"] == 5.0 and 0 < c["total_usage"] < 0.01
    assert gw.get("/v1/credits", headers=auth(unlimited)).json()["data"]["total_credits"] is None


def test_provisioning_api_manages_keys(stack):
    gw, k, _, _ = stack
    assert gw.get("/api/v1/keys").status_code == 401
    created = gw.post("/api/v1/keys", headers=ADMIN, json={"name": "ci", "service": "pipelines", "limit": 2.5, "allowed_models": ["m"]})
    assert created.status_code == 201
    secret, meta = created.json()["key"], created.json()["data"]
    assert secret.startswith("sk-lg-") and meta["limit"] == 2.5 and "key" not in meta
    use = lambda: gw.post("/v1/chat/completions", headers=bearer(secret),  # noqa: E731
                          json={"model": "m", "messages": [{"role": "user", "content": "hi"}]}).status_code
    assert use() == 200
    assert any(x["hash"] == meta["hash"] and x["usage"] > 0 for x in gw.get("/api/v1/keys", headers=ADMIN).json()["data"])
    assert gw.patch(f"/api/v1/keys/{meta['hash']}", headers=ADMIN, json={"disabled": True}).json()["data"]["disabled"] is True
    assert use() == 403
    assert gw.delete(f"/api/v1/keys/{meta['hash']}", headers=ADMIN).status_code == 200
    assert use() == 401
    assert any(r["path"].startswith("/api/v1/keys") for r in admin(gw, "GET", "/audit")["data"])


def test_provisioning_api_respects_roles(sso_gw, idp):
    sso_login(sso_gw, idp, "carol")  # viewer
    assert sso_gw.get("/api/v1/keys").status_code == 200
    r = sso_gw.post("/api/v1/keys", json={"name": "x"}, headers={"x-llmgate-csrf": "1"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "insufficient_role"


# ----------------------------------------------------------------------------- observability

def test_prometheus_metrics(stack):
    gw, k, mock, p = stack
    chat(gw, k)
    chat(gw, k, stream=True)
    admin(gw, "POST", f"/providers/{p['id']}/check")
    assert gw.get("/metrics").status_code == 401
    text = gw.get("/metrics", headers=ADMIN).text
    assert 'llmgate_requests_total{model="m",provider="alpha",result="success"} 2' in text
    assert 'llmgate_tokens_total{model="m",provider="alpha",type="completion"}' in text
    assert 'llmgate_provider_up{provider="alpha",kind="vllm"} 1' in text
    assert 'llmgate_engine_requests_running{provider="alpha"}' in text and "# TYPE llmgate_cost_usd_total counter" in text


def test_usage_webhook_receives_metadata_only(mock_factory):
    received: list[dict] = []
    sink = FastAPI()

    @sink.post("/events")
    async def events_in(request: Request):
        received.append(await request.json())
        return {}

    receiver = MockServer(make_app=lambda port: sink).start()
    try:
        settings = Settings(database_path=":memory:", admin_token="test-admin", health_interval_s=0,
                            usage_webhook_url=receiver.root + "/events", _env_file=None)
        with TestClient(create_app(settings)) as gw:
            mock = mock_factory()
            p = provider(gw, "alpha", mock)
            model(gw, "m", [{"provider_id": p["id"]}])
            k = key(gw)
            assert chat(gw, k).status_code == 200
            assert gw.post("/v1/chat/completions", headers=auth(k), json={"model": "m", "messages": [{"role": "user", "content": "TOP-SECRET-PROMPT"}]}).status_code == 200
            deadline = time.time() + 5
            while len(received) < 2 and time.time() < deadline:
                time.sleep(0.05)
    finally:
        receiver.stop()
    assert len(received) == 2
    ev = received[-1]
    assert ev["type"] == "generation" and ev["model"] == "m" and ev["provider"] == "alpha" and ev["service"] == "svc"
    assert ev["total_tokens"] > 0 and ev["cost_usd"] > 0 and ev["request_id"].startswith("gen-")
    assert "TOP-SECRET-PROMPT" not in json.dumps(received)  # no prompt or completion content leaves the gateway


def test_catalog_can_be_made_private(gw_factory, mock_factory):
    gw = gw_factory(public_catalog=False)
    p = provider(gw, "alpha", mock_factory())
    model(gw, "m", [{"provider_id": p["id"]}])
    k = key(gw)
    for path in ("/api/v1/models", "/api/v1/models/count", "/api/v1/models/m/endpoints", "/api/v1/providers"):
        assert gw.get(path).status_code == 401, path
        assert gw.get(path, headers=auth(k)).status_code == 200, path
