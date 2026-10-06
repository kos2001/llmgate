from __future__ import annotations

import json

from conftest import admin, make_stack


def sse_events(text: str) -> list[str]:
    return [line[6:] for line in text.splitlines() if line.startswith("data: ")]


def test_chat_roundtrip_records_usage_and_cost(gw, mock_factory):
    mock = mock_factory()
    st = make_stack(gw, [mock.base_url], in_price=1.0, out_price=2.0)
    r = st.chat("hello world")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model"] == "chat-model"  # public name, not the upstream one
    u = body["usage"]
    assert u["total_tokens"] == u["prompt_tokens"] + u["completion_tokens"] > 0
    assert u["cost"] == round((u["prompt_tokens"] * 1 + u["completion_tokens"] * 2) / 1e6, 10)

    logs = admin(gw, "GET", "/usage/logs")["data"]
    assert len(logs) == 1
    log = logs[0]
    assert (log["prompt_tokens"], log["completion_tokens"]) == (u["prompt_tokens"], u["completion_tokens"])
    assert log["success"] == 1 and log["usage_estimated"] == 0 and log["provider_name"] == "vllm-0"
    assert log["service_name"] == "svc" and log["upstream_model"] == "mock-chat"


def test_streaming_hides_injected_usage_but_bills_it(gw, mock_factory):
    st = make_stack(gw, [mock_factory().base_url])
    r = st.chat("stream me", stream=True)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = sse_events(r.text)
    assert events[-1] == "[DONE]"
    chunks = [json.loads(e) for e in events[:-1]]
    assert all(c["model"] == "chat-model" for c in chunks)
    assert all(c["choices"] for c in chunks), "usage-only chunk must be hidden when the client did not ask"
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert "mock response" in text

    log = admin(gw, "GET", "/usage/logs")["data"][0]
    assert log["stream"] == 1 and log["usage_estimated"] == 0
    assert log["completion_tokens"] > 0 and log["ttft_ms"] is not None


def test_streaming_keeps_usage_chunk_when_client_asks(gw, mock_factory):
    st = make_stack(gw, [mock_factory().base_url])
    r = st.chat("hi", stream=True, stream_options={"include_usage": True})
    chunks = [json.loads(e) for e in sse_events(r.text)[:-1]]
    assert chunks[-1]["choices"] == [] and chunks[-1]["usage"]["total_tokens"] > 0


def test_missing_usage_is_estimated(gw, mock_factory):
    # Provider that never reports usage: gateway must still account tokens.
    mock = mock_factory()
    st = make_stack(gw, [mock.base_url])
    import httpx
    orig = httpx.AsyncClient.build_request

    def with_flag(self, *a, **kw):
        kw["headers"] = {**(kw.get("headers") or {}), "x-mock-no-usage": "1"}
        return orig(self, *a, **kw)

    httpx.AsyncClient.build_request = with_flag
    try:
        assert st.chat("안녕하세요 반갑습니다").status_code == 200
        assert st.chat("안녕하세요", stream=True).status_code == 200
    finally:
        httpx.AsyncClient.build_request = orig
    rows = admin(gw, "GET", "/usage/logs")["data"]
    assert len(rows) == 2 and all(r["usage_estimated"] == 1 and r["total_tokens"] > 0 for r in rows)


def test_failover_to_second_provider(gw, mock_factory):
    bad = mock_factory(error_rate=1.0)  # always 503
    good = mock_factory()
    st = make_stack(gw, [bad.base_url, good.base_url])
    r = st.chat("fallback please")
    assert r.status_code == 200
    assert r.headers["x-llmgate-provider"] == "vllm-1"
    log = admin(gw, "GET", "/usage/logs")["data"][0]
    assert log["attempts"] == 2 and log["provider_name"] == "vllm-1"
    providers = {p["name"]: p for p in admin(gw, "GET", "/providers")}
    assert providers["vllm-0"]["status"] == "unhealthy"


def test_all_providers_down_returns_502_and_logs_failure(gw, mock_factory):
    st = make_stack(gw, [mock_factory(error_rate=1.0).base_url])
    r = st.chat("nope")
    assert r.status_code == 502 and r.json()["error"]["code"] == "upstream_unavailable"
    log = admin(gw, "GET", "/usage/logs")["data"][0]
    assert log["success"] == 0 and log["status_code"] == 502


def test_client_errors_are_not_retried(gw, mock_factory):
    first, second = mock_factory(), mock_factory()
    st = make_stack(gw, [first.base_url, second.base_url], upstream="does-not-exist")
    r = st.chat("x")
    assert r.status_code == 404  # upstream's own answer is passed through
    assert second.app.state.mock.request_count == 0


def test_embeddings_and_kind_check(gw, mock_factory):
    mock = mock_factory(["mock-chat", "mock-embed"])
    st = make_stack(gw, [mock.base_url])
    p = admin(gw, "GET", "/providers")[0]
    admin(gw, "POST", "/models", json={"name": "embed-model", "kind": "embedding", "input_price_per_m": 0.1,
                                       "routes": [{"provider_id": p["id"], "upstream_model": "mock-embed"}]})
    r = gw.post("/v1/embeddings", json={"model": "embed-model", "input": ["a b c", "d"]}, headers=st.headers())
    assert r.status_code == 200 and len(r.json()["data"]) == 2
    assert r.json()["usage"]["prompt_tokens"] > 0
    wrong = gw.post("/v1/embeddings", json={"model": "chat-model", "input": "x"}, headers=st.headers())
    assert wrong.status_code == 400 and wrong.json()["error"]["code"] == "wrong_model_kind"


def test_legacy_completions(gw, mock_factory):
    st = make_stack(gw, [mock_factory().base_url])
    r = gw.post("/v1/completions", json={"model": "chat-model", "prompt": "once upon"}, headers=st.headers())
    assert r.status_code == 200 and r.json()["choices"][0]["text"]
