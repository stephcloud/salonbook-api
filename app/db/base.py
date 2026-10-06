from sqlmodel import SQLModel

# Import every model module here so SQLModel.metadata is complete for Alembic.
from app.models import (  # noqa: F401
    availability_rule,
    booking,
    salon,
    service,
    stylist_service,
    user,
)

metadata = SQLModel.metadata
