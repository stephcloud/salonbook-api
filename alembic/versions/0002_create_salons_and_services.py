"""create salons and services tables

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-05
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "salons",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("address", sa.String(length=255), nullable=False),
        sa.Column("phone", sa.String(length=30), nullable=False),
        sa.Column(
            "cancellation_hours",
            sa.Integer(),
            server_default=sa.text("24"),
            nullable=False,
        ),
        sa.Column(
            "deposit_amount", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "cancellation_hours >= 0", name="ck_salons_cancellation_hours"
        ),
        sa.CheckConstraint("deposit_amount >= 0", name="ck_salons_deposit_amount"),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_salons_owner_id"), "salons", ["owner_id"])
    op.create_index("ix_salons_created_at_id", "salons", ["created_at", "id"])

    price_type = sa.Enum("fixed", "quote", name="pricetype")
    op.create_table(
        "services",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("salon_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("duration_minutes", sa.Integer(), nullable=False),
        sa.Column("price_type", price_type, nullable=False),
        sa.Column("price", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("duration_minutes > 0", name="ck_services_duration_minutes"),
        sa.CheckConstraint(
            "(price_type = 'quote' AND price IS NULL) "
            "OR (price_type = 'fixed' AND price IS NOT NULL AND price > 0)",
            name="ck_services_price_matches_type",
        ),
        sa.ForeignKeyConstraint(["salon_id"], ["salons.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_services_salon_id_created_at_id",
        "services",
        ["salon_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_services_salon_id_created_at_id", table_name="services")
    op.drop_table("services")
    op.drop_index("ix_salons_created_at_id", table_name="salons")
    op.drop_index(op.f("ix_salons_owner_id"), table_name="salons")
    op.drop_table("salons")
    sa.Enum(name="pricetype").drop(op.get_bind(), checkfirst=True)
