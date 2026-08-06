"""Router aggregation and the public-route allow-list.

Adding an endpoint means touching this file in exactly one of two ways:

- New router? Include it in `api_router` below. Guard it at the `APIRouter(...)`
  constructor — see `admin.py`.
- Genuinely public route (no login possible or required)? Add its path to
  `PUBLIC_PATHS`.

Anything else fails `tests/test_auth_coverage.py`, which walks the mounted app
and refuses any route that is neither guarded nor listed here. Forgetting to
guard a route is the single easiest security mistake to make in FastAPI, so it
is a build failure rather than a code-review hope.
"""

from fastapi import APIRouter

from app.api.routes import (
    admin,
    auth,
    boarding,
    fleet,
    helper,
    shop,
    tracking,
    wallet_page,
)

# Every router in the API. `api_router` is built from this tuple rather than
# from a list of include_router() calls, so the auth-coverage test and the
# mounted app can never disagree about what exists.
ROUTERS: tuple[APIRouter, ...] = (
    auth.router,
    admin.router,
    fleet.router,
    helper.router,
    tracking.router,
    shop.router,
    boarding.router,
    wallet_page.router,
)

api_router = APIRouter()
for _router in ROUTERS:
    api_router.include_router(_router)

# Routes that are unauthenticated **by design**. Every entry needs a reason,
# because every entry is an attack surface.
PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        "/health",  # liveness probe for nginx/compose — must work without creds
        "/auth/register/student",  # no account exists yet
        "/auth/register/helper",  # no account exists yet
        "/auth/verify-email",  # the token in the emailed link IS the credential
        "/auth/login",  # issues the credential
        "/auth/refresh",  # the refresh token IS the credential
        # The payment gateway redirects the student's browser here with a
        # form POST that carries no credential of ours. Authenticating it is
        # impossible; instead nothing in the request is trusted, and the
        # payment is confirmed by a server-to-server validation call before
        # any ticket is issued. See app/api/routes/shop.py.
        "/shop/payments/return",
        # SSLCommerz posts here server-to-server, so there is no session to
        # authenticate at all. Same defence as the return: the request is a
        # lookup key, and the payment is confirmed by calling the gateway back.
        "/shop/payments/ipn",
        # The student wallet page. Public like any single-page app: it ships no
        # data, and authenticates from inside against /auth/login.
        "/wallet",
        # FastAPI's own docs endpoints.
        "/docs",
        "/docs/oauth2-redirect",
        "/redoc",
        "/openapi.json",
    }
)
