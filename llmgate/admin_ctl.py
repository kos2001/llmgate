"""Admin API for end-user budgets, model aliases, guardrails and the response cache."""

from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from . import guardrails as gr
from .admin_common import (ADMIN_DEPS, bool_cols, encode_json, flags, get_db, insert_row, not_found, prepare_limits,
                           require_row, update_row)
from .db import Database, now
from .errors import GatewayError
from .limits import window_start
from .schemas import AliasIn, CustomerIn, CustomerUpdate, GuardrailIn, GuardrailUpdate
from .usage import period_usage

router = APIRouter(prefix="/admin/api", tags=["controls"], dependencies=ADMIN_DEPS)


# ------------------------------------------------------------------------------ customers (end users)

def _customer(db: Database, c: dict[str, Any]) -> dict[str, Any]:
    flags(c, "blocked")
    tokens, cost = period_usage(db, end_user=c["user_id"], since=window_start(db, "customers", c))
    c["usage"] = {"tokens": tokens, "cost_usd": cost}
    return c


@router.get("/customers")
def list_customers(request: Request) -> list[dict[str, Any]]:
    db = get_db(request)
    known = [_customer(db, c) for c in db.query("SELECT * FROM customers ORDER BY user_id")]
    seen = {c["user_id"] for c in known}
    # end-users that only ever appeared in requests (no budget row) are listed too, with their spend
    for r in db.query("SELECT end_user, SUM(total_tokens) AS tokens, SUM(cost_usd) AS cost FROM usage_logs "
                      "WHERE end_user IS NOT NULL GROUP BY end_user ORDER BY cost DESC LIMIT 200"):
        if r["end_user"] not in seen:
            known.append({"id": None, "user_id": r["end_user"], "alias": None, "max_budget_usd": None, "budget_duration_s": None,
                          "blocked": False, "usage": {"tokens": r["tokens"] or 0, "cost_usd": r["cost"] or 0}, "unregistered": True})
    return known


@router.post("/customers", status_code=201)
def create_customer(body: CustomerIn, request: Request) -> dict[str, Any]:
    db = get_db(request)
    values = prepare_limits(body.model_dump())
    cid = insert_row(db, "customers", bool_cols({**values, "created_at": now()}, "blocked"))
    return _customer(db, require_row(db, "customers", cid, "Customer"))


@router.patch("/customers/{user_id:path}")
def update_customer(user_id: str, body: CustomerUpdate, request: Request) -> dict[str, Any]:
    db = get_db(request)
    row = db.one("SELECT * FROM customers WHERE user_id = ?", (user_id,))
    if row is None:
        raise not_found("Customer")
    update_row(db, "customers", row["id"], bool_cols(prepare_limits(body.model_dump(exclude_unset=True)), "blocked"))
    return _customer(db, require_row(db, "customers", row["id"], "Customer"))


@router.delete("/customers/{user_id:path}", status_code=204)
def delete_customer(user_id: str, request: Request) -> None:
    if get_db(request).execute("DELETE FROM customers WHERE user_id = ?", (user_id,)).rowcount == 0:
        raise not_found("Customer")


# ------------------------------------------------------------------------------------------ aliases

@router.get("/aliases")
def list_aliases(request: Request) -> list[dict[str, Any]]:
    return get_db(request).query("SELECT * FROM model_aliases ORDER BY alias")


@router.post("/aliases", status_code=201)
def set_alias(body: AliasIn, request: Request) -> dict[str, Any]:
    db = get_db(request)
    if db.one("SELECT 1 FROM models WHERE name = ?", (body.model_name,)) is None:
        raise GatewayError(404, f"Model '{body.model_name}' is not in the registry.", type="not_found_error", code="model_not_found")
    if db.one("SELECT 1 FROM models WHERE name = ?", (body.alias,)):
        raise GatewayError(409, "An alias cannot shadow a real model name.", code="conflict")
    db.execute("INSERT INTO model_aliases (alias, model_name) VALUES (?, ?) ON CONFLICT(alias) DO UPDATE SET model_name = excluded.model_name",
               (body.alias, body.model_name))
    return body.model_dump()


@router.delete("/aliases/{alias:path}", status_code=204)
def delete_alias(alias: str, request: Request) -> None:
    if get_db(request).execute("DELETE FROM model_aliases WHERE alias = ?", (alias,)).rowcount == 0:
        raise not_found("Alias")


