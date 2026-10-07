"""Populate a database with realistic data: providers, models, services/keys, customers, guardrails and usage history.

Used by ``llmgate dev`` (first run), ``llmgate seed`` and the demo. Inserts go straight into the database, so it works
before the server starts. Deterministic for a given ``rng_seed``.
"""

from __future__ import annotations

import json
import math
import random
import time
from typing import Any

from . import seed_data as D
from .db import Database, now
from .security import generate_api_key
from .usage import record_usage

KEY_PREFIX = "sk-lg-"


def _json(v: Any) -> str | None:
    return json.dumps(v) if v else None


def _insert_providers(db: Database, urls: dict[str, str | None]) -> dict[str, dict[str, Any]]:
    out = {}
    for name, kind, engine, enabled, quant, data_collection in D.PROVIDERS:
        url = urls.get(engine) if engine else "http://127.0.0.1:11434/v1"
        pid = db.insert("INSERT INTO providers (name, kind, base_url, enabled, quantization, data_collection, created_at) VALUES (?,?,?,?,?,?,?)",
                        (name, kind, url, enabled, quant, data_collection, now()))
        out[name] = {"id": pid, "name": name}
    return out


def _insert_models(db: Database, providers: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out = {}
    for (name, kind, ctx, pin, pout, params, modal, max_out, strategy, fallbacks, ctx_fb, routes, desc) in D.MODELS:
        mid = db.insert(
            "INSERT INTO models (name, description, kind, context_length, input_price_per_m, output_price_per_m, supported_parameters, "
            "input_modalities, output_modalities, max_completion_tokens, routing_strategy, fallbacks, context_window_fallbacks, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, desc, kind, ctx, pin, pout, _json(params), json.dumps(modal), json.dumps(["text"]), max_out, strategy,
             _json(fallbacks), _json(ctx_fb), now()))
        out[name] = {"id": mid, "routes": []}
        for provider, upstream, prio, weight, rin, rout, rpm in routes:
            rid = db.insert("INSERT INTO model_routes (model_id, provider_id, upstream_model, priority, weight, input_price_per_m, "
                            "output_price_per_m, rpm_limit) VALUES (?,?,?,?,?,?,?,?)",
                            (mid, providers[provider]["id"], upstream, prio, weight, rin, rout, rpm))
            out[name]["routes"].append({"id": rid, "provider_id": providers[provider]["id"], "upstream": upstream, "weight": weight,
                                        "in": rin if rin is not None else pin, "out": rout if rout is not None else pout})
    for alias, target in D.ALIASES.items():
        db.execute("INSERT INTO model_aliases (alias, model_name) VALUES (?, ?)", (alias, target))
    return out


def _insert_services(db: Database) -> tuple[list[dict[str, Any]], dict[str, str]]:
    keys, secrets = [], {}
    for sname, desc, limits, key_specs in D.SERVICES:
        sid = db.insert(
            "INSERT INTO services (name, description, monthly_budget_usd, monthly_token_limit, budget_duration_s, budget_anchor, rpm_limit, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (sname, desc, limits.get("budget"), limits.get("tokens"), limits["budget_days"] * 86400 if "budget_days" in limits else None,
             now() if "budget_days" in limits else None, limits.get("rpm"), now() - 20 * 86400))
        for kname, allowed, kl, status in key_specs:
            plaintext, prefix, digest = generate_api_key(KEY_PREFIX)
            kid = db.insert(
                "INSERT INTO api_keys (service_id, name, key_prefix, key_hash, enabled, allowed_models, rpm_limit, tpm_limit, max_parallel, "
                "monthly_budget_usd, budget_duration_s, budget_anchor, model_budgets, expires_at, last_used_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (sid, kname, prefix, digest, int(status == "active"), _json(allowed), kl.get("rpm"), kl.get("tpm"), kl.get("parallel"),
                 kl.get("budget"), kl["budget_days"] * 86400 if "budget_days" in kl else None, now() if "budget_days" in kl else None,
                 _json(kl.get("model_budgets")), now() + kl["expires_in_days"] * 86400 if "expires_in_days" in kl else None,
                 now() - 120, now() - 20 * 86400))
            keys.append({"id": kid, "service_id": sid, "service": sname, "name": kname, "allowed": allowed, "status": status})
            secrets[f"{sname}/{kname}"] = plaintext
    return keys, secrets


