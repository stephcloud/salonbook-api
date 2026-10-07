FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /code

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Migrate, then serve. Render's free tier has no Pre-Deploy Command, so the container does
# it on start; if a migration fails the container exits and the deploy fails before it
# takes traffic. `exec` hands the process to uvicorn so it receives Render's SIGTERM.
# docker-compose.yml overrides this for local dev (--reload, no auto-migrate).
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
