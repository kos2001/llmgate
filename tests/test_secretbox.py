from __future__ import annotations

import os
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from conftest import ADMIN, admin, bearer
from llmgate import secretbox
from llmgate.config import Settings
from llmgate.db import Database
from llmgate.errors import GatewayError
from llmgate.main import create_app
from test_routing_prefs import key


@pytest.fixture(autouse=True)
def clean_keyring():
    """The key ring is process-wide; every test starts and ends without one so other tests see plaintext mode."""
    secretbox.configure(None)
    yield
    secretbox.configure(None)


def test_roundtrip_idempotence_and_plaintext_mode():
    assert secretbox.encrypt("s3cret") == "s3cret" and secretbox.decrypt("s3cret") == "s3cret"  # no key: nothing changes
    secretbox.configure(secretbox.generate_key())
    token = secretbox.encrypt("s3cret")
    assert token.startswith("enc:v1:") and "s3cret" not in token and token != secretbox.encrypt("s3cret")  # random IV per encryption
    assert secretbox.encrypt(token) == token  # never double-encrypts
    assert secretbox.decrypt(token) == "s3cret" and secretbox.decrypt("legacy-plaintext") == "legacy-plaintext"
    assert secretbox.encrypt(None) is None and secretbox.encrypt("") == "" and secretbox.decrypt(None) is None


def test_wrong_and_missing_keys_fail_loudly_without_leaking():
    secretbox.configure(secretbox.generate_key())
    token = secretbox.encrypt("s3cret")
    secretbox.configure(secretbox.generate_key())  # a different key
    with pytest.raises(GatewayError) as wrong:
        secretbox.decrypt(token)
    assert wrong.value.code == "encryption_key_wrong" and "s3cret" not in wrong.value.message and token not in wrong.value.message
    secretbox.configure(None)
    with pytest.raises(GatewayError) as missing:
        secretbox.decrypt(token)
    assert missing.value.code == "encryption_key_missing" and wrong.value.status_code == 500
    with pytest.raises(ValueError):
        secretbox.configure("not-a-fernet-key")


def test_key_rotation_with_a_key_ring(tmp_path):
    old, new = secretbox.generate_key(), secretbox.generate_key()
    db = Database(str(tmp_path / "r.db"))
    secretbox.configure(old)
    db.insert("INSERT INTO providers (name, base_url, api_key, created_at) VALUES ('a', 'http://x/v1', ?, 0)", (secretbox.encrypt("upstream-1"),))
    db.insert("INSERT INTO providers (name, base_url, api_key, created_at) VALUES ('b', 'http://y/v1', 'plain-2', 0)")  # a legacy plaintext row
    db.insert("INSERT INTO providers (name, base_url, created_at) VALUES ('c', 'http://z/v1', 0)")  # no key at all

    secretbox.configure(f"{new},{old}")  # new key first: it encrypts; the old one still decrypts
    preview = secretbox.rewrap_provider_keys(db, dry_run=True)
    assert preview == {"encrypted": 1, "rotated": 1, "unchanged": 0} and db.scalar("SELECT api_key FROM providers WHERE name = 'b'") == "plain-2"
    assert secretbox.rewrap_provider_keys(db) == {"encrypted": 1, "rotated": 1, "unchanged": 0}

    secretbox.configure(new)  # the old key can now be dropped
    rows = {r["name"]: r["api_key"] for r in db.query("SELECT name, api_key FROM providers")}
    assert secretbox.decrypt(rows["a"]) == "upstream-1" and secretbox.decrypt(rows["b"]) == "plain-2" and rows["c"] is None
    assert rows["a"].startswith("enc:v1:") and rows["b"].startswith("enc:v1:")
    db.close()