def _insert_policy(db: Database) -> None:
    for uid, alias, budget, blocked in D.CUSTOMERS:
        db.insert("INSERT INTO customers (user_id, alias, max_budget_usd, blocked, created_at) VALUES (?,?,?,?,?)", (uid, alias, budget, blocked, now()))
    for name, kind, mode, apply_to, config, default_on in D.GUARDRAILS:
        db.insert("INSERT INTO guardrails (name, kind, mode, apply_to, config, default_on, created_at) VALUES (?,?,?,?,?,?,?)",
                  (name, kind, mode, apply_to, json.dumps(config), default_on, now()))
    for email, role in D.PENDING_USERS:  # waiting for their first SSO login; the role is applied then
        db.insert("INSERT INTO users (issuer, subject, email, role, role_source, created_at) VALUES ('', ?, ?, ?, 'manual', ?)",
                  (f"pending:{email}", email, role, now()))


# ---------------------------------------------------------------------------------------------- usage history

_HOURS = [0.2, 0.15, 0.1, 0.1, 0.1, 0.2, 0.4, 0.8, 1.4, 2.0, 2.4, 2.3, 1.8, 2.2, 2.5, 2.4, 2.1, 1.6, 1.0, 0.8, 0.7, 0.6, 0.4, 0.3]  # office-hours curve
_SERVICE_WEIGHT = {"chatbot-web": 8, "doc-summarizer": 3, "internal-search": 5, "data-science": 2, "ci-pipeline": 1, "dev": 1}
_SIZE_FACTOR = {D.QWEN72: 3.0, D.DEEPSEEK: 2.0, D.LLAMA: 1.0, D.QWEN7: 0.9, D.MISTRAL: 0.9, D.QWEN_VL: 1.2}


def _lognormal(rng: random.Random, median: float, sigma: float, lo: int, hi: int) -> int:
    return int(min(hi, max(lo, median * math.exp(rng.gauss(0, sigma)))))


