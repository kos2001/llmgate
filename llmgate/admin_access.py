from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from .admin_common import ADMIN_DEPS, bool_cols, decode_json, flags, get_db, insert_row, prepare_limits, require_row, update_row
from .db import Database, now
from .security import generate_api_key
from .schemas import KeyCreate, KeyUpdate, ServiceCreate, ServiceUpdate
from .usage import month_start, period_usage

router = APIRouter(prefix="/admin/api", tags=["services"], dependencies=ADMIN_DEPS)


def present_key(db: Database, k: dict[str, Any], since: float | None = None) -> dict[str, Any]:
    flags(k, "enabled")
    k.pop("key_hash", None)
    decode_json(k, {"allowed_models": [], "allowed_providers": [], "model_budgets": {}, "metadata": {}})
    tokens, cost = period_usage(db, api_key_id=k["id"], since=since or month_start())
    k["usage_month"] = {"tokens": tokens, "cost_usd": cost}
    return k


def present_service(db: Database, s: dict[str, Any]) -> dict[str, Any]:
    flags(s, "enabled")
    decode_json(s, {"allowed_models": [], "metadata": {}})
    since = month_start()
    tokens, cost = period_usage(db, service_id=s["id"], since=since)
    s["usage_month"] = {"tokens": tokens, "cost_usd": cost}
    s["keys"] = [present_key(db, k, since) for k in
                 db.query("SELECT * FROM api_keys WHERE service_id = ? ORDER BY id", (s["id"],))]
    return s


def _key_values(data: dict[str, Any]) -> dict[str, Any]:
    return bool_cols(prepare_limits(data, "allowed_models", "allowed_providers", "model_budgets", "metadata"), "enabled")


@router.get("/services")
def list_services(request: Request) -> list[dict[str, Any]]:
    db = get_db(request)
    return [present_service(db, s) for s in db.query("SELECT * FROM services ORDER BY name")]


@router.post("/services", status_code=201)
def create_service(body: ServiceCreate, request: Request) -> dict[str, Any]:
    db = get_db(request)
    values = prepare_limits(body.model_dump(), "allowed_models", "metadata")
    sid = insert_row(db, "services", bool_cols({**values, "created_at": now()}, "enabled"))
    return present_service(db, require_row(db, "services", sid, "Service"))


@router.patch("/services/{service_id}")
def update_service(service_id: int, body: ServiceUpdate, request: Request) -> dict[str, Any]:
    db = get_db(request)
    require_row(db, "services", service_id, "Service")
    update_row(db, "services", service_id,
               bool_cols(prepare_limits(body.model_dump(exclude_unset=True), "allowed_models", "metadata"), "enabled"))
    return present_service(db, require_row(db, "services", service_id, "Service"))


@router.delete("/services/{service_id}", status_code=204)
def delete_service(service_id: int, request: Request) -> None:
    db = get_db(request)
    require_row(db, "services", service_id, "Service")
    db.execute("DELETE FROM services WHERE id = ?", (service_id,))


@router.post("/services/{service_id}/keys", status_code=201)
def create_key(service_id: int, body: KeyCreate, request: Request) -> dict[str, Any]:
    """Issue a key. The plaintext is in the response once and is never stored."""
    db = get_db(request)
    require_row(db, "services", service_id, "Service")
    plaintext, prefix, digest = generate_api_key(request.app.state.settings.key_prefix)
    values = _key_values({**body.model_dump(), "service_id": service_id, "key_prefix": prefix,
                          "key_hash": digest, "created_at": now()})
    kid = insert_row(db, "api_keys", values)
    return {**present_key(db, require_row(db, "api_keys", kid, "Key")), "key": plaintext}


@router.patch("/keys/{key_id}")
def update_key(key_id: int, body: KeyUpdate, request: Request) -> dict[str, Any]:
    db = get_db(request)
    require_row(db, "api_keys", key_id, "Key")
    update_row(db, "api_keys", key_id, _key_values(body.model_dump(exclude_unset=True)))
    return present_key(db, require_row(db, "api_keys", key_id, "Key"))


@router.post("/keys/{key_id}/rotate")
def rotate_key(key_id: int, request: Request) -> dict[str, Any]:
    db = get_db(request)
    require_row(db, "api_keys", key_id, "Key")
    plaintext, prefix, digest = generate_api_key(request.app.state.settings.key_prefix)
    update_row(db, "api_keys", key_id, {"key_prefix": prefix, "key_hash": digest})
    return {**present_key(db, require_row(db, "api_keys", key_id, "Key")), "key": plaintext}


@router.delete("/keys/{key_id}", status_code=204)
def delete_key(key_id: int, request: Request) -> None:
    db = get_db(request)
    require_row(db, "api_keys", key_id, "Key")
    db.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
