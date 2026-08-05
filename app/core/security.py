import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.core.config import settings

_ph = PasswordHasher()

ALGORITHM = "HS256"
TokenType = Literal["access", "refresh", "email_verify"]
EMAIL_VERIFY_TTL = timedelta(days=2)

# A hash of a value no one can present, used to spend the same CPU on a login
# for an address that does not exist as on one that does. Computed once at
# import (~50 ms) rather than per request.
_DUMMY_HASH = _ph.hash("timing-equalisation-only-never-a-real-password")


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _ph.verify(password_hash, password)
    except VerifyMismatchError:
        return False


def spend_dummy_verify() -> None:
    """Burn one argon2 verify to hide whether an account exists.

    `/auth/login` short-circuits on an unknown email, so without this the reply
    comes back in microseconds for an address that has never registered and in
    ~50 ms for one that has. That difference is measurable over the network and
    turns the login endpoint into an account-enumeration oracle. Call this on
    the miss branch so both paths cost the same.
    """
    verify_password("wrong", _DUMMY_HASH)


def _create_token(sub: str, role: str, token_type: TokenType, ttl: timedelta) -> str:
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": sub,
        "role": role,
        "type": token_type,
        # Unique id for this token, so it can be revoked individually. Without
        # it a leaked token is valid until `exp` no matter what the server does.
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": now + ttl,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=ALGORITHM)


def create_access_token(sub: str, role: str) -> str:
    return _create_token(sub, role, "access", timedelta(minutes=settings.access_token_ttl_min))


def create_refresh_token(sub: str, role: str) -> str:
    return _create_token(sub, role, "refresh", timedelta(days=settings.refresh_token_ttl_days))


def create_email_verify_token(sub: str, role: str) -> str:
    return _create_token(sub, role, "email_verify", EMAIL_VERIFY_TTL)


def decode_token(token: str, expected_type: TokenType) -> dict[str, Any]:
    """Decode + validate a JWT. Raises jwt.InvalidTokenError on any problem.

    A token with no `jti` is rejected rather than waved through: it predates
    revocation support and therefore cannot be revoked, and an unrevocable
    token is exactly what an attacker with a stolen one wants. The cost is a
    one-off forced re-login for anyone holding a token minted before this
    change — 15 minutes of access tokens, and one sign-in for refresh tokens.
    """
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[ALGORITHM])
    if payload.get("type") != expected_type:
        raise jwt.InvalidTokenError(f"expected {expected_type} token")
    if not payload.get("jti"):
        raise jwt.InvalidTokenError("token has no jti and cannot be revoked")
    return payload
