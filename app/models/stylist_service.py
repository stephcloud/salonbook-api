import uuid

from sqlalchemy import Column, ForeignKey
from sqlmodel import Field, SQLModel


class StylistService(SQLModel, table=True):
    __tablename__ = "stylist_services"

    stylist_id: uuid.UUID = Field(
        sa_column=Column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    )
    service_id: uuid.UUID = Field(
        sa_column=Column(
            ForeignKey("services.id", ondelete="CASCADE"),
            primary_key=True,
            index=True,
        )
    )
