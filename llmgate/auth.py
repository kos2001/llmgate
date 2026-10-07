from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import Request

from .db import Database, now
from .errors import GatewayError
from .jwtutil import as_list, claim_path, header_alg, looks_like_jwt
from .security import hash_key

_LAST_USED_WRITE_INTERVAL_S = 60
_NO_MODELS = ["\0none"]  # sentinel: an empty allow-list would mean "all models"


@dataclass
class AuthContext:
    key: dict[str, Any]
    service: dict[str, Any]

    @property
    def allowed_models(self) -> list[str]:
        """Models this caller may use: the key's list, the service's list, or both intersected (empty = all)."""
        key = json.loads(self.key["allowed_models"]) if self.key.get("allowed_models") else None
        svc = json.loads(self.service["allowed_models"]) if self.service.get("allowed_models") else None
        if key and svc:
            return sorted(set(key) & set(svc)) or _NO_MODELS
        return key or svc or []

    @property
    def allowed_providers(self) -> list[str]:
        raw = self.key.get("allowed_providers")
        return json.loads(raw) if raw else []


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.headers.get("x-api-key")


def _check_service(db: Database, service_id: int) -> dict[str, Any]:
    service = db.one("SELECT * FROM services WHERE id = ?", (service_id,))
    if service is None or not service["enabled"]:
        raise GatewayError(403, "The service owning this key is disabled.", type="permission_error",
                           code="service_disabled")
    return service


def _touch(db: Database, key: dict[str, Any]) -> None:
    ts = now()
    if not key["last_used_at"] or ts - key["last_used_at"] > _LAST_USED_WRITE_INTERVAL_S:
        db.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (ts, key["id"]))


async def authenticate(request: Request) -> AuthContext:
    """Resolve the caller of the inference API: an ``sk-lg-…`` key, or an IdP-issued JWT bound to a service."""
    db: Database = request.app.state.db
    throttle = request.app.state.throttle
    ip = request.client.host if request.client else "?"
    throttle.check(ip)
    token = _bearer(request)
    if not token:
        raise GatewayError(401, "Missing API key. Send 'Authorization: Bearer <key>'.",
                           type="authentication_error", code="missing_api_key")
    if looks_like_jwt(token):  # API keys are never shaped like a JWT
        try:
            return await _authenticate_jwt(request, token)
        except GatewayError as exc:
            if exc.status_code == 401:
                throttle.fail(ip)
            raise

    key = db.one("SELECT * FROM api_keys WHERE key_hash = ?", (hash_key(token),))
    if key is None:
        throttle.fail(ip)
        raise GatewayError(401, "Invalid API key.", type="authentication_error", code="invalid_api_key")
    if not key["enabled"]:
        raise GatewayError(403, "This API key has been disabled.", type="permission_error", code="key_disabled")
    if key["expires_at"] and key["expires_at"] < now():
        raise GatewayError(401, "This API key has expired.", type="authentication_error", code="key_expired")
    service = _check_service(db, key["service_id"])
    _touch(db, key)
    return AuthContext(key=key, service=service)


async def _authenticate_jwt(request: Request, token: str) -> AuthContext:
    st = request.app.state
    db: Database = st.db
    oidc, settings = st.oidc, st.settings
    if not oidc.enabled or header_alg(token) == "HS256":  # gateway session tokens are not inference credentials
        raise GatewayError(401, "Invalid API key.", type="authentication_error", code="invalid_api_key")
    try:
        claims = await oidc.verify(token)
    except jwt.ExpiredSignatureError:
        raise GatewayError(401, "The JWT has expired.", type="authentication_error", code="token_expired") from None
    except jwt.PyJWTError:
        raise GatewayError(401, "Invalid JWT.", type="authentication_error", code="invalid_token") from None

    service_name = claim_path(claims, settings.jwt_service_claim)
    if not isinstance(service_name, str) or not service_name:
        raise GatewayError(403, f"The JWT has no '{settings.jwt_service_claim}' claim naming a service.",
                           type="permission_error", code="no_service_claim")
    service = db.one("SELECT * FROM services WHERE name = ?", (service_name,))
    if service is None:
        raise GatewayError(403, f"Unknown service '{service_name}'.", type="permission_error", code="unknown_service")
    service = _check_service(db, service["id"])

    subject = f"{oidc.issuer}|{claims['sub']}"
    key = db.one("SELECT * FROM api_keys WHERE kind = 'jwt' AND subject = ? AND service_id = ?", (subject, service["id"]))
    if key is None:  # identity-bound virtual key: limits/usage/allow-lists work exactly like a normal key
        label = claims.get("email") or claims.get("preferred_username") or claims.get("azp") or str(claims["sub"])
        digest = "jwt:" + hashlib.sha256(f"{subject}|{service['id']}".encode()).hexdigest()  # never equals a 64-hex key hash
        kid = db.insert(
            "INSERT INTO api_keys (service_id, name, key_prefix, key_hash, kind, subject, created_at) "
            "VALUES (?, ?, ?, ?, 'jwt', ?, ?)",
            (service["id"], str(label)[:80], "jwt:" + str(claims["sub"])[:10], digest, subject, now()))
        key = db.one("SELECT * FROM api_keys WHERE id = ?", (kid,))
    assert key is not None
    if not key["enabled"]:
        raise GatewayError(403, "This identity has been disabled.", type="permission_error", code="key_disabled")

    models_claim = claim_path(claims, settings.jwt_models_claim)
    if models_claim is not None:  # token may narrow, never widen, what the key allows
        wanted = set(as_list(models_claim))
        existing = json.loads(key["allowed_models"]) if key["allowed_models"] else None
        effective = sorted(wanted & set(existing)) if existing else sorted(wanted)
        key = {**key, "allowed_models": json.dumps(effective or _NO_MODELS)}
    _touch(db, key)
    return AuthContext(key=key, service=service)
