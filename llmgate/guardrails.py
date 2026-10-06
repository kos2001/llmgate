"""Content guardrails (LiteLLM's guardrails): block or mask PII, secrets, banned words and prompt injection.

Input rules run on every endpoint. Output rules run on complete (non-streaming) responses; a streamed
reply cannot be inspected before it reaches the client, so output rules do not apply to streams.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .db import Database
from .errors import GatewayError

KINDS = ("pii", "secrets", "keywords", "regex", "injection", "max_chars")


def _luhn(digits: str) -> bool:
    nums = [int(d) for d in digits][::-1]
    return sum(n if i % 2 == 0 else (n * 2 - 9 if n * 2 > 9 else n * 2) for i, n in enumerate(nums)) % 10 == 0


PII: dict[str, tuple[re.Pattern[str], Callable[[str], bool] | None]] = {
    "EMAIL": (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), None),
    "CREDIT_CARD": (re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)"), lambda m: 13 <= len(re.sub(r"\D", "", m)) <= 19 and _luhn(re.sub(r"\D", "", m))),
    "KR_RRN": (re.compile(r"(?<!\d)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])-?[1-4]\d{6}(?!\d)"), None),
    "US_SSN": (re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)"), None),
    "PHONE": (re.compile(r"(?<!\d)(?:\+\d{1,3}[ -]?)?(?:0\d{1,2}|\(\d{2,3}\))[ -]?\d{3,4}[ -]?\d{4}(?!\d)"), None),
}
SECRETS: dict[str, re.Pattern[str]] = {
    "API_KEY": re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    "AWS_ACCESS_KEY": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "GITHUB_TOKEN": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}"),
    "SLACK_TOKEN": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    "PRIVATE_KEY": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "JWT": re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
}
INJECTION = re.compile(
    r"ignore (?:all |any )?(?:the )?(?:previous|prior|above|earlier) (?:instructions|prompts|rules)|"
    r"disregard (?:the |all )?(?:system|previous|above)|reveal (?:your |the )?(?:system prompt|hidden instructions)|"
    r"you are now (?:dan|in developer mode)|이전\s*(?:지시|명령|지침)(?:사항)?(?:을|를)?\s*(?:모두\s*)?무시|시스템\s*프롬프트(?:를|을)?\s*(?:보여|알려|출력|공개)",
    re.I)


@dataclass
class Rule:
    id: int
    name: str
    kind: str
    mode: str
    apply_to: str
    default_on: bool
    config: dict[str, Any] = field(default_factory=dict)


def load_rules(db: Database, requested: list[str] | None = None) -> list[Rule]:
    """Enabled rules that are on by default, plus any the request names explicitly."""
    rules = []
    for r in db.query("SELECT * FROM guardrails WHERE enabled = 1 ORDER BY id"):
        if r["default_on"] or (requested and r["name"] in requested):
            rules.append(Rule(r["id"], r["name"], r["kind"], r["mode"], r["apply_to"], bool(r["default_on"]),
                              json.loads(r["config"]) if r["config"] else {}))
    return rules


def _scan(rule: Rule, text: str) -> tuple[str, list[str]]:
    """Return ``(masked_text, labels_found)`` for one text field."""
    cfg, found = rule.config, []
    out = text
    if rule.kind == "pii":
        wanted = set(cfg.get("entities") or PII)
        for label, (pattern, check) in PII.items():
            if label in wanted:
                def sub(m: re.Match[str], label: str = label, check: Any = check) -> str:
                    if check and not check(m.group(0)):
                        return m.group(0)
                    found.append(label)
                    return f"[{label}]"
                out = pattern.sub(sub, out)
    elif rule.kind == "secrets":
        wanted = set(cfg.get("entities") or SECRETS)
        for label, pattern in SECRETS.items():
            if label in wanted and pattern.search(out):
                found.append(label)
                out = pattern.sub(f"[{label}]", out)
    elif rule.kind == "keywords":
        flags = 0 if cfg.get("case_sensitive") else re.I
        for word in cfg.get("words") or []:
            pattern = re.compile(re.escape(word), flags)
            if pattern.search(out):
                found.append(word)
                out = pattern.sub("***", out)
    elif rule.kind == "regex":
        pattern = re.compile(cfg.get("pattern", "(?!x)x"), re.I if cfg.get("ignore_case", True) else 0)
        if pattern.search(out):
            found.append(cfg.get("label", "REGEX"))
            out = pattern.sub(f"[{cfg.get('label', 'REDACTED')}]", out)
    elif rule.kind == "injection":
        if INJECTION.search(out):
            found.append("PROMPT_INJECTION")
            out = INJECTION.sub("[REDACTED]", out)
    elif rule.kind == "max_chars" and len(out) > int(cfg.get("limit", 100_000)):
        found.append("TOO_LONG")
    return out, found


def _map_strings(obj: Any, fn: Callable[[str], str]) -> Any:
    if isinstance(obj, str):
        return fn(obj)
    if isinstance(obj, list):
        return [_map_strings(v, fn) for v in obj]
    if isinstance(obj, dict):  # only text-bearing keys: never touch ids, urls, roles or types
        return {k: (_map_strings(v, fn) if k in _TEXT_KEYS else v) for k, v in obj.items()}
    return obj


_TEXT_KEYS = {"content", "text", "input", "prompt", "query", "documents", "system", "instructions", "message", "messages",
              "choices", "delta", "output", "document"}


def _apply(rules: list[Rule], payload: dict[str, Any], where: str, keys: tuple[str, ...]) -> tuple[dict[str, Any], list[str]]:
    hits: list[str] = []
    out = copy.deepcopy(payload)
    for rule in rules:
        if rule.apply_to not in (where, "both"):
            continue
        labels: list[str] = []

        def fn(text: str, rule: Rule = rule, labels: list[str] = labels) -> str:
            new, found = _scan(rule, text)
            labels.extend(found)
            return new if rule.mode == "mask" and rule.kind != "max_chars" else text

        for key in keys:
            if key in out:
                out[key] = _map_strings(out[key], fn)
        if labels:
            hits.append(rule.name)
            if rule.mode == "block":
                exc = GatewayError(400, f"Blocked by guardrail '{rule.name}' ({', '.join(sorted(set(labels)))}).",
                                   code="guardrail_blocked")
                exc.guardrail = rule.name  # type: ignore[attr-defined]  # so the usage log can name the rule
                raise exc
    return out, hits


_INPUT_KEYS = ("messages", "system", "prompt", "input", "instructions", "query", "documents")
_OUTPUT_KEYS = ("choices", "content", "output")


def check_input(rules: list[Rule], payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    return _apply(rules, payload, "input", _INPUT_KEYS) if rules else (payload, [])


def check_output(rules: list[Rule], body: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    return _apply(rules, body, "output", _OUTPUT_KEYS) if rules else (body, [])
