"""Auth guards. Every protected endpoint in this API goes through here.

Read this before adding a route — `app/api/routes/admin.py` is the worked
example.

The shape of it
---------------
    get_principal          resolve the bearer token -> Principal (who is this?)
      └─ require(...)      assert role / helper approval  (may they?)

`require()` is a *factory*: call it with the roles a route needs and it hands
back a FastAPI dependency. Attach that to a whole router and every route inside
it is guarded by construction — including the one a teammate adds next month
without reading this file.

Cost of a guard
---------------
Roughly free. FastAPI caches each dependency's result per request, so stacking
`require(admin)` on the router and `get_principal` in the handler resolves the
token **once**, not twice. The token check is an HS256 verify (~10 µs) plus one
Redis GET (~0.15 ms); Postgres is touched only on a cache miss. Never hand-roll
your own token parsing in a handler — you would pay the cost again and drift
from this logic.
"""

import uuid
from collections.abc import Awaitable, Callable

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.authz import Principal, get_principal_cached
from app.core.redis import get_redis
from app.core.revocation import is_revoked
from app.core.security import decode_token
from app.db.session import get_db
from app.models.user import User, UserRole, UserStatus

_bearer = HTTPBearer(auto_error=True)

# One shared instance: the response is deliberately identical for a malformed
# token, an expired one, and a valid token for a deleted user. Distinguishing
# them tells an attacker which half of the guess was right.
_CREDENTIALS_EXC = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Invalid or expired token",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_access_claims(
    creds: HTTPAuthorizationCredentials = Depends(_bearer),
    r: Redis = Depends(get_redis),
) -> dict:
    """Decode the bearer token and reject it if it has been revoked.

    Split out from `get_principal` so `/auth/logout` can reach the caller's
    `jti` — you cannot revoke a token you cannot name. FastAPI caches the result
    per request, so a route depending on both this and `get_principal` still
    decodes once.
    """
    try:
        claims = decode_token(creds.credentials, expected_type="access")
    except jwt.InvalidTokenError as exc:
        raise _CREDENTIALS_EXC from exc

    if await is_revoked(r, claims["jti"]):
        raise _CREDENTIALS_EXC
    return claims


async def get_principal(
    claims: dict = Depends(get_access_claims),
    r: Redis = Depends(get_redis),
    db: AsyncSession = Depends(get_db),
) -> Principal:
    """Authenticate the caller. The single front door — everything builds on it.

    `expected_type="access"` is load-bearing: without it a 30-day refresh token
    would be accepted as a 15-minute access token, silently erasing the whole
    point of short-lived credentials.
    """
    try:
        user_id = uuid.UUID(claims["sub"])
    except (KeyError, ValueError) as exc:
        raise _CREDENTIALS_EXC from exc

    principal = await get_principal_cached(r, db, user_id)
    if principal is None or principal.status is UserStatus.suspended:
        raise _CREDENTIALS_EXC
    return principal


def require(
    *roles: UserRole,
    approved_helper: bool = False,
    active_only: bool = True,
) -> Callable[..., Awaitable[Principal]]:
    """Build a dependency that authorizes the caller. This is the main API.

    Args:
        *roles: allowed roles. Empty means "any authenticated user".
        approved_helper: also demand `helpers.status = 'approved'` (spec §8).
            Helpers self-register as `pending`, so any route that trusts a
            helper's data needs this — a pending account must not be able to
            push GPS fixes.
        active_only: demand `users.status = 'active'`. Leave it on unless the
            route is specifically part of onboarding an inactive account.

    Usage — attach to the router so new routes inherit it:

        router = APIRouter(
            prefix="/admin",
            dependencies=[Depends(require(UserRole.admin))],
        )

    Roles are read from the **database snapshot, never from the JWT `role`
    claim**. A token minted before a demotion still carries the old role; the
    snapshot does not. Authorization decisions must not trust the token beyond
    the subject it identifies.
    """

    async def _guard(principal: Principal = Depends(get_principal)) -> Principal:
        if active_only and not principal.is_active:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Account is not active")
        if roles and principal.role not in roles:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Insufficient role")
        if approved_helper and not principal.is_approved_helper:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Helper account not approved")
        return principal

    return _guard


# --- Ready-made guards for the roles this API actually has ---
# Prefer these over calling require() inline, so the rules live in one place.

require_admin = require(UserRole.admin)
require_student = require(UserRole.student)
require_approved_helper = require(UserRole.helper, approved_helper=True)
require_authenticated = require()


async def get_current_user(
    principal: Principal = Depends(get_principal),
    db: AsyncSession = Depends(get_db),
) -> User:
    """The full ORM `User` row — for routes that must return profile fields.

    Costs one extra SELECT, so only use it where the row itself is the point
    (e.g. `GET /auth/me`). For authorization, `Principal` already has what you
    need and is cached.
    """
    user = await db.get(User, principal.user_id)
    if user is None:
        raise _CREDENTIALS_EXC
    return user
