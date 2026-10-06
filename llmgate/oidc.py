"""OpenID Connect relying-party logic: discovery, JWKS, PKCE authorization-code flow, token validation."""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from .config import Settings
from .errors import GatewayError

_META_TTL_S = 3600
_JWKS_TTL_S = 3600
_JWKS_MIN_REFETCH_S = 30  # floor between forced refreshes (unknown kid), so garbage tokens can't hammer the IdP
_FORBIDDEN_ALGS = {"none", "HS256", "HS384", "HS512"}


def new_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


class OIDCClient:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self.s, self.http = settings, http
        self.issuer = settings.oidc_issuer.rstrip("/")
        self.algorithms = [a.strip() for a in settings.oidc_algorithms.split(",") if a.strip()]
        if _FORBIDDEN_ALGS & set(self.algorithms):
            raise ValueError("LLMGATE_OIDC_ALGORITHMS must list asymmetric algorithms only")
        self._meta: tuple[float, dict[str, Any]] | None = None
        self._keys: dict[str, jwt.PyJWK] = {}
        self._keys_at = 0.0
        self._forced_at = 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.issuer and self.s.oidc_client_id)

    @property
    def audience(self) -> str:
        return self.s.oidc_audience or self.s.oidc_client_id

    async def metadata(self) -> dict[str, Any]:
        if self._meta and time.monotonic() - self._meta[0] < _META_TTL_S:
            return self._meta[1]
        try:
            resp = await self.http.get(f"{self.issuer}/.well-known/openid-configuration", timeout=5)
            resp.raise_for_status()
            meta = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise GatewayError(502, f"OIDC discovery failed: {type(exc).__name__}", type="upstream_error",
                               code="oidc_unavailable") from None
        if meta.get("issuer", "").rstrip("/") != self.issuer:  # RFC 8414 §3.3: reject a mismatched issuer
            raise GatewayError(502, "OIDC issuer mismatch in discovery document", type="upstream_error", code="oidc_issuer_mismatch")
        self._meta = (time.monotonic(), meta)
        return meta

    async def _load_keys(self) -> None:
        meta = await self.metadata()
        try:
            resp = await self.http.get(meta["jwks_uri"], timeout=5)
            resp.raise_for_status()
            keys = jwt.PyJWKSet.from_dict(resp.json()).keys
        except (httpx.HTTPError, ValueError, KeyError, jwt.PyJWTError) as exc:
            raise GatewayError(502, f"Could not load the IdP signing keys: {type(exc).__name__}", type="upstream_error",
                               code="jwks_unavailable") from None
        self._keys = {k.key_id or f"_{i}": k for i, k in enumerate(keys)}
        self._keys_at = time.monotonic()

    async def _key_for(self, kid: str | None) -> jwt.PyJWK:
        now = time.monotonic()
        if not self._keys or now - self._keys_at > _JWKS_TTL_S:
            await self._load_keys()
        key = self._keys.get(kid) if kid else (next(iter(self._keys.values())) if len(self._keys) == 1 else None)
        if key is None and now - self._forced_at > _JWKS_MIN_REFETCH_S:  # key rotation: refetch once
            self._forced_at = now
            await self._load_keys()
            key = self._keys.get(kid) if kid else (next(iter(self._keys.values())) if len(self._keys) == 1 else None)
        if key is None:
            raise jwt.InvalidKeyError("unknown signing key id")
        return key

    async def verify(self, token: str, *, audience: str | None = None, nonce: str | None = None) -> dict[str, Any]:
        """Validate an IdP-issued JWT (signature, issuer, audience, expiry). Raises ``jwt.PyJWTError``."""
        header = jwt.get_unverified_header(token)
        alg = header.get("alg")
        if alg not in self.algorithms:
            raise jwt.InvalidAlgorithmError(f"algorithm {alg!r} is not allowed")
        key = await self._key_for(header.get("kid"))
        claims = jwt.decode(
            token, key.key, algorithms=[alg], audience=audience or self.audience, issuer=self.issuer,
            leeway=self.s.oidc_leeway_s, options={"require": ["exp", "iss", "sub"]},
        )
        if nonce is not None and not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            raise jwt.InvalidTokenError("nonce mismatch")
        return claims

    async def authorize_url(self, redirect_uri: str, state: str, nonce: str, challenge: str, prompt: str | None = None) -> str:
        meta = await self.metadata()
        params = {
            "response_type": "code", "client_id": self.s.oidc_client_id, "redirect_uri": redirect_uri,
            "scope": self.s.oidc_scopes, "state": state, "nonce": nonce,
            "code_challenge": challenge, "code_challenge_method": "S256",
            **({"prompt": prompt} if prompt else {}),  # e.g. select_account: "sign in as someone else"
        }
        return f"{meta['authorization_endpoint']}?{urlencode(params)}"

    async def exchange_code(self, code: str, redirect_uri: str, verifier: str) -> dict[str, Any]:
        meta = await self.metadata()
        data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
                "client_id": self.s.oidc_client_id, "code_verifier": verifier}
        auth = None
        if self.s.oidc_client_secret:
            auth = httpx.BasicAuth(self.s.oidc_client_id, self.s.oidc_client_secret)
            data.pop("client_id")
        try:
            resp = await self.http.post(meta["token_endpoint"], data=data, auth=auth, timeout=10)
        except httpx.HTTPError as exc:
            raise GatewayError(502, f"Token endpoint unreachable: {type(exc).__name__}", type="upstream_error",
                               code="oidc_unavailable") from None
        if resp.status_code != 200:
            raise GatewayError(401, "The identity provider rejected the login (code exchange failed).",
                               type="authentication_error", code="oidc_exchange_failed")
        return resp.json()
