"""Response cache (LiteLLM's ``cache``): identical non-streaming requests are answered from memory at zero cost.

Per-request control mirrors LiteLLM: ``"cache": {"ttl": 600, "no-cache": true, "no-store": true}``
(or ``"cache": false``). ``Cache-Control: no-cache|no-store`` headers are honoured too.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

from .config import Settings
from .errors import GatewayError

# Fields that do not change the answer (or are gateway-only) and so must not split cache entries.
_IGNORED = {"stream", "stream_options", "user", "cache", "num_retries", "tags", "metadata", "provider", "models",
            "route", "guardrails"}


@dataclass
class Policy:
    enabled: bool = False
    ttl: int = 0
    lookup: bool = True
    store: bool = True


def policy_for(settings: Settings, payload: dict[str, Any], headers: Any, streaming: bool) -> Policy:
    raw = payload.get("cache")
    if raw is not None and not isinstance(raw, (bool, dict)):
        raise GatewayError(400, "'cache' must be an object or a boolean.", code="invalid_cache")
    pol = Policy(enabled=settings.cache_ttl_s > 0, ttl=settings.cache_ttl_s)
    if isinstance(raw, bool):
        pol.enabled = raw
        pol.ttl = pol.ttl or 300
    elif isinstance(raw, dict):
        pol.enabled = bool(raw.get("enabled", True))
        pol.ttl = int(raw.get("ttl") or pol.ttl or 300)
        pol.lookup = not raw.get("no-cache", False)
        pol.store = not raw.get("no-store", False)
    control = (headers.get("cache-control") or "").lower()
    pol.lookup &= "no-cache" not in control
    pol.store &= "no-store" not in control
    if streaming:  # SSE replies are not cached
        pol.enabled = False
    return pol


def make_key(settings: Settings, service_id: int, endpoint: str, payload: dict[str, Any]) -> str:
    body = {k: v for k, v in payload.items() if k not in _IGNORED}
    scope = service_id if settings.cache_scope == "service" else 0
    blob = json.dumps([scope, endpoint, body], sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


class ResponseCache:
    def __init__(self, max_entries: int = 2000):
        self.max_entries = max_entries
        self._d: OrderedDict[str, tuple[float, int, bytes]] = OrderedDict()
        self.hits = self.misses = 0

    def get(self, key: str) -> tuple[int, bytes] | None:
        entry = self._d.get(key)
        if entry is None or entry[0] < time.time():
            self._d.pop(key, None)
            self.misses += 1
            return None
        self._d.move_to_end(key)
        self.hits += 1
        return entry[1], entry[2]

    def put(self, key: str, status: int, body: bytes, ttl: int) -> None:
        self._d[key] = (time.time() + ttl, status, body)
        self._d.move_to_end(key)
        while len(self._d) > self.max_entries:
            self._d.popitem(last=False)

    def flush(self) -> int:
        n = len(self._d)
        self._d.clear()
        return n

    def stats(self) -> dict[str, Any]:
        total = self.hits + self.misses
        return {"entries": len(self._d), "hits": self.hits, "misses": self.misses,
                "hit_rate": round(self.hits / total, 4) if total else None, "max_entries": self.max_entries}
