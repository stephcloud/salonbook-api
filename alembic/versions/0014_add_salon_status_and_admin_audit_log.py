"""add_salon_status_and_admin_audit_log

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-16 10:05:00.000000

Hand-written. Admin moderation of salons:

- salons.status (enum salonstatus: pending | approved | suspended), NOT NULL. Added with
  DEFAULT 'approved' so every EXISTING salon stays visible and bookable, then the default
  is switched to 'pending' so every NEW salon waits for an admin. A constant default is a
  catalog-only change in Postgres 11+ (no table rewrite).
- ix_salons_status_created_at_id on (status, created_at, id): serves the public list
  (WHERE status = 'approved' ORDER BY created_at, id) and the admin status filter. A plain
  CREATE INDEX: it blocks writes to salons for the moment it takes to build, which is short
  at this table size.
- admin_audit_log: one row per admin action. target_id deliberately has no foreign key so
  the row outlives a deleted salon or user; admin_id is RESTRICT, so an admin who has acted
  can never be deleted. Ids only, no emails or secrets. A CHECK keeps each action paired
  with its own target type. The action and target type are native enums (house style), so
  a new admin action later needs another small migration that adds the enum value.

Migration 0013 must already be applied (the admin role). Deploy order: this runs on
container start, before the new code serves traffic. The old code neither selects nor
inserts salons.status, and the server default fills it, so it keeps working during the
window. lock_timeout caps each lock wait at 5s and fails the deploy (the old version keeps
serving) instead of letting a queue build behind it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SALON_STATUSES = ("pending", "approved", "suspended")
ADMIN_ACTIONS = ("salon_approved", "salon_suspended", "salon_deleted", "user_deleted")
TARGET_TYPES = ("salon", "user")


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("SET LOCAL lock_timeout = '5s'")

    postgresql.ENUM(*SALON_STATUSES, name="salonstatus").create(
        op.get_bind(), checkfirst=True
    )
    op.add_column(
        "salons",
        sa.Column(
            "status",
            postgresql.ENUM(*SALON_STATUSES, name="salonstatus", create_type=False),
            server_default=sa.text("'approved'"),
            nullable=False,
        ),
    )
    # Existing rows now hold 'approved'. From here on, new salons start as 'pending'.
    op.alter_column("salons", "status", server_default=sa.text("'pending'"))
    op.create_index(
        "ix_salons_status_created_at_id",
        "salons",
        ["status", "created_at", "id"],
    )

    op.create_table(
        "admin_audit_log",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("admin_id", sa.Uuid(), nullable=False),
        sa.Column(
            "action", sa.Enum(*ADMIN_ACTIONS, name="adminaction"), nullable=False
        ),
        sa.Column(
            "target_type",
            sa.Enum(*TARGET_TYPES, name="audittargettype"),
            nullable=False,
        ),
        sa.Column("target_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(action IN ('salon_approved', 'salon_suspended', 'salon_deleted') "
            "AND target_type = 'salon') "
            "OR (action = 'user_deleted' AND target_type = 'user')",
            name="ck_admin_audit_log_action_matches_target",
        ),
        sa.ForeignKeyConstraint(["admin_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_admin_audit_log_admin_id"),
        "admin_audit_log",
        ["admin_id"],
        unique=False,
    )
    op.create_index(
        "ix_admin_audit_log_target",
        "admin_audit_log",
        ["target_type", "target_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index("ix_admin_audit_log_target", table_name="admin_audit_log")
    op.drop_index(op.f("ix_admin_audit_log_admin_id"), table_name="admin_audit_log")
    op.drop_table("admin_audit_log")
    # drop_table leaves the enum types behind; a later upgrade would fail without this.
    sa.Enum(name="audittargettype").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="adminaction").drop(op.get_bind(), checkfirst=True)

    op.drop_index("ix_salons_status_created_at_id", table_name="salons")
    op.drop_column("salons", "status")
    sa.Enum(name="salonstatus").drop(op.get_bind(), checkfirst=True)
