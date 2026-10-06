"""Audit trail for admin changes, as a pure-ASGI middleware so the streaming inference path is untouched."""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .db import Database, now
from .rbac import SAFE_METHODS


class AuditMiddleware:
    def __init__(self, app: ASGIApp, db: Database):
        self.app, self.db = app, db

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not path.startswith(("/admin/api", "/api/v1/keys")) or scope["method"] in SAFE_METHODS:
            await self.app(scope, receive, send)
            return

        status = 0

        async def capture(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            principal = scope.get("state", {}).get("principal")  # set by rbac.admin_guard
            client = scope.get("client")
            self.db.execute(
                "INSERT INTO audit_logs (ts, actor, role, method, path, status, ip) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (now(), principal.label if principal else "anonymous", principal.role if principal else None,
                 scope["method"], path, status or None, client[0] if client else None))
