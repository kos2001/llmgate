from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

from conftest import ADMIN, admin, bearer, sso_login
from llmgate import seed, storage
from llmgate.config import Settings
from llmgate.db import Database
from llmgate.main import create_app
from test_routing_prefs import chat, key, model, provider


@pytest.fixture
def populated(gw, mock_factory):
    p = provider(gw, "alpha", mock_factory())
    model(gw, "m", [{"provider_id": p["id"]}], input_price_per_m=10, output_price_per_m=10)
    k = key(gw)
    for i in range(5):
        assert chat(gw, k, content=f"question {i}", user=f"u{i % 2}", tags=["t"]).status_code == 200
    return gw, k


# ------------------------------------------------------------------------------------------ export

def test_csv_export_has_every_column_and_valid_rows(populated):
    gw, _ = populated
    r = gw.get("/admin/api/usage/export", headers=ADMIN)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv") and "attachment" in r.headers["content-disposition"]
    assert r.headers["cache-control"] == "no-store"
    assert r.content[:3] == b"\xef\xbb\xbf"  # UTF-8 BOM so Excel shows Korean text correctly
    rows = list(csv.DictReader(io.StringIO(r.content.decode("utf-8-sig"))))
    assert len(rows) == 5 and set(rows[0]) == set(__import__("llmgate.admin_storage", fromlist=["COLUMNS"]).COLUMNS)
    first = rows[0]
    assert first["model"] == "m" and first["service"] == "svc" and first["provider"] == "alpha" and first["status_code"] == "200"
    assert int(first["total_tokens"]) == int(first["prompt_tokens"]) + int(first["completion_tokens"]) and float(first["cost_usd"]) > 0
    assert first["time_utc"].endswith("+00:00") and first["end_user"] in ("u0", "u1")


def test_export_filters_formats_and_roles(populated, sso_gw=None):
    gw, k = populated
    chat(gw, k, model_name="ghost")  # a failed request
    errors = list(csv.DictReader(io.StringIO(gw.get("/admin/api/usage/export?only_errors=true", headers=ADMIN).content.decode("utf-8-sig"))))
    assert len(errors) == 1 and errors[0]["status_code"] == "404"
    assert len(list(csv.DictReader(io.StringIO(gw.get("/admin/api/usage/export?model=m", headers=ADMIN).content.decode("utf-8-sig"))))) == 5
    lines = gw.get("/admin/api/usage/export?format=jsonl", headers=ADMIN).text.strip().splitlines()
    assert len(lines) == 6 and json.loads(lines[-1])["request_id"].startswith("gen-")
    assert gw.get("/admin/api/usage/export").status_code == 401  # needs a credential


def test_export_neutralises_spreadsheet_formulas(populated):
    gw, k = populated
    evil = {"Authorization": f"Bearer {k['key']}", "X-Title": "=HYPERLINK(\"http://evil\",\"x\")"}
    gw.post("/v1/chat/completions", headers=evil, json={"model": "m", "user": "@SUM(1+1)", "messages": [{"role": "user", "content": "hi"}]})
    text = gw.get("/admin/api/usage/export", headers=ADMIN).content.decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(text)))
    attack = next(r for r in rows if "HYPERLINK" in r["app"])
    assert attack["app"].startswith("'=") and attack["end_user"].startswith("'@")  # text, not a formula, when opened in Excel/Sheets
    assert not any(c.startswith(("=", "@")) for r in rows for c in r.values())


def test_export_streams_more_than_one_page(tmp_path):
    db = Database(str(tmp_path / "big.db"))
    s = seed.populate(db, "http://127.0.0.1:1/v1", "http://127.0.0.1:2/v1", rows=1800, days=3)
    db.close()
    settings = Settings(database_path=str(tmp_path / "big.db"), admin_token="test-admin", health_interval_s=0, _env_file=None)
    with TestClient(create_app(settings)) as gw:
        r = gw.get("/admin/api/usage/export?days=3", headers=ADMIN)
        rows = list(csv.DictReader(io.StringIO(r.content.decode("utf-8-sig"))))
        assert len(rows) == s["usage_rows"] > 1000  # crosses the 1000-row page boundary without losing or repeating rows
        assert len({x["request_id"] for x in rows}) == len(rows)


# ------------------------------------------------------------------------------------ retention / stats

def test_prune_deletes_only_old_rows_in_batches():
    db = Database(":memory:")
    old, recent = time.time() - 40 * 86400, time.time() - 86400
    db.execute("INSERT INTO services (name, created_at) VALUES ('s', 0)")
    for i in range(storage._BATCH + 123):  # more than one delete batch
        db.insert("INSERT INTO usage_logs (ts, request_id, endpoint) VALUES (?, ?, 'chat')", (old, f"old{i}"))
    for i in range(7):
        db.insert("INSERT INTO usage_logs (ts, request_id, endpoint) VALUES (?, ?, 'chat')", (recent, f"new{i}"))
    assert storage.prune_table(db, "usage_logs", 30) == storage._BATCH + 123
    assert db.scalar("SELECT COUNT(*) FROM usage_logs") == 7 and db.scalar("SELECT MIN(ts) FROM usage_logs") == recent
    assert storage.prune_table(db, "usage_logs", 30) == 0  # idempotent
    with pytest.raises(ValueError):
        storage.prune_table(db, "api_keys", 1)  # only the two log tables may ever be pruned


