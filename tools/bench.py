"""How much does the gateway add? Compares calling a mock vLLM directly with calling it through llmgate.

    python tools/bench.py [--requests 2000] [--concurrency 50]

The mock engine, the gateway and this load generator run as three separate processes on loopback, with an instant mock engine (token delay 0), so the numbers isolate the gateway's own
cost (auth, limits, routing, accounting, one SQLite insert per request) rather than model speed. Treat them as a relative
indication on your machine, not a capacity promise.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from llmgate.db import Database
from llmgate.seed import populate


def spawn(*cmd: str, env: dict[str, str] | None = None) -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, *cmd], env={**os.environ, **(env or {})}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_up(url: str) -> None:
    for _ in range(100):
        try:
            if httpx.get(url, timeout=0.5).status_code < 500:
                return
        except httpx.HTTPError:
            time.sleep(0.2)
    raise SystemExit(f"{url} did not come up")


def stop(proc: subprocess.Popen[bytes]) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
    else:
        proc.terminate()
    proc.wait(timeout=15)


async def run(client: httpx.AsyncClient, url: str, headers: dict, body: dict, n: int, concurrency: int) -> dict:
    latencies: list[float] = []
    failures = 0
    sem = asyncio.Semaphore(concurrency)

    async def one() -> None:
        nonlocal failures
        async with sem:
            t0 = time.perf_counter()
            r = await client.post(url, headers=headers, json=body)
            if r.status_code != 200:
                failures += 1
            latencies.append((time.perf_counter() - t0) * 1000)

    t0 = time.perf_counter()
    await asyncio.gather(*(one() for _ in range(n)))
    wall = time.perf_counter() - t0
    q = statistics.quantiles(latencies, n=100)
    return {"rps": n / wall, "p50": q[49], "p95": q[94], "p99": q[98], "failures": failures}


async def main_async(n: int, concurrency: int, workers: int) -> None:
    """Mock engine, gateway and load generator are three separate processes, so none of them steals the others' interpreter lock."""
    body = {"model": "dev-chat", "max_tokens": 16, "messages": [{"role": "user", "content": "benchmark"}]}
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        engine = "http://127.0.0.1:8991/v1"
        db_path = str(Path(tmp) / "bench.db")
        db = Database(db_path)
        key = populate(db, engine, engine, with_usage=False)["keys_plaintext"]["dev/dev-key"]
        db.close()
        procs = [spawn("-m", "mockvllm.server", "--port", "8991", "--model", "dev-chat", "--model", "dev-embed", "--token-delay", "0"),
                 spawn("-m", "uvicorn", "llmgate.main:create_app", "--factory", "--port", "8992", "--log-level", "error", "--workers", str(workers),
                       env={"LLMGATE_DATABASE_PATH": db_path, "LLMGATE_ADMIN_TOKEN": "bench", "LLMGATE_HEALTH_INTERVAL_S": "0"})]
        try:
            wait_up("http://127.0.0.1:8991/health")
            wait_up("http://127.0.0.1:8992/healthz")
            limits = httpx.Limits(max_connections=concurrency * 2, max_keepalive_connections=concurrency * 2)
            headers = {"Authorization": f"Bearer {key}"}
            async with httpx.AsyncClient(limits=limits, timeout=60) as client:
                await run(client, engine + "/chat/completions", {}, body, 100, concurrency)  # warm-up
                direct = await run(client, engine + "/chat/completions", {}, body, n, concurrency)
                gateway = await run(client, "http://127.0.0.1:8992/v1/chat/completions", headers, body, n, concurrency)
                streamed = await run(client, "http://127.0.0.1:8992/v1/chat/completions", headers, {**body, "stream": True}, max(1, n // 2), concurrency)
        finally:
            for proc in procs:
                stop(proc)
    print(f"\n{n} requests, concurrency {concurrency}, gateway workers {workers} (mock, gateway and client in separate processes)\n")
    print(f"{'':28}{'req/s':>9}{'p50 ms':>9}{'p95 ms':>9}{'p99 ms':>9}{'failed':>8}")
    for name, r in (("direct to mock vLLM", direct), ("through llmgate", gateway), ("through llmgate (stream)", streamed)):
        print(f"{name:28}{r['rps']:9.0f}{r['p50']:9.1f}{r['p95']:9.1f}{r['p99']:9.1f}{r['failures']:8d}")
    print(f"\ngateway overhead at p50: +{gateway['p50'] - direct['p50']:.1f} ms; throughput kept: {100 * gateway['rps'] / direct['rps']:.0f}% of direct")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--requests", type=int, default=2000)
    p.add_argument("--concurrency", type=int, default=50)
    p.add_argument("--workers", type=int, default=1, help="gateway worker processes (RPM/concurrency state is per process)")
    a = p.parse_args()
    asyncio.run(main_async(a.requests, a.concurrency, a.workers))
