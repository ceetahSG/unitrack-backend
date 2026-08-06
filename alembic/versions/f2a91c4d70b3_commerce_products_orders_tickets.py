"""Commerce: ticket products, orders, tickets (spec §6).

Money is stored in paisa as an integer. A NUMERIC would also be correct, but a
float never is, and integers make the "never store money in a float" rule
impossible to break by accident later.

Column names are gateway-neutral rather than the spec's `bkash_*`: the
integration is SSLCommerz, an aggregator offering bKash as one channel, and the
provider has already changed once.

Revision ID: f2a91c4d70b3
Revises: e9c3a7b41f26
Create Date: 2026-08-05
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "f2a91c4d70b3"
down_revision: str | None = "e9c3a7b41f26"
branch_labels: str | None = None
depends_on: str | None = None

_TIMESTAMPS = (
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
)


def upgrade() -> None:
    product_type = postgresql.ENUM("single", "bulk", "package", name="product_type")
    order_status = postgresql.ENUM(
        "initiated", "pending", "paid", "failed", "cancelled", "refunded", name="order_status"
    )
    ticket_status = postgresql.ENUM(
        "active", "exhausted", "expired", "revoked", name="ticket_status"
    )

    op.create_table(
        "ticket_products",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("type", product_type, nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("price_paisa", sa.Integer(), nullable=False),
        sa.Column("ride_count", sa.Integer(), nullable=True),
        sa.Column("validity_days", sa.Integer(), nullable=False, server_default="30"),
        sa.Column(
            "route_scope",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("routes.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        *_TIMESTAMPS,
    )

    op.create_table(
        "orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "student_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("students.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "product_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ticket_products.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("amount_paisa", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False, server_default="BDT"),
        sa.Column("status", order_status, nullable=False, server_default="initiated"),
        # Unique in the database, not checked in Python: two concurrent requests
        # would both pass a check-then-insert and charge the student twice.
        sa.Column("idempotency_key", sa.String(64), nullable=False, unique=True),
        sa.Column("tran_id", sa.String(64), nullable=False, unique=True),
        sa.Column("gateway", sa.String(32), nullable=False, server_default="sslcommerz"),
        sa.Column("gateway_val_id", sa.String(128), nullable=True),
        sa.Column("gateway_bank_tran_id", sa.String(128), nullable=True),
        sa.Column("gateway_card_type", sa.String(64), nullable=True),
        sa.Column("raw_payload", postgresql.JSONB(), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        *_TIMESTAMPS,
    )
    op.create_index("ix_orders_student_created", "orders", ["student_id", "created_at"])
    op.create_index("ix_orders_tran_id", "orders", ["tran_id"])
    # Partial: the reconciler only ever asks for orders that have not settled,
    # and this stays small while paid rows accumulate forever.
    op.create_index(
        "ix_orders_unsettled",
        "orders",
        ["status"],
        postgresql_where=sa.text("status IN ('initiated', 'pending')"),
    )

    op.create_table(
        "tickets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # Unique: a replayed payment callback must not mint a second ticket.
        sa.Column(
            "order_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orders.id", ondelete="RESTRICT"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "student_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("students.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "product_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ticket_products.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("rides_total", sa.Integer(), nullable=True),
        sa.Column("rides_remaining", sa.Integer(), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=False),
        sa.Column("qr_secret", sa.String(64), nullable=False),
        sa.Column("status", ticket_status, nullable=False, server_default="active"),
        *_TIMESTAMPS,
    )
    op.create_index("ix_tickets_student_status", "tickets", ["student_id", "status"])


def downgrade() -> None:
    op.drop_table("tickets")
    op.drop_table("orders")
    op.drop_table("ticket_products")
    for name in ("ticket_status", "order_status", "product_type"):
        op.execute(f"DROP TYPE IF EXISTS {name}")
