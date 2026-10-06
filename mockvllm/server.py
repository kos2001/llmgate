"""A mock vLLM server speaking the OpenAI-compatible protocol.

It reproduces the parts of ``vllm serve`` that a gateway cares about:

* ``GET  /health``                 liveness probe
* ``GET  /v1/models``              served models (with ``max_model_len``)
* ``POST /v1/chat/completions``    streaming + non-streaming, ``usage`` / ``stream_options.include_usage``
* ``POST /v1/completions``
* ``POST /v1/embeddings``
* ``GET  /metrics``                Prometheus text with the ``vllm:*`` gauges/counters

Replies are deterministic (an echo of the prompt), token counts are computed from a
simple regex tokenizer, and a few request headers inject faults so failover can be tested:

* ``x-mock-status: 503``   answer with that HTTP status
* ``x-mock-delay: 0.5``    sleep before answering (seconds)
* ``x-mock-no-usage: 1``   omit the ``usage`` block (like an engine that doesn't report it)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import re
import time
import uuid
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

_TOKEN_RE = re.compile(r"\w+|\s+|[^\w\s]", re.UNICODE)
_HANGUL_RE = re.compile(r"[가-힣]")


# asyncio treats timers shorter than the clock resolution (~15.6 ms on Windows) as already expired,
# so per-token delays below that would silently collapse to zero. Clamp to keep streaming paced.
_MIN_NAP_S = 0.016


async def nap(seconds: float) -> None:
    if seconds > 0:
        await asyncio.sleep(max(seconds, _MIN_NAP_S))


def tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


class MockState:
    def __init__(self, models: list[str], token_delay: float, error_rate: float,
                 api_key: str | None, max_model_len: int):
        self.models = models
        self.token_delay = token_delay
        self.error_rate = error_rate
        self.api_key = api_key
        self.max_model_len = max_model_len
        self.started = int(time.time())
        self.running = 0
        self.waiting = 0
        self.prompt_tokens_total = 0
        self.generation_tokens_total = 0
        self.success_total = 0
        self.request_count = 0
        self.fail_next = 0  # the next N inference requests answer 503 (tests set this)
        self.last_payload: dict[str, Any] | None = None  # what the engine most recently received


def make_reply(model: str, prompt: str) -> str:
    snippet = " ".join(prompt.split())[:160]
    if _HANGUL_RE.search(prompt):
        return f"모의 vLLM 서버({model})의 응답입니다. 입력하신 내용: {snippet}"
    return f"This is a mock response from {model}. You said: {snippet}"


def create_app(
    models: list[str] | None = None,
    token_delay: float = 0.0,
    error_rate: float = 0.0,
    api_key: str | None = None,
    max_model_len: int = 8192,
) -> FastAPI:
    state = MockState(models or ["mock-llama-3-8b-instruct"], token_delay, error_rate, api_key, max_model_len)
    app = FastAPI(title="mock-vllm")
    app.state.mock = state

    def error(status: int, message: str, code: str | None = None) -> JSONResponse:
        return JSONResponse(
            {"object": "error", "message": message, "type": "BadRequestError" if status < 500 else "InternalServerError",
             "param": None, "code": status},
            status_code=status,
        )

    async def preflight(request: Request) -> JSONResponse | None:
        """Auth, fault injection, unknown-model check shared by every inference route."""
        if state.api_key and request.headers.get("authorization") != f"Bearer {state.api_key}":
            return error(401, "Unauthorized")
        delay = request.headers.get("x-mock-delay")
        if delay:
            await asyncio.sleep(float(delay))
        if state.fail_next > 0:
            state.fail_next -= 1
            return error(503, "injected one-off failure")
        forced = request.headers.get("x-mock-status")
        if forced:
            return error(int(forced), f"injected failure {forced}")
        if state.error_rate and random.random() < state.error_rate:
            return error(503, "random injected failure")
        return None

    def check_model(name: str | None) -> JSONResponse | None:
        if name not in state.models:
            return error(404, f"The model `{name}` does not exist.")
        return None

    @app.get("/health")
    async def health() -> PlainTextResponse:
        return PlainTextResponse("")

    @app.get("/version")
    async def version() -> dict[str, str]:
        return {"version": "0.0.0-mock"}

    @app.get("/v1/models")
    async def list_models() -> dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {
                    "id": m, "object": "model", "created": state.started, "owned_by": "vllm",
                    "root": m, "parent": None, "max_model_len": state.max_model_len,
                    "permission": [],
                }
                for m in state.models
            ],
        }

    @app.get("/metrics")
    async def metrics() -> PlainTextResponse:
        lines: list[str] = []
        for model in state.models:
            lbl = f'{{model_name="{model}"}}'
            kv = min(0.95, 0.05 + 0.1 * state.running)
            lines += [
                f"vllm:num_requests_running{lbl} {state.running}",
                f"vllm:num_requests_waiting{lbl} {state.waiting}",
                f"vllm:gpu_cache_usage_perc{lbl} {kv:.4f}",
                f"vllm:prompt_tokens_total{lbl} {state.prompt_tokens_total}",
                f"vllm:generation_tokens_total{lbl} {state.generation_tokens_total}",
                f'vllm:request_success_total{{finished_reason="stop",model_name="{model}"}} {state.success_total}',
            ]
        return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    # ------------------------------------------------------------------ inference

    def build_text_response(endpoint: str, model: str, text: str, prompt_tokens: int,
                            completion_tokens: int, finish: str, include_usage: bool) -> dict[str, Any]:
        rid = f"{'chatcmpl' if endpoint == 'chat' else 'cmpl'}-{uuid.uuid4().hex[:24]}"
        if endpoint == "chat":
            choice = {"index": 0, "message": {"role": "assistant", "content": text},
                      "logprobs": None, "finish_reason": finish}
            obj = "chat.completion"
        else:
            choice = {"index": 0, "text": text, "logprobs": None, "finish_reason": finish}
            obj = "text_completion"
        body: dict[str, Any] = {"id": rid, "object": obj, "created": int(time.time()), "model": model,
                                "choices": [choice]}
        if include_usage:
            body["usage"] = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                             "total_tokens": prompt_tokens + completion_tokens}
        return body

    async def stream_events(endpoint: str, model: str, pieces: list[str], prompt_tokens: int,
                            finish: str, include_usage: bool) -> AsyncIterator[str]:
        rid = f"{'chatcmpl' if endpoint == 'chat' else 'cmpl'}-{uuid.uuid4().hex[:24]}"
        created = int(time.time())
        obj = "chat.completion.chunk" if endpoint == "chat" else "text_completion"

        def event(choices: list[dict[str, Any]], usage: dict[str, int] | None = None) -> str:
            chunk: dict[str, Any] = {"id": rid, "object": obj, "created": created, "model": model, "choices": choices}
            if usage is not None:
                chunk["usage"] = usage
            return f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"

        state.running += 1
        try:
            await nap(state.token_delay)  # "prefill": a real engine's first chunk arrives after the prompt is processed
            if endpoint == "chat":
                yield event([{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}])
            for piece in pieces:
                await nap(state.token_delay)
                if endpoint == "chat":
                    yield event([{"index": 0, "delta": {"content": piece}, "finish_reason": None}])
                else:
                    yield event([{"index": 0, "text": piece, "finish_reason": None}])
            if endpoint == "chat":
                yield event([{"index": 0, "delta": {}, "finish_reason": finish}])
            else:
                yield event([{"index": 0, "text": "", "finish_reason": finish}])
            completion_tokens = len(pieces)
            if include_usage:
                yield event([], {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                                 "total_tokens": prompt_tokens + completion_tokens})
            yield "data: [DONE]\n\n"
            state.prompt_tokens_total += prompt_tokens
            state.generation_tokens_total += completion_tokens
            state.success_total += 1
        finally:
            state.running -= 1

    async def handle_generation(request: Request, endpoint: str) -> Any:
        if (resp := await preflight(request)) is not None:
            return resp
        try:
            payload = await request.json()
        except Exception:
            return error(400, "Invalid JSON body")
        model = payload.get("model")
        if (resp := check_model(model)) is not None:
            return resp
        state.last_payload = payload

        if endpoint == "chat":
            messages = payload.get("messages")
            if not isinstance(messages, list) or not messages:
                return error(400, "'messages' is required")
            prompt_text = " ".join(_content_text(m.get("content")) for m in messages if isinstance(m, dict))
            last_user = next((_content_text(m.get("content")) for m in reversed(messages)
                              if isinstance(m, dict) and m.get("role") == "user"), prompt_text)
            prompt_tokens = sum(4 + len(tokenize(_content_text(m.get("content")))) for m in messages
                                if isinstance(m, dict)) + 3
        else:
            raw = payload.get("prompt", "")
            prompt_text = raw if isinstance(raw, str) else " ".join(str(x) for x in raw)
            last_user = prompt_text
            prompt_tokens = len(tokenize(prompt_text))

        if prompt_tokens > state.max_model_len:
            return error(400, f"This model's maximum context length is {state.max_model_len} tokens. However, you requested "
                              f"{prompt_tokens} tokens in the messages. Please reduce the length of the messages.")
        max_tokens = payload.get("max_tokens") or payload.get("max_completion_tokens") or 256
        pieces = tokenize(make_reply(model, last_user))
        finish = "stop"
        if len(pieces) > max_tokens:
            pieces, finish = pieces[:max_tokens], "length"
        text = "".join(pieces)
        no_usage = request.headers.get("x-mock-no-usage") == "1"
        state.request_count += 1

        if payload.get("stream"):
            include_usage = bool((payload.get("stream_options") or {}).get("include_usage")) and not no_usage
            return StreamingResponse(
                stream_events(endpoint, model, pieces, prompt_tokens, finish, include_usage),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache"},
            )

        state.running += 1
        try:
            if state.token_delay:
                await asyncio.sleep(state.token_delay * len(pieces))
        finally:
            state.running -= 1
        state.prompt_tokens_total += prompt_tokens
        state.generation_tokens_total += len(pieces)
        state.success_total += 1
        return build_text_response(endpoint, model, text, prompt_tokens, len(pieces), finish, not no_usage)

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request) -> Any:
        return await handle_generation(request, "chat")

    @app.post("/v1/completions")
    async def completions(request: Request) -> Any:
        return await handle_generation(request, "completions")

    @app.post("/v1/embeddings")
    async def embeddings(request: Request) -> Any:
        if (resp := await preflight(request)) is not None:
            return resp
        try:
            payload = await request.json()
        except Exception:
            return error(400, "Invalid JSON body")
        if (resp := check_model(payload.get("model"))) is not None:
            return resp
        raw = payload.get("input", "")
        inputs = [raw] if isinstance(raw, str) else [str(x) for x in raw]
        data = []
        total = 0
        for i, text in enumerate(inputs):
            total += len(tokenize(text))
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            vec = [round((b - 127.5) / 127.5, 6) for b in digest[:16]]
            data.append({"object": "embedding", "index": i, "embedding": vec})
        state.prompt_tokens_total += total
        body: dict[str, Any] = {"object": "list", "data": data, "model": payload["model"]}
        if request.headers.get("x-mock-no-usage") != "1":
            body["usage"] = {"prompt_tokens": total, "total_tokens": total}
        return body

    # ------------------------------------------------------- Responses API, Anthropic Messages, rerank

    async def begin(request: Request) -> tuple[dict[str, Any] | None, JSONResponse | None]:
        if (resp := await preflight(request)) is not None:
            return None, resp
        try:
            payload = await request.json()
        except Exception:
            return None, error(400, "Invalid JSON body")
        if (resp := check_model(payload.get("model"))) is not None:
            return None, resp
        state.request_count += 1
        state.last_payload = payload
        return payload, None

    def sse(event: str, data: dict[str, Any]) -> str:
        return "event: " + event + "\ndata: " + json.dumps(data, ensure_ascii=False) + "\n\n"

    @app.post("/v1/responses")
    async def responses(request: Request) -> Any:
        payload, err = await begin(request)
        if err is not None or payload is None:
            return err
        raw = payload.get("input", "")
        prompt = raw if isinstance(raw, str) else " ".join(_content_text(i.get("content")) for i in raw if isinstance(i, dict))
        pieces, pt = tokenize(make_reply(payload["model"], prompt)), len(tokenize(prompt))
        usage = {"input_tokens": pt, "output_tokens": len(pieces), "total_tokens": pt + len(pieces)}
        rid = f"resp_{uuid.uuid4().hex[:20]}"
        body = {"id": rid, "object": "response", "created_at": int(time.time()), "status": "completed", "model": payload["model"],
                "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "".join(pieces)}]}]}
        if request.headers.get("x-mock-no-usage") != "1":
            body["usage"] = usage
        state.prompt_tokens_total += pt
        state.generation_tokens_total += len(pieces)
        if not payload.get("stream"):
            return body

        async def gen() -> AsyncIterator[str]:
            yield sse("response.created", {"type": "response.created", "response": {"id": rid, "model": payload["model"], "status": "in_progress"}})
            for piece in pieces:
                await nap(state.token_delay)
                yield sse("response.output_text.delta", {"type": "response.output_text.delta", "delta": piece})
            yield sse("response.completed", {"type": "response.completed", "response": body})
        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.post("/v1/messages")
    async def messages(request: Request) -> Any:
        payload, err = await begin(request)
        if err is not None or payload is None:
            return err
        msgs = [m for m in payload.get("messages", []) if isinstance(m, dict)]
        prompt = " ".join(_content_text(m.get("content")) for m in msgs)
        pieces, pt = tokenize(make_reply(payload["model"], prompt)), len(tokenize(prompt)) + 3
        mid = f"msg_{uuid.uuid4().hex[:20]}"
        base = {"id": mid, "type": "message", "role": "assistant", "model": payload["model"]}
        state.prompt_tokens_total += pt
        state.generation_tokens_total += len(pieces)
        if not payload.get("stream"):
            body = {**base, "content": [{"type": "text", "text": "".join(pieces)}], "stop_reason": "end_turn"}
            if request.headers.get("x-mock-no-usage") != "1":
                body["usage"] = {"input_tokens": pt, "output_tokens": len(pieces)}
            return body

        async def gen() -> AsyncIterator[str]:
            yield sse("message_start", {"type": "message_start", "message": {**base, "content": [], "usage": {"input_tokens": pt, "output_tokens": 1}}})
            for piece in pieces:
                await nap(state.token_delay)
                yield sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": piece}})
            yield sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": len(pieces)}})
            yield sse("message_stop", {"type": "message_stop"})
        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.post("/v1/rerank")
    async def rerank(request: Request) -> Any:
        payload, err = await begin(request)
        if err is not None or payload is None:
            return err
        query = set(tokenize(str(payload.get("query", "")).lower()))
        docs = [d if isinstance(d, str) else str(d.get("text", "")) for d in payload.get("documents", [])]
        scored = sorted(((len(query & set(tokenize(d.lower()))) / (len(query) or 1), i, d) for i, d in enumerate(docs)), reverse=True)
        total = len(tokenize(str(payload.get("query", "")))) + sum(len(tokenize(d)) for d in docs)
        results = [{"index": i, "document": {"text": d}, "relevance_score": round(sc, 4)} for sc, i, d in scored[: payload.get("top_n") or len(docs)]]
        body: dict[str, Any] = {"id": f"rerank-{uuid.uuid4().hex[:12]}", "model": payload["model"], "results": results}
        if request.headers.get("x-mock-no-usage") != "1":
            body["usage"] = {"total_tokens": total}
        return body

    return app


def run() -> None:
    import uvicorn

    p = argparse.ArgumentParser(description="Mock vLLM (OpenAI-compatible) server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--model", action="append", dest="models", help="served model name (repeatable)")
    p.add_argument("--token-delay", type=float, default=0.02, help="seconds per generated token")
    p.add_argument("--error-rate", type=float, default=0.0, help="probability of a random 503")
    p.add_argument("--api-key", default=None, help="require this bearer token")
    p.add_argument("--max-model-len", type=int, default=8192)
    args = p.parse_args()
    app = create_app(args.models, args.token_delay, args.error_rate, args.api_key, args.max_model_len)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    run()
