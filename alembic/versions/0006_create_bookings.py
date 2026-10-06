"""create_bookings

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-06 10:00:00.000000

Hand-written: autogenerate cannot produce the btree_gist extension or the
exclusion constraint. The constraint is the database-level guarantee that two
active (pending/confirmed) bookings for one stylist never overlap (hard part #1).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Lets a GiST index combine `stylist_id WITH =` with a range `WITH &&`.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.create_table(
        "bookings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("stylist_id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "confirmed",
                "cancelled",
                "completed",
                "no_show",
                name="bookingstatus",
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("ends_at > starts_at", name="ck_bookings_end_after_start"),
        sa.ForeignKeyConstraint(["client_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["service_id"], ["services.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["stylist_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    # Active = pending or confirmed. [) bounds: back-to-back bookings are allowed.
    op.execute(
        "ALTER TABLE bookings ADD CONSTRAINT ex_bookings_stylist_id_no_overlap "
        "EXCLUDE USING gist (stylist_id WITH =, tstzrange(starts_at, ends_at, '[)') WITH &&) "
        "WHERE (status IN ('pending', 'confirmed'))"
    )
    op.create_index(
        op.f("ix_bookings_client_id"), "bookings", ["client_id"], unique=False
    )
    op.create_index(
        "ix_bookings_stylist_id_starts_at",
        "bookings",
        ["stylist_id", "starts_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_bookings_stylist_id_starts_at", table_name="bookings")
    op.drop_index(op.f("ix_bookings_client_id"), table_name="bookings")
    op.drop_table("bookings")  # also drops the exclusion constraint
    # The btree_gist extension is left installed: other objects may rely on it.
    # drop_table leaves the enum type behind; a later upgrade would fail without this.
    sa.Enum(name="bookingstatus").drop(op.get_bind(), checkfirst=True)
