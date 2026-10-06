"""JWT helpers.

Two token families never mix:

* **Session / state tokens** are HS256, signed with the gateway's own secret, ``iss=llmgate``.
* **IdP tokens** are asymmetric (RS/ES/PS), verified against the IdP's JWKS (see ``oidc.py``).

``verify_*`` pins the algorithm list per family, so an attacker cannot present an HS256 token
"signed" with the IdP's public key, nor an unsigned (``alg: none``) one.
"""

from __future__ import annotations

import secrets
import time
from typing import Any

import jwt

SESSION_ISS = "llmgate"
SESSION_AUD = "llmgate-session"
STATE_AUD = "llmgate-oidc-state"
_HS = ["HS256"]


def new_secret() -> str:
    return secrets.token_urlsafe(48)


def looks_like_jwt(token: str) -> bool:
    return token.startswith("eyJ") and token.count(".") == 2


def header_alg(token: str) -> str | None:
    try:
        return jwt.get_unverified_header(token).get("alg")
    except jwt.PyJWTError:
        return None


def _sign(secret: str, aud: str, ttl: int, claims: dict[str, Any]) -> str:
    now = int(time.time())
    body = {**claims, "iss": SESSION_ISS, "aud": aud, "iat": now, "nbf": now, "exp": now + ttl, "jti": secrets.token_hex(8)}
    return jwt.encode(body, secret, algorithm="HS256")


def _verify(secret: str, token: str, aud: str) -> dict[str, Any]:
    return jwt.decode(token, secret, algorithms=_HS, audience=aud, issuer=SESSION_ISS,
                      options={"require": ["exp", "iat", "iss", "aud"]})


def issue_session(secret: str, user_id: int, ttl: int) -> str:
    return _sign(secret, SESSION_AUD, ttl, {"sub": str(user_id)})


def verify_session(secret: str, token: str) -> dict[str, Any]:
    return _verify(secret, token, SESSION_AUD)


def sign_state(secret: str, claims: dict[str, Any], ttl: int = 600) -> str:
    return _sign(secret, STATE_AUD, ttl, claims)


def verify_state(secret: str, token: str) -> dict[str, Any]:
    return _verify(secret, token, STATE_AUD)


def claim_path(claims: dict[str, Any], dotted: str) -> Any:
    """``realm_access.roles`` → claims["realm_access"]["roles"]. A literal key wins over a path."""
    if dotted in claims:
        return claims[dotted]
    cur: Any = claims
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def as_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):  # space- or comma-separated scopes/roles
        return [v for v in value.replace(",", " ").split() if v]
    if isinstance(value, (list, tuple, set)):
        return [str(v) for v in value]
    return [str(value)]
