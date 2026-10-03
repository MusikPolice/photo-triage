from fastapi import FastAPI

from photo_triage.api import health
from photo_triage.settings import Settings, load_settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the API. Settings come from the environment unless given."""
    app = FastAPI(title="photo-triage", docs_url="/api/docs", openapi_url="/api/openapi.json")
    app.state.settings = settings if settings is not None else load_settings()
    app.include_router(health.router, prefix="/api")
    return app
