"""Client-facing, OpenAI- and OpenRouter-compatible routes.

Every route is served under both ``/v1`` (OpenAI) and ``/api/v1`` (OpenRouter's base path), so existing
SDK configs work by only swapping the base URL.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from .auth import authenticate
from .catalog import model_catalog, model_endpoints, providers_list
from .errors import GatewayError
from .proxy import ENDPOINTS, handle_inference
from .usage import month_start, period_usage

router = APIRouter()
BASES = ("/v1", "/api/v1")


def _inference(endpoint: str):
    async def view(request: Request):
        return await handle_inference(request, endpoint)
    view.__name__ = f"inference_{endpoint}"
    return view


for _endpoint, (_, _public, _) in ENDPOINTS.items():
    for _base in BASES:
        router.add_api_route(_base + _public[len("/v1"):], _inference(_endpoint), methods=["POST"], tags=["inference"])


async def _catalog_access(request: Request) -> None:
    """The OpenRouter-style catalog is public by default; LLMGATE_PUBLIC_CATALOG=false requires an API key / JWT."""
    if not request.app.state.settings.public_catalog:
        await authenticate(request)


def _csv(value: str | None) -> list[str] | None:
    return [v.strip() for v in value.split(",") if v.strip()] if value else None


@router.get("/v1/models", tags=["catalog"])
async def list_models(request: Request, supported_parameters: str | None = None, input_modalities: str | None = None) -> dict[str, Any]:
    """Models this key may call (OpenAI shape plus OpenRouter's pricing / context / capability fields)."""
    auth = await authenticate(request)
    return {"object": "list", "data": model_catalog(request.app.state.db, auth.allowed_models,
                                                    supported_parameters=_csv(supported_parameters),
                                                    input_modalities=_csv(input_modalities))}


@router.get("/api/v1/models", tags=["catalog"], dependencies=[Depends(_catalog_access)])
async def public_models(request: Request, supported_parameters: str | None = None, input_modalities: str | None = None,
                        kind: str | None = None) -> dict[str, Any]:
    """Public catalog like ``openrouter.ai/api/v1/models``: no key needed. Filters: supported_parameters, input_modalities."""
    return {"data": model_catalog(request.app.state.db, supported_parameters=_csv(supported_parameters),
                                  input_modalities=_csv(input_modalities), kind=kind)}


@router.get("/api/v1/models/count", tags=["catalog"], dependencies=[Depends(_catalog_access)])
async def models_count(request: Request) -> dict[str, Any]:
    return {"data": {"count": len(model_catalog(request.app.state.db))}}


@router.get("/api/v1/models/{model_id:path}/endpoints", tags=["catalog"], dependencies=[Depends(_catalog_access)])
async def endpoints_of(model_id: str, request: Request) -> dict[str, Any]:
    """Providers serving one model, with 30-minute uptime, latency and throughput (like OpenRouter)."""
    data = model_endpoints(request.app.state.db, model_id)
    if data is None:
        raise GatewayError(404, f"Model '{model_id}' not found.", type="not_found_error", code="model_not_found")
    return {"data": data}


@router.get("/api/v1/providers", tags=["catalog"], dependencies=[Depends(_catalog_access)])
async def providers(request: Request) -> dict[str, Any]:
    return {"data": providers_list(request.app.state.db)}


async def _key_info(request: Request) -> dict[str, Any]:
    """Limits and month-to-date usage of the calling key (OpenRouter's ``GET /api/v1/key``)."""
    db = request.app.state.db
    auth = await authenticate(request)
    key, service = auth.key, auth.service
    since = month_start()
    key_tokens, key_cost = period_usage(db, api_key_id=key["id"], since=since)
    svc_tokens, svc_cost = period_usage(db, service_id=service["id"], since=since)
    return {"data": {
        "label": key["name"], "service": service["name"], "key_prefix": key["key_prefix"], "kind": key["kind"],
        "expires_at": key["expires_at"], "allowed_models": auth.allowed_models, "allowed_providers": auth.allowed_providers,
        "rate_limit": {"requests": key["rpm_limit"], "interval": "1m"},
        "usage": {"tokens": key_tokens, "cost_usd": round(key_cost, 8), "period_start": since},
        "limit": {"tokens": key["monthly_token_limit"], "cost_usd": key["monthly_budget_usd"]},
        "service_usage": {"tokens": svc_tokens, "cost_usd": round(svc_cost, 8)},
        "service_limit": {"tokens": service["monthly_token_limit"], "cost_usd": service["monthly_budget_usd"]},
    }}


async def _credits(request: Request) -> dict[str, Any]:
    """OpenRouter-shaped credits: the tightest monthly USD budget that applies, and what has been spent."""
    db = request.app.state.db
    auth = await authenticate(request)
    since = month_start()
    _, key_cost = period_usage(db, api_key_id=auth.key["id"], since=since)
    _, svc_cost = period_usage(db, service_id=auth.service["id"], since=since)
    budgets = [(b, c) for b, c in ((auth.key["monthly_budget_usd"], key_cost), (auth.service["monthly_budget_usd"], svc_cost)) if b is not None]
    total, used = min(budgets, key=lambda bc: bc[0] - bc[1]) if budgets else (None, svc_cost)
    return {"data": {"total_credits": total, "total_usage": round(used, 8), "period": "month"}}


async def _generation(request: Request, id: str = Query(...)) -> dict[str, Any]:
    """Metadata of one finished request by its ``gen-…`` id (the ``id`` in the completion response)."""
    auth = await authenticate(request)
    row = request.app.state.db.one(
        "SELECT l.*, p.name AS provider_name FROM usage_logs l LEFT JOIN providers p ON p.id = l.provider_id "
        "WHERE l.request_id = ? AND l.service_id = ? ORDER BY l.id DESC LIMIT 1", (id, auth.service["id"]))
    if row is None:
        raise GatewayError(404, f"Generation '{id}' not found.", type="not_found_error", code="generation_not_found")
    return {"data": {
        "id": row["request_id"], "model": row["model"], "requested_model": row["requested_model"],
        "provider_name": row["provider_name"], "upstream_model": row["upstream_model"], "created_at": row["ts"],
        "streamed": bool(row["stream"]), "finish_reason": row["finish_reason"], "api_type": row["endpoint"],
        "tokens_prompt": row["prompt_tokens"], "tokens_completion": row["completion_tokens"],
        "usage_estimated": bool(row["usage_estimated"]), "total_cost": row["cost_usd"],
        "latency": row["ttft_ms"], "generation_time": row["latency_ms"], "attempts": row["attempts"],
        "http_status": row["status_code"], "error": row["error"], "app": row["app_title"], "origin": row["referer"],
        "user": row["end_user"],
    }}


for _base in BASES:
    for _path, _fn, _name in (("/key", _key_info, "key"), ("/credits", _credits, "credits"), ("/generation", _generation, "generation")):
        router.add_api_route(_base + _path, _fn, methods=["GET"], tags=["account"], name=f"{_name}{_base.replace('/', '_')}")
