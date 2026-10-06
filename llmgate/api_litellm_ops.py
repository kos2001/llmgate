"""LiteLLM-compatible management API, part 2: spend reports, model deployments, health, utilities, config import."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from . import usage
from .admin_common import get_db, update_row
from .api_litellm_keys import find_key
from .errors import GatewayError
from .health import apply_check, check_provider
from .litellm_config import add_deployment, import_config, parse_yaml
from .rbac import admin_guard
from .security import hash_key
from .tokens import content_text, estimate_tokens

router = APIRouter(tags=["litellm"])
guard = [Depends(admin_guard)]
KIND_PREFIX = {"vllm": "hosted_vllm", "ollama": "ollama", "tgi": "huggingface", "openai": "openai", "other": "openai"}


def _day(value: str | None, end: bool = False) -> float | None:
    if not value:
        return None
    try:
        d = datetime.strptime(value[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise GatewayError(400, f"Dates are YYYY-MM-DD, got {value!r}.", code="invalid_date") from None
    return (d + timedelta(days=1 if end else 0)).timestamp()  # end_date is inclusive


# --------------------------------------------------------------------------------------------- spend

@router.get("/spend/logs", dependencies=guard)
def spend_logs(request: Request, api_key: str | None = None, user_id: str | None = None, request_id: str | None = None,
               team_id: str | None = None, start_date: str | None = None, end_date: str | None = None,
               limit: int = Query(100, ge=1, le=1000)) -> list[dict[str, Any]]:
    db = get_db(request)
    where, params = ["1 = 1"], []
    for clause, value in (("l.end_user = ?", user_id), ("l.request_id = ?", request_id), ("s.name = ?", team_id),
                          ("l.ts >= ?", _day(start_date)), ("l.ts < ?", _day(end_date, True))):
        if value is not None:
            where.append(clause)
            params.append(value)
    if api_key:
        where.append("k.id = ?")
        params.append(find_key(db, api_key)["id"])
    rows = db.query(
        "SELECT l.*, k.key_hash, k.name AS key_alias, s.name AS team, p.base_url AS api_base FROM usage_logs l "
        "LEFT JOIN api_keys k ON k.id = l.api_key_id LEFT JOIN services s ON s.id = l.service_id "
        "LEFT JOIN providers p ON p.id = l.provider_id WHERE " + " AND ".join(where) + " ORDER BY l.ts DESC LIMIT ?", [*params, limit])
    iso = lambda t: datetime.fromtimestamp(t, tz=timezone.utc).isoformat()  # noqa: E731
    return [{
        "request_id": r["request_id"], "call_type": r["endpoint"], "api_key": r["key_hash"], "model": r["model"], "spend": r["cost_usd"],
        "total_tokens": r["total_tokens"], "prompt_tokens": r["prompt_tokens"], "completion_tokens": r["completion_tokens"],
        "startTime": iso(r["ts"] - (r["latency_ms"] or 0) / 1000), "endTime": iso(r["ts"]), "user": r["end_user"] or "",
        "team_id": r["team"], "api_base": r["api_base"], "cache_hit": "True" if r["cache_hit"] else "False",
        "metadata": {"user_api_key_alias": r["key_alias"], "tags": _tags(r["tags"]), "status": "success" if r["success"] else "failure",
                     "guardrail": r["guardrail"], "error": r["error"]},
    } for r in rows]


def _tags(raw: str | None) -> list[str]:
    import json
    return json.loads(raw) if raw else []


@router.get("/spend/tags", dependencies=guard)
def spend_tags(request: Request, start_date: str | None = None, end_date: str | None = None) -> list[dict[str, Any]]:
    rows = usage.tag_summary(get_db(request), since=_day(start_date), until=_day(end_date, True))
    return [{"individual_request_tag": r["name"], "log_count": r["requests"], "total_spend": r["cost_usd"]} for r in rows]


@router.get("/global/spend/report", dependencies=guard)
def spend_report(request: Request, group_by: str = Query("team", pattern="^(team|customer|api_key)$"), start_date: str | None = None,
                 end_date: str | None = None) -> list[dict[str, Any]]:
    mapping = {"team": "service", "customer": "user", "api_key": "key"}
    rows = usage.summary(get_db(request), mapping[group_by], since=_day(start_date), until=_day(end_date, True))
    return [{"group": r["name"], "total_spend": r["cost_usd"], "total_tokens": r["total_tokens"], "api_requests": r["requests"]} for r in rows]


@router.get("/global/spend/models", dependencies=guard)
def spend_models(request: Request, limit: int = Query(10, ge=1, le=100), start_date: str | None = None, end_date: str | None = None):
    rows = usage.summary(get_db(request), "model", since=_day(start_date), until=_day(end_date, True), limit=limit)
    return [{"model": r["name"], "total_spend": r["cost_usd"], "total_tokens": r["total_tokens"]} for r in rows]


@router.get("/global/spend/keys", dependencies=guard)
def spend_keys(request: Request, limit: int = Query(10, ge=1, le=100)) -> list[dict[str, Any]]:
    db = get_db(request)
    return db.query("SELECT k.key_hash AS api_key, k.name AS key_alias, COALESCE(SUM(l.cost_usd), 0) AS total_spend "
                    "FROM api_keys k LEFT JOIN usage_logs l ON l.api_key_id = k.id GROUP BY k.id ORDER BY total_spend DESC LIMIT ?", (limit,))


@router.get("/global/spend/logs", dependencies=guard)
def spend_daily(request: Request, start_date: str | None = None, end_date: str | None = None) -> list[dict[str, Any]]:
    rows = usage.timeseries(get_db(request), "day", since=_day(start_date) or time.time() - 30 * 86400, until=_day(end_date, True))
    return [{"date": r["bucket"][:10], "spend": r["cost_usd"]} for r in rows]


@router.get("/global/activity", dependencies=guard)
def activity(request: Request, start_date: str | None = None, end_date: str | None = None) -> dict[str, Any]:
    rows = usage.timeseries(get_db(request), "day", since=_day(start_date) or time.time() - 30 * 86400, until=_day(end_date, True))
    daily = [{"date": r["bucket"][:10], "api_requests": r["requests"], "total_tokens": r["total_tokens"],
              "prompt_tokens": r["prompt_tokens"], "completion_tokens": r["completion_tokens"]} for r in rows]
    return {"daily_data": daily, "sum_api_requests": sum(d["api_requests"] for d in daily), "sum_total_tokens": sum(d["total_tokens"] for d in daily)}


# --------------------------------------------------------------------------------------------- models

def _deployments(db: Any) -> list[dict[str, Any]]:
    import json
    out = []
    for r in db.query(
            "SELECT r.id AS route_id, r.upstream_model, r.priority, r.weight, r.rpm_limit, r.tpm_limit, r.max_parallel, "
            " r.input_price_per_m AS r_in, r.output_price_per_m AS r_out, m.*, p.base_url, p.kind AS provider_kind, p.name AS provider_name "
            "FROM model_routes r JOIN models m ON m.id = r.model_id JOIN providers p ON p.id = r.provider_id ORDER BY m.name, r.priority, r.id"):
        params = json.loads(r["supported_parameters"]) if r["supported_parameters"] else []
        modal = json.loads(r["input_modalities"]) if r["input_modalities"] else ["text"]
        pin = r["r_in"] if r["r_in"] is not None else r["input_price_per_m"]
        pout = r["r_out"] if r["r_out"] is not None else r["output_price_per_m"]
        out.append({
            "model_name": r["name"],
            "litellm_params": {"model": f"{KIND_PREFIX.get(r['provider_kind'], 'openai')}/{r['upstream_model']}", "api_base": r["base_url"],
                               "rpm": r["rpm_limit"], "tpm": r["tpm_limit"], "max_parallel_requests": r["max_parallel"],
                               "order": r["priority"], "weight": r["weight"]},
            "model_info": {"id": str(r["route_id"]), "db_model": True, "mode": r["kind"], "provider": r["provider_name"],
                           "input_cost_per_token": pin / 1e6, "output_cost_per_token": pout / 1e6, "max_input_tokens": r["context_length"],
                           "max_output_tokens": r["max_completion_tokens"], "supports_function_calling": "tools" in params,
                           "supports_vision": "image" in modal, "routing_strategy": r["routing_strategy"]},
        })
    return out


@router.get("/model/info", dependencies=guard)
@router.get("/v1/model/info", dependencies=guard)
def model_info(request: Request, litellm_model_id: str | None = None) -> dict[str, Any]:
    data = _deployments(get_db(request))
    if litellm_model_id:
        data = [d for d in data if d["model_info"]["id"] == litellm_model_id]
    return {"data": data}


class DeploymentIn(BaseModel):
    model_name: str
    litellm_params: dict[str, Any]
    model_info: dict[str, Any] = {}


@router.post("/model/new", dependencies=guard)
def model_new(body: DeploymentIn, request: Request) -> dict[str, Any]:
    db = get_db(request)
    with db.transaction():
        made = add_deployment(db, body.model_dump())
    return {"model_id": str(made["route_id"]), **made}


class ModelRef(BaseModel):
    id: str


@router.post("/model/delete", dependencies=guard)
def model_delete(body: ModelRef, request: Request) -> dict[str, Any]:
    db = get_db(request)
    if not body.id.isdigit() or db.execute("DELETE FROM model_routes WHERE id = ?", (int(body.id),)).rowcount == 0:
        raise GatewayError(404, f"Deployment '{body.id}' not found.", type="not_found_error", code="not_found")
    return {"message": f"Model: {body.id} deleted successfully"}


class DeploymentUpdate(BaseModel):
    model_info: dict[str, Any]
    litellm_params: dict[str, Any] = {}


@router.post("/model/update", dependencies=guard)
def model_update(body: DeploymentUpdate, request: Request) -> dict[str, Any]:
    db = get_db(request)
    rid = str(body.model_info.get("id", ""))
    route = db.one("SELECT * FROM model_routes WHERE id = ?", (int(rid),)) if rid.isdigit() else None
    if route is None:
        raise GatewayError(404, f"Deployment '{rid}' not found.", type="not_found_error", code="not_found")
    p, cols = body.litellm_params, {}
    for src, dst in (("rpm", "rpm_limit"), ("tpm", "tpm_limit"), ("max_parallel_requests", "max_parallel"), ("order", "priority"), ("weight", "weight")):
        if src in p:
            cols[dst] = p[src]
    if "model" in p:
        cols["upstream_model"] = str(p["model"]).split("/", 1)[-1]
    update_row(db, "model_routes", route["id"], cols)
    return {"message": "Model updated successfully", "model_id": rid}


# ------------------------------------------------------------------------------------------- health

@router.get("/health", dependencies=guard)
async def health(request: Request, live: bool = False) -> dict[str, Any]:
    """Per-endpoint health. ``?live=true`` probes every provider now; otherwise the last stored probe is reported."""
    st = request.app.state
    if live:
        for p in st.db.query("SELECT * FROM providers WHERE enabled = 1"):
            apply_check(st.db, p["id"], await check_provider(st.client, p, st.settings), st.alerter)
    healthy, unhealthy = [], []
    for d in _deployments(st.db):
        status = st.db.scalar("SELECT status FROM providers WHERE name = ?", (d["model_info"]["provider"],), "unknown")
        entry = {"model": d["litellm_params"]["model"], "api_base": d["litellm_params"]["api_base"], "model_name": d["model_name"]}
        (unhealthy if status == "unhealthy" else healthy).append(entry if status != "unhealthy" else {**entry, "error": "provider unhealthy"})
    return {"healthy_endpoints": healthy, "unhealthy_endpoints": unhealthy, "healthy_count": len(healthy), "unhealthy_count": len(unhealthy)}


@router.get("/health/liveliness")
@router.get("/health/liveness")
async def liveliness() -> str:
    return "I'm alive!"


@router.get("/health/readiness")
async def readiness(request: Request) -> dict[str, Any]:
    st = request.app.state
    try:
        st.db.scalar("SELECT 1")
        db_state = "connected"
    except Exception:
        raise GatewayError(503, "Database unavailable.", type="server_error", code="db_unavailable") from None
    return {"status": "healthy", "db": db_state, "cache": "enabled" if st.settings.cache_ttl_s else "disabled",
            "success_callbacks": ["webhook"] if st.broadcast.enabled else [], "providers": st.db.scalar("SELECT COUNT(*) FROM providers", default=0)}


# -------------------------------------------------------------------------------------------- utils

class TokenCount(BaseModel):
    model: str | None = None
    messages: list[dict[str, Any]] | None = None
    prompt: str | None = None
    text: str | None = None


@router.post("/utils/token_counter", dependencies=guard)
def token_counter(body: TokenCount) -> dict[str, Any]:
    """Estimate prompt tokens (~4 chars/token, ~1 per CJK character). Engines report exact counts in real responses."""
    if body.messages is not None:
        total = sum(4 + estimate_tokens(content_text(m.get("content"))) for m in body.messages) + 3
    else:
        total = estimate_tokens(body.prompt or body.text or "")
    return {"total_tokens": total, "request_model": body.model, "model_used": body.model, "tokenizer_type": "estimate"}


# ------------------------------------------------------------------------------------ config import

class ImportBody(BaseModel):
    yaml: str | None = None
    config: dict[str, Any] | None = None
    dry_run: bool = False


@router.post("/admin/api/import/litellm", tags=["litellm"], dependencies=guard)
def import_litellm(body: ImportBody, request: Request) -> dict[str, Any]:
    """Load a LiteLLM ``config.yaml`` (as text or parsed JSON) into the registry."""
    cfg = body.config if body.config is not None else parse_yaml(body.yaml or "")
    return {**import_config(get_db(request), cfg, body.dry_run), "dry_run": body.dry_run}
