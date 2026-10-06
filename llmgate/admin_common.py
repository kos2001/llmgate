from __future__ import annotations

import json
import sqlite3
from typing import Any

from fastapi import Depends, Request

from .rbac import admin_guard
from .db import Database, now
from .errors import GatewayError


ADMIN_DEPS = [Depends(admin_guard)]


def get_db(request: Request) -> Database:
    return request.app.state.db


def flags(row: dict[str, Any], *names: str) -> dict[str, Any]:
    """Convert 0/1 columns to real booleans for JSON output."""
    for n in names:
        if n in row:
            row[n] = bool(row[n])
    return row


def not_found(what: str) -> GatewayError:
    return GatewayError(404, f"{what} not found.", type="not_found_error", code="not_found")


def require_row(db: Database, table: str, row_id: int, what: str) -> dict[str, Any]:
    row = db.one(f"SELECT * FROM {table} WHERE id = ?", (row_id,))  # table is a literal at every call site
    if row is None:
        raise not_found(what)
    return row


def insert_row(db: Database, table: str, values: dict[str, Any]) -> int:
    cols = list(values)
    try:
        return db.insert(
            f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
            [values[c] for c in cols],
        )
    except sqlite3.IntegrityError as exc:
        raise GatewayError(409, f"Conflict: {exc}", code="conflict") from None


def update_row(db: Database, table: str, row_id: int, changes: dict[str, Any]) -> None:
    if not changes:
        return
    sets = ", ".join(f"{c} = ?" for c in changes)
    try:
        db.execute(f"UPDATE {table} SET {sets} WHERE id = ?", [*changes.values(), row_id])
    except sqlite3.IntegrityError as exc:
        raise GatewayError(409, f"Conflict: {exc}", code="conflict") from None


def bool_cols(changes: dict[str, Any], *names: str) -> dict[str, Any]:
    for n in names:
        if n in changes and changes[n] is not None:
            changes[n] = int(changes[n])
    return changes


def encode_json(data: dict[str, Any], *cols: str) -> dict[str, Any]:
    """Lists are stored as JSON text; an empty list or None is stored as NULL (= unrestricted / unknown)."""
    for col in cols:
        if col in data:
            data[col] = json.dumps(data[col]) if data[col] else None
    return data


def decode_json(row: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    for col, default in defaults.items():
        row[col] = json.loads(row[col]) if row.get(col) else default
    return row


_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(value: Any) -> int | None:
    """``"30d"`` / ``"12h"`` / ``3600`` → seconds. ``None`` or ``""`` clears the window (calendar month)."""
    if value in (None, ""):
        return None
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    if isinstance(value, str) and len(value) > 1 and value[:-1].isdigit() and value[-1] in _UNITS and int(value[:-1]) > 0:
        return int(value[:-1]) * _UNITS[value[-1]]
    raise GatewayError(400, f"Invalid duration {value!r}; use e.g. '30m', '12h', '7d' or a number of seconds.", code="invalid_duration")


def prepare_limits(data: dict[str, Any], *json_cols: str) -> dict[str, Any]:
    """Map API fields to columns: ``budget_duration`` → ``budget_duration_s`` (+ fresh window), JSON-encode ``json_cols``."""
    if "budget_duration" in data:
        seconds = parse_duration(data.pop("budget_duration"))
        data["budget_duration_s"] = seconds
        data["budget_anchor"] = now() if seconds else None
    return encode_json(data, *json_cols)
