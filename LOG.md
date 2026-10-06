# Build Log

<!-- Template: copy this block for each new day.

## Day N
- **Did:**
- **Learned:**
- **Stuck on:**
- **Next:**

-->

## Day 3
- **Did:** Scaffolded the FastAPI project, 2 tests passing, requirements pinned.
- **Learned:**
- **Stuck on:**
- **Next:**

## Day 4
- **Did:** Built auth on `feature/auth` (uncommitted): `User` model + migration `0001`, register/login/me, `get_current_user` and `require_role`, Argon2 (pwdlib) + PyJWT (30 min), generic login errors, SECRET_KEY startup guard (fails closed unless ENVIRONMENT is local/test), CORS allow-list validation. Security, DB and code reviews run and their fixes applied. 36 tests written.
- **Learned:** Two Postgres servers listen on port 5432 here (a native Windows service and the Docker `db` container), so `localhost` reaches the wrong one and fails password auth.
- **Stuck on:** 26 DB-backed tests can't run yet (10 non-DB tests pass). Migration `0001` not yet run against a real DB.
- **Next:** Resolve the port clash, run the full suite, add a migration-vs-models test, add login/register rate limiting (Redis) before deploy, then commit.

## Day 5
- **Did:** Added `Salon` and `Service` resources on `feature/salons`: models, schemas, services, routers (`/salons`, `/salons/{id}/services`, `/services/{id}`), owner-only writes with 403 for non-owners, public paginated GETs (limit default 20, max 100), `price_type` rule (quote => null price, fixed => price) in schema, service layer and a DB CHECK. Hand-written migration `0002`. 35 new tests (71 total passing).
- **Learned:** my `.env` pointed to port 5432, which hits the Windows Postgres service. The Docker database is on 5433, so I fixed `.env` to use 5433 and the tests pass.
- **Stuck on:** migration `0002` is not applied to a live database yet. Autogenerate needs a DB at `0001`, so it was hand-written and checked with `alembic upgrade 0001:0002 --sql`.
- **Next:** run `alembic upgrade head`, then stylists + availability, then bookings and the hard part.
## Day 6
- **Did:** Added stylists: `POST /salons/{id}/stylists` (owner creates a stylist user), `PUT /stylists/{id}/services` (replace set, services must belong to the stylist's salon), public paginated `GET /salons/{id}/stylists` (id, name, service_ids only; no email). `users.salon_id` + CHECK (only stylists may have it), `stylist_services` table. 21 new tests incl. ownership (92 total passing). Security, DB, code reviews run; applied a row lock on the services replace and de-duplicated `email_taken`.
- **Learned:** `users.salon_id -> salons` and `salons.owner_id -> users` is a circular FK; it needs `use_alter` and a constraint name or `create_all`/`drop_all` fail. The test database also had stale tables from before the new FK, so I had to reset `salonbook_test`.
- **Stuck on:** nothing now. Migration `0003` is applied (checked upgrade, downgrade, upgrade again, and `alembic check`). Still to verify: the CHECK constraint exists on the dev DB.
- **Next:** availability rules (6.3), then bookings and the hard part.

## Day 7
- **Did:** Availability rules (working windows, breaks, days off, `GET/PUT /stylists/{id}/availability`). Then `GET /stylists/{id}/slots?service_id=&date=`: the logic is in `services/slots.py` (a pure `compute_slots` plus `get_slots`), it uses the service's own duration, takes away breaks, days off and active bookings, offers starts every 15 min, drops past times, and returns salon-local ISO datetimes. Added the `bookings` table with a hand-written migration `0006` and the hard-part exclusion constraint: `EXCLUDE USING gist (stylist_id WITH =, tstzrange(starts_at, ends_at, '[)') WITH &&) WHERE (status IN ('pending','confirmed'))`, plus `btree_gist` and `updated_at`. Tests for slots, the constraint (5 concurrent inserts for one slot: 1 wins) and `updated_at`; 180 total passing. Security, DB and code reviews run and their fixes applied.
- **Learned:** `tzdata` isn't installed, so `zoneinfo` can't find Africa/Lagos; I used a fixed `SALON_UTC_OFFSET_MINUTES` (default 60) instead of adding a dependency. `alembic check` doesn't compare exclusion constraints, so I built the table from the model in a second scratch DB and compared `pg_get_constraintdef` by hand. Ignoring F401 per file in `ruff.toml` makes ruff flag the `# noqa: F401` as unused (RUF100), so both rules are ignored for `base.py`. `updated_at` is set by SQLAlchemy (`onupdate`), not by a DB trigger, so raw SQL edits won't bump it.
- **Stuck on:** nothing now. Migration `0006` was checked on a scratch DB (upgrade, downgrade -1, upgrade, `alembic check`) and is applied to dev.
- **Next:** `POST /bookings` (re-check the slot server-side, catch the constraint's `IntegrityError` and return 409), the 15-minute expiry job that cancels lapsed pending bookings, the refund-policy and webhook-idempotency tests, then Paystack. Later: fix `DELETE /services/{id}` returning 500 once bookings exist, a per-salon timezone column, rate limiting.
