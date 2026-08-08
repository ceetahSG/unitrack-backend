"""helpers.approved_by releases its reference when the approver is deleted.

The column was a plain FK to users.id with no ON DELETE, so every helper an
admin had ever approved held that admin's row hostage: deleting the account
raised ForeignKeyViolationError. `scripts/seed.py ... --wipe` hit this the
moment any helper had been approved, which on a database that has seen any use
is always — and the failure looked like a broken seed script rather than a
schema problem.

SET NULL rather than CASCADE, emphatically. CASCADE here would mean deleting an
administrator silently deletes every helper they ever approved, taking their
trips and GPS history with them. The approval is a fact about the helper, not a
possession of the admin; when the approver's account goes, the correct record
is that the helper was approved by someone no longer present.

Revision ID: e9c3a7b41f26
Revises: d5b2f7c94e18
Create Date: 2026-08-05
"""

from alembic import op

revision: str = "e9c3a7b41f26"
down_revision: str | None = "d5b2f7c94e18"
branch_labels: str | None = None
depends_on: str | None = None

_CONSTRAINT = "helpers_approved_by_fkey"


def upgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "helpers", type_="foreignkey")
    op.create_foreign_key(
        _CONSTRAINT,
        "helpers",
        "users",
        ["approved_by"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "helpers", type_="foreignkey")
    op.create_foreign_key(_CONSTRAINT, "helpers", "users", ["approved_by"], ["id"])
