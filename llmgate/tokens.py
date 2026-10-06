"""Cheap token estimation, used only when an engine returns no ``usage`` block.

Real tokenizers are model-specific and heavy; this keeps accounting roughly right
(~4 chars/token for latin text, ~1 token per CJK character) without extra dependencies.
"""

from __future__ import annotations

from typing import Any


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    ascii_chars = sum(1 for ch in text if ord(ch) < 0x2E80)
    return max(1, (ascii_chars + 3) // 4 + (len(text) - ascii_chars))


def content_text(content: Any) -> str:
    """Flatten OpenAI / Anthropic / Responses-API content (string, or a list of typed parts)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(content_text(p) for p in content)
    if isinstance(content, dict):
        return content_text(content.get("text") or content.get("content") or "")
    return ""


def estimate_prompt_tokens(endpoint: str, payload: dict[str, Any]) -> int:
    if endpoint in ("chat", "messages"):
        total = estimate_tokens(content_text(payload.get("system")))
        for msg in payload.get("messages") or []:
            if isinstance(msg, dict):
                total += 4 + estimate_tokens(content_text(msg.get("content")))
        return total + 3
    if endpoint == "responses":
        value = payload.get("input")
        text = value if isinstance(value, str) else " ".join(
            content_text(i.get("content") if isinstance(i, dict) else i) for i in value or [])
        return estimate_tokens(content_text(payload.get("instructions"))) + estimate_tokens(text)
    if endpoint == "rerank":
        docs = payload.get("documents") or []
        return estimate_tokens(str(payload.get("query", ""))) + sum(estimate_tokens(content_text(d)) for d in docs)
    value = payload.get("prompt") if endpoint == "completions" else payload.get("input")
    if isinstance(value, str):
        return estimate_tokens(value)
    return sum(estimate_tokens(v) for v in value or [] if isinstance(v, str))


def estimate_completion_tokens(endpoint: str, body: dict[str, Any]) -> int:
    if endpoint == "messages":
        return estimate_tokens(content_text(body.get("content")))
    if endpoint == "responses":
        return sum(estimate_tokens(content_text(item.get("content"))) for item in body.get("output") or [] if isinstance(item, dict))
    if endpoint in ("embeddings", "rerank"):
        return 0
    total = 0
    for choice in body.get("choices") or []:
        if endpoint == "chat":
            total += estimate_tokens(content_text((choice.get("message") or {}).get("content")))
        else:
            total += estimate_tokens(choice.get("text") or "")
    return total
