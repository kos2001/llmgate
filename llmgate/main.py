from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .admin_access import router as access_router
from .admin_ctl import router as ctl_router
from .admin_models import router as models_router
from .admin_providers import router as providers_router
from .admin_users import router as users_router
from .admin_usage import router as usage_router
from .api_auth import router as auth_router
from .api_compat import router as compat_router
from .api_litellm_keys import router as litellm_keys_router
from .api_litellm_ops import router as litellm_ops_router
from .api_openai import router as openai_router
from .alerts import Alerter
from .audit import AuditMiddleware
from .broadcast import Broadcaster
from .cache import ResponseCache
from .config import DEFAULT_ADMIN_TOKEN, Settings, get_settings
from .db import Database
from .errors import GatewayError, gateway_error_handler
from .health import HealthMonitor
from .jwtutil import new_secret
from .limits import RateLimiter
from .oidc import OIDCClient
from .router_state import RouterState

STATIC_DIR = Path(__file__).parent / "static"
log = logging.getLogger("llmgate")


class _RevalidatedStatic(StaticFiles):
    """Static files with ``Cache-Control: no-cache``: the browser revalidates (ETag) instead of reusing stale JS."""

    async def get_response(self, path: str, scope: Any) -> Any:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    db = Database(settings.database_path)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.admin_token == DEFAULT_ADMIN_TOKEN:
            log.warning("LLMGATE_ADMIN_TOKEN is the default value; set your own before exposing the gateway.")
        app.state.client = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=500, max_keepalive_connections=100))
        app.state.oidc = OIDCClient(settings, app.state.client)
        for sink in (app.state.broadcast, app.state.alerter.sink):
            sink.client = app.state.client
            sink.start()
        monitor = HealthMonitor(db, app.state.client, settings, app.state.alerter)
        app.state.monitor = monitor
        monitor.start()
        try:
            yield
        finally:
            await monitor.stop()
            await app.state.broadcast.stop()
            await app.state.alerter.sink.stop()
            await app.state.client.aclose()
            db.close()

    app = FastAPI(title="llmgate", version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None,
                  description="OpenRouter-style gateway for self-hosted LLM engines.")
    app.state.settings = settings
    app.state.db = db
    app.state.limiter = RateLimiter()
    app.state.broadcast = Broadcaster(settings.usage_webhook_url)
    app.state.alerter = Alerter(settings)
    app.state.router = RouterState(settings)
    app.state.cache = ResponseCache(settings.cache_max_entries)
    app.state.jwt_secret = settings.jwt_secret or new_secret()
    if not settings.jwt_secret:
        log.info("LLMGATE_JWT_SECRET is not set: using a random per-process secret (SSO sessions end on restart).")
    app.state.oidc = OIDCClient(settings, None)  # real HTTP client attached in lifespan; keeps tests without lifespan working

    origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"],
                       expose_headers=["x-request-id", "x-llmgate-provider"])
    app.add_exception_handler(GatewayError, gateway_error_handler)
    app.add_middleware(AuditMiddleware, db=db)

    app.include_router(openai_router)
    app.include_router(auth_router)
    for r in (compat_router, litellm_keys_router, litellm_ops_router, providers_router, models_router, access_router, usage_router, users_router, ctl_router):
        app.include_router(r)

    @app.get("/docs", include_in_schema=False)
    async def swagger_ui() -> Any:
        return get_swagger_ui_html(openapi_url="/openapi.json", title="llmgate API", swagger_favicon_url="/favicon.svg")

    @app.get("/redoc", include_in_schema=False)
    async def redoc_ui() -> Any:
        return get_redoc_html(openapi_url="/openapi.json", title="llmgate API", redoc_favicon_url="/favicon.svg")

    if STATIC_DIR.is_dir():  # browsers ask for these at the site root, whatever page they are on
        @app.get("/favicon.ico", include_in_schema=False)
        async def favicon_ico() -> FileResponse:
            return FileResponse(STATIC_DIR / "favicon.ico", media_type="image/x-icon", headers={"Cache-Control": "public, max-age=86400"})

        @app.get("/favicon.svg", include_in_schema=False)
        async def favicon_svg() -> FileResponse:
            return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/dashboard/")

    if STATIC_DIR.is_dir():
        @app.get("/dashboard", include_in_schema=False)
        async def dashboard_redirect() -> RedirectResponse:
            return RedirectResponse("/dashboard/")

        @app.get("/dashboard/", include_in_schema=False)
        async def dashboard() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

        app.mount("/dashboard", _RevalidatedStatic(directory=STATIC_DIR), name="dashboard")
    return app


