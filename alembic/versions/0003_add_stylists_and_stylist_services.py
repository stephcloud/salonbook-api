"""add stylists and stylist_services

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-05

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stylist_services",
        sa.Column("stylist_id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["service_id"], ["services.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["stylist_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("stylist_id", "service_id"),
    )
    op.create_index(
        op.f("ix_stylist_services_service_id"),
        "stylist_services",
        ["service_id"],
        unique=False,
    )

    # users <-> salons is a circular FK, so salon_id is added after both tables exist.
    op.add_column("users", sa.Column("salon_id", sa.Uuid(), nullable=True))
    op.create_index(op.f("ix_users_salon_id"), "users", ["salon_id"], unique=False)
    op.create_foreign_key(
        "fk_users_salon_id_salons",
        "users",
        "salons",
        ["salon_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    # Autogenerate does not emit this one.
    op.create_check_constraint(
        "ck_users_salon_id_only_for_stylist",
        "users",
        "salon_id IS NULL OR role = 'stylist'",
    )


def downgrade() -> None:
    op.drop_constraint("ck_users_salon_id_only_for_stylist", "users", type_="check")
    op.drop_constraint("fk_users_salon_id_salons", "users", type_="foreignkey")
    op.drop_index(op.f("ix_users_salon_id"), table_name="users")
    op.drop_column("users", "salon_id")
    op.drop_index(op.f("ix_stylist_services_service_id"), table_name="stylist_services")
    op.drop_table("stylist_services")
