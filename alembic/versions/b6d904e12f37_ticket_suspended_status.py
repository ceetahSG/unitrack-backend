"""Tickets can be suspended pending review (spec §7.5).

The fraud sweep needs somewhere to put a ticket whose boarding code turned up
on several devices. `revoked` is too strong: a duplicate is suspicious, not
proven — a helper can scan the same screen twice when the first attempt looks
like it failed — and revoking would destroy a paid ticket on a suspicion.

Revision ID: b6d904e12f37
Revises: a4e71b28d905
Create Date: 2026-08-06
"""

from alembic import op

revision: str = "b6d904e12f37"
down_revision: str | None = "a4e71b28d905"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Postgres cannot add an enum value inside a transaction block before 12,
    # and alembic runs migrations in one; IF NOT EXISTS keeps a re-run safe.
    op.execute("ALTER TYPE ticket_status ADD VALUE IF NOT EXISTS 'suspended'")


def downgrade() -> None:
    # Postgres cannot drop a value from an enum. Anything already suspended
    # would have no valid state to fall back to, so this is deliberately a
    # one-way migration rather than a lossy rewrite of live rows.
    pass
