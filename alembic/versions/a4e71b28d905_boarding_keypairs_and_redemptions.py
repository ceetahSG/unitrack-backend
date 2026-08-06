"""Boarding: per-ticket Ed25519 keypairs, and the redemptions log (spec §7.2).

`qr_secret` becomes a keypair. The spec's formula says HMAC, but §7.2 also says
the helper verifies with the ticket's "public verification material" — and those
cannot both hold, because verifying an HMAC needs the key that signed it. Taken
literally, every helper phone would carry every active ticket's secret and one
stolen phone could forge a QR for the whole fleet. Asymmetric keys are what the
wording actually describes, and they make a stolen manifest worthless.

Tickets already issued are migrated rather than invalidated: each one is given a
freshly generated keypair in place of its secret, so a paid ticket keeps working.

Revision ID: a4e71b28d905
Revises: f2a91c4d70b3
Create Date: 2026-08-06
"""

import base64

import sqlalchemy as sa
from alembic import op
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy.dialects import postgresql

revision: str = "a4e71b28d905"
down_revision: str | None = "f2a91c4d70b3"
branch_labels: str | None = None
depends_on: str | None = None

_TIMESTAMPS = (
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
)


def _keypair() -> tuple[str, str]:
    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption()
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return (
        base64.urlsafe_b64encode(private).decode(),
        base64.urlsafe_b64encode(public).decode(),
    )


def upgrade() -> None:
    # Added nullable so existing rows survive the ALTER, backfilled below, then
    # made NOT NULL. Adding them NOT NULL outright would fail on any table that
    # already has tickets in it.
    op.add_column("tickets", sa.Column("qr_private_key", sa.String(64), nullable=True))
    op.add_column("tickets", sa.Column("qr_public_key", sa.String(64), nullable=True))

    connection = op.get_bind()
    rows = connection.execute(sa.text("SELECT id FROM tickets")).fetchall()
    for (ticket_id,) in rows:
        private, public = _keypair()
        connection.execute(
            sa.text(
                "UPDATE tickets SET qr_private_key = :private, qr_public_key = :public "
                "WHERE id = :id"
            ),
            {"private": private, "public": public, "id": ticket_id},
        )

    op.alter_column("tickets", "qr_private_key", nullable=False)
    op.alter_column("tickets", "qr_public_key", nullable=False)
    op.drop_column("tickets", "qr_secret")

    redemption_flag = postgresql.ENUM(
        "ok", "duplicate_suspect", "invalid", name="redemption_flag"
    )

    op.create_table(
        "redemptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "ticket_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tickets.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        # Null when the helper had no live trip: the boarding still happened and
        # the ride still counts, so refusing it would punish the passenger for
        # the helper forgetting to press Start.
        sa.Column(
            "trip_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("trips.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "helper_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("helpers.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("helper_device_id", sa.String(128), nullable=False),
        sa.Column("nonce", sa.String(64), nullable=False),
        sa.Column("passenger_count", sa.Integer(), nullable=False, server_default="1"),
        # Device clock at the scan; the sync can arrive hours later.
        sa.Column("redeemed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("flag", redemption_flag, nullable=False, server_default="ok"),
        *_TIMESTAMPS,
    )

    # Per device, not global. Two offline phones cannot see each other's nonce
    # logs, so a global unique would reject the sync and destroy the evidence
    # the fraud sweep needs. Duplicates across devices are detected, not blocked.
    op.create_unique_constraint(
        "uq_redemptions_device_nonce", "redemptions", ["helper_device_id", "nonce"]
    )
    op.create_index("ix_redemptions_nonce", "redemptions", ["nonce"])
    op.create_index("ix_redemptions_ticket", "redemptions", ["ticket_id", "redeemed_at"])


def downgrade() -> None:
    op.drop_table("redemptions")
    op.execute("DROP TYPE IF EXISTS redemption_flag")

    op.add_column("tickets", sa.Column("qr_secret", sa.String(64), nullable=True))
    op.execute("UPDATE tickets SET qr_secret = encode(gen_random_bytes(32), 'hex')")
    op.alter_column("tickets", "qr_secret", nullable=False)
    op.drop_column("tickets", "qr_public_key")
    op.drop_column("tickets", "qr_private_key")
