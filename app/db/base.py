from sqlmodel import SQLModel

# Import every model module here so SQLModel.metadata is complete for Alembic.
from app.models import user  # noqa: F401

metadata = SQLModel.metadata
