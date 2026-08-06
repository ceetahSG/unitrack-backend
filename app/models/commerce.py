"""Tickets and the money that buys them (spec §6 "Commerce").

Three tables, in the order a purchase moves through them:

    ticket_products   what is for sale
    orders            one attempt to buy, paid or not
    tickets           what the student actually holds afterwards

The split matters. A failed payment must leave a record — reconciliation
depends on being able to see "money left the student, no ticket exists" — so
orders are never deleted and never mutated into tickets.

**Gateway-neutral naming.** The spec was written around bKash PGW; the
integration is SSLCommerz, which is an aggregator that offers bKash as one
channel among cards and other wallets. Columns are therefore named for the role
they play (`gateway`, `gateway_tran_id`, `gateway_val_id`) rather than for a
provider, because the provider has already changed once.

Money is stored in **paisa** as an integer, never as a float. 100.00 BDT is
10000. Floating-point cannot represent 0.1 exactly, and a rounding error in a
ledger is a bug you find months later in a reconciliation report.
"""

import datetime
import enum
import uuid

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# JSONB in Postgres, where it is queryable and indexable for the reconciler;
# plain JSON everywhere else so the SQLite-backed API tests can still build the
# schema. Without the variant, importing this model breaks every test that
# calls `create_all` against SQLite.
_JSON = JSONB().with_variant(JSON(), "sqlite")


class ProductType(enum.StrEnum):
    single = "single"
    bulk = "bulk"
    package = "package"


class OrderStatus(enum.StrEnum):
    """`initiated` means we created it; `pending` means the gateway has it."""

    initiated = "initiated"
    pending = "pending"
    paid = "paid"
    failed = "failed"
    cancelled = "cancelled"
    refunded = "refunded"


class TicketStatus(enum.StrEnum):
    active = "active"
    exhausted = "exhausted"
    expired = "expired"
    revoked = "revoked"


class TicketProduct(Base):
    """A thing a student can buy. Priced in paisa; `ride_count` null = unlimited."""

    __tablename__ = "ticket_products"

    type: Mapped[ProductType] = mapped_column(
        SAEnum(ProductType, name="product_type"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    price_paisa: Mapped[int] = mapped_column(Integer, nullable=False)
    # Null means unlimited rides for the validity window (a monthly pass).
    ride_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    validity_days: Mapped[int] = mapped_column(Integer, nullable=False, default=30)
    # Null means valid on every route.
    route_scope: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("routes.id", ondelete="SET NULL"), nullable=True
    )
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Order(Base):
    """One purchase attempt. Immutable except for its status and gateway ids.

    `idempotency_key` is unique, which is what stops a double-tapped Buy button
    from creating two orders and charging twice. The uniqueness is enforced by
    the database rather than by a check-then-insert, because two concurrent
    requests would both pass the check.

    `tran_id` is our own reference, sent to the gateway and echoed back. It is
    unique and generated per order rather than reusing the primary key, so the
    id we expose to a third party is not the id we join on internally.
    """

    __tablename__ = "orders"
    __table_args__ = (
        Index("ix_orders_student_created", "student_id", "created_at"),
        # The reconciler's query: everything the gateway has but we have not
        # settled. Partial, so it stays small as paid orders accumulate.
        Index(
            "ix_orders_unsettled",
            "status",
            postgresql_where="status IN ('initiated', 'pending')",
        ),
    )

    student_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("students.id", ondelete="RESTRICT"), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ticket_products.id", ondelete="RESTRICT"), nullable=False
    )
    # Copied from the product at purchase time. A later price change must not
    # rewrite what someone already paid.
    amount_paisa: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="BDT")

    status: Mapped[OrderStatus] = mapped_column(
        SAEnum(OrderStatus, name="order_status"), nullable=False, default=OrderStatus.initiated
    )

    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    tran_id: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)

    gateway: Mapped[str] = mapped_column(String(32), nullable=False, default="sslcommerz")
    # Set once the gateway confirms. `val_id` is what the validation API is
    # called with; `bank_tran_id` is the reference a student sees on a statement.
    gateway_val_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    gateway_bank_tran_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    gateway_card_type: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Whole validation response, kept verbatim for the nightly reconciler and
    # for arguing with a payment provider about what they actually sent.
    raw_payload: Mapped[dict | None] = mapped_column(_JSON, nullable=True)

    paid_at: Mapped[datetime.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Ticket(Base):
    """What a student holds after a confirmed payment.

    One per paid order, enforced by the unique constraint on `order_id`: a
    replayed callback must not mint a second ticket for one payment.

    `qr_secret` is the per-ticket HMAC key for the rotating boarding QR
    (spec §7.2). It is generated here so the column exists from the start, but
    nothing reads it until the boarding flow is built — issuing tickets without
    it would mean a migration over live rows later.
    """

    __tablename__ = "tickets"
    __table_args__ = (Index("ix_tickets_student_status", "student_id", "status"),)

    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("orders.id", ondelete="RESTRICT"),
        unique=True,
        nullable=False,
    )
    student_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("students.id", ondelete="RESTRICT"), nullable=False
    )
    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("ticket_products.id", ondelete="RESTRICT"), nullable=False
    )

    # Null total = unlimited within the validity window.
    rides_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rides_remaining: Mapped[int | None] = mapped_column(Integer, nullable=True)

    valid_from: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    valid_to: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    qr_secret: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[TicketStatus] = mapped_column(
        SAEnum(TicketStatus, name="ticket_status"), nullable=False, default=TicketStatus.active
    )
