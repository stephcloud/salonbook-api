from sqlmodel import SQLModel

# Import every model module here so SQLModel.metadata is complete for Alembic.
# e.g. from app.models import user

metadata = SQLModel.metadata
