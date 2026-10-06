from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from llmgate import dev
from llmgate.config import Settings
from llmgate.db import Database
from llmgate.main import create_app


def test_bootstrap_seeds_a_usable_gateway_exactly_once(tmp_path):
    path = tmp_path / "dev.db"
    db = Database(str(path))
    creds = dev.bootstrap(db, "http://127.0.0.1:9/v1", tmp_path / "creds.json")
    assert creds and creds["models"] == ["dev-chat", "dev-embed"]
    assert json.loads((tmp_path / "creds.json").read_text())["api_key"] == creds["api_key"]
    assert dev.bootstrap(db, "http://127.0.0.1:9/v1", tmp_path / "creds.json") is None  # a populated database is left alone
    assert db.scalar("SELECT COUNT(*) FROM services") == 1 and db.scalar("SELECT COUNT(*) FROM providers") == 1
    db.close()

    settings = Settings(database_path=str(path), admin_token=dev.DEV_ADMIN_TOKEN, health_interval_s=0, _env_file=None)
    with TestClient(create_app(settings)) as gw:  # the seeded key works against the seeded registry
        r = gw.get("/v1/models", headers={"Authorization": f"Bearer {creds['api_key']}"})
        assert [m["id"] for m in r.json()["data"]] == ["dev-chat", "dev-embed"]
        prov = gw.get("/admin/api/providers", headers={"Authorization": "Bearer dev-admin"}).json()
        assert [p["name"] for p in prov] == ["mock-vllm"]


def test_bootstrap_without_an_engine_only_creates_the_service_and_key(tmp_path):
    db = Database(str(tmp_path / "d.db"))
    creds = dev.bootstrap(db, None, tmp_path / "c.json")
    assert creds and creds["models"] == [] and db.scalar("SELECT COUNT(*) FROM providers") == 0
    assert db.scalar("SELECT COUNT(*) FROM api_keys") == 1
    db.close()


def test_dev_options_do_not_pin_ports_by_default():
    p = argparse.ArgumentParser()
    dev.add_arguments(p)
    a = p.parse_args([])
    # None (not 8080) so run() can tell "user asked for 8080" from "nobody asked" and only move the latter
    assert (a.host, a.port, a.db, a.mock_port, a.idp_port) == (None, None, None, None, None)
    assert (a.reset, a.no_reload, a.no_mock, a.sso) == (False, False, False, False)
    assert dev.DEFAULTS["port"] == 8080 and len(dev.DEV_JWT_SECRET) >= 32  # long enough for HS256
    assert json.loads(json.dumps(dev.DEFAULTS))["db"] == "data/dev.db"


def test_choose_port_moves_when_busy_but_never_overrides_an_explicit_choice(capsys):
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    busy.listen()
    port = busy.getsockname()[1]
    try:
        assert dev.choose_port("127.0.0.1", port, False, "gateway", "--port") != port  # moved to a free one, and said so
        assert f"port {port} is not available" in capsys.readouterr().out
        with pytest.raises(SystemExit) as exc:
            dev.choose_port("127.0.0.1", port, True, "gateway", "--port")  # the user asked for exactly this port
        assert "--port" in str(exc.value) and str(port) in str(exc.value)
    finally:
        busy.close()
    assert dev.choose_port("127.0.0.1", port, True, "gateway", "--port") == port  # free again: honoured as asked


def test_credentials_file_is_per_database(tmp_path):
    assert dev.credentials_path(tmp_path / "dev.db").name == "dev-credentials.json"
    assert dev.credentials_path(tmp_path / "other.db").name == "other-credentials.json"


def test_seeded_mock_provider_follows_the_mock_when_its_port_changes(tmp_path):
    db = Database(str(tmp_path / "d.db"))
    dev.bootstrap(db, "http://127.0.0.1:8101/v1", tmp_path / "c.json")
    assert dev.bootstrap(db, "http://127.0.0.1:8102/v1", tmp_path / "c.json") is None  # second run: mock had to move
    assert db.scalar("SELECT base_url FROM providers WHERE name = 'mock-vllm'") == "http://127.0.0.1:8102/v1"
    db.close()


def test_python_dash_m_and_lazy_app_work(monkeypatch, tmp_path):
    out = subprocess.run([sys.executable, "-m", "llmgate", "--help"], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0 and "dev" in out.stdout and "import-litellm" in out.stdout  # `python -m llmgate` exists

    import llmgate.main as main_mod
    from llmgate.config import get_settings

    monkeypatch.setenv("LLMGATE_DATABASE_PATH", str(tmp_path / "lazy.db"))
    get_settings.cache_clear()
    monkeypatch.delitem(vars(main_mod), "app", raising=False)
    assert "app" not in vars(main_mod)  # importing builds nothing...
    application = main_mod.app  # ...`uvicorn llmgate.main:app` builds it on first access
    assert application.title == "llmgate" and (tmp_path / "lazy.db").exists()
    with pytest.raises(AttributeError):
        main_mod.nonexistent
    monkeypatch.delitem(vars(main_mod), "app", raising=False)
    get_settings.cache_clear()


def test_llmgate_dev_really_starts_and_stops(tmp_path):
    """The thing that was broken for a user: `llmgate dev` must come up and answer, from any working directory."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen([sys.executable, "-m", "llmgate", "dev", "--no-reload", "--no-mock", "--port", str(port), "--db", "dev.db"],
                            cwd=tmp_path, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        for _ in range(100):
            if proc.poll() is not None:
                raise AssertionError("llmgate dev exited early:\n" + proc.stdout.read().decode("utf-8", "replace")[-1500:])
            try:
                if httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=0.5).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.2)
        else:
            raise AssertionError("llmgate dev did not answer within 20 s")
        base = f"http://127.0.0.1:{port}"
        assert httpx.get(base + "/dashboard/", timeout=5).status_code == 200
        assert httpx.get(base + "/docs", timeout=5).status_code == 200
        assert httpx.get(base + "/admin/api/overview", headers={"Authorization": "Bearer dev-admin"}, timeout=5).status_code == 200
        assert (tmp_path / "dev-credentials.json").exists()  # created next to the database, in the caller's directory
    finally:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        else:
            proc.terminate()
        proc.wait(timeout=20)
        proc.stdout.close()
