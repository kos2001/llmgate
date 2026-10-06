"""OpenRouter's key-management ("provisioning") API and a Prometheus scrape endpoint.

Both use the same authentication and roles as ``/admin/api``: send an admin token, a dashboard
session, or an IdP-issued JWT carrying at least the ``operator`` role (``viewer`` to read).
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from .admin_common import encode_json, get_db, insert_row, require_row, update_row
from .db import Database, now
from .errors import GatewayError
from .rbac import admin_guard
from .security import generate_api_key
from .usage import month_start, period_usage

router = APIRouter(dependencies=[Depends(admin_guard)])


class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    service: str = Field(default="default", description="Service (app/team) the key belongs to; created on demand")
    limit: float | None = Field(default=None, ge=0, description="Monthly budget in USD")
    allowed_models: list[str] = []
    allowed_providers: list[str] = []
    expires_at: float | None = None


class KeyPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    disabled: bool | None = None
    limit: float | None = Field(default=None, ge=0)
    allowed_models: list[str] | None = None
    allowed_providers: list[str] | None = None


def _shape(db: Database, k: dict[str, Any]) -> dict[str, Any]:
    _, cost = period_usage(db, api_key_id=k["id"], since=month_start())
    return {"hash": k["key_hash"], "name": k["name"], "label": k["key_prefix"] + "…", "disabled": not k["enabled"],
            "limit": k["monthly_budget_usd"], "usage": round(cost, 8), "service_id": k["service_id"],
            "allowed_models": json.loads(k["allowed_models"]) if k["allowed_models"] else [],
            "allowed_providers": json.loads(k["allowed_providers"]) if k["allowed_providers"] else [],
            "created_at": k["created_at"], "expires_at": k["expires_at"], "last_used_at": k["last_used_at"], "kind": k["kind"]}


def _by_hash(db: Database, key_hash: str) -> dict[str, Any]:
    row = db.one("SELECT * FROM api_keys WHERE key_hash = ?", (key_hash,))
    if row is None:
        raise GatewayError(404, "Key not found.", type="not_found_error", code="not_found")
    return row


@router.get("/api/v1/keys", tags=["keys"])
def list_keys(request: Request) -> dict[str, Any]:
    db = get_db(request)
    return {"data": [_shape(db, k) for k in db.query("SELECT * FROM api_keys ORDER BY id DESC")]}


@router.post("/api/v1/keys", status_code=201, tags=["keys"])
def create_key(body: KeyIn, request: Request) -> dict[str, Any]:
    """Mint a key programmatically. The secret is returned once, as ``key``."""
    db = get_db(request)
    svc = db.one("SELECT id FROM services WHERE name = ?", (body.service,))
    service_id = svc["id"] if svc else insert_row(db, "services", {"name": body.service, "created_at": now()})
    plaintext, prefix, digest = generate_api_key(request.app.state.settings.key_prefix)
    values = encode_json({"service_id": service_id, "name": body.name, "key_prefix": prefix, "key_hash": digest,
                          "monthly_budget_usd": body.limit, "allowed_models": body.allowed_models,
                          "allowed_providers": body.allowed_providers, "expires_at": body.expires_at, "created_at": now()},
                         "allowed_models", "allowed_providers")
    kid = insert_row(db, "api_keys", values)
    return {"data": _shape(db, require_row(db, "api_keys", kid, "Key")), "key": plaintext}


@router.get("/api/v1/keys/{key_hash}", tags=["keys"])
def get_key(key_hash: str, request: Request) -> dict[str, Any]:
    db = get_db(request)
    return {"data": _shape(db, _by_hash(db, key_hash))}


@router.patch("/api/v1/keys/{key_hash}", tags=["keys"])
def patch_key(key_hash: str, body: KeyPatch, request: Request) -> dict[str, Any]:
    db = get_db(request)
    row = _by_hash(db, key_hash)
    raw = body.model_dump(exclude_unset=True)
    changes: dict[str, Any] = {}
    if "disabled" in raw and raw["disabled"] is not None:
        changes["enabled"] = int(not raw["disabled"])
    if "limit" in raw:
        changes["monthly_budget_usd"] = raw["limit"]
    for col in ("name", "allowed_models", "allowed_providers"):
        if col in raw and raw[col] is not None:
            changes[col] = raw[col]
    update_row(db, "api_keys", row["id"], encode_json(changes, "allowed_models", "allowed_providers"))
    return {"data": _shape(db, require_row(db, "api_keys", row["id"], "Key"))}


@router.delete("/api/v1/keys/{key_hash}", tags=["keys"])
def delete_key(key_hash: str, request: Request) -> dict[str, Any]:
    db = get_db(request)
    row = _by_hash(db, key_hash)
    db.execute("DELETE FROM api_keys WHERE id = ?", (row["id"],))
    return {"data": {"deleted": True}}


# ------------------------------------------------------------------------------------------ metrics

def _esc(v: Any) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _lines(name: str, kind: str, help_: str, samples: list[tuple[dict[str, Any], float]]) -> list[str]:
    out = [f"# HELP {name} {help_}", f"# TYPE {name} {kind}"]
    for labels, value in samples:
        label = ",".join(f'{k}="{_esc(v)}"' for k, v in labels.items())
        out.append(f"{name}{{{label}}} {value}" if label else f"{name} {value}")
    return out


@router.get("/metrics", response_class=PlainTextResponse, tags=["metrics"])
def metrics(request: Request) -> str:
    """Prometheus text format: request/token/cost counters by model and provider, plus provider health."""
    db = get_db(request)
    rows = db.query(
        "SELECT COALESCE(l.model, '') AS model, COALESCE(p.name, '') AS provider, l.success AS success, COUNT(*) AS n, "
        "SUM(l.prompt_tokens) AS pt, SUM(l.completion_tokens) AS ct, SUM(l.cost_usd) AS cost, SUM(l.latency_ms) AS lat "
        "FROM usage_logs l LEFT JOIN providers p ON p.id = l.provider_id GROUP BY 1, 2, 3")
    lab = lambda r: {"model": r["model"], "provider": r["provider"]}  # noqa: E731
    out: list[str] = []
    out += _lines("llmgate_requests_total", "counter", "Inference requests.",
                  [({**lab(r), "result": "success" if r["success"] else "error"}, r["n"]) for r in rows])
    out += _lines("llmgate_tokens_total", "counter", "Tokens processed.",
                  [({**lab(r), "type": t}, r[k] or 0) for r in rows for t, k in (("prompt", "pt"), ("completion", "ct"))])
    out += _lines("llmgate_cost_usd_total", "counter", "Spend at the configured prices.", [(lab(r), round(r["cost"] or 0, 10)) for r in rows])
    out += _lines("llmgate_request_duration_ms_sum", "counter", "Summed request latency in ms.", [(lab(r), round(r["lat"] or 0, 1)) for r in rows])
    providers = db.query("SELECT * FROM providers")
    out += _lines("llmgate_provider_up", "gauge", "1 if the provider is enabled and healthy.",
                  [({"provider": p["name"], "kind": p["kind"]}, int(bool(p["enabled"]) and p["status"] == "healthy")) for p in providers])
    for key, help_ in (("requests_running", "Engine requests running."), ("requests_waiting", "Engine requests queued."),
                       ("kv_cache_usage", "Engine KV-cache usage, 0..1.")):
        samples = []
        for p in providers:
            stats = json.loads(p["engine_stats"]) if p["engine_stats"] else {}
            if key in stats:
                samples.append(({"provider": p["name"]}, stats[key]))
        out += _lines(f"llmgate_engine_{key}", "gauge", help_, samples)
    return "\n".join(out) + "\n"
