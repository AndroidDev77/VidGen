from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from vidgen.telemetry.context import current_context
from vidgen.telemetry.redaction import redact


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
            **current_context(),
        }
        for key in (
            "service",
            "serviceVersion",
            "environment",
            "projectId",
            "attemptId",
            "provider",
            "model",
            "operation",
            "durationMs",
            "status",
            "costUsd",
            "errorCode",
            "failureClass",
        ):
            if hasattr(record, key):
                data[key] = getattr(record, key)
        return json.dumps(redact(data), sort_keys=True, separators=(",", ":"), default=str)


def configure_logging(*, json_mode: bool = True, level: int = logging.INFO) -> None:
    root = logging.getLogger()
    handler = next((h for h in root.handlers if h.name == "vidgen-telemetry"), None)
    if handler is None:
        handler = logging.StreamHandler()
        handler.name = "vidgen-telemetry"
        root.addHandler(handler)
    handler.setFormatter(
        JsonFormatter() if json_mode else logging.Formatter("%(levelname)s %(message)s")
    )
    root.setLevel(level)