# ---------------------------------------------------------------------------------------- guardrails

def _rule(row: dict[str, Any]) -> dict[str, Any]:
    flags(row, "enabled", "default_on")
    row["config"] = json.loads(row["config"]) if row["config"] else {}
    return row


def _validate_config(kind: str, config: dict[str, Any]) -> None:
    if kind == "regex":
        try:
            re.compile(config.get("pattern", ""))
        except re.error as exc:
            raise GatewayError(400, f"Invalid regex: {exc}", code="invalid_guardrail") from None
        if not config.get("pattern"):
            raise GatewayError(400, "A 'regex' guardrail needs config.pattern.", code="invalid_guardrail")
    if kind == "keywords" and not config.get("words"):
        raise GatewayError(400, "A 'keywords' guardrail needs config.words.", code="invalid_guardrail")
    for field in ("entities",):
        known = set(gr.PII) if kind == "pii" else set(gr.SECRETS)
        bad = set(config.get(field) or []) - known
        if kind in ("pii", "secrets") and bad:
            raise GatewayError(400, f"Unknown {kind} entities: {sorted(bad)}. Known: {sorted(known)}.", code="invalid_guardrail")


@router.get("/guardrails")
def list_guardrails(request: Request) -> list[dict[str, Any]]:
    return [_rule(r) for r in get_db(request).query("SELECT * FROM guardrails ORDER BY id")]


@router.post("/guardrails", status_code=201)
def create_guardrail(body: GuardrailIn, request: Request) -> dict[str, Any]:
    _validate_config(body.kind, body.config)
    db = get_db(request)
    values = bool_cols(encode_json({**body.model_dump(), "created_at": now()}, "config"), "enabled", "default_on")
    return _rule(require_row(db, "guardrails", insert_row(db, "guardrails", values), "Guardrail"))


@router.patch("/guardrails/{rule_id}")
def update_guardrail(rule_id: int, body: GuardrailUpdate, request: Request) -> dict[str, Any]:
    db = get_db(request)
    row = require_row(db, "guardrails", rule_id, "Guardrail")
    changes = body.model_dump(exclude_unset=True)
    if "config" in changes:
        _validate_config(row["kind"], changes["config"] or {})
    update_row(db, "guardrails", rule_id, bool_cols(encode_json(changes, "config"), "enabled", "default_on"))
    return _rule(require_row(db, "guardrails", rule_id, "Guardrail"))


@router.delete("/guardrails/{rule_id}", status_code=204)
def delete_guardrail(rule_id: int, request: Request) -> None:
    db = get_db(request)
    require_row(db, "guardrails", rule_id, "Guardrail")
    db.execute("DELETE FROM guardrails WHERE id = ?", (rule_id,))


class TryText(BaseModel):
    text: str
    rule_id: int | None = None  # None = every enabled default-on rule


@router.post("/guardrails/test")
def test_guardrails(body: TryText, request: Request) -> dict[str, Any]:
    """Dry-run text through the rules (input side) without calling any model."""
    db = get_db(request)
    rules = gr.load_rules(db)
    if body.rule_id is not None:
        row = require_row(db, "guardrails", body.rule_id, "Guardrail")
        rules = [r for r in gr.load_rules(db, [row["name"]]) if r.id == body.rule_id]
    out: list[dict[str, Any]] = []
    masked = body.text
    for rule in rules:
        text, labels = gr._scan(rule, masked)
        if labels:
            out.append({"rule": rule.name, "mode": rule.mode, "found": sorted(set(labels))})
            if rule.mode == "mask":
                masked = text
    blocked = any(h["mode"] == "block" for h in out)
    return {"blocked": blocked, "hits": out, "masked_text": None if blocked else masked}


# ------------------------------------------------------------------------------------------- cache

@router.get("/cache")
def cache_stats(request: Request) -> dict[str, Any]:
    s = request.app.state
    return {**s.cache.stats(), "ttl_s": s.settings.cache_ttl_s, "scope": s.settings.cache_scope}


@router.post("/cache/flush")
def cache_flush(request: Request) -> dict[str, int]:
    return {"flushed": request.app.state.cache.flush()}
