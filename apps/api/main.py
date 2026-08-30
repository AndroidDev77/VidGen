from __future__ import annotations

import logging

from fastapi import FastAPI, Response, status
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from apps.api.dependencies import get_engine
from apps.api.errors import register_error_handlers
from apps.api.routes import (
    assets,
    control_commands,
    costs,
    events,
    final_editorial,
    projects,
    publications,
    references,
    renders,
    repair,
    reviews,
    scripts,
    shots,
    storyboards,
    transcripts,
    uploads,
    visual_qa,
    voice_profiles,
    workflows,
    youtube_connections,
)
from apps.api.settings import get_settings
from vidgen.telemetry.bootstrap import initialize_telemetry


def create_app() -> FastAPI:
    # Structured redacted logging and W3C trace propagation are configured
    # before anything else, so a failure during router construction is still
    # emitted in the shape Log Analytics parses. No exporter is installed
    # unless one is configured, which keeps tests and local runs unchanged.
    initialize_telemetry(service_name="vidgen-api")
    application = FastAPI(title="VidGen API", version="0.3.0")
    settings = get_settings()
    # CORS stays off unless a narrow development origin allowlist is configured;
    # local development prefers Vite's dev proxy over cross-origin requests.
    if settings.cors_allowed_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.cors_allowed_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"],
            allow_headers=["*"],
            expose_headers=["ETag"],
        )
    for module in (
        projects,
        voice_profiles,
        control_commands,
        references,
        uploads,
        assets,
        costs,
        workflows,
        events,
        transcripts,
        scripts,
        storyboards,
        shots,
        renders,
        repair,
        reviews,
        visual_qa,
        final_editorial,
        youtube_connections,
        publications,
    ):
        application.include_router(module.router, prefix="/api/v1")
    register_error_handlers(application)

    # Liveness. Deliberately dependency-free: a slow or briefly unavailable
    # database must restart nothing, it must only take this replica out of
    # rotation, which is what the readiness probe below does.
    @application.get("/healthz", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    # Readiness. Checks only that a pooled connection can be acquired and a
    # trivial statement executed. It never runs, inspects or applies an Alembic
    # migration: schema changes are applied exactly once by the dedicated
    # migration job before any new revision receives traffic.
    @application.get("/readyz", tags=["system"])
    def ready(response: Response) -> dict[str, str]:
        try:
            with get_engine().connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError:
            # The reason is logged by the handler chain; it is never returned,
            # because a driver error can carry the connection string.
            logging.getLogger(__name__).warning("readiness check failed", exc_info=True)
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "unavailable"}
        return {"status": "ok"}

    return application


app = create_app()
