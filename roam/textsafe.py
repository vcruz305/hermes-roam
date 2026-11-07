"""Render remote-controlled text safely for users and the model."""

import re

_CONTROL = re.compile(r"[\x00-\x1f\x7f\u2028\u2029\u202a-\u202e\u2066-\u2069]+")
# 0.4.1: block sanitizer that keeps line structure (newlines and tabs) for
# remote-sourced transcripts a user explicitly asked to print.
_BLOCK_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u2028-\u2029\u202a-\u202e\u2066-\u2069]+")
MAX_UNTRUSTED = 160


def untrusted(text: object, limit: int = MAX_UNTRUSTED) -> str:
    """One line, control characters collapsed, length-capped, quoted as remote text."""
    flat = _CONTROL.sub(" ", str(text)).strip()
    if len(flat) > limit:
        flat = flat[: limit - 1] + "…"
    return f'remote said: "{flat}"'


def clean_line(text: object, limit: int = MAX_UNTRUSTED) -> str:
    """One sanitized line of remote-sourced text: every control character
    (newline included) collapsed, length-capped, never quoted or prefixed —
    for bounded fields like archive titles a listing shows."""
    flat = _CONTROL.sub(" ", str(text)).strip()
    if len(flat) > limit:
        flat = flat[: limit - 1] + "…"
    return flat


def clean_block(text: object, limit: int = 2_000_000) -> str:
    """Sanitized remote-sourced block text: control characters other than
    newline and tab are collapsed and the total length is capped, but line
    structure is preserved — for a transcript the user explicitly asked to
    print on their own terminal."""
    flat = _BLOCK_CONTROL.sub(" ", str(text))
    if len(flat) > limit:
        flat = flat[:limit] + "\n… (truncated)"
    return flat
