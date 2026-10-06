"""Turn an upstream response into the client response while accounting usage (JSON and SSE)."""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, AsyncIterator

import httpx
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .db import Database
from .errors import GatewayError
from .guardrails import check_output
from .health import mark_passive
from .tokens import estimate_completion_tokens, estimate_prompt_tokens, estimate_tokens
from .usageutil import StreamUsage, event_text, find_usage, norm_usage

if TYPE_CHECKING:
    from .proxy import Attempt

_OPENAI_SHAPE = {"chat", "completions"}
_NORMALISED = _OPENAI_SHAPE | {"embeddings"}  # responses that always get OpenAI-style usage fields


def _finish_reason(body: dict[str, Any]) -> str | None:
    for choice in body.get("choices") or []:
        if isinstance(choice, dict) and choice.get("finish_reason"):
            return str(choice["finish_reason"])
    delta = body.get("delta")
    candidates = [delta.get("stop_reason") if isinstance(delta, dict) else None, body.get("stop_reason"), body.get("status")]
    return next((c for c in candidates if isinstance(c, str) and c), None)


def _native_usage(att: "Attempt", prompt: int, completion: int) -> dict[str, Any]:
    if att.endpoint == "rerank":
        return {"total_tokens": prompt}
    return {"input_tokens": prompt, "output_tokens": completion}  # Responses API / Anthropic dialect


def finish_json(att: "Attempt", upstream: httpx.Response, body: bytes) -> Response:
    try:
        data = json.loads(body)
    except ValueError:
        att.log(status=502, error="upstream returned invalid JSON")
        raise GatewayError(502, "Upstream returned invalid JSON.", type="upstream_error", code="bad_upstream_response",
                           headers={"x-request-id": att.request_id}) from None

    usage = find_usage(data)
    prompt, completion = norm_usage(usage)
    estimated = not (prompt or completion)
    if estimated:
        prompt = estimate_prompt_tokens(att.endpoint, att.payload)
        completion = estimate_completion_tokens(att.endpoint, data) if isinstance(data, dict) else 0
    if att.rules and isinstance(data, dict):  # output guardrails (complete responses only)
        try:
            data, hits = check_output(att.rules, data)
        except GatewayError as exc:
            if getattr(exc, "guardrail", None):
                att.guardrail_hits.append(exc.guardrail)
            att.log(prompt_tokens=prompt, completion_tokens=completion, estimated=estimated, status=exc.status_code,
                    error=f"{exc.code}: {exc.message}")
            exc.headers["x-request-id"] = att.request_id
            raise
        att.guardrail_hits += [h for h in hits if h not in att.guardrail_hits]
    if isinstance(data, dict):
        if "model" in data:
            data["model"] = att.model_name  # clients see the public name, not the engine's
        if att.endpoint in _OPENAI_SHAPE:
            data["id"] = att.request_id  # like OpenRouter's gen-… ids: look the request up via /api/v1/generation
        if att.endpoint in _NORMALISED:
            data["usage"] = {**(usage or {}), "prompt_tokens": prompt, "completion_tokens": completion,
                             "total_tokens": prompt + completion, "cost": round(att.cost(prompt, completion), 10)}
        else:  # keep the dialect's own field names, just add the cost
            data["usage"] = {**(usage if usage and not estimated else _native_usage(att, prompt, completion)),
                             "cost": round(att.cost(prompt, completion), 10)}
    att.log(prompt_tokens=prompt, completion_tokens=completion, estimated=estimated, status=upstream.status_code,
            finish_reason=_finish_reason(data) if isinstance(data, dict) else None)
    headers = {**att.headers(), "x-llmgate-response-cost": f"{att.cost(prompt, completion):.10f}"}
    response = JSONResponse(data, status_code=upstream.status_code, headers=headers)
    if att.cache_key and 200 <= upstream.status_code < 300:
        att.st.cache.put(att.cache_key, upstream.status_code, response.body, att.cache_ttl)
        response.headers["x-llmgate-cache"] = "miss"
    return response


def _rewrite_event(att: "Attempt", obj: dict[str, Any]) -> None:
    if "model" in obj:
        obj["model"] = att.model_name
    for nested in ("response", "message"):
        if isinstance(obj.get(nested), dict) and "model" in obj[nested]:
            obj[nested]["model"] = att.model_name
    if att.endpoint in _OPENAI_SHAPE and "id" in obj:
        obj["id"] = att.request_id


def stream_response(att: "Attempt", upstream: httpx.Response, db: Database) -> StreamingResponse:
    client_wants_usage = bool((att.payload.get("stream_options") or {}).get("include_usage"))
    hide_usage_chunk = att.endpoint in _OPENAI_SHAPE and not client_wants_usage  # we injected include_usage ourselves

    async def events() -> AsyncIterator[bytes]:
        usage = StreamUsage()
        text_parts: list[str] = []
        ttft_ms: float | None = None
        finish: str | None = None
        error: str | None = None
        status = upstream.status_code
        skip_blank = False
        try:
            async for line in upstream.aiter_lines():
                if skip_blank:
                    skip_blank = False
                    if line == "":
                        continue
                if line.startswith("data:"):
                    data = line[5:].strip()
                    obj = None
                    if data and data != "[DONE]":
                        try:
                            obj = json.loads(data)
                        except ValueError:
                            obj = None
                    if isinstance(obj, dict):
                        if ttft_ms is None:
                            ttft_ms = att.elapsed_ms()
                        chunk_usage = find_usage(obj)
                        usage.add(chunk_usage)
                        if (text := event_text(obj, att.endpoint)):
                            text_parts.append(text)
                        finish = _finish_reason(obj) or finish
                        if chunk_usage is not None and not obj.get("choices") and hide_usage_chunk:
                            skip_blank = True  # the usage-only chunk was ours; the client never asked for it
                            continue
                        _rewrite_event(att, obj)
                        if chunk_usage is not None and att.endpoint in _OPENAI_SHAPE:
                            p, c = norm_usage(chunk_usage)
                            chunk_usage["cost"] = round(att.cost(p, c), 10)
                        line = "data: " + json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
                yield (line + "\n").encode("utf-8")
        except asyncio.CancelledError:
            error = "client disconnected"
            raise
        except httpx.HTTPError as exc:
            error = f"upstream stream failed: {type(exc).__name__}"
            status = 502
            mark_passive(db, att.route["provider_id"], False, error)  # type: ignore[index]
            err = {"error": {"message": error, "type": "upstream_error", "code": "stream_interrupted"}}
            yield f"data: {json.dumps(err)}\n\n".encode()
        finally:
            await asyncio.shield(upstream.aclose())
            if usage.seen:
                prompt, completion, estimated = usage.prompt, usage.completion, False
            else:
                prompt, completion = estimate_prompt_tokens(att.endpoint, att.payload), estimate_tokens("".join(text_parts))
                estimated = True
            att.log(prompt_tokens=prompt, completion_tokens=completion, estimated=estimated, status=status,
                    error=error, ttft_ms=ttft_ms, finish_reason=finish)

    return StreamingResponse(events(), status_code=upstream.status_code, media_type="text/event-stream",
                             headers={**att.headers(), "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
