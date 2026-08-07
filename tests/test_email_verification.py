"""Registration, confirmation, and the ways each could leak or strand someone.

The domain gate establishes that an address *could* belong to a student. It does
not establish that the person typing it owns one — so without confirmation,
anyone can register under a classmate's address, and the classmate is then
locked out of their own university email forever, because it is taken.

These cover that, plus the two failure modes that hurt real people: an account
nobody can reach because the email went missing, and an endpoint that answers
differently for a real address than a fake one.
"""

import uuid

import pytest
from fastapi import status
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from app.api.deps import get_db
from app.core import email as email_module
from app.core.config import settings
from app.core.email import verification_link
from app.core.security import create_email_verify_token
from app.db.base import Base
from app.main import create_app
from app.models.user import User, UserStatus

REGISTRATION = {
    "email": "New.Student@ulab.edu.bd",
    "password": "correct horse battery",
    "name": "New Student",
    "student_id_no": "221011999",
}


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async_session = sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with async_session() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def sent(monkeypatch):
    """Capture what would have been emailed, instead of emailing it."""
    captured: list[dict] = []

    async def _fake(*, to: str, name: str, token: str) -> bool:
        captured.append({"to": to, "name": name, "token": token})
        return True

    # Patched where it is *used*, not where it is defined: auth.py imported the
    # function by name at module load, so patching app.core.email would leave
    # the route holding the original.
    monkeypatch.setattr("app.api.routes.auth.send_verification_email", _fake)
    return captured


@pytest.fixture
async def client(db, sent):
    app = create_app()

    async def _db():
        yield db

    app.dependency_overrides[get_db] = _db
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _user(db: AsyncSession, email: str) -> User | None:
    return (
        await db.execute(select(User).where(User.email == email.lower()))
    ).scalar_one_or_none()


# --- the domain gate --------------------------------------------------------


async def test_a_varsity_address_can_register(client, db) -> None:
    response = await client.post("/auth/register/student", json=REGISTRATION)
    assert response.status_code == status.HTTP_201_CREATED
    assert response.json()["status"] == "pending_email"

    # Stored lowercased, so "New.Student@" and "new.student@" are one account
    # rather than two people who both think they own the address.
    assert await _user(db, "new.student@ulab.edu.bd") is not None


