# Project Context

<!-- EDIT PER PROJECT: fill in this section, leave the rest as is. -->
## Project
- Name: <project>-api
- Purpose: <one or two lines>
- Frontend repo: <project>-web (Next.js on Vercel)
- Deployed on: Render (auto-deploys from `main`)

## Data model
<!-- List main tables and relationships, e.g. users 1-N bookings -->

## Endpoints
<!-- List planned/existing routes, e.g. POST /api/v1/users -->

## Environment variables (names only, never values)
`DATABASE_URL`, `REDIS_URL`, `SECRET_KEY`, `CORS_ORIGINS`, `PORT`

## Deployment
- Backend on Render; start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Frontend on Vercel; add its production URL to `CORS_ORIGINS`
- Render free tier sleeps when idle; expect a cold start

---

## Stack
Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2.0 (async), Alembic, PostgreSQL, Redis, pytest.

## Folder structure
```
app/
├── main.py            # app creation, middleware, router includes
├── core/              # config.py (settings), security.py, redis.py
├── db/                # session.py, base.py
├── models/            # SQLAlchemy models
├── schemas/           # Pydantic schemas
├── api/v1/            # routers (thin: validate, call service, return)
├── services/          # business logic
└── tests/             # mirrors app/ structure
alembic/               # migrations
```

## Naming conventions
- Python files, functions, variables: `snake_case`. Classes and Pydantic schemas: `PascalCase` (`UserCreate`, `UserResponse`).
- Constants and env vars: `UPPER_SNAKE_CASE`.
- Routes: plural nouns, kebab-case, versioned: `/api/v1/booking-slots`.
- DB tables: plural snake_case (`users`). Columns: singular snake_case (`created_at`).
- Migrations: descriptive, e.g. `add_avatar_url_to_users`.
- Branches: `feature/...`, `fix/...`, `chore/...`. Commits: Conventional Commits (`feat:`, `fix:`, `docs:`).

## Code rules
- Routers stay thin; business logic goes in `services/`.
- Type hints everywhere. Separate Pydantic schemas for create, update, and response.
- Never return password hashes or secrets in responses.
- Use dependency injection (`Depends`) for DB sessions and auth.
- Config only through `core/config.py` (pydantic-settings); never read `os.environ` elsewhere.
- Every endpoint gets at least a success test and a failure test.
- Redis: use for caching, rate limiting, sessions; always set a TTL.
- Profile images and files go to object storage (S3/Cloudinary). Store only the URL or key in Postgres.

## Rules for Claude
- Never read or edit `.env`. Update `.env.example` when adding a variable.
- Never edit an existing Alembic migration that has been committed; create a new one.
- Never run destructive DB commands (drop, truncate, downgrade base) without asking.
- Run `/test` before declaring a task done.
- Use the `code-reviewer` subagent after significant changes and `db-reviewer` after model or migration changes.
- Ask before adding new dependencies.
