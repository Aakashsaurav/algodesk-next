"""Secret and token scrubber for logs and journal events.

INV-9: Never leak secrets, tokens, credentials, or sensitive keys into logs or journal.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping, Sequence
from typing import Any

MASK = "***"

_SECRET_KEYWORDS = (
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "private_key",
    "credential",
)

_BEARER_PATTERN = re.compile(r"(?i)\b(Bearer\s+)(?:\"[^\"]*\"|'[^']*'|\S+)")

_KEY_VALUE_PATTERN = re.compile(
    r"(?i)\b([a-zA-Z0-9_\-\.]*(?:token|secret|password|passwd|api_key|apikey|private_key|credential)[a-zA-Z0-9_\-\.]*\s*[=:]\s*)(?:\"([^\"]*)\"|'([^']*)'|([^\s,;&]+))"
)

_BASE64_HEX_32_PATTERN = re.compile(
    r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/=_-]{32,}(?![A-Za-z0-9+/=_-])"
)


_HEX_CHARS = frozenset("0123456789abcdefABCDEF")


def _replace_kv(m: re.Match[str]) -> str:
    prefix = m.group(1)
    if m.group(2) is not None:
        return f'{prefix}"{MASK}"'
    if m.group(3) is not None:
        return f"{prefix}'{MASK}'"
    return f"{prefix}{MASK}"


def _replace_base64_or_hex(m: re.Match[str]) -> str:
    s = m.group(0)
    if len(s) in (40, 64) and all(c in _HEX_CHARS for c in s):
        return s
    return MASK


class Scrubber:
    """Thread-safe scrubber that masks secrets, credentials and token patterns."""

    def __init__(self, extra_patterns: Sequence[str | re.Pattern[str]] | None = None) -> None:
        self._lock = threading.Lock()
        self._registered: set[str] = set()
        self._registered_regex: re.Pattern[str] | None = None
        self._extra_patterns: list[re.Pattern[str]] = []
        if extra_patterns:
            for pat in extra_patterns:
                if isinstance(pat, str):
                    self._extra_patterns.append(re.compile(pat))
                else:
                    self._extra_patterns.append(pat)

    def register(self, value: str) -> None:
        """Register a secret value to scrub. Ignored if shorter than 6 characters."""
        if not isinstance(value, str) or len(value) < 6:
            return
        with self._lock:
            self._registered.add(value)
            escaped = [re.escape(s) for s in sorted(self._registered, key=len, reverse=True)]
            self._registered_regex = re.compile("|".join(escaped))

    def _is_secret_key(self, key: Any) -> bool:
        if not isinstance(key, str):
            return False
        k_lower = key.lower()
        return any(kw in k_lower for kw in _SECRET_KEYWORDS)

    def scrub(self, text: str) -> str:
        """Mask secrets, tokens, registered values and credentials in text."""
        if not isinstance(text, str):
            text = str(text)

        with self._lock:
            reg_regex = self._registered_regex

        if reg_regex is not None:
            text = reg_regex.sub(MASK, text)

        text = _BEARER_PATTERN.sub(rf"\g<1>{MASK}", text)
        text = _KEY_VALUE_PATTERN.sub(_replace_kv, text)
        text = _BASE64_HEX_32_PATTERN.sub(_replace_base64_or_hex, text)

        for pat in self._extra_patterns:
            text = pat.sub(MASK, text)

        return text

    def scrub_obj(self, obj: Any) -> Any:
        """Recursively scrub strings, dicts, lists and tuples without mutating inputs."""
        if isinstance(obj, str):
            return self.scrub(obj)
        if isinstance(obj, Mapping):
            out_dict: dict[Any, Any] = {}
            for k, v in obj.items():
                if self._is_secret_key(k):
                    if isinstance(v, (Mapping, Sequence)) and not isinstance(v, (str, bytes)):
                        out_dict[k] = self.scrub_obj(v)
                    else:
                        out_dict[k] = MASK
                else:
                    out_dict[k] = self.scrub_obj(v)
            return out_dict
        if isinstance(obj, list):
            return [self.scrub_obj(item) for item in obj]
        if isinstance(obj, tuple):
            return tuple(self.scrub_obj(item) for item in obj)
        return obj
