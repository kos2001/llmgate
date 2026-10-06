"""In-memory routing state: in-flight counters, per-route rate limits and provider cooldowns.

Cooldown (LiteLLM's ``allowed_fails`` / ``cooldown_time``): after N consecutive failures a provider is skipped
for a while instead of being retried on every request. If *every* candidate is cooling, they are all tried anyway.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Any

from .config import Settings


class RouterState:
    def __init__(self, settings: Settings):
        self.after_fails = settings.cooldown_after_fails
        self.cooldown_s = settings.cooldown_s
        self.route_inflight: dict[int, int] = defaultdict(int)
        self.provider_inflight: dict[int, int] = defaultdict(int)
        self._fails: dict[int, int] = defaultdict(int)
        self._until: dict[int, float] = {}
        self._hits: dict[int, deque[float]] = defaultdict(deque)  # route rpm window
        self._tokens: dict[int, deque[tuple[float, int]]] = defaultdict(deque)  # route tpm window

    # -- cooldown ------------------------------------------------------------------------------
    def cooldown_remaining(self, provider_id: int) -> float:
        return max(0.0, self._until.get(provider_id, 0.0) - time.monotonic())

    def record(self, provider_id: int, ok: bool) -> None:
        if ok:
            self._fails[provider_id] = 0
            self._until.pop(provider_id, None)
            return
        self._fails[provider_id] += 1
        if self.after_fails and self._fails[provider_id] >= self.after_fails:
            self._fails[provider_id] = 0
            self._until[provider_id] = time.monotonic() + self.cooldown_s

    def clear(self, provider_id: int) -> None:
        self.record(provider_id, True)

    # -- per-route limits ----------------------------------------------------------------------
    def _window(self, route_id: int, t: float) -> tuple[deque[float], deque[tuple[float, int]]]:
        hits, toks = self._hits[route_id], self._tokens[route_id]
        while hits and t - hits[0] >= 60:
            hits.popleft()
        while toks and t - toks[0][0] >= 60:
            toks.popleft()
        return hits, toks

    def tokens_last_minute(self, route_id: int) -> int:
        return sum(n for _, n in self._window(route_id, time.monotonic())[1])

    def available(self, route: dict[str, Any]) -> bool:
        if self.cooldown_remaining(route["provider_id"]) > 0:
            return False
        hits, toks = self._window(route["route_id"], time.monotonic())
        if route.get("rpm_limit") and len(hits) >= route["rpm_limit"]:
            return False
        if route.get("tpm_limit") and sum(n for _, n in toks) >= route["tpm_limit"]:
            return False
        return not (route.get("max_parallel") and self.route_inflight[route["route_id"]] >= route["max_parallel"])

    def begin(self, route: dict[str, Any]) -> None:
        self._hits[route["route_id"]].append(time.monotonic())
        self.route_inflight[route["route_id"]] += 1
        self.provider_inflight[route["provider_id"]] += 1

    def end(self, route: dict[str, Any], tokens: int = 0) -> None:
        rid, pid = route["route_id"], route["provider_id"]
        self.route_inflight[rid] = max(0, self.route_inflight[rid] - 1)
        self.provider_inflight[pid] = max(0, self.provider_inflight[pid] - 1)
        if tokens:
            self._tokens[rid].append((time.monotonic(), tokens))
