import uuid

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.user import UserRole, UserStatus


class StudentRegister(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    name: str = Field(min_length=1, max_length=120)
    student_id_no: str = Field(min_length=1, max_length=64)
    department: str | None = None
    batch: str | None = None
    phone: str | None = None


class HelperRegister(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    name: str = Field(min_length=1, max_length=120)
    phone: str | None = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    """The refresh token to kill. The access token comes from the Bearer header.

    Optional because a client that has already lost its refresh token should
    still be able to end the session it can prove it holds.
    """

    refresh_token: str | None = None


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    # Seconds until `access_token` expires. Present so the response matches the
    # OAuth 2.0 token-response shape (RFC 6749 §5.1) — clients that already
    # speak OAuth need no special-casing, and ours stops hardcoding 15 minutes.
    expires_in: int


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    name: str
    role: UserRole
    status: UserStatus


class ResendVerification(BaseModel):
    """Ask for the confirmation link again.

    Only an address. The account cannot be logged into yet — that is the whole
    problem it solves — so there is no session to authenticate this with.
    """

    email: EmailStr