def test_janitor_applies_the_configured_retention():
    db = Database(":memory:")
    for table, extra in (("usage_logs", "endpoint"), ("audit_logs", "actor, method, path")):
        marks = "'chat'" if table == "usage_logs" else "'x', 'POST', '/p'"
        cols = "ts, request_id, " + extra if table == "usage_logs" else "ts, " + extra
        vals = "?, 'r', " + marks if table == "usage_logs" else "?, " + marks
        db.execute(f"INSERT INTO {table} ({cols}) VALUES ({vals})", (time.time() - 100 * 86400,))
        db.execute(f"INSERT INTO {table} ({cols}) VALUES ({vals})", (time.time(),))
    assert storage.Janitor(db, 0, 0).prune_once() == {"usage_logs": 0, "audit_logs": 0}  # 0 = keep forever
    assert storage.Janitor(db, 30, 0).prune_once() == {"usage_logs": 1, "audit_logs": 0}  # audit untouched unless configured
    assert storage.Janitor(db, 30, 30).prune_once() == {"usage_logs": 0, "audit_logs": 1}


def test_storage_api_reports_and_prunes_with_the_right_roles(gw_factory, tmp_path, mock_factory, idp):
    db = Database(str(tmp_path / "s.db"))
    seed.populate(db, "http://127.0.0.1:1/v1", "http://127.0.0.1:2/v1", rows=200, days=40)
    db.close()
    settings = Settings(database_path=str(tmp_path / "s.db"), admin_token="test-admin", health_interval_s=0, usage_retention_days=90, _env_file=None)
    with TestClient(create_app(settings)) as gw:
        st = gw.get("/admin/api/storage", headers=ADMIN).json()
        assert st["rows"]["usage_logs"] > 200 and st["size_bytes"] > 0 and st["retention"] == {"usage_days": 90, "audit_days": None}
        assert st["oldest_usage_ts"] < time.time() - 30 * 86400
        assert gw.post("/admin/api/storage/prune?days=0.5", headers=ADMIN).status_code == 400  # refuses to wipe nearly everything
        done = gw.post("/admin/api/storage/prune?days=20", headers=ADMIN).json()
        assert done["usage_logs"] > 0 and done["audit_logs"] == 0
        assert gw.get("/admin/api/storage", headers=ADMIN).json()["oldest_usage_ts"] > time.time() - 21 * 86400
    # roles: a viewer may look and export, but not delete
    with TestClient(create_app(sso_settings_for(idp, tmp_path)), base_url="http://127.0.0.1") as sso:
        sso_login(sso, idp, "carol")
        assert sso.get("/admin/api/storage").status_code == 200 and sso.get("/admin/api/usage/export").status_code == 200
        r = sso.post("/admin/api/storage/prune?days=5", headers={"x-llmgate-csrf": "1"})
        assert r.status_code == 403 and r.json()["error"]["code"] == "insufficient_role"


def sso_settings_for(idp, tmp_path):
    from conftest import sso_settings

    return sso_settings(idp, database_path=str(tmp_path / "sso.db"))


# ----------------------------------------------------------------------------------------------- backup

def test_backup_is_a_consistent_openable_copy_and_never_overwrites(tmp_path):
    src = tmp_path / "live.db"
    db = Database(str(src))
    seed.populate(db, "http://127.0.0.1:1/v1", "http://127.0.0.1:2/v1", rows=100)
    out = tmp_path / "backups" / "copy.db"
    size = storage.backup(str(src), str(out))  # taken while `db` still holds the live connection open (WAL mode)
    assert size > 0 and out.exists()
    copy = sqlite3.connect(str(out))
    assert copy.execute("SELECT COUNT(*) FROM usage_logs").fetchone()[0] == db.scalar("SELECT COUNT(*) FROM usage_logs")
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    copy.close()
    with pytest.raises(FileExistsError):
        storage.backup(str(src), str(out))
    db.close()


def test_backup_command_line(tmp_path):
    src = tmp_path / "c.db"
    Database(str(src)).close()
    env = {**os.environ, "LLMGATE_DATABASE_PATH": str(src)}
    run = lambda *a: subprocess.run([sys.executable, "-m", "llmgate", "backup", *a], env=env, capture_output=True, text=True, encoding="utf-8", timeout=90)  # noqa: E731
    first = run(str(tmp_path / "out.db"))
    assert first.returncode == 0 and "Backed up" in first.stdout and (tmp_path / "out.db").exists()
    assert run(str(tmp_path / "out.db")).returncode != 0  # refuses to clobber the existing backup
    _ = admin, bearer  # (imported for the shared helpers above)