def __getattr__(name: str) -> Any:
    """``uvicorn llmgate.main:app`` / ``fastapi dev``: build the app lazily on first access (importing stays side-effect free)."""
    if name == "app":
        application = create_app()
        globals()["app"] = application
        return application
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _import_litellm(path: str, dry_run: bool) -> None:
    import json

    from .litellm_config import import_config, parse_yaml

    settings = get_settings()
    db = Database(settings.database_path)
    with open(path, encoding="utf-8") as fh:
        report = import_config(db, parse_yaml(fh.read()), dry_run)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    verb = "[dry run] would import" if dry_run else "Imported"
    print(f"\n{verb} {len(report['deployments'])} deployment(s) into {settings.database_path}.")
    if report["env"]:
        lines = [f"  {k}={v}" for k, v in report["env"].items()]
        print("Set these in your environment / .env:\n" + "\n".join(lines))


def _seed_cmd(args: Any) -> None:
    import json

    from . import seed

    settings = get_settings()
    if args.reset:
        for suffix in ("", "-wal", "-shm"):
            Path(str(settings.database_path) + suffix).unlink(missing_ok=True)
    db = Database(settings.database_path)
    summary = seed.populate(db, args.engine_a, args.engine_b, with_usage=not args.no_usage)
    if summary is None:
        raise SystemExit(f"{settings.database_path} already has data. Use --reset to wipe it first, or a different LLMGATE_DATABASE_PATH.")
    keys = summary.pop("keys_plaintext")
    summary.pop("models_list")
    print(json.dumps(summary, indent=2))
    print("\nAPI keys (shown once; the database keeps only hashes):")
    for name, key in keys.items():
        print(f"  {name:26} {key}")


def run() -> None:
    import argparse

    import uvicorn

    p = argparse.ArgumentParser(prog="llmgate", description="llmgate: gateway for self-hosted LLM engines")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    sub = p.add_subparsers(dest="cmd")
    imp = sub.add_parser("import-litellm", help="load a LiteLLM config.yaml (model_list, router_settings, ...) into the registry")
    imp.add_argument("file")
    imp.add_argument("--dry-run", action="store_true", help="show what would change, write nothing")
    from . import dev

    sd = sub.add_parser("seed", help="fill the database (LLMGATE_DATABASE_PATH) with sample providers, models, keys and usage history")
    sd.add_argument("--reset", action="store_true", help="wipe the database first")
    sd.add_argument("--no-usage", action="store_true", help="registry only, without 14 days of usage history")
    sd.add_argument("--engine-a", default="http://127.0.0.1:8101/v1", help="base URL of the first vLLM engine")
    sd.add_argument("--engine-b", default="http://127.0.0.1:8102/v1", help="base URL of the second vLLM engine")

    dev.add_arguments(sub.add_parser("dev", help="development mode: mock engine, auto-reload, persistent dev database"))
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.cmd == "import-litellm":
        return _import_litellm(args.file, args.dry_run)
    if args.cmd == "dev":
        return dev.run(args)
    if args.cmd == "seed":
        return _seed_cmd(args)
    uvicorn.run("llmgate.main:create_app", factory=True, host=args.host, port=args.port)


if __name__ == "__main__":
    run()
