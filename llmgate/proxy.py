"""The request path: auth → limits → guardrails → cache → plan (models × providers) → forward with failover → account."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response

from . import cache as cache_mod
from . import secretbox
from . import guardrails, transforms
from .auth import AuthContext, authenticate
from .config import Settings
from .db import Database
from .errors import GatewayError
from .health import mark_passive
from .limits import check_model_budget, enforce_limits
from .routing import auto_candidates, plan, prices
from .streaming import finish_json, stream_response
from .usage import record_usage

log = logging.getLogger("llmgate.proxy")

# endpoint → (upstream path, public path, model kind)
ENDPOINTS = {
    "chat": ("/chat/completions", "/v1/chat/completions", "chat"),
    "completions": ("/completions", "/v1/completions", "chat"),
    "embeddings": ("/embeddings", "/v1/embeddings", "embedding"),
    "responses": ("/responses", "/v1/responses", "chat"),
    "messages": ("/messages", "/v1/messages", "chat"),
    "rerank": ("/rerank", "/v1/rerank", "rerank"),
}
STREAMING = {"chat", "completions", "responses", "messages"}
OPENAI_SHAPE = {"chat", "completions"}  # engines put include_usage / OpenAI ids here
GATEWAY_FIELDS = ("provider", "models", "route", "transforms", "plugins", "cache", "num_retries", "tags", "guardrails")
_CONTEXT_ERROR = re.compile(r"context|maximum.{0,30}tokens|too (?:long|many tokens)|token limit", re.I)


def _retryable(status: int) -> bool:
    return status >= 500 or status in (408, 429)


def _error_text(body: bytes) -> str:
    try:
        err = json.loads(body)
        err = err.get("error", err) if isinstance(err, dict) else err
        return str(err.get("message", err) if isinstance(err, dict) else err)[:500]
    except (ValueError, AttributeError):
        return body[:300].decode("utf-8", "replace")


def _tags(request: Request, payload: dict[str, Any]) -> list[str] | None:
    raw: Any = payload.get("tags")
    if raw is None and isinstance(payload.get("metadata"), dict):
        raw = payload["metadata"].get("tags")
    if raw is None and request.headers.get("x-llmgate-tags"):
        raw = request.headers["x-llmgate-tags"].split(",")
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(isinstance(t, str) for t in raw) or len(raw) > 20:
        raise GatewayError(400, "'tags' must be a list of up to 20 strings.", code="invalid_tags")
    return [t.strip()[:100] for t in raw if t.strip()] or None


class Attempt:
    """Everything known about one inference request; feeds the usage-log row, limits bookkeeping and webhooks."""

    def __init__(self, request: Request, request_id: str, endpoint: str, auth: AuthContext, payload: dict[str, Any]):
        self.st = request.app.state
        self.request_id, self.endpoint, self.auth, self.payload = request_id, endpoint, auth, payload
        self.db: Database = self.st.db
        self.stream = bool(payload.get("stream")) and endpoint in STREAMING
        self.started = time.perf_counter()
        self.model: dict[str, Any] | None = None
        self.model_name: str | None = payload.get("model") if isinstance(payload.get("model"), str) else None
        self.requested_model: str | None = None
        self.route: dict[str, Any] | None = None
        self.attempts = 0
        self.tags: list[str] | None = None
        self.guardrail_hits: list[str] = []
        self.rules: list[guardrails.Rule] = []
        self.cache_hit = False
        self.cache_key: str | None = None
        self.cache_ttl = 0
        self._slot = False  # a max_parallel slot is held
        self._route_open = False  # RouterState.begin() called without a matching end()
        h = request.headers
        self.app_title = (h.get("x-title") or h.get("x-openrouter-title") or "")[:200] or None
        self.referer = (h.get("http-referer") or h.get("referer") or "")[:300] or None
        user = payload.get("user")
        self.end_user = user[:200] if isinstance(user, str) and user else None

    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self.started) * 1000

    def cost(self, prompt: int, completion: int) -> float:
        if not self.model or self.cache_hit:
            return 0.0
        pin, pout = prices(self.route or {}, self.model)
        return (prompt * pin + completion * pout) / 1_000_000

    def begin_route(self, route: dict[str, Any]) -> None:
        self.route = route
        self.attempts += 1
        self.st.router.begin(route)
        self._route_open = True

    def end_route(self, tokens: int = 0) -> None:
        if self._route_open and self.route:
            self.st.router.end(self.route, tokens)
            self._route_open = False

    def hold_slot(self) -> None:
        self._slot = True

    def release(self) -> None:
        if self._slot:
            self.st.limiter.release(self.auth.key["id"])
            self._slot = False

    def log(self, *, prompt_tokens: int = 0, completion_tokens: int = 0, estimated: bool = False, status: int,
            error: str | None = None, ttft_ms: float | None = None, finish_reason: str | None = None) -> None:
        self.end_route(prompt_tokens + completion_tokens)
        self.release()
        fields = dict(
            request_id=self.request_id, service_id=self.auth.service["id"], api_key_id=self.auth.key["id"],
            provider_id=self.route["provider_id"] if self.route and not self.cache_hit else None, model=self.model_name,
            upstream_model=self.route["upstream_model"] if self.route and not self.cache_hit else None, endpoint=self.endpoint,
            stream=int(self.stream), prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            usage_estimated=int(estimated), cost_usd=self.cost(prompt_tokens, completion_tokens),
            latency_ms=round(self.elapsed_ms(), 1), ttft_ms=round(ttft_ms, 1) if ttft_ms is not None else None,
            status_code=status, success=int(200 <= status < 300 and error is None), error=error[:500] if error else None,
            attempts=max(1, self.attempts), app_title=self.app_title, referer=self.referer, end_user=self.end_user,
            requested_model=self.requested_model, finish_reason=finish_reason, cache_hit=int(self.cache_hit),
            tags=json.dumps(self.tags) if self.tags else None, guardrail=",".join(self.guardrail_hits) or None,
        )
        record_usage(self.db, **fields)
        broadcaster, alerter = self.st.broadcast, self.st.alerter
        if broadcaster.enabled:
            broadcaster.emit({**fields, "ts": time.time(), "type": "generation", "total_tokens": prompt_tokens + completion_tokens,
                              "service": self.auth.service["name"], "key": self.auth.key["name"],
                              "provider": self.route["provider_name"] if self.route else None})
        if alerter.enabled and fields["cost_usd"]:
            alerter.check_budgets(self.db, self.auth)

    def headers(self) -> dict[str, str]:
        h = {"x-request-id": self.request_id}
        if self.route and not self.cache_hit:
            h["x-llmgate-provider"] = self.route["provider_name"]
        if self.model_name:
            h["x-llmgate-model"] = self.model_name
        rpm = self.auth.key.get("rpm_limit")
        if rpm:
            h["x-ratelimit-limit-requests"] = str(rpm)
            h["x-ratelimit-remaining-requests"] = str(self.st.limiter.remaining(("key", self.auth.key["id"]), rpm))
        return h


def _read_payload(raw: bytes, limit: int) -> dict[str, Any]:
    if len(raw) > limit:
        raise GatewayError(413, "Request body too large.", code="request_too_large")
    try:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError
    except ValueError:
        raise GatewayError(400, "Request body must be a JSON object.", code="invalid_json") from None
    return payload


def candidate_names(db: Database, settings: Settings, auth: AuthContext, payload: dict[str, Any], endpoint: str) -> list[str]:
    """The models to try, in order: ``model`` then ``models`` (fallbacks); ``llmgate/auto`` expands to a ranked list."""
    primary = payload.get("model")
    extra = payload.get("models") or []
    if not isinstance(extra, list) or not all(isinstance(m, str) for m in extra):
        raise GatewayError(400, "'models' must be a list of model names.", code="invalid_models")
    if primary is not None and not isinstance(primary, str):
        raise GatewayError(400, "'model' must be a string.", code="invalid_model")
    names = [n for n in [primary, *extra] if n]
    if not names:
        raise GatewayError(400, "'model' (or 'models') is required.", code="missing_model")
    expanded: list[str] = []
    for n in names:
        for m in (auto_candidates(db, auth, payload, endpoint) if n == settings.auto_model else [n]):
            if m not in expanded:
                expanded.append(m)
    return expanded


def _upstream_payload(att: Attempt, model: dict[str, Any], route: dict[str, Any]) -> dict[str, Any]:
    body = {k: v for k, v in att.payload.items() if k not in GATEWAY_FIELDS}
    body["model"] = route["upstream_model"]
    if att.stream and att.endpoint in OPENAI_SHAPE:
        body["stream_options"] = {**(body.get("stream_options") or {}), "include_usage": True}  # exact streamed billing
    if att.endpoint == "chat" and "middle-out" in transforms.requested(att.payload) and model["context_length"]:
        reserve = body.get("max_tokens") or body.get("max_completion_tokens") or model["max_completion_tokens"] or 0
        body["messages"] = transforms.middle_out(body.get("messages") or [], model["context_length"] - int(reserve) - 64)
    return body


async def _open_upstream(client: httpx.AsyncClient, settings: Settings, att: Attempt, model: dict[str, Any],
                         route: dict[str, Any]) -> httpx.Response:
    headers = {"content-type": "application/json", "accept": "text/event-stream" if att.stream else "application/json",
               "x-request-id": att.request_id}
    api_key = secretbox.decrypt(route["api_key"])
    if api_key:
        headers["authorization"] = f"Bearer {api_key}"
    timeout = httpx.Timeout(route["timeout_s"] or settings.default_read_timeout_s, connect=settings.connect_timeout_s)
    req = client.build_request("POST", route["base_url"].rstrip("/") + ENDPOINTS[att.endpoint][0],
                               json=_upstream_payload(att, model, route), headers=headers, timeout=timeout)
    return await client.send(req, stream=True)


@dataclass
class Tried:
    response: Response | None = None  # a served answer (success)
    client_error: tuple[int, bytes] | None = None  # upstream 4xx: the request itself is wrong for this model
    last_error: str = "no upstream attempted"


def _retries(settings: Settings, payload: dict[str, Any]) -> int:
    n = payload.get("num_retries")
    return max(0, min(int(n), 5)) if isinstance(n, int) and not isinstance(n, bool) else settings.upstream_retries


async def _try_routes(att: Attempt, model: dict[str, Any], routes: list[dict[str, Any]], st: Any) -> Tried:
    out = Tried()
    retries = _retries(st.settings, att.payload)
    for route in routes:
        for attempt in range(retries + 1):
            att.begin_route(route)
            failed_status: int | None = None
            try:
                upstream = await _open_upstream(st.client, st.settings, att, model, route)
            except httpx.HTTPError as exc:
                att.end_route()
                out.last_error = f"{type(exc).__name__}: {exc}".rstrip(": ")
                st.router.record(route["provider_id"], False)
                mark_passive(st.db, route["provider_id"], False, out.last_error, st.alerter)
            except BaseException:
                att.end_route()
                raise
            else:
                if upstream.status_code < 300:
                    st.router.record(route["provider_id"], True)
                    mark_passive(st.db, route["provider_id"], True, alerter=st.alerter)
                    if att.stream:
                        out.response = stream_response(att, upstream, st.db)  # the route stays open until the stream is logged
                        return out
                    try:
                        body = await upstream.aread()
                    except httpx.HTTPError as exc:
                        att.end_route()
                        out.last_error = f"{type(exc).__name__} while reading response"
                        st.router.record(route["provider_id"], False)
                        mark_passive(st.db, route["provider_id"], False, out.last_error, st.alerter)
                    except BaseException:
                        att.end_route()
                        raise
                    else:
                        out.response = finish_json(att, upstream, body)
                        return out
                    finally:
                        await upstream.aclose()
                else:
                    body = await upstream.aread()
                    await upstream.aclose()
                    att.end_route()
                    failed_status = upstream.status_code
                    out.last_error = f"upstream {failed_status}: {_error_text(body)}"
                    if not _retryable(failed_status):
                        out.client_error = (failed_status, body)  # another provider of this model would say the same
                        return out
                    if failed_status >= 500:
                        st.router.record(route["provider_id"], False)
                        mark_passive(st.db, route["provider_id"], False, out.last_error, st.alerter)
            if attempt < retries:  # same provider again, with exponential backoff
                await asyncio.sleep(min(st.settings.retry_backoff_s * 2 ** attempt, 5.0))
    return out


async def handle_inference(request: Request, endpoint: str) -> Response:
    st = request.app.state
    auth = await authenticate(request)
    request_id = "gen-" + uuid.uuid4().hex[:24]
    payload = _read_payload(await request.body(), st.settings.max_request_bytes)
    att = Attempt(request, request_id, endpoint, auth, payload)
    try:
        return await _serve(request, att, st)
    except BaseException:  # never leak a concurrency slot or an open route
        att.end_route()
        att.release()
        raise


async def _serve(request: Request, att: Attempt, st: Any) -> Response:
    endpoint, auth, db = att.endpoint, att.auth, st.db
    kind = ENDPOINTS[endpoint][2]
    try:
        att.tags = _tags(request, att.payload)
        enforce_limits(db, st.limiter, auth, att.payload)
        att.hold_slot()
        transforms.requested(att.payload)
        wanted = att.payload.get("guardrails")
        if wanted is not None and not (isinstance(wanted, list) and all(isinstance(g, str) for g in wanted)):
            raise GatewayError(400, "'guardrails' must be a list of guardrail names.", code="invalid_guardrails")
        att.rules = guardrails.load_rules(db, wanted)
        att.payload, att.guardrail_hits = guardrails.check_input(att.rules, att.payload)
        names = candidate_names(db, st.settings, auth, att.payload, endpoint)
        policy = cache_mod.policy_for(st.settings, att.payload, request.headers, att.stream)
    except GatewayError as exc:
        if getattr(exc, "guardrail", None):
            att.guardrail_hits.append(exc.guardrail)
        att.log(status=exc.status_code, error=f"{exc.code}: {exc.message}")
        exc.headers["x-request-id"] = att.request_id
        raise

    if policy.enabled:
        key = cache_mod.make_key(st.settings, auth.service["id"], endpoint, att.payload)
        hit = st.cache.get(key) if policy.lookup else None
        if hit is not None:
            return _cached_response(att, *hit)
        if policy.store:
            att.cache_key, att.cache_ttl = key, policy.ttl

    plan_errors: list[GatewayError] = []
    client_error: tuple[int, bytes, str] | None = None
    last_error = "no upstream attempted"
    queue, done, served = deque(names), set(), 0
    while queue:
        name = queue.popleft()
        if name in done:
            continue
        done.add(name)
        try:
            model, routes = plan(db, name, auth, att.payload, kind, st.router)
            check_model_budget(db, auth, model["name"])
        except GatewayError as exc:
            plan_errors.append(exc)
            continue
        att.model, att.model_name = model, model["name"]
        att.requested_model = names[0] if served else None
        served += 1
        tried = await _try_routes(att, model, routes, st)
        if tried.response is not None:
            return tried.response
        last_error = tried.last_error
        if tried.client_error and client_error is None:
            client_error = (*tried.client_error, tried.last_error)
        extra = json.loads(model["fallbacks"]) if model.get("fallbacks") else []
        if tried.client_error and _CONTEXT_ERROR.search(tried.last_error) and model.get("context_window_fallbacks"):
            extra = json.loads(model["context_window_fallbacks"]) + extra  # a bigger-context model goes first
        for fallback in reversed(extra):
            if fallback not in done:
                queue.appendleft(fallback)

    if client_error:  # nothing served; surface the upstream's own complaint
        status, body, text = client_error
        att.log(status=status, error=text)
        return Response(body, status_code=status, media_type="application/json", headers=att.headers())
    if att.attempts == 0:  # no candidate even reached a provider: planning errors (404/403/402/400/503)
        exc = plan_errors[0] if len(names) == 1 else plan_errors[-1]
        att.log(status=exc.status_code, error=f"{exc.code}: {exc.message}")
        exc.headers["x-request-id"] = att.request_id
        raise exc
    att.log(status=502, error=last_error)
    raise GatewayError(502, f"All upstream providers failed for [{', '.join(names)}] ({att.attempts} attempted). "
                            f"Last error: {last_error}", type="upstream_error", code="upstream_unavailable",
                       headers={"x-request-id": att.request_id})


def _cached_response(att: Attempt, status: int, body: bytes) -> Response:
    """Serve a stored answer: new generation id, zero cost, tokens not charged to budgets."""
    data = json.loads(body)
    att.cache_hit = True
    att.model_name = data.get("model") or att.model_name
    if att.endpoint in OPENAI_SHAPE:
        data["id"] = att.request_id
    if isinstance(data.get("usage"), dict):
        data["usage"]["cost"] = 0.0
    att.log(status=status, finish_reason=None)
    return JSONResponse(data, status_code=status, headers={**att.headers(), "x-llmgate-cache": "hit"})
