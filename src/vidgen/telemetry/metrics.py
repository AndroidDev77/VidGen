from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


class Metrics:
    def __init__(self, registry: CollectorRegistry | None = None):
        r = registry or CollectorRegistry()
        self.registry = r
        self.projects_started = Counter(
            "recap_projects_started", "Projects started", ("profile",), registry=r
        )
        self.projects_completed = Counter(
            "recap_projects_completed", "Projects completed", ("status",), registry=r
        )
        self.stage_duration = Histogram(
            "recap_project_stage_duration_seconds", "Stage duration", ("stage",), registry=r
        )
        self.stage_failures = Counter(
            "recap_stage_failures", "Stage failures", ("stage", "error_class"), registry=r
        )
        self.queue_backlog = Gauge(
            "recap_queue_backlog", "Queue backlog", ("task_queue",), registry=r
        )
        self.provider_requests = Counter(
            "recap_provider_requests",
            "Provider requests",
            ("provider", "model", "operation", "status"),
            registry=r,
        )
        self.provider_latency = Histogram(
            "recap_provider_latency_seconds",
            "Provider latency",
            ("provider", "model", "operation"),
            registry=r,
        )
        self.provider_active = Gauge(
            "recap_provider_active_generations",
            "Active generations",
            ("provider", "model"),
            registry=r,
        )
        self.rate_limits = Counter(
            "recap_provider_rate_limit", "Rate limits", ("provider", "model"), registry=r
        )
        self.cost = Counter(
            "recap_generation_cost_usd",
            "Generation cost",
            ("provider", "model", "operation"),
            registry=r,
        )
        self.tokens = Counter(
            "recap_llm_tokens", "LLM tokens", ("model", "direction", "agent"), registry=r
        )
        self.shot_qa = Histogram("recap_shot_qa_score", "Shot QA", ("rubric_version",), registry=r)
        self.shot_retry = Counter("recap_shot_retry", "Shot retries", ("failure_code",), registry=r)
        self.render_factor = Histogram("recap_render_realtime_factor", "Render factor", registry=r)
        self.av_drift = Histogram("recap_av_drift_ms", "A/V drift", registry=r)
        self.human_wait = Histogram("recap_human_wait_seconds", "Human wait", ("gate",), registry=r)


METRIC_LABELS = {
    "provider": frozenset({"provider", "model", "operation", "status"}),
    "failure": frozenset({"stage", "error_class"}),
}
