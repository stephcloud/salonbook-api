# SalonBook API

Backend for SalonBook, a salon booking MVP that takes a deposit at booking time to reduce no-shows.

- **Live API:** https://salonbook-api-etyz.onrender.com
- **Interactive docs:** https://salonbook-api-etyz.onrender.com/docs
- **Health check:** https://salonbook-api-etyz.onrender.com/health
- **Frontend repo:** `salonbook-web` (Next.js, Vercel)
- **Frontend / live site:** https://salonbook-web-theta.vercel.app

> The live service runs on Render's free tier, so the first request after a period of inactivity can take about 50 seconds.

## Stack

FastAPI, SQLModel, Alembic, PostgreSQL, Redis, Paystack (test mode only), Docker. Deployed on Render (Docker, Frankfurt).

## What it does

- Registration and login for clients and salon owners (Argon2 password hashing, JWT with 30-minute expiry). Stylists are created by salon owners.
- Salons, services, stylists, and weekly availability (working hours, breaks, days off).
- A slots endpoint that computes bookable times from availability, minus breaks, days off and existing bookings.
- Bookings that start as `pending`, expire after 15 minutes, and are capped at 3 pending per client.
- Deposit payment through Paystack, confirmed by a signed webhook.
- Cancellation with a refund policy, and refunds sent to Paystack with retry.

All money values are integers in **kobo** (divide by 100 for naira). Times are stored in UTC and the salon timezone is fixed at WAT (UTC+1).

## The three hard parts

1. **No double-booking.** A PostgreSQL exclusion constraint (`btree_gist` on stylist and time range, `[)` bounds) guarantees two active bookings can never overlap, even under concurrent requests. A concurrency test forces two requests past the application check at the same moment, and a mutation check proves the constraint, not timing, decides the winner.
2. **Cancellation and refund policy.** A pure function decides if a refund is due: strictly more than the salon's cancellation window before the start. Salon-owner cancellations always refund. Cancelling twice is safe (idempotent).
3. **Idempotent payments.** The Paystack webhook verifies the `x-paystack-signature` (HMAC-SHA512 on the raw body), locks the booking and payment rows, and acts only on a valid status transition, so a replayed webhook changes state once. A late `charge.success` for a cancelled or expired booking is refunded in full and never revives the booking. No database lock is held during any call to Paystack.

## Booking and payment flow

1. `GET /api/v1/stylists/{id}/slots?service_id=...&date=YYYY-MM-DD`
2. `POST /api/v1/bookings` (client). Creates a pending booking.
3. `POST /api/v1/bookings/{id}/pay` (client). Returns a Paystack `authorization_url`.
4. The client pays on Paystack. Paystack calls `POST /api/v1/payments/webhook`.
5. `GET /api/v1/bookings/{id}` shows `confirmed` and the payment status.
6. `POST /api/v1/bookings/{id}/cancel` moves a paid deposit to `refund_pending`, then `refunded`.

## Endpoints (all under `/api/v1`)

| Area | Endpoints |
|---|---|
| auth | `POST /auth/register`, `POST /auth/login`, `GET /auth/me` |
| salons and services | CRUD, owner only for writes, public paginated reads |
| my salons | `GET /salons/mine` (owner only: 401 without a token, 403 for other roles) |
| stylists | created by the salon owner, service assignment |
| availability | owner sets rules, `GET` reads them |
| slots | `GET /stylists/{id}/slots` (any signed-in user; 401 without a token) |
| bookings | `POST /bookings`, `GET /bookings`, `GET /bookings/{id}`, `POST /bookings/{id}/pay`, `POST /bookings/{id}/cancel` |
| payments | `POST /payments/webhook` (public, authenticated by signature) |

`GET /bookings` and `GET /bookings/{id}` also return nested `salon` (id, name, address, phone, cancellation_hours, deposit_amount, image_url), `service` (id, name, duration_minutes, price_type, price) and `stylist` (id, name, image_url) summaries, loaded in one joined query. No emails or Paystack fields are included.

See `/docs` for exact request and response schemas.

## Run locally

Requires Python, Docker and a virtual environment.

```bash
cp .env.example .env          # then fill in real values locally; never commit .env
docker compose up -d db redis
pip install -r requirements.txt
alembic upgrade head
uvicorn app.main:app --reload
```

Postgres is mapped to host port **5433** (to avoid clashing with a local Postgres on 5432).

### Environment variables

| Variable | Notes |
|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://...` |
| `REDIS_URL` | `redis://...` |
| `SECRET_KEY` | 32+ characters outside `local` and `test` |
| `ENVIRONMENT` | `local`, `test` or `production` |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | default 30 |
| `CORS_ORIGINS` | one origin, comma-separated list, or JSON list. No `*`, no trailing slash |
| `SALON_UTC_OFFSET_MINUTES` | `60` for WAT |
| `PAYSTACK_SECRET_KEY` | test key (`sk_test_...`); required in production |

## Tests

```bash
pytest app/tests
```

463 tests, run against a real PostgreSQL database (`salonbook_test`). The full suite takes about 12 minutes because the schema is rebuilt for each test. Run a single file while developing, for example `pytest app/tests/test_bookings_api.py -x -q`, and run only one test session at a time, since they share the test database.

Lint and format: `ruff check .` and `ruff format --check .` (do not auto-fix `app/db/base.py`).

## Live smoke test

`scripts/smoke_live.py` runs the full flow against a deployed API: it registers throwaway accounts, creates a salon, books a slot, and prints a Paystack test checkout link. Pay with the Paystack test option, press Enter, and it checks the booking is `confirmed`, cancels it, and confirms the refund reaches `refunded`.

```bash
python scripts/smoke_live.py --base-url https://salonbook-api-etyz.onrender.com/api/v1
```

## Deployment

Render web service (Docker, Frankfurt, free tier) with Render Postgres and Key Value (Redis). The Dockerfile runs `alembic upgrade head` on container start, then uvicorn on `$PORT`. Merging to `main` redeploys automatically. Set the Paystack test-mode webhook URL to `https://<service>.onrender.com/api/v1/payments/webhook`.

## Security

Argon2 password hashing, JWT auth, ownership checks on every route, strict CORS, webhook signature verification, idempotent webhook handling, startup guards against weak or missing secrets in production, and `pip-audit` clean as of 8 Oct 2026 (covers `requirements.txt` only). Paystack is used in **test mode only**.

## Roadmap (not in the MVP)

Security headers, login rate limiting, per-salon timezones, salon owner verification, stylist invite links, email verification and password reset, and a faster test setup. See `LOG.md` for the full list.