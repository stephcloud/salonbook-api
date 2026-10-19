import uuid
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    String,
    text,
)
from sqlmodel import Field, SQLModel


class AdminAction(StrEnum):
    SALON_APPROVED = "salon_approved"
    SALON_SUSPENDED = "salon_suspended"
    SALON_DELETED = "salon_deleted"
    USER_DELETED = "user_deleted"


class AuditTargetType(StrEnum):
    SALON = "salon"
    USER = "user"


class AdminAuditLog(SQLModel, table=True):
    """One row per admin action, written in the same transaction as the action.

    Ids only: no emails, names or secrets. target_id has no foreign key on purpose, so the
    row outlives a deleted salon or user. `reason` is free text typed by the admin.
    """

    __tablename__ = "admin_audit_log"
    __table_args__ = (
        CheckConstraint(
            "(action IN ('salon_approved', 'salon_suspended', 'salon_deleted') "
            "AND target_type = 'salon') "
            "OR (action = 'user_deleted' AND target_type = 'user')",
            name="ck_admin_audit_log_action_matches_target",
        ),
        Index("ix_admin_audit_log_target", "target_type", "target_id"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    admin_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
        )
    )
    action: AdminAction = Field(
        sa_column=Column(
            Enum(
                AdminAction,
                name="adminaction",
                values_callable=lambda e: [m.value for m in e],
            ),
            nullable=False,
        )
    )
    target_type: AuditTargetType = Field(
        sa_column=Column(
            Enum(
                AuditTargetType,
                name="audittargettype",
                values_callable=lambda e: [m.value for m in e],
            ),
            nullable=False,
        )
    )
    target_id: uuid.UUID
    reason: str | None = Field(
        default=None, sa_column=Column(String(500), nullable=True)
    )
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(
            DateTime(timezone=True), server_default=text("now()"), nullable=False
        ),
    )
