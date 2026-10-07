"""Data lifecycle: retention (pruning), size statistics and consistent online backups of the SQLite database."""

from __future__ import annotations

import asyncio
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from .db import Database

log = logging.getLogger("llmgate.storage")
_BATCH = 5000  # rows per DELETE, so a big prune never holds the write lock for long


def prune_table(db: Database, table: str, days: float) -> int:
    """Delete rows of ``usage_logs`` / ``audit_logs`` older than ``days``. Returns how many were removed."""
    if table not in ("usage_logs", "audit_logs"):  # table names cannot be bound parameters: allow-list them
        raise ValueError(table)
    cutoff, total = time.time() - days * 86400, 0
    while True:
        deleted = db.execute(f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} WHERE ts < ? LIMIT {_BATCH})", (cutoff,)).rowcount
        total += deleted
        if deleted < _BATCH:
            return total


def stats(db: Database, path: str, usage_days: float = 0, audit_days: float = 0) -> dict[str, Any]:
    size = os.path.getsize(path) if path != ":memory:" and os.path.exists(path) else 0
    wal = os.path.getsize(path + "-wal") if path != ":memory:" and os.path.exists(path + "-wal") else 0
    return {
        "database": path, "size_bytes": size + wal,
        "rows": {t: db.scalar(f"SELECT COUNT(*) FROM {t}", default=0) for t in ("usage_logs", "audit_logs", "api_keys", "services", "models", "providers", "users")},
        "oldest_usage_ts": db.scalar("SELECT MIN(ts) FROM usage_logs"), "newest_usage_ts": db.scalar("SELECT MAX(ts) FROM usage_logs"),
        "retention": {"usage_days": usage_days or None, "audit_days": audit_days or None},
    }


def backup(db_path: str, out_path: str) -> int:
    """Consistent copy of a live database (SQLite's online backup API; safe while the gateway writes, WAL included)."""
    out = Path(out_path)
    if out.exists():
        raise FileExistsError(f"{out} already exists; refusing to overwrite a backup")
    out.parent.mkdir(parents=True, exist_ok=True)
    src, dst = sqlite3.connect(db_path), sqlite3.connect(str(out))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    try:
        out.chmod(0o600)  # contains key hashes and (possibly encrypted) provider credentials
    except OSError:
        pass
    return out.stat().st_size


class Janitor:
    """Background pruning according to ``LLMGATE_USAGE_RETENTION_DAYS`` / ``LLMGATE_AUDIT_RETENTION_DAYS`` (0 = keep forever)."""

    def __init__(self, db: Database, usage_days: float, audit_days: float, interval_s: float = 6 * 3600):
        self.db, self.usage_days, self.audit_days, self.interval_s = db, usage_days, audit_days, interval_s
        self._task: asyncio.Task[None] | None = None

    def prune_once(self) -> dict[str, int]:
        out = {"usage_logs": 0, "audit_logs": 0}
        if self.usage_days > 0:
            out["usage_logs"] = prune_table(self.db, "usage_logs", self.usage_days)
        if self.audit_days > 0:
            out["audit_logs"] = prune_table(self.db, "audit_logs", self.audit_days)
        if any(out.values()):
            log.info("retention pruned %s", out)
        return out

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.prune_once)
            except Exception:  # never let housekeeping take the gateway down
                log.exception("retention pruning failed")
            await asyncio.sleep(self.interval_s)

    def start(self) -> None:
        if self.usage_days > 0 or self.audit_days > 0:
            self._task = asyncio.create_task(self._loop(), name="llmgate-janitor")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
