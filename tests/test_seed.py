from __future__ import annotations

import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from llmgate import seed, seed_data as D
from llmgate.config import Settings
from llmgate.db import Database
from llmgate.main import create_app

A, B = "http://127.0.0.1:8101/v1", "http://127.0.0.1:8102/v1"
ADMIN = {"Authorization": "Bearer test-admin"}


@pytest.fixture
def seeded(tmp_path):
    db = Database(str(tmp_path / "seed.db"))
    summary = seed.populate(db, A, B)
    yield db, summary
    db.close()


def test_populate_fills_every_area_and_refuses_a_populated_database(seeded):
    db, s = seeded
    assert (s["providers"], s["models"], s["services"], s["keys"]) == (3, 10, 6, 9)
    assert s["usage_rows"] >= 800 and s["customers"] == 5 and s["guardrails"] == 5 and s["aliases"] == 4
    assert db.scalar("SELECT COUNT(*) FROM users WHERE issuer = ''") == 2  # pre-provisioned, waiting for first SSO login
    assert seed.populate(db, A, B) is None  # never doubles up on an existing database
    assert db.scalar("SELECT COUNT(*) FROM models") == 10


def test_every_seeded_route_points_at_a_model_its_engine_really_serves(seeded):
    """The seed is only useful if the gateway can actually answer with it, so the names must line up with the mocks."""
    db, _ = seeded
    served = {"vllm-a100-1": set(D.ENGINE_A_MODELS), "vllm-a100-2": set(D.ENGINE_B_MODELS)}
    routes = db.query("SELECT r.upstream_model, p.name AS provider FROM model_routes r JOIN providers p ON p.id = r.provider_id")
    assert len(routes) >= 10
    for r in routes:
        assert r["upstream_model"] in served[r["provider"]], r
    for name in D.ENGINE_A_MODELS + D.ENGINE_B_MODELS:  # and nothing a mock serves is orphaned
        assert db.scalar("SELECT COUNT(*) FROM model_routes WHERE upstream_model = ?", (name,)) >= 1, name


def test_usage_history_is_plausible_and_consistent(seeded):
    db, s = seeded
    total = db.scalar("SELECT COUNT(*) FROM usage_logs")
    assert total == s["usage_rows"]
    assert db.scalar("SELECT COUNT(*) FROM usage_logs WHERE ts > strftime('%s','now') - 86400") >= 40  # the 24 h chart is not empty
    ok = db.scalar("SELECT COUNT(*) FROM usage_logs WHERE success = 1") / total
    assert 0.90 < ok < 0.99  # mostly healthy, with a realistic sprinkling of failures
    assert db.scalar("SELECT COUNT(DISTINCT status_code) FROM usage_logs") >= 4
    assert db.scalar("SELECT SUM(cache_hit) FROM usage_logs") > 0 and db.scalar("SELECT COUNT(*) FROM usage_logs WHERE requested_model IS NOT NULL") > 0
    assert db.scalar("SELECT COUNT(*) FROM usage_logs WHERE guardrail IS NOT NULL") > 0 and db.scalar("SELECT COUNT(*) FROM usage_logs WHERE tags IS NOT NULL") > 0
    assert db.scalar("SELECT COUNT(DISTINCT model) FROM usage_logs") >= 8 and db.scalar("SELECT COUNT(DISTINCT endpoint) FROM usage_logs") >= 3
    # referential integrity and arithmetic
    assert db.scalar("SELECT COUNT(*) FROM usage_logs WHERE service_id NOT IN (SELECT id FROM services)") == 0
    assert db.scalar("SELECT COUNT(*) FROM usage_logs WHERE api_key_id NOT IN (SELECT id FROM api_keys)") == 0
    assert db.scalar("SELECT COUNT(*) FROM usage_logs WHERE provider_id IS NOT NULL AND provider_id NOT IN (SELECT id FROM providers)") == 0
    assert db.scalar("SELECT COUNT(*) FROM usage_logs WHERE total_tokens != prompt_tokens + completion_tokens") == 0
    assert db.scalar("SELECT COUNT(*) FROM usage_logs WHERE cache_hit = 1 AND (cost_usd != 0 OR provider_id IS NOT NULL)") == 0  # cache hits are free
    assert db.scalar("SELECT COUNT(*) FROM usage_logs WHERE success = 0 AND cost_usd != 0") == 0


def test_the_seed_is_deterministic():
    counts = []
    for _ in range(2):
        db = Database(":memory:")
        s = seed.populate(db, A, B, rows=120)
        counts.append((s["usage_rows"], db.scalar("SELECT SUM(prompt_tokens) FROM usage_logs"), db.scalar("SELECT SUM(cache_hit) FROM usage_logs")))
        db.close()
    assert counts[0] == counts[1]


