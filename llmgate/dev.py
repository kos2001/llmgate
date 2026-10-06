"""Development mode: ``llmgate dev``.

One command that gives you a working gateway to build against:

* a mock vLLM engine (so no GPU is needed) and, with ``--sso``, a mock OIDC provider, as child processes;
* the gateway with **auto-reload** (edit ``llmgate/*.py`` and it restarts; the dashboard JS/CSS is served uncached);
* a **persistent** database (``data/dev.db``) that is seeded once with a provider, two models, a service and a key.

Not for production: fixed admin token, fixed session secret, mock engines. Everything binds to 127.0.0.1.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from . import seed, seed_data
from .db import Database, now
from .litellm_config import add_deployment
from .security import generate_api_key

DEV_ADMIN_TOKEN = "dev-admin"
DEV_JWT_SECRET = "dev-only-session-secret-do-not-use-in-prod-0123456789"
CHAT, EMBED = "dev-chat", "dev-embed"


def bootstrap(db: Database, engine_url: str | None, creds_file: Path) -> dict[str, Any] | None:
    """Seed an empty database. Returns the new credentials, or ``None`` when there was nothing to do."""
    if db.scalar("SELECT COUNT(*) FROM providers", default=0) or db.scalar("SELECT COUNT(*) FROM services", default=0):
        if engine_url:  # the mock may have moved to another port since the database was seeded
            db.execute("UPDATE providers SET base_url = ? WHERE name = 'mock-vllm' AND base_url != ?", (engine_url, engine_url))
        return None
    if engine_url:
        for name, mode, pin, pout in ((CHAT, "chat", 0.05, 0.10), (EMBED, "embedding", 0.01, 0.0)):
            add_deployment(db, {"model_name": name, "litellm_params": {"model": f"hosted_vllm/{name}", "api_base": engine_url, "provider_name": "mock-vllm"},
                                "model_info": {"mode": mode, "input_cost_per_token": pin / 1e6, "output_cost_per_token": pout / 1e6,
                                               "max_input_tokens": 8192}})
    sid = db.insert("INSERT INTO services (name, description, created_at) VALUES ('dev', 'created by llmgate dev', ?)", (now(),))
    plaintext, prefix, digest = generate_api_key("sk-lg-")
    db.insert("INSERT INTO api_keys (service_id, name, key_prefix, key_hash, created_at) VALUES (?, 'dev-key', ?, ?, ?)",
              (sid, prefix, digest, now()))
    creds = {"api_key": plaintext, "admin_token": DEV_ADMIN_TOKEN, "models": [CHAT, EMBED] if engine_url else []}
    creds_file.parent.mkdir(parents=True, exist_ok=True)
    creds_file.write_text(json.dumps(creds, indent=2), encoding="utf-8")
    return creds


def port_available(host: str, port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def choose_port(host: str, preferred: int, explicit: bool, what: str, flag: str) -> int:
    """The wanted port if it is free; otherwise the next free one (unless the user asked for this exact port)."""
    if port_available(host, preferred):
        return preferred
    hint = (f"{what}: port {preferred} is not available. Another program is using it, or Windows has reserved it "
            f"(see `netsh interface ipv4 show excludedportrange protocol=tcp`).")
    if explicit:
        raise SystemExit(f"{hint}\nPick another one with {flag} <port>.")
    for candidate in range(preferred + 1, preferred + 50):
        if port_available(host, candidate):
            print(f"  note: {hint}\n        -> using {candidate} instead (set {flag} to choose).", flush=True)
            return candidate
    raise SystemExit(f"{hint}\nNo free port found nearby; pass {flag} <port>.")


def _wait_http(url: str, what: str, proc: "subprocess.Popen[bytes] | None" = None, timeout: float = 20.0) -> None:
    end = time.time() + timeout
    while time.time() < end:
        if proc is not None and proc.poll() is not None:
            raise SystemExit(f"{what} exited immediately (exit code {proc.returncode}); its error output is above.")
        try:
            if httpx.get(url, timeout=1).status_code < 500:
                return
        except httpx.HTTPError:
            time.sleep(0.2)
    raise SystemExit(f"{what} did not come up at {url} within {timeout:.0f}s.")


def credentials_path(db_path: Path) -> Path:
    """One credentials file per database (``dev.db`` -> ``dev-credentials.json``), so ``--db`` never clobbers another."""
    return db_path.with_name(f"{db_path.stem}-credentials.json")


DEFAULTS = {"host": "127.0.0.1", "port": 8080, "db": "data/dev.db", "mock_port": 8101, "idp_port": 8200, "login_as": "alice"}


def run(args: Any) -> None:
    import uvicorn

    host = args.host or DEFAULTS["host"]
    db_path = Path(args.db or DEFAULTS["db"])
    # Decide every port *before* starting anything, so a conflict is explained instead of crashing halfway.
    port = choose_port(host, args.port or DEFAULTS["port"], args.port is not None, "gateway", "--port")
    mock_port = None if args.no_mock else choose_port("127.0.0.1", args.mock_port or DEFAULTS["mock_port"], args.mock_port is not None,
                                                      "mock vLLM", "--mock-port")
    full_seed = not args.no_seed and not args.no_mock  # rich seed data needs the two mock engines it points at
    mock_b_port = choose_port("127.0.0.1", (mock_port or 0) + 1, False, "mock vLLM #2", "--mock-port") if (full_seed and mock_port) else None
    use_sso = not args.no_sso  # SSO is on by default in dev: that is what lets you in without typing the admin token
    as_user = (args.login_as or DEFAULTS["login_as"]).lower()
    idp_port = choose_port("127.0.0.1", args.idp_port or DEFAULTS["idp_port"], args.idp_port is not None, "mock OIDC provider",
                           "--idp-port") if use_sso else None

    if args.reset:
        for suffix in ("", "-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)

    env = os.environ  # inherited by uvicorn's reload worker; explicit user settings always win
    env.setdefault("LLMGATE_ADMIN_TOKEN", DEV_ADMIN_TOKEN)
    env.setdefault("LLMGATE_JWT_SECRET", DEV_JWT_SECRET)  # fixed, so sessions survive auto-reloads
    env.setdefault("LLMGATE_HEALTH_INTERVAL_S", "5")
    env["LLMGATE_DATABASE_PATH"] = str(db_path)

    children: list[subprocess.Popen[bytes]] = []
    engine_url: str | None = None
    try:
        engine_b_url: str | None = None
        if mock_port:
            engine_url = f"http://127.0.0.1:{mock_port}/v1"
            engines = [(mock_port, seed_data.ENGINE_A_MODELS if full_seed else [CHAT, EMBED])]
            if mock_b_port:
                engine_b_url = f"http://127.0.0.1:{mock_b_port}/v1"
                engines.append((mock_b_port, seed_data.ENGINE_B_MODELS))
            for engine_port, served in engines:  # each mock serves exactly the model names the seeded routes point at
                cmd = [sys.executable, "-m", "mockvllm.server", "--port", str(engine_port), "--token-delay", "0.02"]
                for name in served:
                    cmd += ["--model", name]
                proc = subprocess.Popen(cmd)
                children.append(proc)
                _wait_http(f"http://127.0.0.1:{engine_port}/health", "mock vLLM", proc)
        if idp_port:
            idp = f"http://127.0.0.1:{idp_port}"
            idp_cmd = [sys.executable, "-m", "mockoidc.server", "--port", str(idp_port)]
            if as_user != "none":
                idp_cmd += ["--auto-login", as_user]  # the mock IdP approves sign-ins as this user without a picker
            proc = subprocess.Popen(idp_cmd)
            children.append(proc)
            _wait_http(f"{idp}/.well-known/openid-configuration", "mock OIDC provider", proc)
            env.update({"LLMGATE_OIDC_ISSUER": idp, "LLMGATE_OIDC_CLIENT_ID": "llmgate-dashboard",
                        "LLMGATE_OIDC_CLIENT_SECRET": "dashboard-secret", "LLMGATE_OIDC_AUDIENCE": "llmgate-api"})
            env["LLMGATE_DEV_AUTOLOGIN"] = "false" if as_user == "none" else "true"  # dashboard skips the login form
            env.setdefault("LLMGATE_OIDC_ROLE_MAP", '{"llm-admins":"admin","llm-operators":"operator","llm-viewers":"viewer"}')

        db = Database(str(db_path))
        creds_file = credentials_path(db_path)
        created: dict[str, Any] | None = None
        if full_seed and engine_url and engine_b_url:
            summary = seed.populate(db, engine_url, engine_b_url)
            if summary:
                keys = summary["keys_plaintext"]
                creds_file.write_text(json.dumps({"api_key": keys["dev/dev-key"], "admin_token": env["LLMGATE_ADMIN_TOKEN"],
                                                  "models": summary["models_list"], "keys": keys}, indent=2), encoding="utf-8")
                created = {"api_key": keys["dev/dev-key"], "summary": summary}
            else:  # already seeded earlier: the mocks may have moved to other ports since
                seed.sync_engine_urls(db, engine_url, engine_b_url)
                bootstrap(db, engine_url, creds_file)  # also repoints a database created by an older minimal seed
        else:
            created = bootstrap(db, engine_url, creds_file)
        db.close()

        base = f"http://{host}:{port}"
        print("\n  llmgate (development mode)")
        print(f"    dashboard : {base}/dashboard/   admin token: {env['LLMGATE_ADMIN_TOKEN']}")
        print(f"    API docs  : {base}/docs   OpenAI base URL: {base}/v1   OpenRouter-style: {base}/api/v1")
        if engine_url:
            more = f"  +  {engine_b_url}" if engine_b_url else ""
            print(f"    mock vLLM : {engine_url}{more}   ({'10 seeded models' if full_seed else CHAT + ', ' + EMBED})")
        if idp_port:
            who = "pick a user on the sign-in page" if as_user == "none" else f"signed in automatically as {as_user} ({ROLE_OF.get(as_user, '?')})"
            print(f"    SSO       : mock IdP {env['LLMGATE_OIDC_ISSUER']}  -> {who}")
            print("                (switch user: log out, then 'sign in as someone else'; alice=admin bob=operator carol=viewer)")
        if created and created.get("summary"):
            sm = created["summary"]
            print(f"    seed data : {sm['providers']} providers, {sm['models']} models, {sm['services']} services / {sm['keys']} keys, {sm['customers']} customers, "
                  f"{sm['guardrails']} guardrails, {sm['usage_rows']} usage rows (14 days)")
            print(f"    API key   : {created['api_key']}   (service 'dev'; every seeded key is in {creds_file})")
        elif created:
            print(f"    first run : created service 'dev'. API key = {created['api_key']}   (saved in {creds_file})")
        elif creds_file.exists():
            print(f"    API key   : see {creds_file}")
        print(f"    database  : {db_path}  (kept between runs; --reset wipes it)   auto-reload: {'off' if args.no_reload else 'on'}")
        print("    stop      : Ctrl+C\n")

        sys.stdout.flush()  # the banner must be visible even when output is piped to a file
        uvicorn.run("llmgate.main:create_app", factory=True, host=host, port=port, reload=not args.no_reload,
                    reload_dirs=[str(Path(__file__).parent)] if not args.no_reload else None, log_level="info")
    finally:
        for child in children:
            child.terminate()
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()


ROLE_OF = {"alice": "admin", "bob": "operator", "carol": "viewer", "dave": "no role -> denied", "eve": "wrong email domain -> denied"}


def add_arguments(p: Any) -> None:
    """Defaults are None on purpose: ``run`` must know whether a port was asked for (then it is not silently changed)."""
    p.add_argument("--host", default=None, help=f"default {DEFAULTS['host']}")
    p.add_argument("--port", type=int, default=None, help=f"gateway port (default {DEFAULTS['port']}; next free one if busy)")
    p.add_argument("--db", default=None, help=f"persistent dev database (default {DEFAULTS['db']})")
    p.add_argument("--reset", action="store_true", help="delete the dev database first")
    p.add_argument("--no-reload", action="store_true", help="disable auto-reload")
    p.add_argument("--no-seed", action="store_true", help="first run: only a service + key, without the sample data")
    p.add_argument("--no-mock", action="store_true", help="do not start the mock vLLM; add your own provider in the dashboard")
    p.add_argument("--no-sso", action="store_true", help="no mock OIDC provider: sign in with the admin token instead")
    p.add_argument("--sso", action="store_true", help="(default; accepted for compatibility)")
    p.add_argument("--as", dest="login_as", default=None, choices=[*ROLE_OF, "none"],
                   help="sign in automatically as this mock user (default alice = admin); 'none' shows the user picker")
    p.add_argument("--mock-port", type=int, default=None, help=f"mock vLLM port (default {DEFAULTS['mock_port']})")
    p.add_argument("--idp-port", type=int, default=None, help=f"mock IdP port (default {DEFAULTS['idp_port']})")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="llmgate development mode")
    add_arguments(parser)
    run(parser.parse_args())
