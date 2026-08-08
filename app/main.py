import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import api_router
from app.core.config import settings

logging.basicConfig(level=logging.INFO)

logger = logging.getLogger("unitrack.startup")

_is_prod = settings.env == "prod"

# Defaults that are correct for a laptop and dangerous or broken on a server.
# Each one has already caused a real failure or would be a live vulnerability.
_LOCAL_ONLY_HOSTS = ("localhost", "127.0.0.1")


def _warn_about_local_defaults() -> None:
    """Shout about settings that are silently wrong for a deployment.

    None of these stop the API from starting, and that is exactly the problem:
    a misconfigured deployment looks completely healthy. `PUBLIC_BASE_URL` left
    at its default cost a real payment — no `ipn_url` was registered, so the
    gateway had no way to report the outcome and the money was only recovered
    later by the reconciler.
    """
    if not _is_prod:
        return

    if any(host in settings.public_base_url for host in _LOCAL_ONLY_HOSTS):
        logger.error(
            "PUBLIC_BASE_URL is %r in production. No ipn_url will be registered, so "
            "payment gateways cannot report outcomes and every settlement will depend "
            "on the student's browser returning. Set it to this API's public origin.",
            settings.public_base_url,
        )

    if settings.jwt_secret == "change-me-in-prod" or len(settings.jwt_secret) < 32:
        logger.error(
            "JWT_SECRET is the default or shorter than 32 bytes. Anyone who knows it "
            "can forge an admin token. Generate one with "
            "`python -c \"import secrets; print(secrets.token_urlsafe(48))\"`."
        )

    if not settings.sslcommerz_store_id or not settings.sslcommerz_store_password:
        logger.warning(
            "SSLCommerz credentials are unset; ticket purchase will answer 502."
        )


def create_app() -> FastAPI:
    _warn_about_local_defaults()

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
