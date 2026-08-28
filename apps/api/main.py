from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from apps.api.errors import register_error_handlers
from apps.api.routes import (
    assets,
    costs,
    events,
    projects,
    references,
    renders,
    reviews,
    scripts,
    shots,
    storyboards,
    transcripts,
    uploads,
    visual_qa,
    workflows,
)
from apps.api.settings import get_settings


def create_app() -> FastAPI:
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
        reviews,
        visual_qa,
    ):
        application.include_router(module.router, prefix="/api/v1")
    register_error_handlers(application)

    @application.get("/healthz", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return application


app = create_app()
