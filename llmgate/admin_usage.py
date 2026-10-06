from __future__ import annotations

import time
from typing import Any, Literal

from fastapi import APIRouter, Query, Request

from . import usage
from .admin_common import ADMIN_DEPS, get_db

router = APIRouter(prefix="/admin/api", tags=["usage"], dependencies=ADMIN_DEPS)

GroupBy = Literal["model", "service", "key", "provider", "endpoint", "app", "user"]


def _range(days: float, since: float | None, until: float | None) -> tuple[float, float | None]:
    return (since if since is not None else time.time() - days * 86400), until


@router.get("/overview")
def overview(request: Request, days: float = Query(7, gt=0, le=366)) -> dict[str, Any]:
    db = get_db(request)
    since, _ = _range(days, None, None)
    return {
        "days": days,
        "totals": usage.totals(db, since=since),
        "counts": {
            "providers": db.scalar("SELECT COUNT(*) FROM providers", default=0),
            "providers_healthy": db.scalar("SELECT COUNT(*) FROM providers WHERE enabled = 1 AND status = 'healthy'", default=0),
            "models": db.scalar("SELECT COUNT(*) FROM models WHERE enabled = 1", default=0),
            "services": db.scalar("SELECT COUNT(*) FROM services WHERE enabled = 1", default=0),
            "keys": db.scalar("SELECT COUNT(*) FROM api_keys WHERE enabled = 1", default=0),
        },
        "top_models": usage.summary(db, "model", since=since, limit=5),
        "top_services": usage.summary(db, "service", since=since, limit=5),
    }


@router.get("/usage/summary")
def usage_summary(request: Request, group_by: GroupBy = "model", days: float = Query(7, gt=0, le=366),
                  since: float | None = None, until: float | None = None, service_id: int | None = None,
                  api_key_id: int | None = None, model: str | None = None,
                  limit: int = Query(100, ge=1, le=1000)) -> list[dict[str, Any]]:
    s, u = _range(days, since, until)
    return usage.summary(get_db(request), group_by, since=s, until=u, service_id=service_id,
                         api_key_id=api_key_id, model=model, limit=limit)


@router.get("/usage/timeseries")
def usage_timeseries(request: Request, bucket: Literal["hour", "day"] = "day",
                     split: GroupBy | None = None, days: float = Query(7, gt=0, le=366),
                     since: float | None = None, until: float | None = None, service_id: int | None = None,
                     api_key_id: int | None = None, model: str | None = None) -> list[dict[str, Any]]:
    s, u = _range(days, since, until)
    return usage.timeseries(get_db(request), bucket, since=s, until=u, split=split, service_id=service_id,
                            api_key_id=api_key_id, model=model)


@router.get("/usage/logs")
def usage_logs(request: Request, days: float = Query(7, gt=0, le=366), since: float | None = None,
               until: float | None = None, service_id: int | None = None, api_key_id: int | None = None,
               model: str | None = None, provider_id: int | None = None, only_errors: bool = False,
               limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)) -> dict[str, Any]:
    s, u = _range(days, since, until)
    rows, total = usage.logs(get_db(request), since=s, until=u, service_id=service_id, api_key_id=api_key_id,
                             model=model, provider_id=provider_id, only_errors=only_errors,
                             limit=limit, offset=offset)
    return {"total": total, "limit": limit, "offset": offset, "data": rows}


@router.get("/usage/tags")
def usage_tags(request: Request, days: float = Query(7, gt=0, le=366), since: float | None = None,
               until: float | None = None, service_id: int | None = None) -> list[dict[str, Any]]:
    s, u = _range(days, since, until)
    return usage.tag_summary(get_db(request), since=s, until=u, service_id=service_id)
