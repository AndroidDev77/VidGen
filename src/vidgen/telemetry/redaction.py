"""Deterministic recursive removal of sensitive telemetry values."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACTED = "[REDACTED]"
SENSITIVE = (
    "authorization",
    "cookie",
    "token",
    "secret",
    "password",
    "api_key",
    "apikey",
    "prompt",
    "transcript",
    "request_body",
    "response_body",
    "credential",
)
SIGNED_QUERY = ("sig", "signature", "se", "sp", "sv", "sas", "token", "key")


def _sensitive(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(part in normalized for part in SENSITIVE)


def redact_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if not parsed.scheme or not parsed.netloc:
            return value
        host = parsed.hostname or ""
        netloc = host + (f":{parsed.port}" if parsed.port else "")
        query = urlencode(
            [
                (k, REDACTED if k.lower() in SIGNED_QUERY else v)
                for k, v in parse_qsl(parsed.query, keep_blank_values=True)
            ]
        )
        return urlunsplit((parsed.scheme, netloc, parsed.path, query, ""))
    except ValueError:
        return REDACTED


def redact(value: Any, *, key: str = "") -> Any:
    if _sensitive(key):
        return REDACTED
    if isinstance(value, BaseException):
        return {"type": type(value).__name__, "message": REDACTED}
    if isinstance(value, Mapping):
        return {
            str(k): redact(v, key=str(k))
            for k, v in sorted(value.items(), key=lambda item: str(item[0]))
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact(item) for item in value]
    if isinstance(value, bytes):
        return REDACTED
    if isinstance(value, str) and "://" in value:
        return redact_url(value)
    return value
