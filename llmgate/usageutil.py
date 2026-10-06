"""Read token usage out of the different response dialects (OpenAI, Responses API, Anthropic, rerank)."""

from __future__ import annotations

from typing import Any


def norm_usage(u: Any) -> tuple[int, int]:
    """``(prompt, completion)`` from ``prompt_tokens/completion_tokens`` or ``input_tokens/output_tokens``."""
    if not isinstance(u, dict):
        return 0, 0
    prompt = u.get("prompt_tokens", u.get("input_tokens")) or 0
    completion = u.get("completion_tokens", u.get("output_tokens")) or 0
    if not prompt and not completion:
        prompt = u.get("total_tokens") or 0  # rerank / embeddings engines that only report a total
    return int(prompt), int(completion)


def find_usage(obj: Any) -> dict[str, Any] | None:
    """The usage dict of a body or a stream event, including ``response.usage`` and ``message.usage`` nesting."""
    if not isinstance(obj, dict):
        return None
    for holder in (obj, obj.get("response"), obj.get("message")):
        if isinstance(holder, dict) and isinstance(holder.get("usage"), dict):
            return holder["usage"]
    return None


def event_text(obj: dict[str, Any], endpoint: str) -> str:
    """The generated text carried by one stream event (any dialect), for estimation when usage is absent."""
    parts: list[str] = []
    for choice in obj.get("choices") or []:
        piece = (choice.get("delta") or {}).get("content") if endpoint == "chat" else choice.get("text")
        if isinstance(piece, str):
            parts.append(piece)
    delta = obj.get("delta")
    if isinstance(delta, str):  # Responses API: response.output_text.delta
        parts.append(delta)
    elif isinstance(delta, dict) and isinstance(delta.get("text"), str):  # Anthropic: content_block_delta
        parts.append(delta["text"])
    return "".join(parts)


class StreamUsage:
    """Accumulates usage across events. Anthropic splits input/output over two events, so keep the max of each."""

    def __init__(self) -> None:
        self.prompt = self.completion = 0
        self.seen = False

    def add(self, usage: dict[str, Any] | None) -> None:
        p, c = norm_usage(usage)
        if p or c:
            self.seen = True
            self.prompt, self.completion = max(self.prompt, p), max(self.completion, c)
