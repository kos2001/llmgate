"""Admin API for the data lifecycle: usage export (CSV / JSON Lines), storage statistics and manual pruning."""

from __future__ import annotations

import csv
import io
import json
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

from . import storage
from .admin_common import ADMIN_DEPS, get_db
from .db import Database
from .errors import GatewayError

router = APIRouter(prefix="/admin/api", tags=["storage"], dependencies=ADMIN_DEPS)

COLUMNS = ["time_utc", "request_id", "service", "key", "provider", "model", "requested_model", "upstream_model", "endpoint", "stream",
           "prompt_tokens", "completion_tokens", "total_tokens", "usage_estimated", "cost_usd", "latency_ms", "ttft_ms", "status_code",
           "success", "finish_reason", "attempts", "cache_hit", "guardrail", "app", "end_user", "tags", "error"]
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
_PAGE = 1000


def _cell(value: Any) -> Any:
    """Neutralise spreadsheet formula injection: text a client controls (app title, user, error) must never run as a formula."""
    if isinstance(value, str) and value.startswith(_FORMULA_START):
        return "'" + value
    return value


def _rows(db: Database, where: str, params: list[Any]) -> Iterator[dict[str, Any]]:
    last = None
    while True:  # keyset pagination: constant memory however large the export
        cursor = " AND l.id < ?" if last is not None else ""
        page = db.query(
            "SELECT l.*, s.name AS service_name, k.name AS key_name, p.name AS provider_name FROM usage_logs l "
            "LEFT JOIN services s ON s.id = l.service_id LEFT JOIN api_keys k ON k.id = l.api_key_id "
            f"LEFT JOIN providers p ON p.id = l.provider_id WHERE {where}{cursor} ORDER BY l.id DESC LIMIT {_PAGE}",
            [*params, *([last] if last is not None else [])])
        yield from page
        if len(page) < _PAGE:
            return
        last = page[-1]["id"]


def _record(r: dict[str, Any]) -> list[Any]:
    iso = datetime.fromtimestamp(r["ts"], tz=timezone.utc).isoformat(timespec="seconds")
    values = [iso, r["request_id"], r["service_name"], r["key_name"], r["provider_name"], r["model"], r["requested_model"], r["upstream_model"],
              r["endpoint"], r["stream"], r["prompt_tokens"], r["completion_tokens"], r["total_tokens"], r["usage_estimated"], r["cost_usd"],
              r["latency_ms"], r["ttft_ms"], r["status_code"], r["success"], r["finish_reason"], r["attempts"], r["cache_hit"], r["guardrail"],
              r["app_title"], r["end_user"], r["tags"], r["error"]]
    return [_cell(v) for v in values]


@router.get("/usage/export")
def export_usage(request: Request, fmt: Literal["csv", "jsonl"] = Query("csv", alias="format"), days: float = Query(7, gt=0, le=3660),
                 service_id: int | None = None, api_key_id: int | None = None, model: str | None = None, only_errors: bool = False) -> StreamingResponse:
    """Download usage rows (newest first) matching the filters. Streamed in pages, so it works for millions of rows."""
    where, params = ["l.ts >= ?"], [time.time() - days * 86400]
    for clause, value in (("l.service_id = ?", service_id), ("l.api_key_id = ?", api_key_id), ("l.model = ?", model)):
        if value is not None:
            where.append(clause)
            params.append(value)
    if only_errors:
        where.append("l.success = 0")
    db, clause = get_db(request), " AND ".join(where)

    def csv_stream() -> Iterator[str]:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(COLUMNS)
        yield "\ufeff" + buf.getvalue()  # BOM: Excel opens UTF-8 (Korean text) correctly
        for row in _rows(db, clause, params):
            buf.seek(0)
            buf.truncate()
            writer.writerow(_record(row))
            yield buf.getvalue()

    def jsonl_stream() -> Iterator[str]:
        for row in _rows(db, clause, params):
            yield json.dumps(dict(zip(COLUMNS, _record(row), strict=True)), ensure_ascii=False) + "\n"

    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    csv_type = fmt == "csv"
    return StreamingResponse(csv_stream() if csv_type else jsonl_stream(), media_type="text/csv; charset=utf-8" if csv_type else "application/x-ndjson",
                             headers={"Content-Disposition": f'attachment; filename="llmgate-usage-{stamp}.{fmt}"'})


@router.get("/storage")
def storage_stats(request: Request) -> dict[str, Any]:
    s = request.app.state.settings
    return storage.stats(get_db(request), s.database_path, s.usage_retention_days, s.audit_retention_days)


@router.post("/storage/prune")
def prune(request: Request, days: float = Query(..., gt=0, le=3660), include_audit: bool = False) -> dict[str, int]:
    """Delete usage rows older than ``days`` now (admin). Audit rows only when asked: they are the record of who changed what."""
    if days < 1:
        raise GatewayError(400, "Refusing to prune less than one day of history; use a larger value.", code="invalid_prune_window")
    db = get_db(request)
    out = {"usage_logs": storage.prune_table(db, "usage_logs", days), "audit_logs": 0}
    if include_audit:
        out["audit_logs"] = storage.prune_table(db, "audit_logs", days)
    return out
