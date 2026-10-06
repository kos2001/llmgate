"""One-command tour: mock vLLM x2 + mock OIDC + the gateway, seeded with ``seed.populate`` and then driven with live traffic.

    python -m llmgate.demo            # then open http://127.0.0.1:8080/dashboard/

The demo wipes ``data/demo.db`` every run. For day-to-day development use ``llmgate dev`` (persistent database).
"""

from __future__ import annotations

import argparse
import random
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from mockoidc.server import create_app as create_oidc
from mockvllm.server import create_app as create_mock

from . import seed, seed_data as D
from .config import Settings
from .main import create_app

ADMIN_TOKEN = "demo-admin"
IDP = "http://127.0.0.1:8200"
PROMPTS = ["안녕하세요, 오늘 서울 날씨를 알려줘", "Summarize the benefits of paged attention in two sentences.",
           "파이썬으로 피보나치 수열을 구현해줘", "Write a haiku about GPUs.", "vLLM과 TGI의 차이점은?"]


def serve(app: Any, port: int) -> uvicorn.Server:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    return server


def sso_login(base: str, user: str) -> None:
    """Sign a mock user in through the real authorization-code + PKCE flow so the Users tab has content."""
    with httpx.Client(base_url=base, follow_redirects=False) as browser:
        to_idp = browser.get("/auth/oidc/login").headers["location"]
        back = httpx.get(to_idp + f"&login_as={user}", follow_redirects=False).headers["location"]
        browser.get(back)


def traffic(base: str, keys: dict[str, str], n: int) -> None:
    """Live requests through the real gateway: streaming, embeddings, cache, guardrails, aliases and a JWT caller."""
    rng = random.Random(7)
    chat_keys = [k for k in ("chatbot-web/prod", "doc-summarizer/prod", "data-science/notebook") if k in keys]
    with httpx.Client(base_url=base, timeout=60) as c:
        for i in range(n):
            name = rng.choice(chat_keys)
            h = {"Authorization": f"Bearer {keys[name]}", "X-Title": D.APPS[name.split("/")[0]]}
            body: dict[str, Any] = {"model": rng.choice([D.LLAMA, D.LLAMA, D.QWEN7, "gpt-4o-mini"]), "user": rng.choice(D.END_USERS[:3]),
                                    "messages": [{"role": "user", "content": rng.choice(PROMPTS)}], "tags": rng.choice([["prod"], ["eval"], ["experiment-7"]])}
            if name.startswith("data-science"):
                body["model"] = D.LLAMA
            if i % 9 == 0:
                body.update(messages=[{"role": "user", "content": "What is paged attention?"}], cache={"ttl": 300}, model=D.LLAMA)
            if rng.random() < 0.5 and "cache" not in body:
                with c.stream("POST", "/v1/chat/completions", headers=h, json={**body, "stream": True}) as r:
                    for _ in r.iter_lines():
                        pass
            else:
                c.post("/v1/chat/completions", headers=h, json=body)
        c.post("/v1/embeddings", headers={"Authorization": f"Bearer {keys['internal-search/prod']}"}, json={"model": D.BGE, "input": PROMPTS[:2]})
        h = {"Authorization": f"Bearer {keys['chatbot-web/prod']}", "X-Title": "Chat UI"}
        c.post("/v1/chat/completions", headers=h, json={"model": D.LLAMA, "messages": [{"role": "user", "content": "my mail is kim@example.com"}]})
        c.post("/v1/chat/completions", headers=h, json={"model": D.LLAMA, "messages": [{"role": "user", "content": "key sk-abcdefghijklmnopqrstuvwx"}]})
        c.post("/v1/chat/completions", headers=h, json={"model": D.LLAMA, "user": "user-spam", "messages": [{"role": "user", "content": "hi"}]})
        token = httpx.post(f"{IDP}/token", data={"grant_type": "client_credentials"}, auth=("ci-pipeline", "ci-secret")).json()["access_token"]
        c.post("/v1/chat/completions", headers={"Authorization": f"Bearer {token}", "X-Title": "CI pipeline"},
               json={"model": D.QWEN7, "messages": [{"role": "user", "content": "run the nightly eval"}], "tags": ["ci"]})


def main() -> None:
    p = argparse.ArgumentParser(description="llmgate demo with mock vLLM engines and a mock OIDC provider")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--db", default="data/demo.db")
    p.add_argument("--requests", type=int, default=40, help="live requests to send through the gateway")
    args = p.parse_args()

    from .dev import port_available

    taken = [port for port in (args.port, 8101, 8102, 8200) if not port_available("127.0.0.1", port)]
    if taken:  # fail up front with an explanation instead of crashing halfway through seeding
        raise SystemExit(f"The demo needs ports {sorted({args.port, 8101, 8102, 8200})} but {taken} are not available "
                         f"(in use, or reserved by Windows). Stop the other program, or use `llmgate dev`, which picks free ports.")

    db = Path(args.db)
    for suffix in ("", "-wal", "-shm"):
        Path(str(db) + suffix).unlink(missing_ok=True)

    engine_a, engine_b = "http://127.0.0.1:8101/v1", "http://127.0.0.1:8102/v1"
    serve(create_mock(D.ENGINE_A_MODELS, token_delay=0.02, max_model_len=131072), 8101)
    serve(create_mock(D.ENGINE_B_MODELS, token_delay=0.03, max_model_len=32768), 8102)
    serve(create_oidc(IDP), 8200)
    settings = Settings(
        database_path=str(db), admin_token=ADMIN_TOKEN, health_interval_s=10, jwt_secret="demo-session-secret-change-me-0123456789",
        oidc_issuer=IDP, oidc_client_id="llmgate-dashboard", oidc_client_secret="dashboard-secret", oidc_audience="llmgate-api",
        oidc_allowed_email_domains="example.com", cache_ttl_s=0, _env_file=None)
    app = create_app(settings)
    summary = seed.populate(app.state.db, engine_a, engine_b)
    assert summary is not None
    serve(app, args.port)

    base = f"http://127.0.0.1:{args.port}"
    for user in ("alice", "bob", "carol"):
        sso_login(base, user)
    traffic(base, summary["keys_plaintext"], args.requests)

    print(f"\n  Dashboard : {base}/dashboard/")
    print(f"     SSO (mock IdP user picker): alice=admin  bob=operator  carol=viewer   |   admin token: {ADMIN_TOKEN}")
    print(f"  OpenAI API: {base}/v1   OpenRouter-style: {base}/api/v1   (catalog: {base}/api/v1/models)")
    print(f"  LiteLLM-style admin API: {base}/key/generate, /team/new, /spend/logs ...  (Bearer {ADMIN_TOKEN})")
    print("  Mock vLLM : http://127.0.0.1:8101 , http://127.0.0.1:8102     Mock OIDC IdP: " + IDP)
    print(f"  Seed data : {summary['models']} models, {summary['keys']} keys, {summary['usage_rows']} usage rows")
    for name, key in summary["keys_plaintext"].items():
        print(f"  key[{name:24}] {key}")
    print("\n  Ctrl+C to stop.\n")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
