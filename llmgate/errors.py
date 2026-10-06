from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse


class GatewayError(Exception):
    """An error rendered in the OpenAI error envelope: ``{"error": {...}}``."""

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        type: str = "invalid_request_error",
        code: str | None = None,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.type = type
        self.code = code
        self.headers = headers or {}

    def body(self) -> dict[str, Any]:
        return {"error": {"message": self.message, "type": self.type, "param": None, "code": self.code}}


async def gateway_error_handler(_: Request, exc: GatewayError) -> JSONResponse:
    return JSONResponse(exc.body(), status_code=exc.status_code, headers=exc.headers)
