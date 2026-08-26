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
