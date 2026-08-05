"""Refresh-token rotation, and the grace window that keeps it from signing
helpers out mid-route.

Rotation on its own is straightforward: the token you present is revoked as
part of the exchange. The complication is that the helper app refreshes from
two places at once — the UI isolate and the GPS service isolate share one
refresh token in secure storage and hit the 15-minute expiry together. Naive
rotation makes the slower of the two look exactly like a replay attack, and the
client's response to a failed refresh is to wipe the session.

So the contract these tests pin is: **concurrent refreshes converge on one
pair**, while a genuine replay outside the window still fails.
"""

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.core.config import settings
from app.core.revocation import (
    ROTATION_GRACE_S,
    claim_rotation,
    recall_rotation,
    rotated_key,
)
from app.core.security import ALGORITHM, decode_token


class _FakeRedis:
    """Enough of Redis for `SET NX EX` and `GET`, with no TTL clock.

    Expiry is simulated by `expire_all()` rather than by sleeping, so the tests
    stay instant and deterministic.
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return None
        self.store[key] = value
        if ex is not None:
            self.ttls[key] = ex
        return True

    async def get(self, key):
        return self.store.get(key)

    def expire_all(self) -> None:
        self.store.clear()


async def test_first_caller_wins_the_claim() -> None:
    r = _FakeRedis()

    winner = await claim_rotation(r, "jti-1", '{"pair":"A"}')

    # None means "you won — use the pair you minted".
    assert winner is None
    assert r.store[rotated_key("jti-1")] == '{"pair":"A"}'


async def test_second_caller_receives_the_first_callers_pair() -> None:
    """The isolate race. Neither caller may get an error, and both must agree.

    If they were handed different pairs, whichever wrote to secure storage last
    would strand the other holding tokens nothing else knows about.
    """
    r = _FakeRedis()

    first = await claim_rotation(r, "jti-1", '{"pair":"A"}')
    second = await claim_rotation(r, "jti-1", '{"pair":"B"}')

    assert first is None  # minted A, kept A
    assert second == '{"pair":"A"}'  # discarded B, took A
    # The losing caller's pair never reaches the store.
    assert r.store[rotated_key("jti-1")] == '{"pair":"A"}'


async def test_a_third_caller_also_converges() -> None:
    """More than two refreshes in the window still produce exactly one pair."""
    r = _FakeRedis()

    await claim_rotation(r, "jti-1", '{"pair":"A"}')
    results = [await claim_rotation(r, "jti-1", f'{{"pair":"{n}"}}') for n in "BCD"]

    assert results == ['{"pair":"A"}'] * 3


async def test_replay_after_the_window_has_no_record() -> None:
    """Past the grace window a revoked token is just a replay, and gets a 401.

    `recall_rotation` returning None is what the endpoint turns into that 401.
    """
    r = _FakeRedis()
    await claim_rotation(r, "jti-1", '{"pair":"A"}')

    r.expire_all()

    assert await recall_rotation(r, "jti-1") is None


async def test_rotations_do_not_leak_across_tokens() -> None:
    """Each token's record is keyed by its own jti.

    A shared key would let any revoked token collect whichever pair was written
    most recently — handing a replayed token a live session.
    """
    r = _FakeRedis()

    await claim_rotation(r, "jti-1", '{"pair":"A"}')
    await claim_rotation(r, "jti-2", '{"pair":"B"}')

    assert await recall_rotation(r, "jti-1") == '{"pair":"A"}'
    assert await recall_rotation(r, "jti-2") == '{"pair":"B"}'


async def test_lookup_failure_denies_rather_than_mints() -> None:
    """A Redis error during recall must not be read as "no replay here".

    Returning a pair on a failed lookup would turn an outage into free token
    minting for anyone holding an old refresh token.
    """

    class _BrokenRedis:
        async def get(self, key):
            raise ConnectionError("redis is down")

    assert await recall_rotation(_BrokenRedis(), "jti-1") is None


def test_grace_window_stays_short() -> None:
    """The window is the whole security cost of this feature.

    Long enough for two isolates milliseconds apart; short enough that a stolen
    refresh token is near-useless. If this ever needs minutes, the client is
    doing something that should be fixed in the client.
    """
    assert 0 < ROTATION_GRACE_S <= 120


def test_a_token_minted_before_revocation_existed_is_rejected() -> None:
    """No `jti` means no way to revoke it, so it is refused outright.

    Waving these through would leave a permanent bypass: anyone holding a token
    issued before rotation shipped could refresh forever, immune to logout. The
    cost is a one-off re-login, which is why this is checked in `decode_token`
    rather than papered over at the Redis layer — an empty `jti` must never
    reach a key builder in the first place.
    """
    now = datetime.now(UTC)
    legacy = jwt.encode(
        {
            "sub": "3cf1b2d2-4584-420c-b6fb-d5b10587f32f",
            "role": "helper",
            "type": "refresh",
            "iat": now,
            "exp": now + timedelta(days=1),
        },
        settings.jwt_secret,
        algorithm=ALGORITHM,
    )

    with pytest.raises(jwt.InvalidTokenError):
        decode_token(legacy, expected_type="refresh")
