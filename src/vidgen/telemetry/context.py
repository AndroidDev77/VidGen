from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

_context: ContextVar[dict[str, Any] | None] = ContextVar("vidgen_telemetry_context", default=None)


def current_context() -> dict[str, Any]:
    return dict(_context.get() or {})


@contextmanager
def telemetry_context(**fields: Any) -> Iterator[None]:
    token = _context.set(
        {**(_context.get() or {}), **{k: v for k, v in fields.items() if v is not None}}
    )
    try:
        yield
    finally:
        _context.reset(token)
