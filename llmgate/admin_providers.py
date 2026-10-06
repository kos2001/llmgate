from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi import APIRouter, Request

from .admin_common import ADMIN_DEPS, bool_cols, flags, get_db, insert_row, require_row, update_row
from .db import now
from .errors import GatewayError
from .health import apply_check, check_provider
from .schemas import ProviderCreate, ProviderUpdate

router = APIRouter(prefix="/admin/api/providers", tags=["providers"], dependencies=ADMIN_DEPS)


def present(p: dict[str, Any], stats24: dict[int, dict[str, Any]] | None = None) -> dict[str, Any]:
    flags(p, "enabled")
    key = p.pop("api_key", None)
    p["has_api_key"] = bool(key)
    p["engine_stats"] = json.loads(p["engine_stats"]) if p.get("engine_stats") else None
    if stats24 is not None:
        p["usage_24h"] = stats24.get(p["id"], {"requests": 0, "tokens": 0})
    return p


@router.get("")
def list_providers(request: Request) -> list[dict[str, Any]]:
    db = get_db(request)
    rows = db.query(
        "SELECT provider_id, COUNT(*) AS requests, SUM(total_tokens) AS tokens FROM usage_logs "
        "WHERE ts >= ? AND provider_id IS NOT NULL GROUP BY provider_id", (now() - 86400,))
    stats = {r["provider_id"]: {"requests": r["requests"], "tokens": r["tokens"] or 0} for r in rows}
    counts = {r["provider_id"]: r["n"] for r in db.query(
        "SELECT provider_id, COUNT(*) AS n FROM model_routes GROUP BY provider_id")}
    out = []
    for p in db.query("SELECT * FROM providers ORDER BY name"):
        item = present(p, stats)
        item["routes"] = counts.get(item["id"], 0)
        item["cooldown_s"] = round(request.app.state.router.cooldown_remaining(item["id"]))
        out.append(item)
    return out


@router.post("", status_code=201)
def create_provider(body: ProviderCreate, request: Request) -> dict[str, Any]:
    db = get_db(request)
    values = bool_cols({**body.model_dump(), "created_at": now()}, "enabled")
    pid = insert_row(db, "providers", values)
    return present(require_row(db, "providers", pid, "Provider"))


@router.get("/{provider_id}")
def get_provider(provider_id: int, request: Request) -> dict[str, Any]:
    return present(require_row(get_db(request), "providers", provider_id, "Provider"))


@router.patch("/{provider_id}")
def update_provider(provider_id: int, body: ProviderUpdate, request: Request) -> dict[str, Any]:
    db = get_db(request)
    require_row(db, "providers", provider_id, "Provider")
    update_row(db, "providers", provider_id, bool_cols(body.model_dump(exclude_unset=True), "enabled"))
    return present(require_row(db, "providers", provider_id, "Provider"))


@router.delete("/{provider_id}", status_code=204)
def delete_provider(provider_id: int, request: Request) -> None:
    db = get_db(request)
    require_row(db, "providers", provider_id, "Provider")
    db.execute("DELETE FROM providers WHERE id = ?", (provider_id,))


@router.post("/{provider_id}/check")
async def check(provider_id: int, request: Request) -> dict[str, Any]:
    """Probe the engine now (``/health`` + ``/metrics``) and store the result."""
    st = request.app.state
    provider = require_row(st.db, "providers", provider_id, "Provider")
    apply_check(st.db, provider_id, await check_provider(st.client, provider, st.settings), st.alerter)
    return present(require_row(st.db, "providers", provider_id, "Provider"))


@router.post("/{provider_id}/sync-models")
async def sync_models(provider_id: int, request: Request, register: bool = False) -> dict[str, Any]:
    """List the models the engine serves; with ``register=true`` add the missing ones to the registry."""
    st = request.app.state
    provider = require_row(st.db, "providers", provider_id, "Provider")
    headers = {"authorization": f"Bearer {provider['api_key']}"} if provider["api_key"] else {}
    try:
        resp = await st.client.get(provider["base_url"] + "/models", headers=headers, timeout=st.settings.health_timeout_s * 2)
        resp.raise_for_status()
        discovered = resp.json().get("data", [])
    except (httpx.HTTPError, ValueError) as exc:
        raise GatewayError(502, f"Could not list models from the provider: {exc}", type="upstream_error",
                           code="sync_failed") from None
    created = []
    if register:
        for m in discovered:
            upstream = m.get("id")
            if not upstream or st.db.one(
                    "SELECT 1 FROM model_routes WHERE provider_id = ? AND upstream_model = ?", (provider_id, upstream)):
                continue
            model = st.db.one("SELECT id FROM models WHERE name = ?", (upstream,))
            model_id = model["id"] if model else insert_row(st.db, "models", {
                "name": upstream, "context_length": m.get("max_model_len"),
                "kind": "embedding" if "embed" in upstream.lower() else "chat", "created_at": now()})
            insert_row(st.db, "model_routes", {"model_id": model_id, "provider_id": provider_id,
                                               "upstream_model": upstream})
            created.append(upstream)
    return {"discovered": [m.get("id") for m in discovered], "registered": created}
