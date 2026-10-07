"""Who is calling the admin API (authentication) and what may they do (authorization)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import Request

from .config import Settings
from .db import Database, now
from .errors import GatewayError
from .jwtutil import as_list, claim_path, header_alg, issue_session, looks_like_jwt, verify_session
from .oidc import OIDCClient
from .security import constant_time_equals

ROLES = ("viewer", "operator", "admin")
RANK = {r: i for i, r in enumerate(ROLES)}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSRF_HEADER = "x-llmgate-csrf"


@dataclass
class Principal:
    kind: str  # admin_token | session | idp_jwt
    subject: str
    role: str
    email: str | None = None
    name: str | None = None
    user_id: int | None = None

    def can(self, role: str) -> bool:
        return RANK[self.role] >= RANK[role]

    @property
    def label(self) -> str:
        return self.email or self.name or self.subject

    def public(self) -> dict[str, Any]:
        return {"kind": self.kind, "subject": self.subject, "email": self.email, "name": self.name,
                "role": self.role, "user_id": self.user_id, "permissions": [r for r in ROLES if self.can(r)]}


def required_role(method: str, path: str) -> str:
    """Single, auditable authorization policy for ``/admin/api/*``.

    * user/role management, auth settings, audit log  → admin (even to read)
    * anything that changes a provider (holds upstream credentials/URLs) → admin
    * other writes (models, routes, services, keys)   → operator
    * reads                                           → viewer
    """
    if path.startswith(("/admin/api/users", "/admin/api/auth", "/admin/api/audit")):
        return "admin"
    if method in SAFE_METHODS:
        return "viewer"
    if path.startswith(("/admin/api/providers", "/admin/api/import", "/admin/api/storage/prune", "/model/")):
        return "admin"  # these create or alter providers (URLs + upstream credentials)
    if path.startswith("/utils/") or path == "/admin/api/guardrails/test":
        return "viewer"  # read-only helpers that happen to be POST
    return "operator"


# --------------------------------------------------------------------------------------- claims → role

def role_map(settings: Settings) -> dict[str, str]:
    try:
        raw = json.loads(settings.oidc_role_map or "{}")
    except ValueError:
        raise ValueError("LLMGATE_OIDC_ROLE_MAP must be a JSON object") from None
    bad = {v for v in raw.values() if v not in RANK}
    if bad:
        raise ValueError(f"LLMGATE_OIDC_ROLE_MAP has unknown roles: {sorted(bad)}")
    return raw


def map_role(claims: dict[str, Any], settings: Settings) -> str | None:
    """Highest role granted by the token's roles claim. IdP values equal to a role name map to themselves."""
    mapping = role_map(settings)
    granted = [mapping.get(v) or (v if v in RANK else None) for v in as_list(claim_path(claims, settings.oidc_roles_claim))]
    granted = [g for g in granted if g]
    return max(granted, key=RANK.__getitem__) if granted else None


def upsert_user(db: Database, settings: Settings, issuer: str, claims: dict[str, Any]) -> dict[str, Any]:
    """JIT-provision (or refresh) the local user behind an IdP identity and decide their role."""
    email = claims.get("email")
    domains = [d.strip().lower() for d in settings.oidc_allowed_email_domains.split(",") if d.strip()]
    if domains and (not email or email.rsplit("@", 1)[-1].lower() not in domains):
        raise GatewayError(403, "Your email domain is not allowed to sign in.", type="permission_error", code="domain_not_allowed")

    subject = str(claims["sub"])
    user = db.one("SELECT * FROM users WHERE issuer = ? AND subject = ?", (issuer, subject))
    if user is None and email and claims.get("email_verified", True):  # adopt an admin-pre-provisioned row
        pending = db.one("SELECT * FROM users WHERE issuer = '' AND lower(email) = lower(?)", (email,))
        if pending:
            db.execute("UPDATE users SET issuer = ?, subject = ? WHERE id = ?", (issuer, subject, pending["id"]))
            user = {**pending, "issuer": issuer, "subject": subject}

    claimed = map_role(claims, settings)
    default = settings.oidc_default_role
    role = claimed or (default if default in RANK else None)
    if user and user["role_source"] == "manual":
        role = user["role"]
    if role is None:
        raise GatewayError(403, "You are signed in but have not been granted any role for this gateway.",
                           type="permission_error", code="no_role")
    if user and not user["enabled"]:
        raise GatewayError(403, "This account has been disabled.", type="permission_error", code="user_disabled")

    ts = now()
    name = claims.get("name") or claims.get("preferred_username")
    if user is None:
        uid = db.insert(
            "INSERT INTO users (issuer, subject, email, name, role, role_source, login_count, last_login_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'claims', 1, ?, ?)", (issuer, subject, email, name, role, ts, ts))
    else:
        uid = user["id"]
        db.execute("UPDATE users SET email = COALESCE(?, email), name = COALESCE(?, name), role = ?, "
                   "login_count = login_count + 1, last_login_at = ? WHERE id = ?", (email, name, role, ts, uid))
    return db.one("SELECT * FROM users WHERE id = ?", (uid,))  # type: ignore[return-value]


def _principal_from_user(user: dict[str, Any], kind: str) -> Principal:
    return Principal(kind, user["subject"], user["role"], user["email"], user["name"], user["id"])


# --------------------------------------------------------------------------------------- authentication

def bearer_token(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else None


async def resolve_principal(request: Request) -> Principal | None:
    st = request.app.state
    settings: Settings = st.settings
    db: Database = st.db
    oidc: OIDCClient = st.oidc
    token = bearer_token(request)
    from_cookie = False
    if token is None:
        token = request.cookies.get(settings.session_cookie)
        from_cookie = token is not None
    if not token:
        return None

    if not from_cookie and settings.admin_token_enabled and constant_time_equals(token, settings.admin_token):
        return Principal("admin_token", "admin-token", "admin")
    if not looks_like_jwt(token):
        return None

    try:
        if header_alg(token) == "HS256":  # our own session token
            claims = verify_session(st.jwt_secret, token)
            user = db.one("SELECT * FROM users WHERE id = ?", (int(claims["sub"]),))
            if user is None or not user["enabled"]:
                return None
            principal = _principal_from_user(user, "session")
        elif oidc.enabled and not from_cookie:  # an IdP-issued access/ID token used directly as a bearer
            claims = await oidc.verify(token)
            user = upsert_user(db, settings, oidc.issuer, claims)
            principal = _principal_from_user(user, "idp_jwt")
        else:
            return None
    except (jwt.PyJWTError, ValueError, KeyError):
        return None

    if from_cookie and request.method not in SAFE_METHODS and CSRF_HEADER not in request.headers:
        raise GatewayError(403, f"Missing {CSRF_HEADER} header (CSRF protection).", type="permission_error", code="csrf")
    return principal


async def admin_guard(request: Request) -> Principal:
    """FastAPI dependency for every admin route: authenticate, then enforce ``required_role``."""
    throttle = request.app.state.throttle
    ip = request.client.host if request.client else "?"
    throttle.check(ip)
    principal = await resolve_principal(request)
    if principal is None:
        if bearer_token(request) is not None:  # a credential was presented and refused (no credential at all is not an "attack")
            throttle.fail(ip)
        raise GatewayError(401, "Authentication required. Sign in with SSO or send an admin/JWT bearer token.",
                           type="authentication_error", code="invalid_admin_token")
    request.state.principal = principal
    need = required_role(request.method, request.url.path)
    if not principal.can(need):
        raise GatewayError(403, f"This action needs the '{need}' role; you have '{principal.role}'.",
                           type="permission_error", code="insufficient_role")
    return principal


def start_session(request: Request, user: dict[str, Any]) -> str:
    st = request.app.state
    return issue_session(st.jwt_secret, user["id"], st.settings.session_ttl_s)
