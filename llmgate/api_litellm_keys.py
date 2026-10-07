"""LiteLLM-compatible management API, part 1: virtual keys (``/key/*``), teams (``/team/*``), customers (``/customer/*``).

LiteLLM scripts that talk to a proxy with the master key work unchanged: send ``Authorization: Bearer <admin token>``.
A *team* is a llmgate service (``team_id`` is the service name), a *virtual key* is an API key and a *customer* is an
end-user (the ``user`` field of a request).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from .admin_common import get_db, insert_row, parse_duration, prepare_limits, update_row
from .auth import authenticate
from .db import Database, now
from .errors import GatewayError
from .limits import window_start
from .rbac import admin_guard, resolve_principal
from .security import generate_api_key, hash_key
from .usage import period_usage

router = APIRouter(tags=["litellm"])
guard = [Depends(admin_guard)]


def _iso(ts: float | None) -> str | None:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None


def _loads(raw: str | None, default: Any) -> Any:
    import json
    return json.loads(raw) if raw else default


# ------------------------------------------------------------------------------------------- shapes

def key_obj(db: Database, k: dict[str, Any], plaintext: str | None = None) -> dict[str, Any]:
    svc = db.one("SELECT name FROM services WHERE id = ?", (k["service_id"],))
    _, spend = period_usage(db, api_key_id=k["id"], since=window_start(db, "api_keys", k))
    out = {
        "key": plaintext, "key_name": k["key_prefix"] + "…", "token": k["key_hash"], "key_alias": k["name"],
        "team_id": svc["name"] if svc else None, "models": _loads(k["allowed_models"], []),
        "spend": round(spend, 10), "max_budget": k["monthly_budget_usd"], "budget_duration_s": k["budget_duration_s"],
        "budget_reset_at": _iso(k["budget_anchor"] + k["budget_duration_s"]) if k["budget_anchor"] and k["budget_duration_s"] else None,
        "rpm_limit": k["rpm_limit"], "tpm_limit": k["tpm_limit"], "max_parallel_requests": k["max_parallel"],
        "model_max_budget": _loads(k["model_budgets"], {}), "metadata": _loads(k["metadata"], {}),
        "expires": _iso(k["expires_at"]), "blocked": not k["enabled"], "created_at": _iso(k["created_at"]),
        "last_used_at": _iso(k["last_used_at"]),
    }
    return out


def team_obj(db: Database, s: dict[str, Any]) -> dict[str, Any]:
    _, spend = period_usage(db, service_id=s["id"], since=window_start(db, "services", s))
    return {
        "team_id": s["name"], "team_alias": s["name"], "id": s["id"], "description": s["description"],
        "models": _loads(s["allowed_models"], []), "spend": round(spend, 10), "max_budget": s["monthly_budget_usd"],
        "budget_duration_s": s["budget_duration_s"], "rpm_limit": s["rpm_limit"], "tpm_limit": s["tpm_limit"],
        "metadata": _loads(s["metadata"], {}), "blocked": not s["enabled"], "created_at": _iso(s["created_at"]),
        "members": db.scalar("SELECT COUNT(*) FROM api_keys WHERE service_id = ?", (s["id"],), 0),
    }


def customer_obj(db: Database, c: dict[str, Any]) -> dict[str, Any]:
    _, spend = period_usage(db, end_user=c["user_id"], since=window_start(db, "customers", c))
    return {"user_id": c["user_id"], "alias": c["alias"], "spend": round(spend, 10), "blocked": bool(c["blocked"]),
            "litellm_budget_table": {"max_budget": c["max_budget_usd"], "budget_duration_s": c["budget_duration_s"]}}


# --------------------------------------------------------------------------------------------- lookups

def find_key(db: Database, token: str) -> dict[str, Any]:
    """A key by its plaintext (``sk-…``) or by its hash (LiteLLM's ``token``)."""
    row = db.one("SELECT * FROM api_keys WHERE key_hash = ?", (token,)) or db.one(
        "SELECT * FROM api_keys WHERE key_hash = ?", (hash_key(token),))
    if row is None:
        raise GatewayError(404, "Key not found.", type="not_found_error", code="not_found")
    return row


def find_team(db: Database, team_id: str | int) -> dict[str, Any]:
    row = db.one("SELECT * FROM services WHERE name = ?", (str(team_id),)) or (
        db.one("SELECT * FROM services WHERE id = ?", (int(team_id),)) if str(team_id).isdigit() else None)
    if row is None:
        raise GatewayError(404, f"Team '{team_id}' not found.", type="not_found_error", code="not_found")
    return row


def _team_for_key(db: Database, team_id: str | None) -> int:
    if team_id:
        return find_team(db, team_id)["id"]
    row = db.one("SELECT id FROM services WHERE name = 'default'")
    return row["id"] if row else insert_row(db, "services", {"name": "default", "created_at": now()})


# ----------------------------------------------------------------------------------------------- keys

class KeyGenerate(BaseModel):
    key: str | None = Field(default=None, min_length=16, description="Bring your own key string")
    key_alias: str | None = None
    team_id: str | None = None
    models: list[str] = []
    duration: str | int | None = Field(default=None, description="Key lifetime, e.g. '30d'")
    max_budget: float | None = Field(default=None, ge=0)
    budget_duration: str | int | None = None
    rpm_limit: int | None = Field(default=None, gt=0)
    tpm_limit: int | None = Field(default=None, gt=0)
    max_parallel_requests: int | None = Field(default=None, gt=0)
    model_max_budget: dict[str, Any] | None = None
    metadata: dict | None = None
    blocked: bool = False


class KeyUpdate(BaseModel):
    key: str
    key_alias: str | None = None
    models: list[str] | None = None
    max_budget: float | None = Field(default=None, ge=0)
    budget_duration: str | int | None = None
    rpm_limit: int | None = Field(default=None, gt=0)
    tpm_limit: int | None = Field(default=None, gt=0)
    max_parallel_requests: int | None = Field(default=None, gt=0)
    model_max_budget: dict[str, Any] | None = None
    metadata: dict | None = None
    duration: str | int | None = None
    team_id: str | None = None


def _model_budgets(raw: dict[str, Any] | None) -> dict[str, float] | None:
    """Accept both ``{model: 1.0}`` and LiteLLM's ``{model: {"budget_limit": 1.0, "time_period": "1d"}}``."""
    if not raw:
        return None
    return {m: float(v["budget_limit"] if isinstance(v, dict) else v) for m, v in raw.items()}


def _key_columns(b: dict[str, Any]) -> dict[str, Any]:
    """Translate LiteLLM key fields (those present in ``b``) into column changes."""
    cols: dict[str, Any] = {}
    renames = {"key_alias": "name", "models": "allowed_models", "max_budget": "monthly_budget_usd", "budget_duration": "budget_duration",
               "rpm_limit": "rpm_limit", "tpm_limit": "tpm_limit", "max_parallel_requests": "max_parallel",
               "metadata": "metadata"}
    for src, dst in renames.items():
        if src in b:
            cols[dst] = b[src]
    if "model_max_budget" in b:
        cols["model_budgets"] = _model_budgets(b["model_max_budget"])
    if "duration" in b:
        seconds = parse_duration(b["duration"])
        cols["expires_at"] = now() + seconds if seconds else None
    return prepare_limits(cols, "allowed_models", "metadata", "model_budgets")


@router.post("/key/generate", dependencies=guard)
def key_generate(body: KeyGenerate, request: Request) -> dict[str, Any]:
    db = get_db(request)
    prefix = request.app.state.settings.key_prefix
    if body.key:
        plaintext, display, digest = body.key, body.key[: len(prefix) + 6], hash_key(body.key)
    else:
        plaintext, display, digest = generate_api_key(prefix)
    cols = _key_columns(body.model_dump(exclude_unset=True, exclude={"key", "team_id", "blocked"}))
    cols.setdefault("name", display)
    kid = insert_row(db, "api_keys", {**cols, "service_id": _team_for_key(db, body.team_id), "key_prefix": display, "key_hash": digest,
                                       "enabled": int(not body.blocked), "created_at": now()})
    return key_obj(db, db.one("SELECT * FROM api_keys WHERE id = ?", (kid,)), plaintext)  # type: ignore[arg-type]


@router.post("/key/update", dependencies=guard)
def key_update(body: KeyUpdate, request: Request) -> dict[str, Any]:
    db = get_db(request)
    row = find_key(db, body.key)
    changes = _key_columns(body.model_dump(exclude_unset=True, exclude={"key", "team_id"}))
    if body.team_id:
        changes["service_id"] = find_team(db, body.team_id)["id"]
    update_row(db, "api_keys", row["id"], changes)
    return key_obj(db, db.one("SELECT * FROM api_keys WHERE id = ?", (row["id"],)))  # type: ignore[arg-type]


class KeyDelete(BaseModel):
    keys: list[str] = []
    key_aliases: list[str] = []


@router.post("/key/delete", dependencies=guard)
def key_delete(body: KeyDelete, request: Request) -> dict[str, Any]:
    db = get_db(request)
    rows = [find_key(db, k) for k in body.keys]
    for alias in body.key_aliases:
        rows += db.query("SELECT * FROM api_keys WHERE name = ?", (alias,))
    for r in rows:
        db.execute("DELETE FROM api_keys WHERE id = ?", (r["id"],))
    return {"deleted_keys": [r["key_hash"] for r in rows]}


class KeyRef(BaseModel):
    key: str


@router.post("/key/block", dependencies=guard)
def key_block(body: KeyRef, request: Request) -> dict[str, Any]:
    return _set_blocked(request, body.key, True)


@router.post("/key/unblock", dependencies=guard)
def key_unblock(body: KeyRef, request: Request) -> dict[str, Any]:
    return _set_blocked(request, body.key, False)


def _set_blocked(request: Request, token: str, blocked: bool) -> dict[str, Any]:
    db = get_db(request)
    row = find_key(db, token)
    update_row(db, "api_keys", row["id"], {"enabled": int(not blocked)})
    return key_obj(db, db.one("SELECT * FROM api_keys WHERE id = ?", (row["id"],)))  # type: ignore[arg-type]


@router.post("/key/regenerate", dependencies=guard)
def key_regenerate(body: KeyRef, request: Request) -> dict[str, Any]:
    db = get_db(request)
    row = find_key(db, body.key)
    plaintext, display, digest = generate_api_key(request.app.state.settings.key_prefix)
    update_row(db, "api_keys", row["id"], {"key_prefix": display, "key_hash": digest})
    return key_obj(db, db.one("SELECT * FROM api_keys WHERE id = ?", (row["id"],)), plaintext)  # type: ignore[arg-type]


@router.get("/key/info")
async def key_info(request: Request, key: str | None = None) -> dict[str, Any]:
    """Any admin may look up a key; a virtual key may look up itself (no ``key`` argument)."""
    db = get_db(request)
    principal = await resolve_principal(request)
    if principal is not None:
        row = find_key(db, key) if key else None
        if row is None:
            raise GatewayError(400, "Pass ?key=<key or token> (an admin has no key of their own).", code="missing_key")
    else:
        auth = await authenticate(request)  # a virtual key calling about itself
        row = auth.key
        if key and hash_key(key) != row["key_hash"] and key != row["key_hash"]:
            raise GatewayError(403, "A key can only read its own info.", type="permission_error", code="forbidden")
    return {"key": row["key_hash"], "info": key_obj(db, row)}


@router.get("/key/list", dependencies=guard)
def key_list(request: Request, team_id: str | None = None, key_alias: str | None = None, return_full_object: bool = False,
             page: int = Query(1, ge=1), size: int = Query(50, ge=1, le=500)) -> dict[str, Any]:
    db = get_db(request)
    where, params = [], []
    if team_id:
        where.append("service_id = ?")
        params.append(find_team(db, team_id)["id"])
    if key_alias:
        where.append("name = ?")
        params.append(key_alias)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = db.scalar(f"SELECT COUNT(*) FROM api_keys{clause}", params, 0)
    rows = db.query(f"SELECT * FROM api_keys{clause} ORDER BY id LIMIT ? OFFSET ?", [*params, size, (page - 1) * size])
    return {"keys": [key_obj(db, r) if return_full_object else r["key_hash"] for r in rows], "total_count": total,
            "current_page": page, "total_pages": max(1, -(-total // size))}


# ----------------------------------------------------------------------------------------------- teams

class TeamNew(BaseModel):
    team_alias: str | None = None
    team_id: str | None = None
    max_budget: float | None = Field(default=None, ge=0)
    budget_duration: str | int | None = None
    models: list[str] = []
    rpm_limit: int | None = Field(default=None, gt=0)
    tpm_limit: int | None = Field(default=None, gt=0)
    metadata: dict | None = None
    blocked: bool = False


class TeamUpdate(TeamNew):
    team_id: str  # type: ignore[assignment]


def _team_columns(b: dict[str, Any]) -> dict[str, Any]:
    renames = {"max_budget": "monthly_budget_usd", "budget_duration": "budget_duration", "models": "allowed_models",
               "rpm_limit": "rpm_limit", "tpm_limit": "tpm_limit", "metadata": "metadata"}
    cols = {dst: b[src] for src, dst in renames.items() if src in b}
    if "blocked" in b:
        cols["enabled"] = int(not b["blocked"])
    return prepare_limits(cols, "allowed_models", "metadata")


@router.post("/team/new", dependencies=guard)
def team_new(body: TeamNew, request: Request) -> dict[str, Any]:
    db = get_db(request)
    name = body.team_id or body.team_alias
    if not name:
        raise GatewayError(400, "Provide team_alias (or team_id).", code="missing_team")
    sid = insert_row(db, "services", {**_team_columns(body.model_dump(exclude_unset=True, exclude={"team_id", "team_alias"})),
                                       "name": name, "created_at": now()})
    return team_obj(db, db.one("SELECT * FROM services WHERE id = ?", (sid,)))  # type: ignore[arg-type]


@router.post("/team/update", dependencies=guard)
def team_update(body: TeamUpdate, request: Request) -> dict[str, Any]:
    db = get_db(request)
    row = find_team(db, body.team_id)
    cols = _team_columns(body.model_dump(exclude_unset=True, exclude={"team_id", "team_alias"}))
    if body.team_alias and body.team_alias != row["name"]:
        cols["name"] = body.team_alias
    update_row(db, "services", row["id"], cols)
    return team_obj(db, db.one("SELECT * FROM services WHERE id = ?", (row["id"],)))  # type: ignore[arg-type]


class TeamDelete(BaseModel):
    team_ids: list[str]


@router.post("/team/delete", dependencies=guard)
def team_delete(body: TeamDelete, request: Request) -> dict[str, Any]:
    db = get_db(request)
    rows = [find_team(db, t) for t in body.team_ids]
    for r in rows:
        db.execute("DELETE FROM services WHERE id = ?", (r["id"],))
    return {"deleted_teams": [r["name"] for r in rows]}


class TeamRef(BaseModel):
    team_id: str


@router.post("/team/block", dependencies=guard)
def team_block(body: TeamRef, request: Request) -> dict[str, Any]:
    return _team_blocked(request, body.team_id, True)


@router.post("/team/unblock", dependencies=guard)
def team_unblock(body: TeamRef, request: Request) -> dict[str, Any]:
    return _team_blocked(request, body.team_id, False)


def _team_blocked(request: Request, team_id: str, blocked: bool) -> dict[str, Any]:
    db = get_db(request)
    row = find_team(db, team_id)
    update_row(db, "services", row["id"], {"enabled": int(not blocked)})
    return team_obj(db, db.one("SELECT * FROM services WHERE id = ?", (row["id"],)))  # type: ignore[arg-type]


@router.get("/team/info", dependencies=guard)
def team_info(request: Request, team_id: str) -> dict[str, Any]:
    db = get_db(request)
    row = find_team(db, team_id)
    keys = db.query("SELECT * FROM api_keys WHERE service_id = ? ORDER BY id", (row["id"],))
    return {"team_id": row["name"], "team_info": team_obj(db, row), "keys": [key_obj(db, k) for k in keys]}


@router.get("/team/list", dependencies=guard)
def team_list(request: Request) -> list[dict[str, Any]]:
    db = get_db(request)
    return [team_obj(db, s) for s in db.query("SELECT * FROM services ORDER BY name")]


# -------------------------------------------------------------------------------------------- customers

class CustomerNew(BaseModel):
    user_id: str
    alias: str | None = None
    max_budget: float | None = Field(default=None, ge=0)
    budget_duration: str | int | None = None
    blocked: bool = False


class CustomerIds(BaseModel):
    user_ids: list[str]


def _customer(db: Database, user_id: str) -> dict[str, Any]:
    row = db.one("SELECT * FROM customers WHERE user_id = ?", (user_id,))
    if row is None:
        raise GatewayError(404, f"Customer '{user_id}' not found.", type="not_found_error", code="not_found")
    return row


@router.post("/customer/new", dependencies=guard)
def customer_new(body: CustomerNew, request: Request) -> dict[str, Any]:
    db = get_db(request)
    cols = prepare_limits({"user_id": body.user_id, "alias": body.alias, "max_budget_usd": body.max_budget,
                           "budget_duration": body.budget_duration, "blocked": int(body.blocked), "created_at": now()})
    insert_row(db, "customers", cols)
    return customer_obj(db, _customer(db, body.user_id))


@router.post("/customer/update", dependencies=guard)
def customer_update(body: CustomerNew, request: Request) -> dict[str, Any]:
    db = get_db(request)
    row = _customer(db, body.user_id)
    given = body.model_dump(exclude_unset=True)
    cols: dict[str, Any] = {}
    for src, dst in (("alias", "alias"), ("max_budget", "max_budget_usd"), ("budget_duration", "budget_duration")):
        if src in given:
            cols[dst] = given[src]
    if "blocked" in given:
        cols["blocked"] = int(body.blocked)
    update_row(db, "customers", row["id"], prepare_limits(cols))
    return customer_obj(db, _customer(db, body.user_id))


@router.post("/customer/delete", dependencies=guard)
def customer_delete(body: CustomerIds, request: Request) -> dict[str, Any]:
    db = get_db(request)
    for u in body.user_ids:
        db.execute("DELETE FROM customers WHERE id = ?", (_customer(db, u)["id"],))
    return {"deleted_customers": body.user_ids}


@router.post("/customer/block", dependencies=guard)
def customer_block(body: CustomerIds, request: Request) -> dict[str, Any]:
    return _customers_blocked(request, body.user_ids, True)


@router.post("/customer/unblock", dependencies=guard)
def customer_unblock(body: CustomerIds, request: Request) -> dict[str, Any]:
    return _customers_blocked(request, body.user_ids, False)


def _customers_blocked(request: Request, user_ids: list[str], blocked: bool) -> dict[str, Any]:
    db = get_db(request)
    for u in user_ids:
        update_row(db, "customers", _customer(db, u)["id"], {"blocked": int(blocked)})
    return {"blocked_users" if blocked else "unblocked_users": user_ids}


@router.get("/customer/info", dependencies=guard)
def customer_info(request: Request, end_user_id: str) -> dict[str, Any]:
    db = get_db(request)
    return customer_obj(db, _customer(db, end_user_id))


@router.get("/customer/list", dependencies=guard)
def customer_list(request: Request) -> list[dict[str, Any]]:
    db = get_db(request)
    return [customer_obj(db, c) for c in db.query("SELECT * FROM customers ORDER BY user_id")]