def test_provider_keys_are_encrypted_in_the_database_and_still_work(mock_factory):
    upstream = mock_factory(api_key="upstream-secret")  # this engine rejects requests that lack the right bearer token
    settings = Settings(database_path=":memory:", admin_token="test-admin", health_interval_s=0, encryption_key=secretbox.generate_key(), _env_file=None)
    with TestClient(create_app(settings)) as gw:
        p = admin(gw, "POST", "/providers", json={"name": "guarded", "base_url": upstream.base_url, "api_key": "upstream-secret"})
        assert p["has_api_key"] is True and "api_key" not in p  # never echoed back
        raw = gw.app.state.db.scalar("SELECT api_key FROM providers WHERE id = ?", (p["id"],))
        assert raw.startswith("enc:v1:") and "upstream-secret" not in raw  # what a stolen database file would contain

        admin(gw, "POST", "/models", json={"name": "m", "routes": [{"provider_id": p["id"], "upstream_model": "mock-chat"}]})
        k = key(gw)
        r = gw.post("/v1/chat/completions", headers=bearer(k["key"]), json={"model": "m", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 200, r.text  # the decrypted key reached the engine
        assert gw.post(f"/admin/api/providers/{p['id']}/check", headers=ADMIN).json()["status"] == "healthy"
        assert gw.post(f"/admin/api/providers/{p['id']}/sync-models", headers=ADMIN).json()["discovered"] == ["mock-chat"]

        admin(gw, "PATCH", f"/providers/{p['id']}", json={"api_key": "rotated-upstream-secret"})  # updates are encrypted too
        raw2 = gw.app.state.db.scalar("SELECT api_key FROM providers WHERE id = ?", (p["id"],))
        assert raw2.startswith("enc:v1:") and raw2 != raw and secretbox.decrypt(raw2) == "rotated-upstream-secret"


def test_a_database_encrypted_by_another_key_fails_the_request_cleanly(mock_factory):
    upstream = mock_factory()
    key_a, key_b = secretbox.generate_key(), secretbox.generate_key()
    with TestClient(create_app(Settings(database_path=":memory:", admin_token="test-admin", health_interval_s=0, encryption_key=key_a, _env_file=None))) as gw:
        p = admin(gw, "POST", "/providers", json={"name": "p", "base_url": upstream.base_url, "api_key": "k"})
        admin(gw, "POST", "/models", json={"name": "m", "routes": [{"provider_id": p["id"], "upstream_model": "mock-chat"}]})
        k = key(gw)
        secretbox.configure(key_b)  # operator restarted with the wrong key
        r = gw.post("/v1/chat/completions", headers=bearer(k["key"]), json={"model": "m", "messages": [{"role": "user", "content": "hi"}]})
        assert r.status_code == 500 and r.json()["error"]["code"] == "encryption_key_wrong" and "enc:v1" not in r.text


def test_command_line_genkey_and_encrypt_secrets(tmp_path):
    env = {**os.environ, "LLMGATE_DATABASE_PATH": str(tmp_path / "c.db")}
    run = lambda *a, **e: subprocess.run([sys.executable, "-m", "llmgate", *a], env={**env, **e}, capture_output=True, text=True, encoding="utf-8", timeout=90)  # noqa: E731
    key_text = run("genkey").stdout.strip()
    assert len(key_text) == 44 and key_text.endswith("=")
    db = Database(str(tmp_path / "c.db"))
    db.insert("INSERT INTO providers (name, base_url, api_key, created_at) VALUES ('a', 'http://x/v1', 'plain', 0)")
    db.close()
    assert "Set LLMGATE_ENCRYPTION_KEY" in (run("encrypt-secrets").stderr + run("encrypt-secrets").stdout)  # refuses without a key
    done = run("encrypt-secrets", "--dry-run", LLMGATE_ENCRYPTION_KEY=key_text)
    assert "1 encrypted" in done.stdout and "[dry run]" in done.stdout
    assert "1 encrypted" in run("encrypt-secrets", LLMGATE_ENCRYPTION_KEY=key_text).stdout
    db = Database(str(tmp_path / "c.db"))
    assert db.scalar("SELECT api_key FROM providers").startswith("enc:v1:")
    db.close()
