"""OpenRouter-style model catalog, per-model endpoint stats and provider listing."""

from __future__ import annotations

import json
import time
from typing import Any

from .db import Database
from .routing import prices


def per_token(price_per_m: float) -> str:
    """OpenRouter quotes USD per *token* as a decimal string."""
    return f"{price_per_m / 1_000_000:.10f}".rstrip("0").rstrip(".") or "0"


def _list(raw: str | None, default: list[str] | None) -> list[str] | None:
    return json.loads(raw) if raw else default


def _entry(m: dict[str, Any]) -> dict[str, Any]:
    inputs, outputs = _list(m["input_modalities"], ["text"]), _list(m["output_modalities"], ["text"])
    kind_out = {"embedding": "embeddings", "rerank": "scores"}.get(m["kind"])
    return {
        "id": m["name"], "canonical_slug": m["name"], "object": "model", "owned_by": "llmgate",
        "name": m["display_name"] or m["name"], "created": int(m["created_at"]), "description": m["description"],
        "context_length": m["context_length"],
        "architecture": {"modality": f"{'+'.join(inputs or [])}->{kind_out or '+'.join(outputs or [])}",
                         "input_modalities": inputs, "output_modalities": [kind_out] if kind_out else outputs,
                         "tokenizer": None, "instruct_type": None},
        "pricing": {"prompt": per_token(m["input_price_per_m"]), "completion": per_token(m["output_price_per_m"])},
        "top_provider": {"context_length": m["context_length"], "max_completion_tokens": m["max_completion_tokens"],
                         "is_moderated": False},
        "per_request_limits": None,
        "supported_parameters": _list(m["supported_parameters"], None),
        "status": "available" if m["live_routes"] else ("degraded" if m["routes"] else "no_provider"),
    }


def model_catalog(db: Database, allowed: list[str] | None = None, *, supported_parameters: list[str] | None = None,
                  input_modalities: list[str] | None = None, kind: str | None = None) -> list[dict[str, Any]]:
    rows = db.query(
        "SELECT m.*, "
        "  (SELECT COUNT(*) FROM model_routes r JOIN providers p ON p.id = r.provider_id "
        "   WHERE r.model_id = m.id AND r.enabled = 1 AND p.enabled = 1 AND p.status != 'unhealthy') AS live_routes, "
        "  (SELECT COUNT(*) FROM model_routes r JOIN providers p ON p.id = r.provider_id "
        "   WHERE r.model_id = m.id AND r.enabled = 1 AND p.enabled = 1) AS routes "
        "FROM models m WHERE m.enabled = 1 ORDER BY m.name")
    out = []
    for m in rows:
        if allowed and m["name"] not in allowed:
            continue
        if kind and m["kind"] != kind:
            continue
        entry = _entry(m)
        have = entry["supported_parameters"]
        if supported_parameters and have is not None and not set(supported_parameters) <= set(have):
            continue
        if supported_parameters and have is None:  # unknown capabilities: cannot promise the filter
            continue
        if input_modalities and not set(input_modalities) <= set(entry["architecture"]["input_modalities"] or []):
            continue
        out.append(entry)
    return out


def _percentiles(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"p50": None, "p75": None, "p90": None, "p99": None}
    s = sorted(values)
    pick = lambda q: round(s[min(len(s) - 1, int(q * len(s)))], 1)  # noqa: E731
    return {"p50": pick(0.50), "p75": pick(0.75), "p90": pick(0.90), "p99": pick(0.99)}


def model_endpoints(db: Database, name: str, window_s: float = 1800.0) -> dict[str, Any] | None:
    """One entry per provider route of a model, with 30-minute uptime / latency / throughput (``/models/{id}/endpoints``)."""
    m = db.one("SELECT * FROM models WHERE name = ? AND enabled = 1", (name,))
    if m is None:
        return None
    m["live_routes"] = m["routes"] = 0
    since = time.time() - window_s
    endpoints = []
    routes = db.query(
        "SELECT r.*, p.id AS provider_id, p.name AS provider_name, p.status AS provider_status, p.quantization, "
        "       p.data_collection, p.kind AS provider_kind FROM model_routes r JOIN providers p ON p.id = r.provider_id "
        "WHERE r.model_id = ? AND r.enabled = 1 AND p.enabled = 1 ORDER BY r.priority, r.id", (m["id"],))
    for r in routes:
        logs = db.query("SELECT success, latency_ms, ttft_ms, completion_tokens FROM usage_logs "
                        "WHERE model = ? AND provider_id = ? AND ts >= ? ORDER BY id DESC LIMIT 1000", (name, r["provider_id"], since))
        ok = [l for l in logs if l["success"]]
        tps = [l["completion_tokens"] * 1000 / l["latency_ms"] for l in ok if l["completion_tokens"] and l["latency_ms"]]
        pin, pout = prices({"route_in": r["input_price_per_m"], "route_out": r["output_price_per_m"]}, m)
        endpoints.append({
            "name": f"{r['provider_name']} | {name}", "model_name": m["display_name"] or name,
            "provider_name": r["provider_name"], "tag": r["provider_name"].lower(), "context_length": m["context_length"],
            "max_completion_tokens": m["max_completion_tokens"], "quantization": r["quantization"],
            "pricing": {"prompt": per_token(pin), "completion": per_token(pout)},
            "supported_parameters": _list(m["supported_parameters"], None), "data_collection": r["data_collection"],
            "priority": r["priority"], "weight": r["weight"],
            "status": {"healthy": 0, "unknown": 0, "unhealthy": -1}.get(r["provider_status"], 0),
            "uptime_last_30m": round(100 * len(ok) / len(logs), 2) if logs else None,
            "latency_last_30m": _percentiles([l["ttft_ms"] for l in ok if l["ttft_ms"] is not None] or [l["latency_ms"] for l in ok]),
            "throughput_last_30m": _percentiles(tps), "requests_last_30m": len(logs),
        })
    entry = _entry(m)
    entry.pop("status", None)
    return {**entry, "endpoints": endpoints}


def providers_list(db: Database) -> list[dict[str, Any]]:
    return [{"name": p["name"], "slug": p["name"].lower(), "engine": p["kind"], "status": p["status"],
             "data_collection": p["data_collection"], "quantization": p["quantization"]}
            for p in db.query("SELECT * FROM providers WHERE enabled = 1 ORDER BY name")]
