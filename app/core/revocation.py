"""Token revocation — the denylist that makes a JWT killable before it expires.

Why this exists
---------------
A JWT is valid because it verifies, not because a server says so. That is the
whole point of it, and also its one sharp edge: until this module existed,
`POST /auth/refresh` handed out a new pair while the old refresh token stayed
good for the rest of its 30 days, and there was no way to end a session at all.
A stolen refresh token was a month of access, and "log out" was a lie the client
told itself by deleting local storage.

The fix is the standard one: every token carries a unique `jti`, and a revoked
`jti` goes into Redis until the moment the token would have expired anyway.
Storing it any longer wastes memory — past `exp` the signature check rejects it
without our help.

Failure policy — read before changing
-------------------------------------
Redis being down must not mean nobody can use the API, so the two paths differ
deliberately:

- **Access tokens** (`is_revoked`) fail **open**. They live 15 minutes, they are
  checked on every single request, and refusing all traffic during a cache
  outage trades a small security window for a total outage. The exposure is
  bounded by the TTL.
- **Refresh and logout** (`is_revoked_strict`) fail **closed**. These are rare,
  they are the high-value long-lived credential, and a caller who cannot refresh
  right now still holds a working access token for up to 15 minutes. Blocking is
  cheap here; guessing is not.

This mirrors the same availability-over-latency call made in `app/core/authz.py`
for the principal cache, but stops short of applying it to the 30-day credential.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from redis.asyncio import Redis

logger = logging.getLogger("unitrack.revocation")


# How long a just-rotated refresh token keeps returning its replacement instead
# of a 401. Long enough to cover two clients refreshing the same token within
# moments of each other; short enough that a stolen token is near-useless.
ROTATION_GRACE_S = 60


def revoked_key(jti: str) -> str:
    return f"auth:revoked:{jti}"


def rotated_key(jti: str) -> str:
    return f"auth:rotated:{jti}"


async def claim_rotation(r: Redis, jti: str, pair_json: str) -> str | None:
    """Try to be the one that rotates `jti`. Returns the winner's pair.

    Why this is not a plain "revoke then issue"
    -------------------------------------------
    The helper app runs its GPS service in a separate isolate with its own
    `ApiClient`, so two independent callers share one refresh token and their
    access tokens expire at the same moment. Both refresh at once. With naive
    rotation the slower one presents a token the faster one just revoked, gets a
    401, and the client wipes the session — signing the helper out and killing
    tracking in the middle of a route.

    So the write is a `SET NX`: the first caller to claim `jti` stores the pair
    it minted, and everyone else arriving during the grace window is handed
    *that same pair* rather than an error. Both isolates converge on identical
    tokens, so whichever writes to storage last writes the same value.

    Returns `None` if this caller won the claim (use your own pair), or the
    stored JSON if another caller got there first (return theirs instead).
    """
    won = await r.set(rotated_key(jti), pair_json, nx=True, ex=ROTATION_GRACE_S)
    if won:
        return None
    return await r.get(rotated_key(jti))


async def recall_rotation(r: Redis, jti: str) -> str | None:
    """The pair that replaced `jti`, if it was rotated within the grace window.

    A revoked token with no rotation record is a genuine replay — an old token,
    or a stolen one used after the window closed. That still gets a 401.
    """
    try:
        return await r.get(rotated_key(jti))
    except Exception:  # noqa: BLE001 - a lookup failure must not mint tokens
        logger.error("rotation lookup unavailable for jti=%s", jti)
        return None


def _ttl_seconds(claims: dict[str, Any]) -> int:
    """Seconds until this token expires on its own, floored at 1.

    The denylist entry only has to outlive the token. `exp` is a UTC epoch int
    per RFC 7519, so this needs no timezone handling.
    """
    remaining = int(claims.get("exp", 0)) - int(time.time())
    return max(remaining, 1)


async def revoke(r: Redis, claims: dict[str, Any]) -> None:
    """Deny-list this token until its natural expiry. Idempotent.

    Takes the decoded claims rather than a `jti` so the TTL comes from the same
    token being revoked — a caller cannot accidentally pair one token's id with
    another's lifetime.
    """
    jti = claims.get("jti")
    if not jti:
        return
    try:
        await r.set(revoked_key(jti), "1", ex=_ttl_seconds(claims))
    except Exception:  # noqa: BLE001 - see "Failure policy" above
        # A revoke that does not land is the dangerous direction, so say so
        # loudly. The caller still succeeds; the token dies at `exp` instead.
        logger.error("failed to revoke jti=%s — token stays valid until exp", jti)


async def is_revoked(r: Redis, jti: str) -> bool:
    """Hot-path check for access tokens. Fails **open** on a Redis error."""
    try:
        return await r.exists(revoked_key(jti)) == 1
    except Exception:  # noqa: BLE001
        logger.warning("revocation check unavailable, allowing jti=%s", jti)
        return False


async def is_revoked_strict(r: Redis, jti: str) -> bool:
    """Refresh/logout check. Fails **closed** — a Redis error reports revoked."""
    try:
        return await r.exists(revoked_key(jti)) == 1
    except Exception:  # noqa: BLE001
        logger.error("revocation check unavailable, refusing jti=%s", jti)
        return True
