"""Brute-force protection: too many *rejected credentials* from one client address → a temporary 429.

Only attempts that present a credential and are refused count (a wrong API key / admin token / JWT). A request with no
credential, or a valid one that lacks permission, does not. Success does not clear the record (otherwise a valid low-privilege login between guesses would reset it); the sliding window does. It is held in process memory, so a restart
or a second instance starts fresh; put a reverse proxy / WAF in front for anything stronger. Behind a proxy every client
shares the proxy's address, so raise ``LLMGATE_AUTH_FAIL_LIMIT`` or set it to 0 and let the proxy do this job.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict, deque

from .errors import GatewayError


class AuthThrottle:
    def __init__(self, limit: int, window_s: float):
        self.limit, self.window_s = limit, window_s
        self._fails: dict[str, deque[float]] = defaultdict(deque)

    def _trim(self, ip: str, now: float) -> deque[float]:
        q = self._fails[ip]
        while q and now - q[0] >= self.window_s:
            q.popleft()
        if not q:
            self._fails.pop(ip, None)
            return deque()
        return q

    def check(self, ip: str) -> None:
        """Raise 429 if this address has already used up its failures in the current window."""
        if self.limit <= 0:
            return
        now = time.monotonic()
        q = self._trim(ip, now)
        if len(q) >= self.limit:
            retry = max(1, math.ceil(self.window_s - (now - q[0])))
            raise GatewayError(429, f"Too many rejected credentials from this address. Try again in {retry}s.",
                               type="rate_limit_error", code="too_many_failed_attempts", headers={"Retry-After": str(retry)})

    def fail(self, ip: str) -> None:
        if self.limit > 0:
            self._fails[ip].append(time.monotonic())