async def test_an_outside_address_is_refused_by_the_server(client) -> None:
    """The gate is server-side, not a UI hint. A form is trivially bypassed."""
    response = await client.post(
        "/auth/register/student", json={**REGISTRATION, "email": "someone@gmail.com"}
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN


async def test_a_lookalike_domain_is_refused(client) -> None:
    """`ulab.edu.bd.evil.com` ends with the allowed domain as a substring.
    Matching on the part after the final `@` is what makes this a 403 rather
    than a way in."""
    for bad in ("a@ulab.edu.bd.evil.com", "a@notulab.edu.bd", "a@sub.ulab.edu.bd"):
        response = await client.post(
            "/auth/register/student", json={**REGISTRATION, "email": bad}
        )
        assert response.status_code == status.HTTP_403_FORBIDDEN, bad


async def test_the_same_address_cannot_be_registered_twice(client) -> None:
    assert (await client.post("/auth/register/student", json=REGISTRATION)).status_code == 201
    second = await client.post("/auth/register/student", json=REGISTRATION)
    assert second.status_code == status.HTTP_409_CONFLICT


# --- confirmation -----------------------------------------------------------


async def test_registration_sends_a_link_to_the_address_given(client, sent) -> None:
    await client.post("/auth/register/student", json=REGISTRATION)
    assert len(sent) == 1
    assert sent[0]["to"] == "new.student@ulab.edu.bd"


async def test_an_unconfirmed_account_cannot_log_in(client) -> None:
    """The whole point of confirmation. Registering must not be enough."""
    await client.post("/auth/register/student", json=REGISTRATION)

    response = await client.post(
        "/auth/login",
        json={"email": REGISTRATION["email"], "password": REGISTRATION["password"]},
    )
    assert response.status_code == status.HTTP_403_FORBIDDEN
    # Says what to do about it. "Account is not active" sends someone who only
    # needs to check their inbox looking for an administrator.
    assert "inbox" in response.json()["detail"].lower()


async def test_clicking_the_link_activates_and_then_login_works(client, sent) -> None:
    await client.post("/auth/register/student", json=REGISTRATION)

    verified = await client.get(
        "/auth/verify-email", params={"token": sent[0]["token"]}
    )
    assert verified.status_code == status.HTTP_200_OK
    assert verified.json()["status"] == "active"

    login = await client.post(
        "/auth/login",
        json={"email": REGISTRATION["email"], "password": REGISTRATION["password"]},
    )
    assert login.status_code == status.HTTP_200_OK
    assert login.json()["access_token"]


async def test_a_forged_or_expired_token_activates_nothing(client, db) -> None:
    await client.post("/auth/register/student", json=REGISTRATION)

    for bad in ("not-a-token", create_email_verify_token(str(uuid.uuid4()), "student")):
        response = await client.get("/auth/verify-email", params={"token": bad})
        assert response.status_code in (
            status.HTTP_400_BAD_REQUEST,
            status.HTTP_404_NOT_FOUND,
        )

    user = await _user(db, REGISTRATION["email"])
    assert user is not None
    assert user.status is UserStatus.pending_email


async def test_an_access_token_is_not_a_verification_token(client) -> None:
    """`expected_type` is load-bearing. Without it, any token this server ever
    signed would activate an account."""
    from app.core.security import create_access_token

    await client.post("/auth/register/student", json=REGISTRATION)
    response = await client.get(
        "/auth/verify-email",
        params={"token": create_access_token(str(uuid.uuid4()), "student")},
    )
    assert response.status_code == status.HTTP_400_BAD_REQUEST


async def test_confirming_twice_is_harmless(client, sent) -> None:
    """People click links twice, and mail scanners click them once first."""
    await client.post("/auth/register/student", json=REGISTRATION)
    token = sent[0]["token"]

    first = await client.get("/auth/verify-email", params={"token": token})
    second = await client.get("/auth/verify-email", params={"token": token})
    assert first.status_code == second.status_code == status.HTTP_200_OK
    assert second.json()["status"] == "active"


# --- resend -----------------------------------------------------------------


async def test_a_lost_email_can_be_resent(client, sent) -> None:
    """Otherwise one spam filter is an account its owner can never use and
    never re-register, because the address is already taken."""
    await client.post("/auth/register/student", json=REGISTRATION)
    sent.clear()

    response = await client.post(
        "/auth/resend-verification", json={"email": REGISTRATION["email"]}
    )
    assert response.status_code == status.HTTP_202_ACCEPTED
    assert len(sent) == 1


async def test_resend_answers_identically_for_an_address_that_does_not_exist(
    client, sent
) -> None:
    """Unauthenticated, so a different answer here would let anyone enumerate
    who at the university holds an account."""
    await client.post("/auth/register/student", json=REGISTRATION)
    sent.clear()

    real = await client.post(
        "/auth/resend-verification", json={"email": REGISTRATION["email"]}
    )
    fake = await client.post(
        "/auth/resend-verification", json={"email": "nobody@ulab.edu.bd"}
    )

    assert real.status_code == fake.status_code == status.HTTP_202_ACCEPTED
    assert real.json() == fake.json()
    # Only the real one actually sends, which is the part that must not be
    # observable from outside.
    assert len(sent) == 1


async def test_resend_does_nothing_for_an_already_active_account(client, sent, db) -> None:
    await client.post("/auth/register/student", json=REGISTRATION)
    await client.get("/auth/verify-email", params={"token": sent[0]["token"]})
    sent.clear()

    response = await client.post(
        "/auth/resend-verification", json={"email": REGISTRATION["email"]}
    )
    assert response.status_code == status.HTTP_202_ACCEPTED
    assert sent == []


# --- the link itself --------------------------------------------------------


def test_the_link_points_at_the_student_app_not_the_api(monkeypatch) -> None:
    """A person reads this. Landing them on raw JSON looks like a broken site."""
    monkeypatch.setattr(settings, "student_app_url", "https://unitrack.example.edu/")
    assert verification_link("abc").startswith("https://unitrack.example.edu/verify?token=")


def test_the_link_falls_back_to_the_api_when_no_app_is_configured(monkeypatch) -> None:
    """Still a working link rather than a broken one — the API has its own
    bare verify endpoint."""
    monkeypatch.setattr(settings, "student_app_url", "")
    monkeypatch.setattr(settings, "public_base_url", "https://api.example.edu")
    assert verification_link("abc") == "https://api.example.edu/verify?token=abc"


def test_a_token_is_url_encoded_into_the_link() -> None:
    """JWTs are base64url today. A format that ever grew a `+` or `&` would
    otherwise truncate silently at the query parser."""
    assert "a%2Bb%26c" in verification_link("a+b&c")


async def test_sending_is_disabled_rather_than_broken_without_smtp(monkeypatch) -> None:
    """No relay configured is a supported state — it is what a laptop looks
    like. Registration must still work, and the link goes to the log."""
    monkeypatch.setattr(settings, "smtp_host", "")
    assert not settings.email_enabled
    assert (
        await email_module.send_verification_email(
            to="a@ulab.edu.bd", name="A", token="tok"
        )
        is False
    )
