import logging

from fastapi import FastAPI

from app.api.routes import api_router
from app.core.config import settings

logging.basicConfig(level=logging.INFO)

_is_prod = settings.env == "prod"


def create_app() -> FastAPI:
    app = FastAPI(
        title="UniTrack API",
        version="0.1.0",
        description="Hub API for the UniTrack bus ticketing & live-tracking platform.",
        # Interactive docs expose the full schema and are a recon tool for attackers.
        # Disable all three endpoints in production; they remain available in dev.
        docs_url=None if _is_prod else "/docs",
        redoc_url=None if _is_prod else "/redoc",
        openapi_url=None if _is_prod else "/openapi.json",
    )

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # One line, forever. New routers register in app/api/routes/__init__.py,
    # where the auth-coverage test can also see them.
    app.include_router(api_router)
    return app


app = create_app()
