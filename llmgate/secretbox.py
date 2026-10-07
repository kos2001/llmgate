"""Encrypt upstream credentials at rest (optional).

With ``LLMGATE_ENCRYPTION_KEY`` set, provider API keys are stored as ``enc:v1:<Fernet token>`` instead of plaintext, so a
leaked database file or backup does not leak the keys of the engines/services behind the gateway. Gateway API keys are
unaffected (they were never stored at all, only their SHA-256).

* Generate a key with ``llmgate genkey``. Several comma-separated keys are accepted: the first encrypts, all decrypt,
  which is how you rotate (put the new key first, run ``llmgate encrypt-secrets``, then drop the old one).
* Without a key nothing changes: values are stored and read as plaintext, and already-plaintext rows keep working
  after you turn encryption on (``llmgate encrypt-secrets`` converts them).
* A key that is wrong or missing for an encrypted row fails the request with a clear 500, never with the ciphertext.
"""

from __future__ import annotations

from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from .errors import GatewayError

PREFIX = "enc:v1:"
_box: MultiFernet | None = None


def generate_key() -> str:
    return Fernet.generate_key().decode()


def configure(keys: str | None) -> None:
    """Set (or clear) the process-wide key ring. Raises ``ValueError`` for a malformed key at startup, not mid-request."""
    global _box
    parts = [k.strip() for k in (keys or "").split(",") if k.strip()]
    try:
        _box = MultiFernet([Fernet(k.encode()) for k in parts]) if parts else None
    except (ValueError, TypeError) as exc:
        raise ValueError("LLMGATE_ENCRYPTION_KEY must be Fernet keys (generate one with `llmgate genkey`)") from exc


def enabled() -> bool:
    return _box is not None


def is_encrypted(value: str | None) -> bool:
    return bool(value) and value.startswith(PREFIX)  # type: ignore[union-attr]


def encrypt(value: str | None) -> str | None:
    if not value or is_encrypted(value) or _box is None:
        return value
    return PREFIX + _box.encrypt(value.encode()).decode()


def decrypt(value: str | None) -> str | None:
    if not is_encrypted(value):
        return value
    if _box is None:
        raise GatewayError(500, "A provider credential is encrypted but LLMGATE_ENCRYPTION_KEY is not set.",
                           type="server_error", code="encryption_key_missing")
    try:
        return _box.decrypt(value[len(PREFIX):].encode()).decode()  # type: ignore[index]
    except InvalidToken:
        raise GatewayError(500, "A provider credential could not be decrypted: LLMGATE_ENCRYPTION_KEY is not the key it was stored with.",
                           type="server_error", code="encryption_key_wrong") from None


def rewrap_provider_keys(db: Any, *, dry_run: bool = False) -> dict[str, int]:
    """Encrypt every plaintext provider key and re-encrypt the rest with the current primary key (rotation)."""
    done = {"encrypted": 0, "rotated": 0, "unchanged": 0}
    for row in db.query("SELECT id, api_key FROM providers WHERE api_key IS NOT NULL AND api_key != ''"):
        plain = decrypt(row["api_key"])
        fresh = encrypt(plain) if _box is not None else plain
        if fresh == row["api_key"] and not is_encrypted(row["api_key"]):
            done["unchanged"] += 1
            continue
        done["rotated" if is_encrypted(row["api_key"]) else "encrypted"] += 1
        if not dry_run:
            db.execute("UPDATE providers SET api_key = ? WHERE id = ?", (fresh, row["id"]))
    return done
