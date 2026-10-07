from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field

from .admin_common import ADMIN_DEPS, flags, get_db, insert_row, require_row, update_row
from .db import now
from .errors import GatewayError
from .rbac import ROLES, role_map

router = APIRouter(prefix="/admin/api", tags=["users"], dependencies=ADMIN_DEPS)

Role = Literal["viewer", "operator", "admin"]


class UserCreate(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+$")
    role: Role = "viewer"


class UserUpdate(BaseModel):
    role: Role | None = None
    enabled: bool | None = None
    release_role: bool = Field(default=False, description="Hand the role back to IdP claims")


def _present(u: dict[str, Any]) -> dict[str, Any]:
    flags(u, "enabled")
    u["pending"] = u["issuer"] == ""  # pre-provisioned, has not signed in yet
    return u


@router.get("/users")
def list_users(request: Request) -> list[dict[str, Any]]:
    return [_present(u) for u in get_db(request).query("SELECT * FROM users ORDER BY role DESC, email, id")]


@router.post("/users", status_code=201)
def pre_provision(body: UserCreate, request: Request) -> dict[str, Any]:
    """Grant a role to someone before their first SSO login; the row is adopted by matching email."""
    db = get_db(request)
    uid = insert_row(db, "users", {"issuer": "", "subject": f"pending:{body.email.lower()}", "email": body.email,
                                   "role": body.role, "role_source": "manual", "created_at": now()})
    return _present(require_row(db, "users", uid, "User"))


@router.patch("/users/{user_id}")
def update_user(user_id: int, body: UserUpdate, request: Request) -> dict[str, Any]:
    db = get_db(request)
    require_row(db, "users", user_id, "User")  # 404 if missing
    me = getattr(request.state, "principal", None)
    changes: dict[str, Any] = {}
    if body.role is not None:
        changes.update(role=body.role, role_source="manual")
    if body.release_role:
        changes["role_source"] = "claims"
    if body.enabled is not None:
        changes["enabled"] = int(body.enabled)
    if me and me.user_id == user_id and (changes.get("enabled") == 0 or ("role" in changes and changes["role"] != "admin")):
        raise GatewayError(400, "You cannot demote or disable your own account.", code="self_lockout")
    update_row(db, "users", user_id, changes)
    return _present(require_row(db, "users", user_id, "User"))


@router.delete("/users/{user_id}", status_code=204)
def delete_user(user_id: int, request: Request) -> None:
    db = get_db(request)
    require_row(db, "users", user_id, "User")
    me = getattr(request.state, "principal", None)
    if me and me.user_id == user_id:
        raise GatewayError(400, "You cannot delete your own account.", code="self_lockout")
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))


@router.get("/auth/settings")
def auth_settings(request: Request) -> dict[str, Any]:
    """Effective (non-secret) authentication and authorization configuration."""
    st = request.app.state
    s = st.settings
    return {
        "admin_token_enabled": s.admin_token_enabled,
        "session_ttl_s": s.session_ttl_s,
        "oidc": {
            "enabled": st.oidc.enabled, "issuer": s.oidc_issuer or None, "client_id": s.oidc_client_id or None,
            "scopes": s.oidc_scopes, "audience": st.oidc.audience or None, "algorithms": st.oidc.algorithms,
            "roles_claim": s.oidc_roles_claim, "role_map": role_map(s), "default_role": s.oidc_default_role,
            "allowed_email_domains": [d.strip() for d in s.oidc_allowed_email_domains.split(",") if d.strip()],
        },
        "jwt_inference": {"service_claim": s.jwt_service_claim, "models_claim": s.jwt_models_claim},
        "roles": [{"name": r, "can": {"viewer": "대시보드·사용량 조회", "operator": "+ 모델·라우트·서비스·키 관리",
                                       "admin": "+ 프로바이더·사용자·권한·감사 로그"}[r]} for r in ROLES],
        "users": request.app.state.db.scalar("SELECT COUNT(*) FROM users", default=0),
    }


@router.get("/audit")
def audit_log(request: Request, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)) -> dict[str, Any]:
    db = get_db(request)
    return {"total": db.scalar("SELECT COUNT(*) FROM audit_logs", default=0),
            "data": db.query("SELECT * FROM audit_logs ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))}
