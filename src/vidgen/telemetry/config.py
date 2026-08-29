from pydantic_settings import BaseSettings, SettingsConfigDict


class TelemetrySettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="VIDGEN_", extra="ignore")
    telemetry_enabled: bool = True
    telemetry_logging_mode: str = "json"
    service_name: str = "vidgen"
    service_version: str = "0.1.0"
    deployment_environment: str = "development"
    otel_exporter_otlp_endpoint: str | None = None
    metric_export_interval_ms: int = 60_000
    #: Application Insights ingestion target, resolved from Key Vault by the
    #: workload's managed identity. It carries an instrumentation key, so it is
    #: a secret: it is never logged and never baked into an image.
    applicationinsights_connection_string: str | None = None
    #: Bounded queue for the batching span processor. A saturated exporter drops
    #: spans rather than growing without limit inside a worker container.
    trace_export_queue_size: int = 2048
    trace_export_batch_delay_ms: int = 5_000
