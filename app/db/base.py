from sqlmodel import SQLModel

# Import every model module here so SQLModel.metadata is complete for Alembic.
from app.models import (  # noqa: F401
    admin_audit_log,
    availability_rule,
    booking,
    payment,
    salon,
    service,
    stylist_service,
    user,
)

metadata = SQLModel.metadata
