"""Provider health checks and engine metric scraping."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

import httpx

from . import secretbox
from .config import Settings
from .db import Database, now

log = logging.getLogger("llmgate.health")

_METRIC_RE = re.compile(r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{[^}]*\})?\s+(?P<value>[-+0-9.eE]+|NaN|\+Inf|-Inf)\s*$")

# exposed name -> (candidate vLLM metric names, aggregation)
_WANTED: dict[str, tuple[tuple[str, ...], str]] = {
    "requests_running": (("vllm:num_requests_running",), "sum"),
    "requests_waiting": (("vllm:num_requests_waiting",), "sum"),
    "kv_cache_usage": (("vllm:gpu_cache_usage_perc", "vllm:kv_cache_usage_perc"), "max"),
    "prompt_tokens_total": (("vllm:prompt_tokens_total",), "sum"),
    "generation_tokens_total": (("vllm:generation_tokens_total",), "sum"),
}


def parse_engine_metrics(text: str) -> dict[str, float]:
    """Pull the handful of ``vllm:*`` series the dashboard shows out of Prometheus text."""
    raw: dict[str, list[float]] = {}
    for line in text.splitlines():
        if line.startswith("#"):
            continue
        m = _METRIC_RE.match(line.strip())
        if not m:
            continue
        try:
            raw.setdefault(m["name"], []).append(float(m["value"]))
        except ValueError:
            continue
    out: dict[str, float] = {}
    for label, (names, agg) in _WANTED.items():
        for name in names:
            if name in raw:
                out[label] = max(raw[name]) if agg == "max" else sum(raw[name])
                break
    return out


def engine_root(base_url: str) -> str:
    root = base_url.rstrip("/")
    return root[:-3] if root.endswith("/v1") else root


def _auth(provider: dict[str, Any]) -> dict[str, str]:
    key = secretbox.decrypt(provider.get("api_key"))
    return {"authorization": f"Bearer {key}"} if key else {}


async def check_provider(client: httpx.AsyncClient, provider: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Probe one provider. Returns ``{status, latency_ms, error, engine_stats}`` (does not touch the DB)."""
    timeout = httpx.Timeout(settings.health_timeout_s)
    headers = _auth(provider)
    root = engine_root(provider["base_url"])
    started = time.perf_counter()
    error: str | None = None
    healthy = False
    try:
        resp = await client.get(f"{root}/health", headers=headers, timeout=timeout)
        if resp.status_code == 404:  # engines without /health (Ollama, hosted APIs): list models instead
            resp = await client.get(f"{provider['base_url'].rstrip('/')}/models", headers=headers, timeout=timeout)
        healthy = resp.status_code < 400
        if not healthy:
            error = f"HTTP {resp.status_code}"
    except httpx.HTTPError as exc:
        error = f"{type(exc).__name__}: {exc}".rstrip(": ")
    latency_ms = (time.perf_counter() - started) * 1000

    stats: dict[str, float] = {}
    if healthy and provider.get("kind") in ("vllm", "tgi"):
        try:
            resp = await client.get(f"{root}/metrics", headers=headers, timeout=timeout)
            if resp.status_code == 200:
                stats = parse_engine_metrics(resp.text)
        except httpx.HTTPError:
            pass  # metrics are best-effort
    return {
        "status": "healthy" if healthy else "unhealthy",
        "latency_ms": round(latency_ms, 1),
        "error": error,
        "engine_stats": stats,
    }


def _notify(db: Database, alerter: Any, provider_id: int, new: str, error: str | None) -> None:
    before = db.one("SELECT name, status FROM providers WHERE id = ?", (provider_id,))
    if alerter and before:
        alerter.provider_changed(before["name"], before["status"], new, error)


def apply_check(db: Database, provider_id: int, result: dict[str, Any], alerter: Any = None) -> None:
    healthy = result["status"] == "healthy"
    _notify(db, alerter, provider_id, result["status"], result["error"])
    db.execute(
        "UPDATE providers SET status = ?, latency_ms = ?, last_error = ?, last_checked_at = ?, "
        "engine_stats = ?, consecutive_failures = CASE WHEN ? THEN 0 ELSE consecutive_failures + 1 END "
        "WHERE id = ?",
        (result["status"], result["latency_ms"], result["error"], now(),
         json.dumps(result["engine_stats"]) if result["engine_stats"] else None, int(healthy), provider_id),
    )


def mark_passive(db: Database, provider_id: int, ok: bool, error: str | None = None, alerter: Any = None) -> None:
    """Fold a real request's outcome into the provider's status without waiting for the next probe."""
    _notify(db, alerter, provider_id, "healthy" if ok else "unhealthy", error)
    if ok:
        db.execute(
            "UPDATE providers SET status = 'healthy', consecutive_failures = 0, last_error = NULL "
            "WHERE id = ? AND status != 'healthy'", (provider_id,))
    else:
        db.execute(
            "UPDATE providers SET status = 'unhealthy', consecutive_failures = consecutive_failures + 1, "
            "last_error = ? WHERE id = ?", ((error or "request failed")[:300], provider_id))


class HealthMonitor:
    def __init__(self, db: Database, client: httpx.AsyncClient, settings: Settings, alerter: Any = None):
        self.db, self.client, self.settings, self.alerter = db, client, settings, alerter
        self._task: asyncio.Task[None] | None = None

    async def check_all(self) -> None:
        providers = self.db.query("SELECT * FROM providers WHERE enabled = 1")
        results = await asyncio.gather(
            *(check_provider(self.client, p, self.settings) for p in providers), return_exceptions=True)
        for provider, result in zip(providers, results, strict=True):
            if isinstance(result, Exception):
                log.warning("health check crashed for %s: %r", provider["name"], result)
                continue
            apply_check(self.db, provider["id"], result, self.alerter)

    async def _loop(self) -> None:
        while True:
            try:
                await self.check_all()
            except Exception:  # keep the loop alive no matter what
                log.exception("health loop iteration failed")
            await asyncio.sleep(self.settings.health_interval_s)

    def start(self) -> None:
        if self.settings.health_interval_s > 0:
            self._task = asyncio.create_task(self._loop(), name="llmgate-health")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