def test_seeded_gateway_answers_real_requests(tmp_path, mock_factory):
    """Seed against live mock engines, then use the seeded keys exactly as a developer would."""
    a, b = mock_factory(D.ENGINE_A_MODELS), mock_factory(D.ENGINE_B_MODELS)
    path = tmp_path / "live.db"
    db = Database(str(path))
    s = seed.populate(db, a.base_url, b.base_url)
    db.close()
    keys = s["keys_plaintext"]
    auth = lambda name: {"Authorization": f"Bearer {keys[name]}"}  # noqa: E731
    chat = lambda name, model, **kw: gw.post("/v1/chat/completions", headers=auth(name), json={"model": model, "messages": [{"role": "user", "content": "hi"}], **kw})  # noqa: E731
    settings = Settings(database_path=str(path), admin_token="test-admin", health_interval_s=0, _env_file=None)
    with TestClient(create_app(settings)) as gw:
        for provider in gw.get("/admin/api/providers", headers=ADMIN).json():
            if provider["enabled"]:
                assert gw.post(f"/admin/api/providers/{provider['id']}/check", headers=ADMIN).json()["status"] == "healthy"

        assert chat("dev/dev-key", D.DEV_CHAT).status_code == 200
        assert chat("chatbot-web/prod", D.LLAMA).status_code == 200
        r = chat("chatbot-web/prod", "gpt-4")  # an alias: served by the second engine
        assert r.status_code == 200 and r.json()["model"] == D.QWEN72 and r.headers["x-llmgate-provider"] == "vllm-a100-2"
        assert gw.post("/v1/embeddings", headers=auth("internal-search/prod"), json={"model": D.BGE, "input": ["a", "b"]}).status_code == 200
        assert gw.post("/v1/rerank", headers=auth("internal-search/prod"), json={"model": D.RERANK, "query": "red", "documents": ["red", "blue"]}).status_code == 200

        assert chat("internal-search/prod", D.LLAMA).json()["error"]["code"] == "model_not_allowed"  # key allow-list
        assert chat("data-science/old-laptop", D.LLAMA).status_code == 403  # revoked key
        assert chat("chatbot-web/staging", D.QWEN72).status_code == 403
        assert chat("chatbot-web/prod", D.LLAMA, user="user-spam").json()["error"]["code"] == "customer_blocked"

        masked = gw.post("/v1/chat/completions", headers=auth("dev/dev-key"),
                         json={"model": D.DEV_CHAT, "messages": [{"role": "user", "content": "mail me at kim@example.com"}]})
        assert masked.status_code == 200  # the seeded mask-pii rule lets it through, but the engine must only ever see the masked text
        seen = str(a.app.state.mock.last_payload["messages"])
        assert "[EMAIL]" in seen and "kim@example.com" not in seen
        blocked = gw.post("/v1/chat/completions", headers=auth("dev/dev-key"),
                          json={"model": D.DEV_CHAT, "messages": [{"role": "user", "content": "token sk-abcdefghijklmnopqrstuvwx"}]})
        assert blocked.json()["error"]["code"] == "guardrail_blocked"

        catalog = {m["id"]: m for m in gw.get("/api/v1/models").json()["data"]}
        assert len(catalog) == 10 and catalog[D.QWEN_VL]["architecture"]["input_modalities"] == ["text", "image"]
        assert catalog[D.LLAMA]["status"] == "available" and catalog[D.QWEN72]["supported_parameters"] == ["tools", "response_format", "reasoning"]
        overview = gw.get("/admin/api/overview?days=14", headers=ADMIN).json()
        assert overview["totals"]["requests"] >= 800 and overview["counts"]["providers_healthy"] == 2
        assert {s["name"] for s in gw.get("/admin/api/services", headers=ADMIN).json()} == {"dev", "chatbot-web", "doc-summarizer", "internal-search", "data-science", "ci-pipeline"}


def test_sync_engine_urls_follows_moved_engines(seeded):
    db, _ = seeded
    seed.sync_engine_urls(db, "http://127.0.0.1:9101/v1", "http://127.0.0.1:9102/v1")
    urls = {r["name"]: r["base_url"] for r in db.query("SELECT name, base_url FROM providers")}
    assert urls["vllm-a100-1"].endswith(":9101/v1") and urls["vllm-a100-2"].endswith(":9102/v1") and urls["ollama-edge"].endswith(":11434/v1")


def test_seed_command_line(tmp_path):
    env = {**os.environ, "LLMGATE_DATABASE_PATH": str(tmp_path / "cli.db")}
    run = lambda *a: subprocess.run([sys.executable, "-m", "llmgate", "seed", *a], env=env, capture_output=True, text=True, encoding="utf-8", timeout=90)  # noqa: E731
    first = run("--no-usage")
    assert first.returncode == 0 and "usage_rows\": 0" in first.stdout and "sk-lg-" in first.stdout
    again = run()
    assert again.returncode != 0 and "already has data" in (again.stderr + again.stdout)
    assert run("--reset").returncode == 0