def backfill_usage(db: Database, models: dict[str, dict[str, Any]], keys: list[dict[str, Any]], *, days: int = 14, rows: int = 800,
                   recent: int = 70, rng_seed: int = 7) -> int:
    """Synthetic but plausible history: office-hours curve, quieter weekends, errors, cache hits, fallbacks, guardrail hits."""
    rng = random.Random(rng_seed)
    t_now = time.time()
    usable = [k for k in keys if k["status"] == "active"]
    service_choices = [k for k in usable for _ in range(_SERVICE_WEIGHT.get(k["service"], 1))]
    kinds = {m[0]: m[1] for m in D.MODELS}

    def timestamp(recent_only: bool) -> float:
        while True:
            back = rng.random() * (1 if recent_only else days)
            ts = t_now - back * 86400
            hour = int((ts % 86400) // 3600)
            weekday = int(((ts // 86400) + 4) % 7)  # 0 = Monday
            if rng.random() < _HOURS[hour] / 2.5 * (0.45 if weekday >= 5 else 1.0):
                return ts

    n = 0
    for i in range(rows + recent):
        key = rng.choice(service_choices)
        pool = [m for m in (key["allowed"] or D.CHAT_MODELS) if m in models]
        model = rng.choices(pool, weights=[3 if m == D.LLAMA else 2 if m == D.QWEN7 else 1 for m in pool])[0]
        kind = kinds[model]
        route = rng.choices(models[model]["routes"], weights=[r["weight"] for r in models[model]["routes"]])[0]
        endpoint = {"embedding": "embeddings", "rerank": "rerank"}.get(kind) or rng.choices(["chat", "completions", "responses", "messages"], [88, 4, 5, 3])[0]
        size = _SIZE_FACTOR.get(model, 1.0)
        pt = _lognormal(rng, 450 if kind == "chat" else 900, 0.9, 8, 6000)
        ct = _lognormal(rng, 260, 0.8, 4, 3000) if kind == "chat" else 0
        ttft = (110 + 90 * size + rng.random() * 70) * (1.4 if route["provider_id"] != models[D.LLAMA]["routes"][0]["provider_id"] else 1)
        latency = ttft + ct * 14 * size + rng.random() * 40
        stream = int(kind == "chat" and rng.random() < 0.55)
        roll = rng.random()
        status, error, attempts, requested, guardrail, cache_hit, finish = 200, None, 1, None, None, 0, ("length" if ct > 1800 else "stop") if kind == "chat" else None
        if roll < 0.025:
            status, error, attempts, pt, ct, finish = 502, "upstream 503: engine overloaded", 2, 0, 0, None
        elif roll < 0.035:
            status, error, pt, ct, latency, ttft, finish = 429, "rate_limit_exceeded: 600 requests/minute for this key", 0, 0, 3, None, None
        elif roll < 0.040:
            status, error, pt, ct, latency, ttft, finish = 402, "budget_exceeded: monthly budget reached for this key", 0, 0, 3, None, None
        elif roll < 0.048:
            status, error, pt, ct, latency, ttft, finish, guardrail = 400, "guardrail_blocked: Blocked by guardrail 'block-secrets' (API_KEY).", 0, 0, 4, None, None, "block-secrets"
        elif roll < 0.10 and kind == "chat":
            cache_hit, pt, ct, latency, ttft, route = 1, 0, 0, 4 + rng.random() * 6, None, None  # type: ignore[assignment]
        elif roll < 0.12 and model == D.LLAMA:
            requested, attempts = D.QWEN72, 2
        elif roll < 0.14 and kind == "chat":
            guardrail = "mask-pii"
        if cache_hit:
            stream = 0
        price_in, price_out = (route["in"], route["out"]) if route else (0.0, 0.0)
        cost = (pt * price_in + ct * price_out) / 1e6
        tags = rng.choice(D.TAGS)
        record_usage(
            db, ts=timestamp(i >= rows), request_id=f"gen-seed{rng.getrandbits(48):012x}", service_id=key["service_id"], api_key_id=key["id"],
            provider_id=route["provider_id"] if route else None, model=model, upstream_model=route["upstream"] if route else None,
            endpoint=endpoint, stream=stream, prompt_tokens=pt, completion_tokens=ct, cost_usd=cost, latency_ms=round(latency, 1),
            ttft_ms=round(ttft, 1) if (ttft and stream) else None, status_code=status, success=int(status == 200), error=error, attempts=attempts,
            app_title=D.APPS.get(key["service"]), referer="https://app.example.com" if key["service"] == "chatbot-web" else None,
            end_user=rng.choice(D.END_USERS) if key["service"] == "chatbot-web" else None, requested_model=requested, finish_reason=finish,
            tags=json.dumps(tags) if tags else None, cache_hit=cache_hit, guardrail=guardrail)
        n += 1
    return n


def populate(db: Database, engine_a: str, engine_b: str, *, with_usage: bool = True, days: int = 14, rows: int = 800) -> dict[str, Any] | None:
    """Seed an empty database. Returns a summary with the generated keys, or ``None`` if the database already has content."""
    if any(db.scalar(f"SELECT COUNT(*) FROM {t}", default=0) for t in ("providers", "models", "services")):
        return None
    with db.transaction():
        providers = _insert_providers(db, {"a": engine_a, "b": engine_b})
        models = _insert_models(db, providers)
        keys, secrets = _insert_services(db)
        _insert_policy(db)
        usage_rows = backfill_usage(db, models, keys, days=days, rows=rows) if with_usage else 0
    return {"providers": len(providers), "models": len(models), "services": len(D.SERVICES), "keys": len(keys), "usage_rows": usage_rows,
            "customers": len(D.CUSTOMERS), "guardrails": len(D.GUARDRAILS), "aliases": len(D.ALIASES), "keys_plaintext": secrets,
            "models_list": list(models)}


def sync_engine_urls(db: Database, engine_a: str, engine_b: str) -> None:
    """The mock engines may have moved to other ports since the database was seeded; keep the providers pointing at them."""
    for name, url in (("vllm-a100-1", engine_a), ("vllm-a100-2", engine_b)):
        db.execute("UPDATE providers SET base_url = ? WHERE name = ? AND base_url != ?", (url, name, url))
