from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS providers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    kind          TEXT NOT NULL DEFAULT 'vllm',      -- vllm | ollama | tgi | openai | other
    base_url      TEXT NOT NULL,                      -- OpenAI-compatible root, e.g. http://host:8000/v1
    api_key       TEXT,                               -- bearer token sent upstream (optional)
    enabled       INTEGER NOT NULL DEFAULT 1,
    timeout_s     REAL,
    status        TEXT NOT NULL DEFAULT 'unknown',   -- unknown | healthy | unhealthy
    last_checked_at REAL,
    last_error    TEXT,
    latency_ms    REAL,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    engine_stats  TEXT,                               -- JSON scraped from the engine's /metrics
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS models (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,               -- public id clients put in "model"
    display_name  TEXT,
    description   TEXT,
    kind          TEXT NOT NULL DEFAULT 'chat',       -- chat | embedding
    context_length INTEGER,
    input_price_per_m  REAL NOT NULL DEFAULT 0,       -- USD per 1M prompt tokens
    output_price_per_m REAL NOT NULL DEFAULT 0,       -- USD per 1M completion tokens
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS model_routes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    model_id      INTEGER NOT NULL REFERENCES models(id) ON DELETE CASCADE,
    provider_id   INTEGER NOT NULL REFERENCES providers(id) ON DELETE CASCADE,
    upstream_model TEXT NOT NULL,                     -- name the engine knows the model by
    priority      INTEGER NOT NULL DEFAULT 0,         -- lower is tried first
    weight        INTEGER NOT NULL DEFAULT 1,         -- load-balancing weight inside a priority tier
    enabled       INTEGER NOT NULL DEFAULT 1,
    UNIQUE (model_id, provider_id, upstream_model)
);

