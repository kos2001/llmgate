from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from .admin_common import ADMIN_DEPS, bool_cols, decode_json, encode_json, flags, get_db, insert_row, require_row, update_row
from .db import Database, now
from .schemas import ModelCreate, ModelUpdate, RouteCreate, RouteUpdate

router = APIRouter(prefix="/admin/api", tags=["models"], dependencies=ADMIN_DEPS)


def _routes(db: Database, model_id: int) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT r.*, p.name AS provider_name, p.status AS provider_status, p.kind AS provider_kind "
        "FROM model_routes r JOIN providers p ON p.id = r.provider_id WHERE r.model_id = ? "
        "ORDER BY r.priority, r.id", (model_id,))
    return [flags(r, "enabled") for r in rows]


JSON_COLS = ("supported_parameters", "input_modalities", "output_modalities", "fallbacks", "context_window_fallbacks")


def present(db: Database, m: dict[str, Any]) -> dict[str, Any]:
    flags(m, "enabled")
    decode_json(m, {"supported_parameters": None, "input_modalities": ["text"], "output_modalities": ["text"],
                    "fallbacks": [], "context_window_fallbacks": []})
    m["routes"] = _routes(db, m["id"])
    return m


@router.get("/models")
def list_models(request: Request) -> list[dict[str, Any]]:
    db = get_db(request)
    usage = {r["model"]: r for r in db.query(
        "SELECT model, COUNT(*) AS requests, SUM(total_tokens) AS tokens, SUM(cost_usd) AS cost_usd "
        "FROM usage_logs WHERE ts >= ? GROUP BY model", (now() - 30 * 86400,))}
    out = []
    for m in db.query("SELECT * FROM models ORDER BY name"):
        item = present(db, m)
        u = usage.get(item["name"])
        item["usage_30d"] = {"requests": u["requests"], "tokens": u["tokens"] or 0,
                             "cost_usd": u["cost_usd"] or 0} if u else {"requests": 0, "tokens": 0, "cost_usd": 0}
        out.append(item)
    return out


@router.post("/models", status_code=201)
def create_model(body: ModelCreate, request: Request) -> dict[str, Any]:
    db = get_db(request)
    values = encode_json(body.model_dump(exclude={"routes"}), *JSON_COLS)
    with db.transaction():
        model_id = insert_row(db, "models", bool_cols({**values, "created_at": now()}, "enabled"))
        for route in body.routes:
            require_row(db, "providers", route.provider_id, "Provider")
            insert_row(db, "model_routes", bool_cols({**route.model_dump(), "model_id": model_id}, "enabled"))
    return present(db, require_row(db, "models", model_id, "Model"))


@router.get("/models/{model_id}")
def get_model(model_id: int, request: Request) -> dict[str, Any]:
    db = get_db(request)
    return present(db, require_row(db, "models", model_id, "Model"))


@router.patch("/models/{model_id}")
def update_model(model_id: int, body: ModelUpdate, request: Request) -> dict[str, Any]:
    db = get_db(request)
    require_row(db, "models", model_id, "Model")
    update_row(db, "models", model_id,
               bool_cols(encode_json(body.model_dump(exclude_unset=True), *JSON_COLS), "enabled"))
    return present(db, require_row(db, "models", model_id, "Model"))


@router.delete("/models/{model_id}", status_code=204)
def delete_model(model_id: int, request: Request) -> None:
    db = get_db(request)
    require_row(db, "models", model_id, "Model")
    db.execute("DELETE FROM models WHERE id = ?", (model_id,))


@router.post("/models/{model_id}/routes", status_code=201)
def add_route(model_id: int, body: RouteCreate, request: Request) -> dict[str, Any]:
    db = get_db(request)
    require_row(db, "models", model_id, "Model")
    require_row(db, "providers", body.provider_id, "Provider")
    insert_row(db, "model_routes", bool_cols({**body.model_dump(), "model_id": model_id}, "enabled"))
    return present(db, require_row(db, "models", model_id, "Model"))


@router.patch("/routes/{route_id}")
def update_route(route_id: int, body: RouteUpdate, request: Request) -> dict[str, Any]:
    db = get_db(request)
    route = require_row(db, "model_routes", route_id, "Route")
    update_row(db, "model_routes", route_id, bool_cols(body.model_dump(exclude_unset=True), "enabled"))
    return present(db, require_row(db, "models", route["model_id"], "Model"))


@router.delete("/routes/{route_id}", status_code=204)
def delete_route(route_id: int, request: Request) -> None:
    db = get_db(request)
    require_row(db, "model_routes", route_id, "Route")
    db.execute("DELETE FROM model_routes WHERE id = ?", (route_id,))
