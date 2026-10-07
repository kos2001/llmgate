from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_ADMIN_TOKEN = "admin-change-me"


class Settings(BaseSettings):
    """Runtime configuration. Every field can be set via an ``LLMGATE_*`` env var or ``.env``."""

    model_config = SettingsConfigDict(env_prefix="LLMGATE_", env_file=".env", extra="ignore")

    admin_token: str = DEFAULT_ADMIN_TOKEN
    database_path: str = "data/llmgate.db"
    key_prefix: str = "sk-lg-"

    # Upstream HTTP behaviour
    connect_timeout_s: float = 5.0
    default_read_timeout_s: float = 120.0
    max_request_bytes: int = 10 * 1024 * 1024

    # Background provider health checks (0 disables the loop)
    health_interval_s: float = 30.0
    health_timeout_s: float = 3.0

    # Providers with this many consecutive failed checks are tried last
    unhealthy_after: int = 1

    cors_origins: str = "*"
    # Data retention in days; 0 keeps everything. Old usage/audit rows are deleted in the background every few hours.
    usage_retention_days: float = 0
    audit_retention_days: float = 0
    # Optional: Fernet key(s) (`llmgate genkey`, comma separated to rotate) that encrypt provider API keys at rest.
    encryption_key: str = ""
    # Rejected-credential throttle per client address (0 disables). Behind a reverse proxy all clients share its address.
    auth_fail_limit: int = 30
    auth_fail_window_s: float = 60.0

    # ---- OpenRouter-style features ---------------------------------------------------------
    public_catalog: bool = True  # /api/v1/models, /endpoints and /providers need no key (like openrouter.ai); false = require one
    auto_model: str = "llmgate/auto"  # pseudo-model that picks a real model per prompt
    usage_webhook_url: str = ""  # if set, every request's metadata is POSTed here

    # ---- LiteLLM-style router / cache / alerts ------------------------------------------------
    cooldown_after_fails: int = 3  # consecutive failures before a provider is skipped (0 = never)
    cooldown_s: float = 30.0
    upstream_retries: int = 0  # extra attempts on the same provider before failing over
    retry_backoff_s: float = 0.25
    cache_ttl_s: int = 0  # >0 caches identical non-streaming requests; requests can opt in/out with "cache"
    cache_max_entries: int = 2000
    cache_scope: str = "service"  # service: each service has its own entries; global: shared
    alert_webhook_url: str = ""  # Slack-compatible ({"text": ...}) alerts: provider down/up, budget thresholds
    alert_budget_thresholds: str = "0.8,1.0"

    # ---- Authentication -------------------------------------------------------------------
    # The static admin token stays valid as a break-glass credential unless disabled.
    admin_token_enabled: bool = True
    # Signs dashboard session JWTs (HS256). Unset = random per process (sessions end on restart).
    jwt_secret: str = ""
    session_ttl_s: int = 8 * 3600
    session_cookie: str = "llmgate_session"
    cookie_secure: bool = False  # set true behind HTTPS

    # OIDC / SSO (any compliant IdP: Keycloak, Okta, Azure AD, Google, Auth0, ...). Empty issuer = off.
    oidc_issuer: str = ""
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_scopes: str = "openid profile email"
    oidc_redirect_uri: str = ""  # e.g. https://gw.example.com/auth/oidc/callback ; derived from the request if empty
    oidc_audience: str = ""  # accepted `aud` for IdP-issued bearer JWTs; defaults to the client id
    oidc_algorithms: str = "RS256,ES256,PS256"
    oidc_leeway_s: int = 30

    # Claim → role. `roles_claim` may be a dotted path (e.g. realm_access.roles or groups).
    oidc_roles_claim: str = "roles"
    oidc_role_map: str = '{"llm-admins":"admin","llm-operators":"operator","llm-viewers":"viewer"}'
    oidc_default_role: str = "none"  # role for signed-in users with no mapped group: viewer|operator|admin, or "none" = deny
    oidc_allowed_email_domains: str = ""  # comma separated; empty = any

    # Development only (`llmgate dev` sets it): the dashboard goes straight to SSO sign-in instead of showing the login form.
    # Authentication itself is unchanged: the browser still does the real authorization-code + PKCE flow with the IdP.
    dev_autologin: bool = False

    # JWT bearer for the inference API: which claim names the service a token acts for.
    jwt_service_claim: str = "llmgate_service"
    jwt_models_claim: str = "llmgate_models"  # optional list restricting models for that token


@lru_cache
def get_settings() -> Settings:
    return Settings()
