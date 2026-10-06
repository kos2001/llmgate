from __future__ import annotations

import hashlib
import secrets


def generate_api_key(prefix: str) -> tuple[str, str, str]:
    """Return ``(plaintext, display_prefix, sha256_hex)``.

    Keys carry 256 bits of entropy, so a plain SHA-256 digest is enough for lookup
    (no need for a slow password hash). The plaintext is shown to the caller once.
    """
    plaintext = prefix + secrets.token_urlsafe(32)
    return plaintext, plaintext[: len(prefix) + 6], hash_key(plaintext)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
