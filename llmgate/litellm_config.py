"""Import a LiteLLM proxy ``config.yaml`` (``model_list`` / ``router_settings`` …) into the llmgate registry.

Registry data (providers, models, routes, aliases, fallbacks, routing strategy) is written to the database.
Process-level settings that live in the environment here (master key, cache, retries, cooldown) cannot be
changed from a request, so they are returned as ready-to-paste ``LLMGATE_*`` suggestions instead.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any
from urllib.parse import urlparse

from .db import Database, now
from .errors import GatewayError

# LiteLLM provider prefix → llmgate engine kind
PREFIX_KIND = {"hosted_vllm": "vllm", "vllm": "vllm", "ollama": "ollama", "ollama_chat": "ollama", "huggingface": "tgi",
               "openai": "other", "text-completion-openai": "other", "azure": "other"}
STRATEGIES = {"simple-shuffle": "weighted", "least-busy": "least-busy", "latency-based-routing": "latency",
              "usage-based-routing": "usage", "usage-based-routing-v2": "usage", "cost-based-routing": "cost"}


def _env(value: Any) -> Any:
    """``os.environ/NAME`` → the variable's value (LiteLLM's secret reference syntax)."""
    if isinstance(value, str) and value.startswith("os.environ/"):
        return os.environ.get(value.split("/", 1)[1])
    return value


def split_litellm_model(model: str) -> tuple[str, str]:
    """``hosted_vllm/Qwen/Qwen2.5-7B`` → (``vllm``, ``Qwen/Qwen2.5-7B``); unknown prefixes stay part of the name."""
    head, _, rest = model.partition("/")
    return (PREFIX_KIND[head], rest) if head in PREFIX_KIND and rest else ("other", model)


def _provider_name(api_base: str) -> str:
    u = urlparse(api_base)
    return re.sub(r"[^A-Za-z0-9_.-]", "-", f"{u.hostname}-{u.port}" if u.port else str(u.hostname))


def _ensure_provider(db: Database, api_base: str, kind: str, api_key: str | None, timeout: float | None,
                     name: str | None = None) -> dict[str, Any]:
    base = api_base.rstrip("/")
    existing = db.one("SELECT * FROM providers WHERE base_url = ?", (base,))
    if existing:
        return existing
    pid = db.insert("INSERT INTO providers (name, kind, base_url, api_key, timeout_s, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (name or _provider_name(base), kind, base, api_key, timeout, now()))
    return db.one("SELECT * FROM providers WHERE id = ?", (pid,))  # type: ignore[return-value]


def add_deployment(db: Database, entry: dict[str, Any]) -> dict[str, Any]:
    """Register one LiteLLM ``model_list`` entry as provider + model + route. Idempotent per (model, provider, upstream)."""
    name = entry.get("model_name")
    params = entry.get("litellm_params") or {}
    info = entry.get("model_info") or {}
    if not name or not params.get("model"):
        raise GatewayError(400, "Each deployment needs model_name and litellm_params.model.", code="invalid_deployment")
    api_base = _env(params.get("api_base"))
    if not api_base or not str(api_base).startswith(("http://", "https://")):
        raise GatewayError(400, f"'{name}': litellm_params.api_base must be an http(s) URL (self-hosted engines only).", code="invalid_deployment")
    kind, upstream = split_litellm_model(params["model"])
    provider = _ensure_provider(db, str(api_base), kind, _env(params.get("api_key")), params.get("timeout") or params.get("request_timeout"),
                         params.get("provider_name"))

    mode = info.get("mode", "chat")
    model = db.one("SELECT * FROM models WHERE name = ?", (name,))
    if model is None:
        mid = db.insert(
            "INSERT INTO models (name, kind, context_length, input_price_per_m, output_price_per_m, max_completion_tokens, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (name, mode if mode in ("chat", "embedding", "rerank") else "chat", info.get("max_input_tokens") or info.get("max_tokens"),
             float(info.get("input_cost_per_token", 0)) * 1_000_000, float(info.get("output_cost_per_token", 0)) * 1_000_000,
             info.get("max_output_tokens"), now()))
        model = db.one("SELECT * FROM models WHERE id = ?", (mid,))
    assert model is not None
    route = db.one("SELECT * FROM model_routes WHERE model_id = ? AND provider_id = ? AND upstream_model = ?",
                   (model["id"], provider["id"], upstream))
    if route is None:
        rid = db.insert(
            "INSERT INTO model_routes (model_id, provider_id, upstream_model, priority, weight, rpm_limit, tpm_limit, max_parallel) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (model["id"], provider["id"], upstream, int(params.get("order", 0)), int(params.get("weight", 1)),
             params.get("rpm"), params.get("tpm"), params.get("max_parallel_requests")))
        route = db.one("SELECT * FROM model_routes WHERE id = ?", (rid,))
    return {"model": model["name"], "provider": provider["name"], "route_id": route["id"], "upstream_model": upstream}  # type: ignore[index]


class _DryRun(Exception):
    """Raised inside the transaction to roll everything back after a preview."""


def import_config(db: Database, cfg: dict[str, Any], dry_run: bool = False) -> dict[str, Any]:
    """Apply a parsed LiteLLM config. Returns what was created plus settings to apply by environment variable."""
    report: dict[str, Any] = {"deployments": [], "aliases": [], "fallbacks": {}, "strategy": None, "warnings": [], "env": {}}
    rs = {**(cfg.get("litellm_settings") or {}), **(cfg.get("router_settings") or {})}

    def _apply() -> None:
        for entry in cfg.get("model_list") or []:
            if str(entry.get("model_name", "")).endswith("*"):
                report["warnings"].append(f"skipped wildcard model '{entry['model_name']}' (register concrete models instead)")
                continue
            try:
                report["deployments"].append(add_deployment(db, entry))
            except GatewayError as exc:
                report["warnings"].append(exc.message)

        strategy = STRATEGIES.get(rs.get("routing_strategy", "simple-shuffle"))
        if rs.get("routing_strategy") and strategy is None:
            report["warnings"].append(f"unsupported routing_strategy '{rs['routing_strategy']}'; using weighted")
        report["strategy"] = strategy or "weighted"
        names = sorted({d["model"] for d in report["deployments"]})
        if strategy and names:
            db.execute(f"UPDATE models SET routing_strategy = ? WHERE name IN ({','.join('?' * len(names))})", [strategy, *names])

        for column, key in (("fallbacks", "fallbacks"), ("context_window_fallbacks", "context_window_fallbacks")):
            for rule in rs.get(key) or []:
                for primary, backups in (rule or {}).items():
                    if db.one("SELECT 1 FROM models WHERE name = ?", (primary,)):
                        db.execute(f"UPDATE models SET {column} = ? WHERE name = ?", (json.dumps(list(backups)), primary))
                        report["fallbacks"].setdefault(primary, {})[column] = list(backups)
                    else:
                        report["warnings"].append(f"{key}: unknown model '{primary}'")

        for alias, target in (rs.get("model_group_alias") or {}).items():
            if db.one("SELECT 1 FROM models WHERE name = ?", (target,)) and not db.one("SELECT 1 FROM models WHERE name = ?", (alias,)):
                db.execute("INSERT INTO model_aliases (alias, model_name) VALUES (?, ?) ON CONFLICT(alias) DO UPDATE SET model_name = excluded.model_name",
                           (alias, target))
                report["aliases"].append({"alias": alias, "model": target})
            else:
                report["warnings"].append(f"model_group_alias '{alias}' → '{target}' skipped (unknown target or name clash)")

    try:
        with db.transaction():
            _apply()
            if dry_run:
                raise _DryRun
    except _DryRun:
        pass

    env = report["env"]
    if (cfg.get("general_settings") or {}).get("master_key"):
        env["LLMGATE_ADMIN_TOKEN"] = "<your master_key>"
    cache = (cfg.get("litellm_settings") or {}).get("cache")
    if cache:
        env["LLMGATE_CACHE_TTL_S"] = str(((cfg.get("litellm_settings") or {}).get("cache_params") or {}).get("ttl", 600))
    if rs.get("num_retries") is not None:
        env["LLMGATE_UPSTREAM_RETRIES"] = str(rs["num_retries"])
    if rs.get("cooldown_time") is not None:
        env["LLMGATE_COOLDOWN_S"] = str(rs["cooldown_time"])
    if rs.get("allowed_fails") is not None:
        env["LLMGATE_COOLDOWN_AFTER_FAILS"] = str(rs["allowed_fails"])
    return report


def parse_yaml(text: str) -> dict[str, Any]:
    import yaml

    try:
        cfg = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise GatewayError(400, f"Invalid YAML: {exc}", code="invalid_yaml") from None
    if not isinstance(cfg, dict):
        raise GatewayError(400, "The config must be a YAML mapping.", code="invalid_yaml")
    return cfg
