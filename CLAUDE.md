# Project Context

## Project
- Name: salonbook-api
- Purpose: Backend for SalonBook. Clients book a stylist and pay a deposit to confirm, so salon no-shows stop costing money.
- Frontend repo: salonbook-web (Next.js on Vercel)
- Deployed on: Render (auto-deploys from `main`)
- Two-week MVP: auth with roles + ownership checks, 5-7 tables, migrations + seed script, ONE hard part with 3 tests, 15+ tests total. Log progress in `LOG.md` daily.

## The hard part (build and test this first)
1. **No overlapping bookings, enforced in the database.** Variable service durations. Use a Postgres exclusion constraint on (`stylist_id`, time range) in a hand-written Alembic migration (needs `btree_gist`). Only pending and confirmed bookings block a slot.
2. **Deposit and refund policy applied exactly once.** Full refund if cancelled more than 24h before start, none after (hours configurable per salon).
3. **Idempotent payments.** Unique Paystack reference; replaying the same webhook must not double-confirm or double-refund.

Three tests prove it: (a) two concurrent bookings for one slot, only one succeeds; (b) refund boundary at exactly 24h; (c) same webhook replayed twice changes state once.

## Data model (7 tables)
- `users`: id, name, email, password_hash, role (owner | stylist | client), salon_id (stylists only), image_url (nullable; a stylist's photo)
- `salons`: id, owner_id, name, address, phone, cancellation_hours (default 24), deposit_amount, image_url (nullable)
- `services`: id, salon_id, name, duration_minutes, price_type (fixed | quote), price (nullable when quote)
- `stylist_services`: stylist_id, service_id
- `availability_rules`: id, stylist_id, weekday, start_time, end_time, plus breaks and days off
- `bookings`: id, client_id, stylist_id, service_id, starts_at, ends_at, status (pending | confirmed | cancelled | completed | no_show), exclusion constraint on active bookings
- `payments`: id, booking_id, paystack_reference (unique), amount, status (pending | paid | refunded), refunded_at

Pricing note: some salons only quote the final price after seeing the client's hair in person. The deposit is always a fixed amount per salon; the full price may be `quote`.

## Endpoints (all under /api/v1)
- Auth: POST /auth/register, POST /auth/login, GET /auth/me
- Salons: POST/GET /salons, GET/PATCH /salons/{id} (owner only for writes); image_url is optional, https:// only, max 500 chars, and PATCH with `null` clears it. The public stylist list returns only id, name, service_ids and image_url.
- Salon bookings: GET /salons/{id}/bookings (that salon's owner only: 403 anyone else, 404 unknown salon; paginated, ordered by starts_at; filters status, stylist_id, from/to as salon-local dates, inclusive; includes cancelled; returns id, status, starts_at, ends_at, refund_due, payment status, stylist (id, name), service (id, name, duration) and the client's name only, never email or Paystack fields)
- Services: POST/GET /salons/{id}/services, PATCH/DELETE /services/{id}
- Stylists: POST /salons/{id}/stylists (optional image_url; there is no stylist update endpoint yet), PUT /stylists/{id}/services, PUT /stylists/{id}/availability
- Availability: GET /stylists/{id}/slots?service_id=&date=
- Bookings: POST /bookings (clients only; re-checks the slot, creates a pending booking, caps a client at 3 unexpired pending bookings, cancels the stylist's lapsed pending bookings in the same transaction; Paystack init and the payments row come in the payments step), GET /bookings (the current client's own, paginated, newest start first), GET /bookings/{id} (the booking's client or its salon's owner; 404 unknown, 403 anyone else; returns status, refund_due, starts_at and the latest payment's status and amount, both null if none), POST /bookings/{id}/pay, POST /bookings/{id}/cancel, POST /bookings/{id}/reschedule
- Payments: POST /payments/webhook (Paystack, verify signature)

## Environment variables (names only, never values)
`DATABASE_URL`, `REDIS_URL`, `SECRET_KEY`, `CORS_ORIGINS`, `PORT`, `PAYSTACK_SECRET_KEY`, `RESEND_API_KEY`

## Deployment
- Backend on Render (Docker runtime). The container's start command is the Dockerfile `CMD`: `alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port $PORT`. Render's free tier has no Pre-Deploy Command, so migrations run on container start; leave Render's "Docker Command" setting empty so it doesn't override the `CMD`. A failed migration fails the deploy. If the service moves to a paid plan, switch to a Pre-Deploy Command of `alembic upgrade head` and drop it from the `CMD`.
- Local dev with Docker Compose (api + postgres + redis)
- Add the Vercel URL to `CORS_ORIGINS`
- Paystack in TEST mode only; webhook URL points to the Render service
- Render free tier sleeps when idle; expect a cold start

---

## Stack
Python 3.13, FastAPI, SQLModel (on SQLAlchemy 2.0), Alembic, PostgreSQL, Redis, pytest, Docker Compose. Integrations: Paystack (test mode), Resend (email).

## Folder structure
```
app/
├── main.py            # app creation, middleware, router includes
├── core/              # config.py (settings), security.py, redis.py
├── db/                # session.py, base.py
├── models/            # SQLModel models (table=True)
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


## Security rules (always follow)
- Every endpoint is protected by default. A route is public only if the task says so.
- Every route that takes a resource ID must check ownership or role before reading or changing it. Add a test where user A tries to access user B's data.
- Passwords: Argon2 via pwdlib[argon2]. Never log or return passwords, hashes, tokens, or API keys.
- JWTs: PyJWT, short expiry (exp), signed with SECRET_KEY from settings. Login errors are generic ("invalid credentials").
- Public registration allows only client and owner. Stylists are created by a salon owner.
- Validate all input with Pydantic (types, lengths, enums, EmailStr). Response models must never include password_hash.
- Never build SQL from strings. Use SQLModel/SQLAlchemy queries only.
- CORS: explicit allow-list from CORS_ORIGINS, never *.
- Paystack: verify x-paystack-signature on the raw body, handle webhooks idempotently, take amounts from the database (never from the request).
- Unpaid pending bookings expire after 15 minutes.
- Do not add dependencies without asking. Run pip-audit before each deploy.
- Use the security-reviewer subagent after auth, after each new resource, and after payments.

## Rules for Claude
- Never read or edit `.env`. Update `.env.example` when adding a variable.
- Never edit an existing Alembic migration that has been committed; create a new one.
- Never run destructive DB commands (drop, truncate, downgrade base) without asking.
- Run `/test` before declaring a task done.
- Use the `code-reviewer` subagent after significant changes and `db-reviewer` after model or migration changes.
- Ask before adding new dependencies.
- Never put payment or booking-state logic in routers; keep it in `services/` so the hard-part tests can call it directly.
- Payment and refund changes must be idempotent: check current status before changing it, inside a transaction.
- Keep the `# noqa: F401` on the model imports in `app/db/base.py`. Without it, ruff removes them and Alembic autogenerate sees empty metadata.
- Never run `ruff --fix` (or `ruff check --fix`) on `app/db/base.py`. Exclude that file when running it on the project.
- Show the user every migration (autogenerated or hand-written) for review before applying it.
- Be suspicious of any migration that drops tables: it usually means the metadata is wrong, not that the tables should go.