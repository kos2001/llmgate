"""Per-request routing controls, modelled on OpenRouter's ``provider`` object, model variants and ``models`` fallbacks."""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from typing import Any

from .auth import AuthContext
from .db import Database
from .errors import GatewayError
from .router import candidate_routes, order_routes, route_stats
from .tokens import content_text, estimate_prompt_tokens

VARIANTS = {"nitro": "throughput", "floor": "price", "free": None}  # model suffixes: ``name:nitro``
SORTS = {"price", "throughput", "latency"}
# request parameter → the capability name a model lists in ``supported_parameters``
PARAM_CAPS = {"tools": "tools", "tool_choice": "tool_choice", "response_format": "response_format",
              "structured_outputs": "structured_outputs", "reasoning": "reasoning", "include_reasoning": "reasoning",
              "logprobs": "logprobs", "top_logprobs": "logprobs", "seed": "seed", "stop": "stop"}


@dataclass
class Prefs:
    order: list[str] = field(default_factory=list)
    only: list[str] = field(default_factory=list)
    ignore: list[str] = field(default_factory=list)
    quantizations: list[str] = field(default_factory=list)
    allow_fallbacks: bool = True
    require_parameters: bool = False
    data_collection: str | None = None
    sort: str | None = None
    max_price: dict[str, float] = field(default_factory=dict)
    free_only: bool = False


def _names(obj: dict[str, Any], key: str) -> list[str]:
    value = obj.get(key) or []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise GatewayError(400, f"provider.{key} must be a list of strings.", code="invalid_provider_preferences")
    return value


def parse_prefs(payload: dict[str, Any], variant: str | None) -> Prefs:
    raw = payload.get("provider") or {}
    if not isinstance(raw, dict):
        raise GatewayError(400, "'provider' must be an object.", code="invalid_provider_preferences")
    prefs = Prefs(order=_names(raw, "order"), only=_names(raw, "only"), ignore=_names(raw, "ignore"),
                  quantizations=_names(raw, "quantizations"), allow_fallbacks=bool(raw.get("allow_fallbacks", True)),
                  require_parameters=bool(raw.get("require_parameters", False)))
    if raw.get("sort") is not None:
        if raw["sort"] not in SORTS:
            raise GatewayError(400, f"provider.sort must be one of {sorted(SORTS)}.", code="invalid_provider_preferences")
        prefs.sort = raw["sort"]
    if raw.get("data_collection") is not None:
        if raw["data_collection"] not in ("allow", "deny"):
            raise GatewayError(400, "provider.data_collection must be 'allow' or 'deny'.", code="invalid_provider_preferences")
        prefs.data_collection = raw["data_collection"]
    mp = raw.get("max_price") or {}
    if not isinstance(mp, dict) or not all(isinstance(v, (int, float)) for v in mp.values()):
        raise GatewayError(400, "provider.max_price must map 'prompt'/'completion' to USD per 1M tokens.",
                           code="invalid_provider_preferences")
    prefs.max_price = {k: float(v) for k, v in mp.items() if k in ("prompt", "completion")}
    if variant == "free":
        prefs.free_only = True
    elif variant and prefs.sort is None:
        prefs.sort = VARIANTS[variant]
    return prefs


def split_variant(db: Database, name: str) -> tuple[str, str | None]:
    """``llama:nitro`` → (``llama``, ``nitro``) unless a model literally named ``llama:nitro`` exists."""
    if db.one("SELECT 1 FROM models WHERE name = ?", (name,)):
        return name, None
    base, sep, suffix = name.rpartition(":")
    return (base, suffix) if sep and suffix in VARIANTS else (name, None)


def prices(route: dict[str, Any], model: dict[str, Any]) -> tuple[float, float]:
    """Effective USD per 1M tokens: the provider-specific override when set, else the model price."""
    pin = route["route_in"] if route.get("route_in") is not None else model["input_price_per_m"]
    pout = route["route_out"] if route.get("route_out") is not None else model["output_price_per_m"]
    return pin, pout


def missing_capabilities(model: dict[str, Any], payload: dict[str, Any]) -> list[str]:
    declared = json.loads(model["supported_parameters"]) if model.get("supported_parameters") else None
    if declared is None:
        return []
    return sorted({cap for key, cap in PARAM_CAPS.items() if payload.get(key) not in (None, False) and cap not in declared})


