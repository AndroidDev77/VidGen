from __future__ import annotations

from threading import Lock

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter

from vidgen.telemetry.config import TelemetrySettings

_lock = Lock()
_provider: TracerProvider | None = None


def initialize_tracing(
    settings: TelemetrySettings,
    exporter: SpanExporter | None = None,
    *,
    span_processor: SpanProcessor | None = None,
) -> trace.Tracer:
    """Install the process tracer provider.

    ``exporter`` keeps the existing synchronous ``SimpleSpanProcessor`` used by
    the tests. ``span_processor`` takes precedence and lets a deployment supply
    a batching processor, so exporting a span never blocks an activity.
    """
    global _provider
    if not settings.telemetry_enabled:
        return trace.NoOpTracerProvider().get_tracer(settings.service_name)
    with _lock:
        if _provider is None:
            _provider = TracerProvider(
                resource=Resource.create(
                    {
                        "service.name": settings.service_name,
                        "service.version": settings.service_version,
                        "deployment.environment": settings.deployment_environment,
                    }
                )
            )
            if span_processor is not None:
                _provider.add_span_processor(span_processor)
            elif exporter is not None:
                _provider.add_span_processor(SimpleSpanProcessor(exporter))
    return _provider.get_tracer(settings.service_name)


def shutdown_tracing(timeout_millis: int = 2000) -> None:
    if _provider is not None:
        _provider.shutdown()
