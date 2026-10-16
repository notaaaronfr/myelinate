"""Prometheus metrics, structured JSON logging and optional OpenTelemetry tracing."""

from __future__ import annotations

import logging

import structlog
from prometheus_client import Counter, Histogram


class _Metrics:
    def __init__(self) -> None:
        self.recalls = Counter("myelinate_recalls_total", "Recall requests")
        self.recall_skills = Histogram("myelinate_recall_skills", "Skills injected per recall", buckets=(0, 1, 2, 3, 5, 8))
        self.episodes = Counter("myelinate_episodes_total", "Recorded episodes", ["outcome", "framework"])
        self.evolutions = Counter("myelinate_evolutions_total", "Skill evolution runs", ["result"])
        self.jobs_enqueued = Counter("myelinate_jobs_enqueued_total", "Jobs enqueued", ["kind"])
        self.jobs_done = Counter("myelinate_jobs_total", "Jobs finished", ["kind", "status"])
        self.llm_calls = Counter("myelinate_llm_calls_total", "Learning-side LLM calls", ["purpose"])
        self.llm_tokens = Counter("myelinate_llm_tokens_total", "Learning-side LLM tokens", ["purpose", "kind"])
        self.experiments = Counter("myelinate_experiments_total", "Canary decisions", ["verdict"])
        self.changesets = Counter("myelinate_changesets_total", "Changesets by outcome", ["source", "status"])
        self.http = Histogram("myelinate_http_seconds", "HTTP latency", ["route", "method", "status"])


metrics = _Metrics()


def configure_logging(json_logs: bool) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    renderer = structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )


def configure_tracing(app) -> None:  # type: ignore[no-untyped-def]
    """Enabled when the `otel` extra is installed and OTEL_EXPORTER_OTLP_ENDPOINT is set."""
    import os

    if not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        structlog.get_logger().warning("otel.missing", hint="pip install 'myelinate[otel]'")
        return
    provider = TracerProvider(resource=Resource.create({"service.name": "myelinate-gateway"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    FastAPIInstrumentor.instrument_app(app)
