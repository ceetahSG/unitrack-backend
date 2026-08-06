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

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
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


class RedemptionFlag(enum.StrEnum):
    """How much a recorded boarding should be trusted.

    `ok` is the ordinary case. `duplicate_suspect` is what offline validation
    inevitably produces: two helper devices, neither able to see the other's
    nonce log, both accept the same QR. Cryptography cannot prevent that — only
    detection after the fact can, which is why the flag exists rather than a
    hard rejection that would break offline boarding entirely.
    """

    ok = "ok"
    duplicate_suspect = "duplicate_suspect"
    invalid = "invalid"


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

    Each ticket carries its own **Ed25519 keypair** for the rotating boarding
    QR (spec §7.2). The student's device signs with the private key; the helper
    verifies with the public key, which is all their offline manifest carries.

    This is why it is a keypair and not the HMAC secret the spec's formula
    literally describes: verifying an HMAC needs the same key that signs it, so
    every helper phone would have to hold every active ticket's secret, and one
    stolen helper phone could forge a QR for the entire fleet. §7.2 calls the
    synced material "public verification material", which only makes sense
    asymmetrically. A stolen manifest is now worth nothing.

    The private key is stored so a student signing in on a new device can
    re-sync it. Losing it would strand a paid ticket on a cleared cache.
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

    # Raw 32-byte Ed25519 keys, base64url encoded. Raw rather than PEM so the
    # QR payload and the mobile clients stay small and format-agnostic.
    qr_private_key: Mapped[str] = mapped_column(String(64), nullable=False)
    qr_public_key: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[TicketStatus] = mapped_column(
        SAEnum(TicketStatus, name="ticket_status"), nullable=False, default=TicketStatus.active
    )


class Redemption(Base):
    """One boarding recorded against a ticket (spec §6, §7.2).

    Written when a helper's device syncs, which may be long after the boarding
    happened — a bus can finish a route with no signal. So `redeemed_at` is the
    **device's** clock at the moment of scanning and `synced_at` is when the
    server heard about it; conflating them would make every offline trip look
    like it happened at the depot.

    The nonce is unique **per device**, which is the strongest guarantee a
    device can offer alone: its own SQLite log stops the same QR being scanned
    twice on that phone. Two different phones cannot see each other's logs
    while offline, so global uniqueness is deliberately *not* a constraint here
    — it is detected afterwards by the fraud sweep and recorded as
    `duplicate_suspect`. A unique index would instead reject the sync outright
    and lose the evidence.
    """

    __tablename__ = "redemptions"
    __table_args__ = (
        # The fraud sweep's query: the same nonce seen on more than one device.
        Index("ix_redemptions_nonce", "nonce"),
        # A device re-syncing a batch must not double-record its own scans.
        UniqueConstraint("helper_device_id", "nonce", name="uq_redemptions_device_nonce"),
        Index("ix_redemptions_ticket", "ticket_id", "redeemed_at"),
    )

    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tickets.id", ondelete="RESTRICT"), nullable=False
    )
    # Null when the helper had no live trip — the boarding still happened and
    # the ride still counts, so refusing it would punish the passenger for the
    # helper forgetting to press Start.
    trip_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("trips.id", ondelete="SET NULL"), nullable=True
    )
    helper_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("helpers.id", ondelete="RESTRICT"), nullable=False
    )
    helper_device_id: Mapped[str] = mapped_column(String(128), nullable=False)

    nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    passenger_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    redeemed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    synced_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    flag: Mapped[RedemptionFlag] = mapped_column(
        SAEnum(RedemptionFlag, name="redemption_flag"),
        nullable=False,
        default=RedemptionFlag.ok,
    )
