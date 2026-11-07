"""Render remote-controlled text safely for users and the model."""

import re

_CONTROL = re.compile(r"[\x00-\x1f\x7f\u2028\u2029\u202a-\u202e\u2066-\u2069]+")
MAX_UNTRUSTED = 160


def untrusted(text: object, limit: int = MAX_UNTRUSTED) -> str:
    """One line, control characters collapsed, length-capped, quoted as remote text."""
    flat = _CONTROL.sub(" ", str(text)).strip()
    if len(flat) > limit:
        flat = flat[: limit - 1] + "…"
    return f'remote said: "{flat}"'
