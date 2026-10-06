"""Alerts (LiteLLM's Slack alerting): provider down/recovered and budget-threshold notices to a webhook.

The payload is Slack-compatible (``{"text": ...}``) and also carries a machine-readable ``event``.
Delivery is fire-and-forget through the same bounded queue the usage broadcast uses.
"""

from __future__ import annotations

import logging
from typing import Any

from .broadcast import Broadcaster
from .config import Settings
from .db import Database
from .limits import window_start
from .usage import period_usage

log = logging.getLogger("llmgate.alerts")


class Alerter:
    def __init__(self, settings: Settings):
        self.sink = Broadcaster(settings.alert_webhook_url)
        self.thresholds = sorted({float(t) for t in settings.alert_budget_thresholds.split(",") if t.strip()})
        self._sent: set[tuple[Any, ...]] = set()

    @property
    def enabled(self) -> bool:
        return self.sink.enabled

    def notify(self, event: str, text: str, **data: Any) -> None:
        if self.enabled:
            self.sink.emit({"text": f":rotating_light: {text}", "event": event, **data})

    def provider_changed(self, name: str, old: str, new: str, error: str | None = None) -> None:
        if old == new:
            return
        if new == "unhealthy":
            self.notify("provider_down", f"Provider *{name}* is DOWN" + (f": {error}" if error else ""), provider=name)
        elif new == "healthy" and old == "unhealthy":
            self.notify("provider_up", f"Provider *{name}* recovered", provider=name)

    def check_budgets(self, db: Database, auth: Any) -> None:
        """After a request: warn once per window when a key/service crosses a configured fraction of its budget."""
        if not self.enabled:
            return
        for scope, table, row, column in (("key", "api_keys", auth.key, "api_key_id"), ("service", "services", auth.service, "service_id")):
            limit = row["monthly_budget_usd"]
            if not limit:
                continue
            since = window_start(db, table, row)
            _, spent = period_usage(db, since=since, **{column: row["id"]})
            for t in self.thresholds:
                marker = (scope, row["id"], since, t)
                if spent >= limit * t and marker not in self._sent:
                    self._sent.add(marker)
                    name = row["name"]
                    self.notify("budget_threshold", f"{scope.title()} *{name}* used {spent / limit:.0%} of its budget "
                                f"(${spent:.4f} / ${limit:.2f})", scope=scope, name=name, spent=spent, limit=limit, threshold=t)
