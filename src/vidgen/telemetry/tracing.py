from __future__ import annotations

from threading import Lock

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter

from vidgen.telemetry.config import TelemetrySettings

_lock = Lock()
_provider: TracerProvider | None = None


def initialize_tracing(
    settings: TelemetrySettings, exporter: SpanExporter | None = None
) -> trace.Tracer:
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
            if exporter is not None:
                _provider.add_span_processor(SimpleSpanProcessor(exporter))
    return _provider.get_tracer(settings.service_name)


def shutdown_tracing(timeout_millis: int = 2000) -> None:
    if _provider is not None:
        _provider.shutdown()
