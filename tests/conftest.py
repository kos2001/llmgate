from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Iterator

import pytest
import uvicorn
from fastapi.testclient import TestClient

from llmgate.config import Settings
from llmgate.main import create_app
from mockvllm.server import create_app as create_mock

ADMIN = {"Authorization": "Bearer test-admin"}


class MockServer:
    """A real uvicorn server on a free port. ``models`` builds a mock vLLM unless ``make_app(port)`` is given."""

    def __init__(self, models: list[str] | None = None, make_app: Any = None, **kwargs: Any):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.app = make_app(self.port) if make_app else create_mock(models, **kwargs)
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    @property
    def root(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "MockServer":
        self.thread.start()
        while not self.server.started:
            time.sleep(0.02)
        return self

    def stop(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


@pytest.fixture
def mock_factory() -> Iterator[Any]:
    servers: list[MockServer] = []

    def make(models: list[str] | None = None, **kwargs: Any) -> MockServer:
        srv = MockServer(models or ["mock-chat"], **kwargs).start()
        servers.append(srv)
        return srv

    yield make
    for s in servers:
        s.stop()


@pytest.fixture
def gw() -> Iterator[TestClient]:
    settings = Settings(database_path=":memory:", admin_token="test-admin", health_interval_s=0,
                        connect_timeout_s=1.0, _env_file=None)
    with TestClient(create_app(settings)) as client:
        yield client


@pytest.fixture
def gw_factory() -> Iterator[Any]:
    """Build gateways with custom Settings; all are shut down when the test ends."""
    import contextlib

    stack = contextlib.ExitStack()

    def make(**over: Any) -> TestClient:
        settings = Settings(**{"database_path": ":memory:", "admin_token": "test-admin", "health_interval_s": 0,
                               "connect_timeout_s": 1.0, **over}, _env_file=None)
        return stack.enter_context(TestClient(create_app(settings)))

    yield make
    stack.close()


@dataclass
class Stack:
    gw: TestClient
    key: str
    service_id: int
    key_id: int
    model: str = "chat-model"

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key}"}

    def chat(self, content: str = "hello", **extra: Any):
        body = {"model": self.model, "messages": [{"role": "user", "content": content}], **extra}
        return self.gw.post("/v1/chat/completions", json=body, headers=self.headers())


def admin(gw: TestClient, method: str, path: str, **kw: Any):
    resp = gw.request(method, "/admin/api" + path, headers=ADMIN, **kw)
    assert resp.status_code < 300, resp.text
    return resp.json() if resp.content else None


def make_stack(gw: TestClient, provider_urls: list[str], *, upstream: str = "mock-chat",
               key_opts: dict[str, Any] | None = None, in_price: float = 1.0, out_price: float = 2.0) -> Stack:
    """One public model 'chat-model' routed over the given providers (one priority tier each)."""
    routes = []
    for i, url in enumerate(provider_urls):
        p = admin(gw, "POST", "/providers", json={"name": f"vllm-{i}", "base_url": url})
        routes.append({"provider_id": p["id"], "upstream_model": upstream, "priority": i})
    admin(gw, "POST", "/models", json={"name": "chat-model", "context_length": 8192, "input_price_per_m": in_price,
                                       "output_price_per_m": out_price, "routes": routes})
    svc = admin(gw, "POST", "/services", json={"name": "svc"})
    key = admin(gw, "POST", f"/services/{svc['id']}/keys", json={"name": "k1", **(key_opts or {})})
    return Stack(gw, key["key"], svc["id"], key["id"])


# ------------------------------------------------------------------------------------------ SSO

import httpx  # noqa: E402

from mockoidc.server import create_app as create_oidc  # noqa: E402


@pytest.fixture
def idp() -> Iterator[MockServer]:
    srv = MockServer(make_app=lambda port: create_oidc(f"http://127.0.0.1:{port}")).start()
    yield srv
    srv.stop()


def sso_settings(idp: MockServer, **over: Any) -> Settings:
    base: dict[str, Any] = dict(
        database_path=":memory:", admin_token="test-admin", health_interval_s=0, connect_timeout_s=1.0,
        jwt_secret="test-secret-test-secret-test-secret-0123", oidc_issuer=idp.root,
        oidc_client_id="llmgate-dashboard", oidc_client_secret="dashboard-secret", oidc_audience="llmgate-api",
        oidc_allowed_email_domains="example.com", oidc_default_role="viewer",
    )
    return Settings(**{**base, **over}, _env_file=None)


@pytest.fixture
def sso_gw(idp: MockServer) -> Iterator[TestClient]:
    with TestClient(create_app(sso_settings(idp)), base_url="http://127.0.0.1") as client:
        yield client


def sso_login(gw: TestClient, idp: MockServer, user: str, next: str | None = None) -> httpx.Response:
    """Play the browser through the whole authorization-code flow; returns the callback response."""
    gw.cookies.clear()
    r = gw.get("/auth/oidc/login" + (f"?next={next}" if next else ""), follow_redirects=False)
    assert r.status_code == 302, r.text
    to_idp = httpx.get(r.headers["location"] + f"&login_as={user}", follow_redirects=False)
    assert to_idp.status_code == 302, to_idp.text
    return gw.get(to_idp.headers["location"], follow_redirects=False)


def idp_token(idp: MockServer, user: str, aud: str = "llmgate-api", ttl: int = 3600) -> str:
    return idp.app.state.issue_user_token(user, aud, ttl)


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
