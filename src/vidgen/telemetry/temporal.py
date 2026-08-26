from opentelemetry import propagate


def inject_trace_headers() -> dict[str, str]:
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return {k: v for k, v in carrier.items() if k in {"traceparent", "tracestate"}}


def extract_trace_headers(headers: dict[str, str]) -> object:
    return propagate.extract(
        {k: v for k, v in headers.items() if k in {"traceparent", "tracestate"}}
    )
