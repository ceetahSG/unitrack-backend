"""Seeded accounts must be able to reach the login endpoint.

The seed scripts write to Postgres directly, so they bypass every schema the
API applies on the way in. That makes it possible to create an account that
looks perfectly healthy in `psql` and still cannot sign in — which is exactly
what happened with `@unitrack.test`: `LoginRequest.email` is an `EmailStr`, and
email-validator rejects RFC 2606 reserved TLDs, so `POST /auth/login` answered
422 before ever looking at the password.

Nobody notices until a teammate follows the setup guide and cannot get past
step 6. This test closes that gap by running the seeded addresses through the
same schema the endpoint uses.
"""

import pytest
from pydantic import ValidationError

from app.schemas.auth import LoginRequest
from scripts.seed import _ALL_EMAILS


@pytest.mark.parametrize("email", sorted(_ALL_EMAILS))
def test_seeded_account_passes_the_login_schema(email: str) -> None:
    LoginRequest(email=email, password="irrelevant-but-long-enough")


def test_the_schema_would_actually_catch_a_reserved_tld() -> None:
    """Guards the guard.

    If `LoginRequest` ever stopped validating the address, the test above would
    pass for every input and silently stop protecting anything.
    """
    with pytest.raises(ValidationError):
        LoginRequest(email="someone@unitrack.test", password="irrelevant-but-long")
