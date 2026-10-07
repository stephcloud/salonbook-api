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


## Step 7: bookings and the hard part
- **Did:** Built the slot list (`GET /stylists/{id}/slots`, logic in `services/slots.py`, uses the service's own duration, removes breaks, days off and active bookings). Added the `bookings` table with a hand-written migration `0006` that enables `btree_gist` and adds the exclusion constraint. Built `POST /bookings`: clients only, `client_id` from the token, the server re-checks the slot, computes `ends_at` from the service duration, creates a `pending` booking, cancels the stylist's lapsed pending bookings in the same transaction, caps a client at 3 unexpired pending bookings, and turns a lost race into a 409. Added the expiry function with an in-process loop (60 s) and a one-shot entry point for a Render Cron Job. Wrote the concurrency test: two clients, separate sessions, one 201 and one 409, plus raw-SQL tests that hit the constraint with no service code. Reviews run and their fixes applied. 243 tests passing.
- **Learned:**
  - **Why an `if` is unsafe:** "check the slot is free, then insert" has a gap between the check and the insert. Two requests can both read "free" before either one writes, so each one is correct on its own and together they double-book. The check result is stale by the time I act on it. Only the database can close the gap, because it checks and writes in one atomic step.
  - **`btree_gist` and `[)`:** a GiST index handles range overlap (`&&`) but cannot compare a plain uuid with `=`. `btree_gist` adds that, so `stylist_id WITH =` and `tstzrange(...) WITH &&` can live in one exclusion constraint: same stylist and overlapping time is forbidden. The `[)` bounds make the start inclusive and the end exclusive, so a booking that ends at 11:00 and one that starts at 11:00 do not overlap and back-to-back appointments are allowed. The `WHERE status IN ('pending','confirmed')` part means only active bookings hold a slot.
  - **Why the barrier test is needed:** a plain `asyncio.gather` of two requests often lets one finish before the other starts, and then the slot re-check rejects the second one and the constraint is never tested. The barrier makes both requests pass the re-check and see the slot as free before either inserts, so only the database can decide. Postgres can also resolve two simultaneous inserts by aborting one as a deadlock (`40P01`) instead of `23P01`, so the service treats all three lost-race error codes as a 409.
  - **What the mutation check proved:** when I removed the constraint, the race test and the raw overlap tests failed (two 201s, "DID NOT RAISE"), so they really depend on the database rule and not on the re-check. Removing the in-transaction cleanup, the slot re-check, the explicit rollback, the client row lock and the `FOR NO KEY UPDATE` each made specific tests fail. That shows these tests would catch someone breaking those pieces, and that they were not passing by luck.
- **Later:**
  - **Pending-expiry at scale:** the expiry scan needs a partial index on pending bookings (a new migration), batching with a limit and a lock timeout, and probably one scheduled job instead of a loop in every worker. On Render's free tier the loop sleeps with the app, which is fine because `POST /bookings` cleans up for itself, but the slot list can show a lapsed hold as taken until the job runs.
  - **Slot-griefing and rate limiting:** the cap is per account and registration is open, so someone could script many accounts and hold slots for free. Needs Redis rate limits (with TTL) on register and `POST /bookings`, and maybe a per-stylist cap on pending holds.
  - **Late `charge.success` webhook:** a payment can arrive after the booking expired and was cancelled. The webhook must lock the booking row, check its status, and refund or flag it, never revive it, because the slot may already be rebooked. Expiry also writes `cancelled`, the same as a user cancel, so the refund rule must check for a paid payment.
  - **Idempotency keys:** a repeated identical `POST /bookings` gets a 409 because the client's own pending booking holds the slot. An idempotency key would let a retry return the original booking.
  - **`DELETE /services/{id}` fix:** it returns a 500 once a booking references the service, because the foreign key is `RESTRICT`. Return a 409, or soft-delete services with an `is_active` flag.

## Day 8 (payments, Stage A)
- **Did:** `payments` table (migration `0008`; `0009` narrows the live-payment unique index to pending/paid), `PaystackClient.initialize_transaction`, `POST /bookings/{id}/pay` (payment row committed before Paystack is called; a failed init marks the row `failed`), `POST /payments/webhook` (HMAC-SHA512 on the raw body first, locks booking then payment, replays are no-ops, late/lapsed/cancelled payments go to `refund_pending` and are never confirmed, mismatched amount/currency never confirms). `expiry_cutoff` made public so the webhook shares the 15-minute rule. Reject a 0 deposit on salon create/update (422). `PAYSTACK_SECRET_KEY` is now required at boot in production. Hard parts (a) replay and (c) bad signature tested. Refunds (Stage B) not built yet: `refund_pending` rows are not refunded.
- **Learned:** moving a stale failed row to `refund_pending` while a newer payment is open broke the old unique index (the webhook would have 500ed and Paystack retried forever), found by a test; "live" must mean pending or paid only. Booking and payment are locked in that order everywhere (booking `FOR NO KEY UPDATE`, then payment).
- **Later (payments review):**
  - **Webhook body size and rate limit:** the body is read in full before the signature is checked. Add a Content-Length cap, and Redis rate limiting with a TTL.
  - **Response models:** `POST /payments/webhook` returns a bare `dict[str, str]`. Give it a small named response schema.
  - **Misleading "no deposit" 409:** `_deposit_for` returns 0 when a stylist has no salon, so broken data is reported as "this salon does not take a deposit". Use a distinct error and log it.
  - **Currency and callback:** `NGN` is hard-coded and Paystack initialize sends no `callback_url`. Make the currency a per-salon or config value, and set the callback to the web app's return page.
  - **Stuck pending payments:** a `pending` payment with no `authorization_url` (process died mid-init) blocks `/pay` until the booking lapses. The Stage B sweeper can mark such rows `failed` after a short while.
  - **Webhook vs cancel race test:** no test yet for a webhook racing `cancel_booking` or the expiry job. The lock order handles it; a test would pin it.

## Day 8
- **Did:** `POST /bookings/{id}/cancel`. The refund rule is a pure function, `services/refund_policy.is_refundable(starts_at, now, cancellation_hours)`: refund only when cancelled *more than* `cancellation_hours` before start, so exactly at the limit gets no refund (matches the spec wording). `services/bookings.cancel_booking` locks the row (`FOR UPDATE`), then checks the caller (the booking's client or the salon's owner, else 403), then the status: pending/confirmed are cancelled, an already-cancelled booking returns 200 unchanged, completed/no_show are 409, and so is a booking whose `starts_at` has passed. When the salon owner cancels a confirmed booking `refund_due` is always true; a client's cancel follows the salon's window. It records `refund_due` and `cancelled_at` on the booking (pending bookings get `refund_due=false`, nothing was paid); the real Paystack refund is left for the payments step. Migration `0007` adds the two nullable columns. Tests: boundary, double cancel (timestamps change once), parallel double cancel, wrong user, owner cancel inside the window, started booking 409, slot free again; 266 total passing. DB, security and code reviews run and the fixes that were not policy calls applied.
- **Learned:** `rollback()` expires ORM instances even with `expire_on_commit=False`, so the idempotent no-op path ends with `commit()` instead, or serialising the returned booking would fail. A row lock on an object already in the session needs `populate_existing=True` to avoid acting on a stale status.
- **Stuck on:** nothing now. The three policy questions from the reviews are decided: owner cancel always refunds, started bookings are 409, someone else's booking stays 403.
- **Next:** apply `0007` once reviewed. Payments step: the webhook must lock the booking row and, if it is already cancelled when `charge.success` arrives, flag a refund instead of confirming. Later: 404 instead of 403 for someone else's booking (a 403 lets a caller confirm a booking id exists; UUIDv4 makes it low risk, so it was kept for now).
