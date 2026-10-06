"""Rate limits, concurrency caps and spend budgets (per key, service, model and end-user)."""

from __future__ import annotations

import json
import math
import time
from collections import defaultdict, deque
from typing import Any

from .auth import AuthContext
from .db import Database
from .errors import GatewayError
from .usage import month_start, period_usage


class RateLimiter:
    """In-process sliding-window request limiter (60 s) plus an in-flight counter per key."""

    def __init__(self, window_s: float = 60.0):
        self.window_s = window_s
        self._hits: dict[Any, deque[float]] = defaultdict(deque)
        self.inflight: dict[int, int] = defaultdict(int)

    def _trim(self, q: deque[float], t: float) -> None:
        while q and t - q[0] >= self.window_s:
            q.popleft()

    def hit(self, bucket: Any, limit: int | None) -> float | None:
        """Register a request. Returns seconds to wait if the limit is exceeded, else ``None``."""
        if not limit:
            return None
        t = time.monotonic()
        q = self._hits[bucket]
        self._trim(q, t)
        if len(q) >= limit:
            return max(0.0, self.window_s - (t - q[0]))
        q.append(t)
        return None

    def remaining(self, bucket: Any, limit: int | None) -> int | None:
        if not limit:
            return None
        q = self._hits[bucket]
        self._trim(q, time.monotonic())
        return max(0, limit - len(q))

    def acquire(self, key_id: int, limit: int | None) -> bool:
        if limit and self.inflight[key_id] >= limit:
            return False
        self.inflight[key_id] += 1
        return True

    def release(self, key_id: int) -> None:
        self.inflight[key_id] = max(0, self.inflight[key_id] - 1)


def window_start(db: Database, table: str, row: dict[str, Any]) -> float:
    """Start of the budget window. ``budget_duration_s`` windows reset on a fixed cadence; otherwise calendar month."""
    duration = row.get("budget_duration_s")
    if not duration:
        return month_start()
    anchor = row.get("budget_anchor") or row["created_at"]
    t = time.time()
    if t >= anchor + duration:  # table is a literal chosen by our own callers
        anchor += ((t - anchor) // duration) * duration
        db.execute(f"UPDATE {table} SET budget_anchor = ? WHERE id = ?", (anchor, row["id"]))
        row["budget_anchor"] = anchor
    return anchor


def _retry_after(seconds: float) -> dict[str, str]:
    return {"Retry-After": str(max(1, math.ceil(seconds)))}


def enforce_limits(db: Database, limiter: RateLimiter, auth: AuthContext, payload: dict[str, Any] | None = None) -> None:
    key, service = auth.key, auth.service
    for scope, row in (("key", key), ("service", service)):
        retry = limiter.hit((scope, row["id"]), row["rpm_limit"])
        if retry is not None:
            raise GatewayError(429, f"Rate limit exceeded: {row['rpm_limit']} requests/minute for this {scope}.",
                               type="rate_limit_error", code="rate_limit_exceeded", headers=_retry_after(retry))
        if row.get("tpm_limit"):
            used = db.scalar("SELECT COALESCE(SUM(total_tokens), 0) FROM usage_logs WHERE ts >= ? AND "
                             + ("api_key_id" if scope == "key" else "service_id") + " = ?", (time.time() - 60, row["id"]), 0)
            if used >= row["tpm_limit"]:
                raise GatewayError(429, f"Token rate limit exceeded: {row['tpm_limit']} tokens/minute for this {scope}.",
                                   type="rate_limit_error", code="tpm_limit_exceeded", headers=_retry_after(30))

    for scope, row, table, column in (("key", key, "api_keys", "api_key_id"), ("service", service, "services", "service_id")):
        token_limit, budget = row["monthly_token_limit"], row["monthly_budget_usd"]
        if token_limit is None and budget is None:
            continue
        tokens, cost = period_usage(db, **{column: row["id"]}, since=window_start(db, table, row))
        if token_limit is not None and tokens >= token_limit:
            raise GatewayError(402, f"Token limit reached for this {scope} ({tokens}/{token_limit}).",
                               type="insufficient_quota", code="token_limit_exceeded")
        if budget is not None and cost >= budget:
            raise GatewayError(402, f"Budget reached for this {scope} (${cost:.4f}/${budget:.2f}).",
                               type="insufficient_quota", code="budget_exceeded")

    user = payload.get("user") if payload else None
    if isinstance(user, str) and user:
        customer = db.one("SELECT * FROM customers WHERE user_id = ?", (user,))
        if customer:
            if customer["blocked"]:
                raise GatewayError(403, f"The end-user '{user}' is blocked.", type="permission_error", code="customer_blocked")
            if customer["max_budget_usd"] is not None:
                _, cost = period_usage(db, end_user=user, since=window_start(db, "customers", customer))
                if cost >= customer["max_budget_usd"]:
                    raise GatewayError(402, f"The end-user '{user}' reached their budget (${cost:.4f}/${customer['max_budget_usd']:.2f}).",
                                       type="insufficient_quota", code="customer_budget_exceeded")

    if not limiter.acquire(key["id"], key["max_parallel"]):
        raise GatewayError(429, f"Too many concurrent requests: this key allows {key['max_parallel']} at once.",
                           type="rate_limit_error", code="max_parallel_requests", headers=_retry_after(1))


def check_model_budget(db: Database, auth: AuthContext, model: str) -> None:
    """Per-model spend cap on a key (``model_budgets``: {model: USD per budget window})."""
    raw = auth.key.get("model_budgets")
    caps = json.loads(raw) if raw else {}
    cap = caps.get(model)
    if cap is None:
        return
    _, cost = period_usage(db, api_key_id=auth.key["id"], model=model, since=window_start(db, "api_keys", auth.key))
    if cost >= cap:
        raise GatewayError(402, f"This key reached its budget for '{model}' (${cost:.4f}/${cap:.2f}).",
                           type="insufficient_quota", code="model_budget_exceeded")
