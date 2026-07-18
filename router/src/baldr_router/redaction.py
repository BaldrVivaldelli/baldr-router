from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from typing import Any, Iterable

REDACTED = "<redacted>"
_SECRET_ENV_NAMES = (
    "CONTEXT7_API_KEY",
    "KIRO_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "XAI_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
)
_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/=]{8,}"),
    re.compile(r"(?i)\bBasic\s+[A-Za-z0-9+/=]{8,}"),
    re.compile(r"\bctx7sk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(
        r"(?i)(api[_-]?key|authorization|token|password|secret)"
        r"([\"']?\s*(?:[=:]|\bis\b|\bwas\b)\s*[\"']?)"
        r"([^\"'\s,;\]\}]{6,})"
    ),
)
_SENSITIVE_KEYS = re.compile(
    r"(?i)(api[_-]?key|authorization|token|password|secret|credential)"
)
_SAFE_SENSITIVE_KEYS = re.compile(
    r"(?i)(^|_)(input_tokens|output_tokens|cached_input_tokens|"
    r"reasoning_output_tokens|token_count|tokens_used|write_authorization)$"
)


def known_secret_values(extra: Iterable[str] | None = None) -> tuple[str, ...]:
    values = {os.environ.get(name, "").strip() for name in _SECRET_ENV_NAMES}
    if extra:
        values.update(str(value).strip() for value in extra)
    return tuple(
        sorted((value for value in values if len(value) >= 6), key=len, reverse=True)
    )


def redact_text(value: str, *, secrets: Iterable[str] | None = None) -> str:
    text = str(value)
    for secret in known_secret_values(secrets):
        text = text.replace(secret, REDACTED)
    for pattern in _PATTERNS:
        if pattern.groups >= 3:
            text = pattern.sub(
                lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", text
            )
        else:
            text = pattern.sub(REDACTED, text)
    return text


def redact_value(value: Any, *, secrets: Iterable[str] | None = None) -> Any:
    if isinstance(value, str):
        return redact_text(value, secrets=secrets)
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _SENSITIVE_KEYS.search(key_text) and item not in (None, False, ""):
                # Status booleans such as api_key_available are safe and useful.
                if (
                    key_text.lower().endswith(("_available", "_configured", "_source"))
                    or _SAFE_SENSITIVE_KEYS.search(key_text)
                    or (isinstance(item, (int, float)) and "token" in key_text.lower())
                ):
                    out[key_text] = redact_value(item, secrets=secrets)
                else:
                    out[key_text] = REDACTED
            else:
                out[key_text] = redact_value(item, secrets=secrets)
        return out
    if isinstance(value, tuple):
        return tuple(redact_value(item, secrets=secrets) for item in value)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [redact_value(item, secrets=secrets) for item in value]
    return value
