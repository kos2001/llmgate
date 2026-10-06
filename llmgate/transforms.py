"""Prompt transforms. Currently ``middle-out`` (OpenRouter): shrink an over-long chat from the middle."""

from __future__ import annotations

from typing import Any

from .errors import GatewayError
from .tokens import content_text, estimate_tokens

_SUPPORTED = {"middle-out"}


def requested(payload: dict[str, Any]) -> list[str]:
    transforms = payload.get("transforms") or []
    if not isinstance(transforms, list) or not all(isinstance(t, str) for t in transforms):
        raise GatewayError(400, "'transforms' must be a list of strings.", code="invalid_transforms")
    unknown = [t for t in transforms if t not in _SUPPORTED]
    if unknown:
        raise GatewayError(400, f"Unsupported transforms: {', '.join(unknown)}. Supported: {', '.join(sorted(_SUPPORTED))}.",
                           code="invalid_transforms")
    return transforms


def _cost(msg: dict[str, Any]) -> int:
    return 4 + estimate_tokens(content_text(msg.get("content")))


def middle_out(messages: list[dict[str, Any]], budget: int) -> list[dict[str, Any]]:
    """Drop messages from the middle until the estimate fits ``budget`` tokens.

    System messages and the final message are never dropped. If that is still too long, the
    largest remaining message loses the middle of its text instead.
    """
    msgs = [m for m in messages if isinstance(m, dict)]
    if sum(_cost(m) for m in msgs) <= budget:
        return messages
    droppable = [i for i, m in enumerate(msgs) if m.get("role") != "system" and i != len(msgs) - 1]
    alive = set(range(len(msgs)))
    while droppable and sum(_cost(msgs[i]) for i in alive) > budget:
        alive.discard(droppable.pop(len(droppable) // 2))  # always the current middle
    out = [m for i, m in enumerate(msgs) if i in alive]
    if sum(_cost(m) for m in out) > budget:
        big = max(range(len(out)), key=lambda i: _cost(out[i]))
        text = content_text(out[big].get("content"))
        excess_chars = (sum(_cost(m) for m in out) - budget) * 4
        cut = max(0, len(text) - excess_chars - 8)
        if cut < len(text):
            out[big] = {**out[big], "content": text[: cut // 2] + " … " + text[len(text) - cut // 2:]}
    return out