CREATE TABLE IF NOT EXISTS services (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    description   TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    monthly_token_limit INTEGER,                      -- NULL = unlimited
    monthly_budget_usd  REAL,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    service_id    INTEGER NOT NULL REFERENCES services(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    key_prefix    TEXT NOT NULL,                      -- first chars, safe to display
    key_hash      TEXT NOT NULL UNIQUE,               -- sha256 of the full key
    enabled       INTEGER NOT NULL DEFAULT 1,
    allowed_models TEXT,                              -- JSON list; NULL/[] = every model
    rpm_limit     INTEGER,
    monthly_token_limit INTEGER,
    monthly_budget_usd  REAL,
    expires_at    REAL,
    last_used_at  REAL,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    request_id    TEXT NOT NULL,
    service_id    INTEGER,
    api_key_id    INTEGER,
    provider_id   INTEGER,
    model         TEXT,
    upstream_model TEXT,
    endpoint      TEXT NOT NULL,
    stream        INTEGER NOT NULL DEFAULT 0,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    usage_estimated   INTEGER NOT NULL DEFAULT 0,     -- 1 when the engine reported no usage
    cost_usd      REAL NOT NULL DEFAULT 0,
    latency_ms    REAL,
    ttft_ms       REAL,
    status_code   INTEGER,
    success       INTEGER NOT NULL DEFAULT 1,
    error         TEXT,
    attempts      INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_usage_ts ON usage_logs (ts);
CREATE INDEX IF NOT EXISTS idx_usage_key_ts ON usage_logs (api_key_id, ts);
CREATE INDEX IF NOT EXISTS idx_usage_service_ts ON usage_logs (service_id, ts);
CREATE INDEX IF NOT EXISTS idx_usage_model_ts ON usage_logs (model, ts);
CREATE INDEX IF NOT EXISTS idx_routes_model ON model_routes (model_id);
CREATE INDEX IF NOT EXISTS idx_keys_service ON api_keys (service_id);
"""


CREATE_EXTRA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    issuer        TEXT NOT NULL,
    subject       TEXT NOT NULL,
    email         TEXT,
    name          TEXT,
    role          TEXT NOT NULL DEFAULT 'viewer',     -- viewer | operator | admin
    role_source   TEXT NOT NULL DEFAULT 'claims',     -- claims (IdP decides) | manual (an admin pinned it)
    enabled       INTEGER NOT NULL DEFAULT 1,
    login_count   INTEGER NOT NULL DEFAULT 0,
    last_login_at REAL,
    created_at    REAL NOT NULL,
    UNIQUE (issuer, subject)
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    actor   TEXT NOT NULL,
    role    TEXT,
    method  TEXT NOT NULL,
    path    TEXT NOT NULL,
    status  INTEGER,
    ip      TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_logs (ts);

CREATE TABLE IF NOT EXISTS customers (            -- end users, identified by the request's "user" field
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       TEXT NOT NULL UNIQUE,
    alias         TEXT,
    max_budget_usd REAL,
    budget_duration_s INTEGER,
    budget_anchor REAL,
    blocked       INTEGER NOT NULL DEFAULT 0,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS model_aliases (        -- "gpt-4" -> a model of this registry
    alias      TEXT PRIMARY KEY,
    model_name TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS guardrails (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL UNIQUE,
    kind     TEXT NOT NULL,                        -- pii | secrets | keywords | regex | injection | max_chars
    mode     TEXT NOT NULL DEFAULT 'block',        -- block | mask
    apply_to TEXT NOT NULL DEFAULT 'input',        -- input | output | both
    config   TEXT,                                 -- JSON, kind specific
    enabled  INTEGER NOT NULL DEFAULT 1,
    default_on INTEGER NOT NULL DEFAULT 1,         -- 0 = only when a request names it in "guardrails"
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_request ON usage_logs (request_id);
"""

# (table, column, DDL). Applied to fresh and existing databases alike, so adding a column is one line here.
COLUMNS: list[tuple[str, str, str]] = [
    ("providers", "data_collection", "TEXT NOT NULL DEFAULT 'deny'"),   # allow | deny (does the engine log prompts?)
    ("providers", "quantization", "TEXT"),
    ("models", "supported_parameters", "TEXT"),                          # JSON list, e.g. ["tools","response_format"]
    ("models", "input_modalities", "TEXT"),                              # JSON list, default ["text"]
    ("models", "output_modalities", "TEXT"),
    ("models", "max_completion_tokens", "INTEGER"),
    ("model_routes", "input_price_per_m", "REAL"),                       # per-provider price override (NULL = model price)
    ("model_routes", "output_price_per_m", "REAL"),
    ("api_keys", "kind", "TEXT NOT NULL DEFAULT 'api_key'"),             # api_key | jwt (identity-bound)
    ("api_keys", "subject", "TEXT"),
    ("api_keys", "allowed_providers", "TEXT"),                           # JSON list; NULL = every provider
    ("usage_logs", "app_title", "TEXT"),                                 # X-Title
    ("usage_logs", "referer", "TEXT"),                                   # HTTP-Referer
    ("usage_logs", "end_user", "TEXT"),                                  # request "user" field
    ("usage_logs", "requested_model", "TEXT"),                           # set when a fallback model answered instead
    ("usage_logs", "finish_reason", "TEXT"),
    # --- LiteLLM-style controls
    ("api_keys", "budget_duration_s", "INTEGER"),                        # NULL = calendar month
    ("api_keys", "budget_anchor", "REAL"),                               # start of the current budget window
    ("api_keys", "tpm_limit", "INTEGER"),
    ("api_keys", "max_parallel", "INTEGER"),
    ("api_keys", "model_budgets", "TEXT"),                               # JSON {model: USD} per budget window
    ("api_keys", "metadata", "TEXT"),                                    # JSON, free-form
    ("services", "budget_duration_s", "INTEGER"),
    ("services", "budget_anchor", "REAL"),
    ("services", "rpm_limit", "INTEGER"),
    ("services", "tpm_limit", "INTEGER"),
    ("services", "allowed_models", "TEXT"),                              # JSON; team-level model access (NULL = all)
    ("services", "metadata", "TEXT"),
    ("models", "routing_strategy", "TEXT NOT NULL DEFAULT 'weighted'"),  # weighted | least-busy | latency | cost | usage
    ("models", "fallbacks", "TEXT"),                                     # JSON list of model names tried after a failure
    ("models", "context_window_fallbacks", "TEXT"),                      # ... only after a context-length error
    ("model_routes", "rpm_limit", "INTEGER"),
    ("model_routes", "tpm_limit", "INTEGER"),
    ("model_routes", "max_parallel", "INTEGER"),
    ("usage_logs", "tags", "TEXT"),                                      # JSON list from request tags / metadata.tags
    ("usage_logs", "cache_hit", "INTEGER NOT NULL DEFAULT 0"),
    ("usage_logs", "guardrail", "TEXT"),                                 # name(s) of guardrails that acted on the request
]


def now() -> float:
    return time.time()


class Database:
    """Thin sqlite3 wrapper: one shared connection, serialised by a lock.

    Statements here are small indexed lookups/inserts, so running them inline on the
    event loop is cheaper than hopping to a thread pool for every call.
    """

    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        for table, column, ddl in COLUMNS:
            have = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if column not in have:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        self._conn.executescript(CREATE_EXTRA)
        self._conn.execute("CREATE INDEX IF NOT EXISTS idx_usage_app ON usage_logs (app_title)")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, tuple(params))

    def insert(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._lock:
            return int(self._conn.execute(sql, tuple(params)).lastrowid)

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, tuple(params)).fetchone()
            return dict(row) if row else None

    def scalar(self, sql: str, params: Iterable[Any] = (), default: Any = None) -> Any:
        with self._lock:
            row = self._conn.execute(sql, tuple(params)).fetchone()
            return row[0] if row and row[0] is not None else default

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                yield
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")
