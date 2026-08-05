"""The safety net that stops a stale `Principal` from outliving a suspension.

`app/core/authz.py` caches each user's authorization snapshot in Redis, so a
write to `users` or `helpers` that does not clear the cache leaves the old
answer in place for up to `PRINCIPAL_TTL_S`. An endpoint author forgetting one
call used to be enough to keep a suspended account working.

These tests pin the mechanism that removes that possibility. They need no
database and no Redis — the listener is a pure function of what a session
holds, which is the whole reason it can be tested at all.
"""

import uuid

from sqlalchemy.orm import Session

from app.core.authz import _TOUCHED_KEY, _record_touched_principals
from app.models.user import Helper, HelperStatus, User, UserRole, UserStatus


class _FakeSession:
    """Just the four attributes the listener reads."""

    def __init__(self, new=(), dirty=(), deleted=()) -> None:
        self.new = new
        self.dirty = dirty
        self.deleted = deleted
        self.info: dict = {}


def _user(**kw) -> User:
    return User(
        id=kw.get("id", uuid.uuid4()),
        email="x@example.com",
        password_hash="x",
        role=UserRole.helper,
        name="X",
        status=UserStatus.active,
    )


def test_a_modified_user_is_recorded() -> None:
    user = _user()
    session = _FakeSession(dirty=(user,))

    _record_touched_principals(session, None)

    assert session.info[_TOUCHED_KEY] == {user.id}


def test_a_helper_is_recorded_under_its_user_id_not_its_own() -> None:
    """The cache is keyed by user, and `helpers.id` is not `users.id`.

    Recording the helper's own primary key would delete a key that never
    existed, so approving or suspending a helper would appear to invalidate
    while the real snapshot — and its `helper_status` — stayed cached.
    """
    user_id = uuid.uuid4()
    helper = Helper(id=uuid.uuid4(), user_id=user_id, status=HelperStatus.approved)
    session = _FakeSession(dirty=(helper,))

    _record_touched_principals(session, None)

    assert session.info[_TOUCHED_KEY] == {user_id}
    assert helper.id not in session.info[_TOUCHED_KEY]


def test_deletions_and_inserts_count_too() -> None:
    """A deleted user must lose its cached snapshot, not keep it until the TTL."""
    inserted, removed = _user(), _user()
    session = _FakeSession(new=(inserted,), deleted=(removed,))

    _record_touched_principals(session, None)

    assert session.info[_TOUCHED_KEY] == {inserted.id, removed.id}


def test_unrelated_rows_are_ignored() -> None:
    """Only `users` and `helpers` feed the principal cache.

    Every flush in the app passes through this listener, including the GPS and
    trip write paths. Recording rows it does not cache would add a pointless
    Redis DEL to the hottest endpoint in the system.
    """
    session = _FakeSession(dirty=(object(),))

    _record_touched_principals(session, None)

    assert not session.info.get(_TOUCHED_KEY)


def test_repeated_flushes_accumulate_into_one_set() -> None:
    """Several flushes in one request end as a single batch of invalidations."""
    first, second = _user(), _user()
    session = _FakeSession(dirty=(first,))

    _record_touched_principals(session, None)
    session.dirty = (second,)
    _record_touched_principals(session, None)

    assert session.info[_TOUCHED_KEY] == {first.id, second.id}


def test_listener_is_registered_on_the_session_class() -> None:
    """Registration is the load-bearing half — an unregistered hook is inert.

    Asserting on the real `Session` event registry catches the case where the
    decorator is removed or the module stops being imported, which no
    behavioural test above would notice.
    """
    from sqlalchemy import event

    assert event.contains(Session, "after_flush", _record_touched_principals)
