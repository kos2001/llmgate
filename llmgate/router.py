"""Model → upstream route selection.

A public model maps to one or more routes (provider + upstream model name).
Routes are grouped by ``priority`` (lower first); inside a tier one is picked by
``weight`` (weighted random order), so a tier load-balances and later tiers act as fallbacks.
Providers currently marked unhealthy are pushed behind every healthy one but never dropped,
so a stale health flag cannot black-hole traffic.
"""

from __future__ import annotations

import random
from typing import Any

from .db import Database

_ROUTE_SQL = """
SELECT r.id AS route_id, r.upstream_model, r.priority, r.weight,
       p.id AS provider_id, p.name AS provider_name, p.kind, p.base_url, p.api_key,
       p.timeout_s, p.status AS provider_status, p.data_collection, p.quantization,
       r.input_price_per_m AS route_in, r.output_price_per_m AS route_out,
       r.rpm_limit, r.tpm_limit, r.max_parallel
FROM model_routes r
JOIN providers p ON p.id = r.provider_id
WHERE r.model_id = ? AND r.enabled = 1 AND p.enabled = 1
"""


def candidate_routes(db: Database, model_id: int) -> list[dict[str, Any]]:
    return db.query(_ROUTE_SQL, (model_id,))


def order_routes(routes: list[dict[str, Any]], rng: random.Random | None = None) -> list[dict[str, Any]]:
    rng = rng or random
    # Efraimidis–Spirakis weighted shuffle: sort by u^(1/w) descending.
    def shuffle_key(route: dict[str, Any]) -> float:
        return rng.random() ** (1.0 / max(1, route["weight"]))

    return sorted(
        routes,
        key=lambda r: (r["provider_status"] == "unhealthy", r["priority"], -shuffle_key(r)),
    )


def route_stats(db: Database, model: str, window_s: float = 1800.0) -> dict[int, dict[str, Any]]:
    """Recent per-provider behaviour for one model: uptime, latency, TTFT and throughput (tokens/s)."""
    import time

    rows = db.query(
        "SELECT provider_id, COUNT(*) AS requests, SUM(success) AS ok, "
        " AVG(CASE WHEN success = 1 THEN latency_ms END) AS latency_ms, "
        " AVG(CASE WHEN success = 1 THEN ttft_ms END) AS ttft_ms, "
        " SUM(CASE WHEN success = 1 AND completion_tokens > 0 THEN completion_tokens END) * 1000.0 / "
        "   NULLIF(SUM(CASE WHEN success = 1 AND completion_tokens > 0 THEN latency_ms END), 0) AS tokens_per_s "
        "FROM usage_logs WHERE model = ? AND ts >= ? AND provider_id IS NOT NULL GROUP BY provider_id",
        (model, time.time() - window_s))
    return {r["provider_id"]: r for r in rows}
