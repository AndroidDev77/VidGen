from __future__ import annotations

from fastapi import FastAPI

from apps.api.routes import assets, costs, projects, uploads


def create_app() -> FastAPI:
    application = FastAPI(title="VidGen API", version="0.2.0")
    application.include_router(projects.router, prefix="/api/v1")
    application.include_router(uploads.router, prefix="/api/v1")
    application.include_router(assets.router, prefix="/api/v1")
    application.include_router(costs.router, prefix="/api/v1")

    @application.get("/healthz", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return application


app = create_app()