def required_modalities(payload: dict[str, Any]) -> set[str]:
    kinds = {"image_url": "image", "input_image": "image", "image": "image", "input_audio": "audio", "file": "file"}
    found: set[str] = set()
    for msg in payload.get("messages") or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        for part in content if isinstance(content, list) else []:
            if isinstance(part, dict) and part.get("type") in kinds:
                found.add(kinds[part["type"]])
    return found


def filter_routes(routes: list[dict[str, Any]], model: dict[str, Any], prefs: Prefs, auth: AuthContext,
                  payload: dict[str, Any]) -> list[dict[str, Any]]:
    need = missing_capabilities(model, payload)
    if prefs.require_parameters and need:
        raise GatewayError(400, f"No endpoint for '{model['name']}' supports the requested parameters: {', '.join(need)}.",
                           code="unsupported_parameters")
    have = set(json.loads(model["input_modalities"])) if model.get("input_modalities") else {"text"}
    lacking = required_modalities(payload) - have
    if lacking:
        raise GatewayError(400, f"The model '{model['name']}' does not accept {', '.join(sorted(lacking))} input.",
                           code="unsupported_modality")

    allowed = auth.allowed_providers
    steps: list[tuple[str, Any]] = [
        ("the key's allowed providers", lambda r: not allowed or r["provider_name"] in allowed),
        ("provider.only", lambda r: not prefs.only or r["provider_name"] in prefs.only),
        ("provider.ignore", lambda r: r["provider_name"] not in prefs.ignore),
        ("provider.data_collection", lambda r: prefs.data_collection is None or r["data_collection"] == prefs.data_collection),
        ("provider.quantizations", lambda r: not prefs.quantizations or r["quantization"] in prefs.quantizations),
        ("provider.max_price", lambda r: all(prices(r, model)[i] <= prefs.max_price[k]
                                             for i, k in enumerate(("prompt", "completion")) if k in prefs.max_price)),
        ("the :free variant", lambda r: not prefs.free_only or prices(r, model) == (0, 0)),
    ]
    for label, keep in steps:
        routes = [r for r in routes if keep(r)]
        if not routes:
            raise GatewayError(404, f"No provider for '{model['name']}' matches your routing preferences (nothing left after {label}).",
                               type="not_found_error", code="no_matching_provider")
    return routes


def order_for(db: Database, model: dict[str, Any], routes: list[dict[str, Any]], prefs: Prefs, rs: Any = None) -> list[dict[str, Any]]:
    """Order a model's candidate routes: availability, then the model's strategy (or the request's ``sort``)."""
    if rs is not None:  # skip cooling / saturated routes, unless that would leave nothing to try
        routes = [r for r in routes if rs.available(r)] or routes
    strategy = model.get("routing_strategy") or "weighted"
    inf = float("inf")
    stats = route_stats(db, model["name"]) if prefs.sort in ("latency", "throughput") or strategy == "latency" else {}

    def latency(r: dict[str, Any]) -> float:
        st = stats.get(r["provider_id"], {})
        return st.get("ttft_ms") or st.get("latency_ms") or -1.0  # unmeasured first, so every route gets sampled

    if prefs.sort:
        def key(r: dict[str, Any]) -> float:
            st = stats.get(r["provider_id"], {})
            if prefs.sort == "price":
                return sum(prices(r, model))
            if prefs.sort == "throughput":
                return -st["tokens_per_s"] if st.get("tokens_per_s") else inf
            return latency(r) if latency(r) >= 0 else inf
        ordered = sorted(order_routes(routes), key=lambda r: (r["provider_status"] == "unhealthy", key(r)))
    elif strategy == "least-busy" and rs is not None:
        ordered = sorted(order_routes(routes), key=lambda r: (r["provider_status"] == "unhealthy", rs.provider_inflight[r["provider_id"]]))
    elif strategy == "latency":
        ordered = sorted(order_routes(routes), key=lambda r: (r["provider_status"] == "unhealthy", latency(r)))
    elif strategy == "cost":
        ordered = sorted(order_routes(routes), key=lambda r: (r["provider_status"] == "unhealthy", sum(prices(r, model))))
    elif strategy == "usage" and rs is not None:
        ordered = sorted(order_routes(routes), key=lambda r: (r["provider_status"] == "unhealthy", rs.tokens_last_minute(r["route_id"])))
    else:
        ordered = order_routes(routes)  # weighted: health, priority tiers, weighted shuffle
    if prefs.order:
        rank = {name: i for i, name in enumerate(prefs.order)}
        ordered = sorted(ordered, key=lambda r: rank.get(r["provider_name"], len(rank)))  # stable: others keep their order
    return ordered if prefs.allow_fallbacks else ordered[:1]


