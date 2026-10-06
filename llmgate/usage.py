"""Usage accounting: write one row per request, aggregate for the dashboard."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from .db import Database


def month_start(ts: float | None = None) -> float:
    d = datetime.fromtimestamp(ts if ts is not None else time.time(), tz=timezone.utc)
    return d.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()


def compute_cost(model: dict[str, Any], prompt_tokens: int, completion_tokens: int) -> float:
    return (prompt_tokens * model["input_price_per_m"] + completion_tokens * model["output_price_per_m"]) / 1_000_000


def record_usage(db: Database, **f: Any) -> None:
    f.setdefault("ts", time.time())
    f["total_tokens"] = f.get("prompt_tokens", 0) + f.get("completion_tokens", 0)
    cols = list(f)
    db.execute(
        f"INSERT INTO usage_logs ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
        [f[c] for c in cols],
    )


def period_usage(db: Database, *, since: float, api_key_id: int | None = None, service_id: int | None = None,
                 model: str | None = None, end_user: str | None = None) -> tuple[int, float]:
    clauses, params = ["ts >= ?", "cache_hit = 0"], [since]
    for sql, value in (("api_key_id = ?", api_key_id), ("service_id = ?", service_id), ("model = ?", model),
                       ("end_user = ?", end_user)):
        if value is not None:
            clauses.append(sql)
            params.append(value)
    row = db.one(
        f"SELECT COALESCE(SUM(total_tokens), 0) AS tokens, COALESCE(SUM(cost_usd), 0) AS cost "
        f"FROM usage_logs WHERE {' AND '.join(clauses)}", params,
    )
    return int(row["tokens"]), float(row["cost"])


# --------------------------------------------------------------------------- queries

# group_by name -> (select expression, extra join). Whitelisted so user input never reaches SQL.
_GROUPS: dict[str, tuple[str, str]] = {
    "model": ("COALESCE(l.model, '(unknown)')", ""),
    "service": ("COALESCE(s.name, '(deleted)')", "LEFT JOIN services s ON s.id = l.service_id"),
    "key": ("COALESCE(s.name || ' / ' || k.name, '(deleted)')",
            "LEFT JOIN api_keys k ON k.id = l.api_key_id LEFT JOIN services s ON s.id = k.service_id"),
    "provider": ("COALESCE(p.name, '(none)')", "LEFT JOIN providers p ON p.id = l.provider_id"),
    "endpoint": ("l.endpoint", ""),
    "app": ("COALESCE(l.app_title, '(none)')", ""),
    "user": ("COALESCE(l.end_user, '(none)')", ""),
}

_AGG = (
    "COUNT(*) AS requests, "
    "COALESCE(SUM(1 - l.success), 0) AS errors, "
    "COALESCE(SUM(l.prompt_tokens), 0) AS prompt_tokens, "
    "COALESCE(SUM(l.completion_tokens), 0) AS completion_tokens, "
    "COALESCE(SUM(l.total_tokens), 0) AS total_tokens, "
    "COALESCE(SUM(l.cost_usd), 0) AS cost_usd, "
    "AVG(l.latency_ms) AS avg_latency_ms, "
    "AVG(l.ttft_ms) AS avg_ttft_ms, "
    "COALESCE(SUM(l.cache_hit), 0) AS cache_hits"
)


def _filters(since: float | None, until: float | None, service_id: int | None, api_key_id: int | None,
             model: str | None, provider_id: int | None = None) -> tuple[str, list[Any]]:
    clauses, params = [], []
    for sql, value in (
        ("l.ts >= ?", since), ("l.ts < ?", until), ("l.service_id = ?", service_id),
        ("l.api_key_id = ?", api_key_id), ("l.model = ?", model), ("l.provider_id = ?", provider_id),
    ):
        if value is not None:
            clauses.append(sql)
            params.append(value)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def summary(db: Database, group_by: str, *, since: float | None = None, until: float | None = None,
            service_id: int | None = None, api_key_id: int | None = None, model: str | None = None,
            limit: int = 100) -> list[dict[str, Any]]:
    expr, join = _GROUPS[group_by]
    where, params = _filters(since, until, service_id, api_key_id, model)
    return db.query(
        f"SELECT {expr} AS name, {_AGG} FROM usage_logs l {join}{where} "
        f"GROUP BY {expr} ORDER BY total_tokens DESC, requests DESC LIMIT ?",
        [*params, limit],
    )


def totals(db: Database, *, since: float | None = None, until: float | None = None,
           service_id: int | None = None, api_key_id: int | None = None, model: str | None = None) -> dict[str, Any]:
    where, params = _filters(since, until, service_id, api_key_id, model)
    return db.one(f"SELECT {_AGG} FROM usage_logs l{where}", params) or {}


_BUCKETS = {"hour": "%Y-%m-%dT%H:00:00Z", "day": "%Y-%m-%dT00:00:00Z"}


def timeseries(db: Database, bucket: str, *, since: float, until: float | None = None,
               split: str | None = None, service_id: int | None = None, api_key_id: int | None = None,
               model: str | None = None) -> list[dict[str, Any]]:
    fmt = _BUCKETS[bucket]
    where, params = _filters(since, until, service_id, api_key_id, model)
    if split:
        expr, join = _GROUPS[split]
        return db.query(
            f"SELECT strftime('{fmt}', l.ts, 'unixepoch') AS bucket, {expr} AS name, {_AGG} "
            f"FROM usage_logs l {join}{where} GROUP BY bucket, {expr} ORDER BY bucket", params,
        )
    return db.query(
        f"SELECT strftime('{fmt}', l.ts, 'unixepoch') AS bucket, {_AGG} "
        f"FROM usage_logs l{where} GROUP BY bucket ORDER BY bucket", params,
    )


def logs(db: Database, *, since: float | None = None, until: float | None = None,
         service_id: int | None = None, api_key_id: int | None = None, model: str | None = None,
         provider_id: int | None = None, only_errors: bool = False, limit: int = 50, offset: int = 0
         ) -> tuple[list[dict[str, Any]], int]:
    where, params = _filters(since, until, service_id, api_key_id, model, provider_id)
    if only_errors:
        where += (" AND " if where else " WHERE ") + "l.success = 0"
    total = db.scalar(f"SELECT COUNT(*) FROM usage_logs l{where}", params, 0)
    rows = db.query(
        "SELECT l.*, s.name AS service_name, k.name AS key_name, p.name AS provider_name "
        "FROM usage_logs l "
        "LEFT JOIN services s ON s.id = l.service_id "
        "LEFT JOIN api_keys k ON k.id = l.api_key_id "
        f"LEFT JOIN providers p ON p.id = l.provider_id{where} "
        "ORDER BY l.ts DESC, l.id DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    )
    return rows, int(total)


def tag_summary(db: Database, *, since: float | None = None, until: float | None = None, service_id: int | None = None,
                limit: int = 100) -> list[dict[str, Any]]:
    """Spend per request tag (LiteLLM's tag-based tracking). A request with several tags counts toward each."""
    where, params = _filters(since, until, service_id, None, None)
    where += (" AND " if where else " WHERE ") + "l.tags IS NOT NULL"
    return db.query(
        f"SELECT t.value AS name, {_AGG} FROM usage_logs l, json_each(l.tags) t{where} "
        f"GROUP BY t.value ORDER BY cost_usd DESC, requests DESC LIMIT ?", [*params, limit])
