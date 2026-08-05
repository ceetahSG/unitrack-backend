import enum
import uuid

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class UserRole(enum.StrEnum):
    student = "student"
    helper = "helper"
    admin = "admin"


class UserStatus(enum.StrEnum):
    pending_email = "pending_email"
    active = "active"
    pending_approval = "pending_approval"
    suspended = "suspended"


class HelperStatus(enum.StrEnum):
    pending = "pending"
    approved = "approved"
    suspended = "suspended"


class User(Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(
        SAEnum(UserRole, name="user_role"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    photo_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[UserStatus] = mapped_column(
        SAEnum(UserStatus, name="user_status"), nullable=False
    )

    student: Mapped["Student | None"] = relationship(back_populates="user", uselist=False)
    helper: Mapped["Helper | None"] = relationship(
        back_populates="user", uselist=False, foreign_keys="Helper.user_id"
    )


class Student(Base):
    __tablename__ = "students"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    student_id_no: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    department: Mapped[str | None] = mapped_column(String(120), nullable=True)
    batch: Mapped[str | None] = mapped_column(String(32), nullable=True)
    default_stop_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="student")


class Helper(Base):
    __tablename__ = "helpers"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    # SET NULL, not CASCADE: deleting an admin must not delete every helper they
    # approved. The approval is a fact about the helper; losing the approver
    # means it was approved by someone no longer on the system, not that it
    # never happened. Without any ON DELETE the admin's row could never be
    # deleted at all — see migration e9c3a7b41f26.
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[HelperStatus] = mapped_column(
        SAEnum(HelperStatus, name="helper_status"), nullable=False, default=HelperStatus.pending
    )
    current_device_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    user: Mapped[User] = relationship(back_populates="helper", foreign_keys=[user_id])