def resolve_alias(db: Database, name: str) -> str:
    row = db.one("SELECT model_name FROM model_aliases WHERE alias = ?", (name,))
    return row["model_name"] if row else name


def resolve_model(db: Database, name: str, auth: AuthContext) -> dict[str, Any]:
    model = db.one("SELECT * FROM models WHERE name = ?", (name,))
    if model is None or not model["enabled"]:
        raise GatewayError(404, f"The model '{name}' does not exist or is disabled.", type="not_found_error", code="model_not_found")
    if auth.allowed_models and name not in auth.allowed_models:
        raise GatewayError(403, f"This API key may not use the model '{name}'.", type="permission_error", code="model_not_allowed")
    return model


def plan(db: Database, name: str, auth: AuthContext, payload: dict[str, Any], kind: str,
         rs: Any = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate one candidate model and return it with its ordered routes."""
    base, variant = split_variant(db, name)  # `alias:floor` and `model:floor` alike
    base = resolve_alias(db, base)
    model = resolve_model(db, base, auth)
    if model["kind"] != kind:
        raise GatewayError(400, f"The model '{base}' is a {model['kind']} model and cannot serve this endpoint.", code="wrong_model_kind")
    prefs = parse_prefs(payload, variant)
    routes = candidate_routes(db, model["id"])
    if not routes:
        raise GatewayError(503, f"No enabled provider is serving '{base}'.", type="server_error", code="no_available_provider")
    return model, order_for(db, model, filter_routes(routes, model, prefs, auth, payload), prefs, rs)


# ---------------------------------------------------------------------------------------- auto router

_COMPLEX = re.compile(r"```|\bdef \w+\(|\bclass \w+|\bSELECT\b.*\bFROM\b|step[- ]by[- ]step|prove|derive|integral|"
                      r"refactor|debug|algorithm|코드|구현|증명|알고리즘|단계별", re.I | re.S)


def auto_candidates(db: Database, auth: AuthContext, payload: dict[str, Any], endpoint: str) -> list[str]:
    """Pick models for ``llmgate/auto``: cheap ones for easy prompts, the strongest first for hard ones.

    "Strength" is approximated by price (what you pay for is what you get on a self-hosted fleet), so a
    prompt that looks like code/maths, is long, or uses tools prefers the most expensive eligible model.
    """
    patterns: list[str] = []
    for plugin in payload.get("plugins") or []:
        if isinstance(plugin, dict) and plugin.get("id") == "auto-router":
            patterns = [p for p in plugin.get("allowed_models") or [] if isinstance(p, str)]
    rows = db.query(
        "SELECT m.* FROM models m WHERE m.enabled = 1 AND m.kind = 'chat' AND EXISTS ("
        " SELECT 1 FROM model_routes r JOIN providers p ON p.id = r.provider_id "
        " WHERE r.model_id = m.id AND r.enabled = 1 AND p.enabled = 1 AND p.status != 'unhealthy')")
    pool = [m for m in rows if (not auth.allowed_models or m["name"] in auth.allowed_models)
            and (not patterns or any(fnmatch.fnmatch(m["name"], p) for p in patterns))
            and not missing_capabilities(m, payload) and not (required_modalities(payload) - set(json.loads(m["input_modalities"] or '["text"]')))]
    if not pool:
        raise GatewayError(404, "No model is eligible for automatic routing.", type="not_found_error", code="no_eligible_model")
    text = " ".join(content_text(m.get("content")) for m in payload.get("messages") or [] if isinstance(m, dict))
    hard = bool(_COMPLEX.search(text)) or estimate_prompt_tokens(endpoint, payload) > 1500 or bool(payload.get("tools"))
    pool.sort(key=lambda m: m["input_price_per_m"] + m["output_price_per_m"], reverse=hard)
    return [m["name"] for m in pool]
