"""Security headers, as pure ASGI so streaming responses pass through untouched.

* every response: ``X-Content-Type-Options: nosniff``, ``Referrer-Policy: same-origin``
* dashboard (``/dashboard``): a strict Content-Security-Policy (the page runs same-origin module scripts only, no inline
  scripts, no third-party hosts) and ``frame-ancestors 'none'`` so it cannot be clickjacked
* admin / auth JSON: ``Cache-Control: no-store`` so keys, usage and user data are never kept by a browser or proxy cache

``/docs`` and ``/redoc`` load their UI from a CDN, so they intentionally get no CSP.
"""

from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

DASHBOARD_CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; "
                 "font-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
_NO_STORE_PREFIXES = ("/admin/api", "/auth/", "/api/v1/keys", "/key/", "/team/", "/customer/", "/spend/", "/global/", "/model/", "/metrics")


class SecurityHeaders:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")

        async def with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                present = {k.lower() for k, _ in headers}

                def add(name: bytes, value: str) -> None:
                    if name not in present:
                        headers.append((name, value.encode()))

                add(b"x-content-type-options", "nosniff")
                add(b"referrer-policy", "same-origin")
                if path.startswith("/dashboard"):
                    add(b"content-security-policy", DASHBOARD_CSP)
                    add(b"x-frame-options", "DENY")
                if path.startswith(_NO_STORE_PREFIXES):
                    add(b"cache-control", "no-store")
            await send(message)

        await self.app(scope, receive, with_headers)
