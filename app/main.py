import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

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

    # Browser clients only. Added when `CORS_ORIGINS` is set, and skipped
    # entirely when it is not, so an API with no web client advertises nothing.
    #
    # `allow_credentials` stays False on purpose: authentication here is a
    # bearer token in a header, not a cookie, so the browser has no credentials
    # to attach. Turning it on would be the switch that makes a wildcard origin
    # catastrophic, and it buys this API nothing. Revisit only if auth ever
    # moves to cookies.
    if origins := settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["authorization", "content-type"],
        )

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # One line, forever. New routers register in app/api/routes/__init__.py,
    # where the auth-coverage test can also see them.
    app.include_router(api_router)
    return app


app = create_app()
