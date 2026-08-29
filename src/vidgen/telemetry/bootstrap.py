"""One entry point that connects the T23 telemetry stack to its export target.

T23 already defines the shape of what is emitted: redacted structured JSON logs,
bounded metric labels, W3C trace context, and a provider-attempt span for every
paid call. T24 only chooses where that goes.

* **Logs** are written to stdout as redacted JSON. Container Apps ships stdout
  to the Log Analytics workspace, so nothing is exported twice and no log
  shipper needs a credential.
* **Traces** are exported to Application Insights when a connection string is
  configured, otherwise to a plain OTLP endpoint when one is, otherwise nowhere.
  Export is batched, so a slow ingestion endpoint never blocks an activity.

The redaction, the bounded label sets and the "never send prompts, transcripts,
caption text, credentials, signed URLs or media" rule all continue to be
enforced by T23 itself; this module adds no new attributes.
"""

from __future__ import annotations

import logging

from opentelemetry.trace import Tracer

from vidgen.telemetry.config import TelemetrySettings
from vidgen.telemetry.logging import configure_logging
from vidgen.telemetry.tracing import initialize_tracing

_LOGGER = logging.getLogger(__name__)


def build_span_processor(settings: TelemetrySettings) -> object | None:
    """Return a batching span processor for the configured export target."""
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    if settings.applicationinsights_connection_string:
        try:
            from azure.monitor.opentelemetry.exporter import AzureMonitorTraceExporter
        except ImportError:  # pragma: no cover - the azure extra is not installed
            _LOGGER.warning(
                "applicationinsights connection string configured but the 'azure' "
                "extra is not installed; traces will not be exported"
            )
            return None
        exporter: object = AzureMonitorTraceExporter(
            connection_string=settings.applicationinsights_connection_string
        )
    elif settings.otel_exporter_otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # type: ignore[import-not-found]
                OTLPSpanExporter,
            )
        except ImportError:  # pragma: no cover - the OTLP exporter is optional
            _LOGGER.warning(
                "an OTLP endpoint is configured but opentelemetry-exporter-otlp is not "
                "installed; traces will not be exported"
            )
            return None
        exporter = OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint)
    else:
        return None
    return BatchSpanProcessor(
        exporter,  # type: ignore[arg-type]
        max_queue_size=settings.trace_export_queue_size,
        schedule_delay_millis=settings.trace_export_batch_delay_ms,
    )


def initialize_telemetry(
    *,
    service_name: str | None = None,
    settings: TelemetrySettings | None = None,
) -> Tracer:
    """Configure logging and tracing for a deployed process.

    Safe to call more than once: :func:`initialize_tracing` installs at most one
    provider and :func:`configure_logging` reuses its named handler.
    """
    resolved = settings or TelemetrySettings()
    if service_name:
        resolved = resolved.model_copy(update={"service_name": service_name})
    configure_logging(json_mode=resolved.telemetry_logging_mode == "json")
    processor = build_span_processor(resolved)
    return initialize_tracing(resolved, span_processor=processor)  # type: ignore[arg-type]
