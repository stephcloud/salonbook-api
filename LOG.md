# Build Log

SalonBook API: clients book a stylist and pay a deposit so salon no-shows stop costing money. Newest entry last. Each day uses the same four headings: **Did**, **Learned**, **Stuck on**, **Next** (plus **Later** for ideas that are not scheduled).

## Status (8 Oct 2026)

- **Live:** Render (Docker runtime, Frankfurt, free tier). Migrations run on container start. Paystack is in test mode only.
- **Tests:** 463 passing on `main`; `ruff` and `pip-audit` clean.
- **Hard part:** no double booking (database exclusion constraint), refund policy applied once, idempotent webhooks. All three tested, and the payment and refund path was proven against real Paystack test mode.
- **Security:** Tier 1 complete. Tier 2 (security headers, login rate limiting) is next.

## Open items

- [ ] Deploy `salonbook-web` to Vercel, then set `CORS_ORIGINS` on Render to its URL (no trailing slash) and redeploy.
- [ ] `POST /bookings/{id}/reschedule` is in the spec but not built: build it or drop it from `CLAUDE.md`.
- [ ] Security headers (HSTS in production only, no CSP). See the last Day 9 entry.
- [ ] Login rate limiting (Redis, own branch, with tests).
- [ ] Optional live smoke run for the late-payment refund path (let a booking go unpaid for 15 minutes, or pay late).
- [ ] Refactors: shared ownership check, test-helper coupling, split the payments service.

Other deferred ideas live in each entry's **Later** list (Day 7, Day 8 Stage A and B, Day 9). Done: `DELETE /services/{id}` now returns 409.

## Contents

- [Day 3: project scaffold](#day-3-project-scaffold)
- [Day 4: auth](#day-4-auth)
- [Day 5: salons and services](#day-5-salons-and-services)
- [Day 6: stylists](#day-6-stylists)
- [Day 7: availability, slots and the bookings table](#day-7-availability-slots-and-the-bookings-table)
- [Day 7 (continued): bookings and the hard part](#day-7-continued-bookings-and-the-hard-part)
- [Day 8: cancel and the refund policy](#day-8-cancel-and-the-refund-policy)
- [Day 8: payments, Stage A](#day-8-payments-stage-a)
- [Day 8: payments, Stage B (refunds)](#day-8-payments-stage-b-refunds)
- [Day 9: booking reads and the live smoke script](#day-9-booking-reads-and-the-live-smoke-script)
- [Day 9: live deploy, smoke test and delete fix](#day-9-live-deploy-smoke-test-and-delete-fix)

<!-- Template: copy this block for each new day.

## Day N: topic
- **Did:**
- **Learned:**
- **Stuck on:**
- **Next:**

-->

---

## Day 3: project scaffold

- **Did:** Scaffolded the FastAPI project, 2 tests passing, requirements pinned.
- **Learned:**
- **Stuck on:**
- **Next:**

## Day 4: auth

- **Did:** Built auth on `feature/auth` (uncommitted): `User` model + migration `0001`, register/login/me, `get_current_user` and `require_role`, Argon2 (pwdlib) + PyJWT (30 min), generic login errors, SECRET_KEY startup guard (fails closed unless ENVIRONMENT is local/test), CORS allow-list validation. Security, DB and code reviews run and their fixes applied. 36 tests written.
- **Learned:** Two Postgres servers listen on port 5432 here (a native Windows service and the Docker `db` container), so `localhost` reaches the wrong one and fails password auth.
- **Stuck on:** 26 DB-backed tests can't run yet (10 non-DB tests pass). Migration `0001` not yet run against a real DB.
- **Next:** Resolve the port clash, run the full suite, add a migration-vs-models test, add login/register rate limiting (Redis) before deploy, then commit.

## Day 5: salons and services

- **Did:** Added `Salon` and `Service` resources on `feature/salons`: models, schemas, services, routers (`/salons`, `/salons/{id}/services`, `/services/{id}`), owner-only writes with 403 for non-owners, public paginated GETs (limit default 20, max 100), `price_type` rule (quote => null price, fixed => price) in schema, service layer and a DB CHECK. Hand-written migration `0002`. 35 new tests (71 total passing).
- **Learned:** my `.env` pointed to port 5432, which hits the Windows Postgres service. The Docker database is on 5433, so I fixed `.env` to use 5433 and the tests pass.
- **Stuck on:** migration `0002` is not applied to a live database yet. Autogenerate needs a DB at `0001`, so it was hand-written and checked with `alembic upgrade 0001:0002 --sql`.
- **Next:** run `alembic upgrade head`, then stylists + availability, then bookings and the hard part.

## Day 6: stylists

- **Did:** Added stylists: `POST /salons/{id}/stylists` (owner creates a stylist user), `PUT /stylists/{id}/services` (replace set, services must belong to the stylist's salon), public paginated `GET /salons/{id}/stylists` (id, name, service_ids only; no email). `users.salon_id` + CHECK (only stylists may have it), `stylist_services` table. 21 new tests incl. ownership (92 total passing). Security, DB, code reviews run; applied a row lock on the services replace and de-duplicated `email_taken`.
- **Learned:** `users.salon_id -> salons` and `salons.owner_id -> users` is a circular FK; it needs `use_alter` and a constraint name or `create_all`/`drop_all` fail. The test database also had stale tables from before the new FK, so I had to reset `salonbook_test`.
- **Stuck on:** nothing now. Migration `0003` is applied (checked upgrade, downgrade, upgrade again, and `alembic check`). Still to verify: the CHECK constraint exists on the dev DB.
- **Next:** availability rules (6.3), then bookings and the hard part.

## Day 7: availability, slots and the bookings table

- **Did:** Availability rules (working windows, breaks, days off, `GET/PUT /stylists/{id}/availability`). Then `GET /stylists/{id}/slots?service_id=&date=`: the logic is in `services/slots.py` (a pure `compute_slots` plus `get_slots`), it uses the service's own duration, takes away breaks, days off and active bookings, offers starts every 15 min, drops past times, and returns salon-local ISO datetimes. Added the `bookings` table with a hand-written migration `0006` and the hard-part exclusion constraint: `EXCLUDE USING gist (stylist_id WITH =, tstzrange(starts_at, ends_at, '[)') WITH &&) WHERE (status IN ('pending','confirmed'))`, plus `btree_gist` and `updated_at`. Tests for slots, the constraint (5 concurrent inserts for one slot: 1 wins) and `updated_at`; 180 total passing. Security, DB and code reviews run and their fixes applied.
- **Learned:** `tzdata` isn't installed, so `zoneinfo` can't find Africa/Lagos; I used a fixed `SALON_UTC_OFFSET_MINUTES` (default 60) instead of adding a dependency. `alembic check` doesn't compare exclusion constraints, so I built the table from the model in a second scratch DB and compared `pg_get_constraintdef` by hand. Ignoring F401 per file in `ruff.toml` makes ruff flag the `# noqa: F401` as unused (RUF100), so both rules are ignored for `base.py`. `updated_at` is set by SQLAlchemy (`onupdate`), not by a DB trigger, so raw SQL edits won't bump it.
- **Stuck on:** nothing now. Migration `0006` was checked on a scratch DB (upgrade, downgrade -1, upgrade, `alembic check`) and is applied to dev.
- **Next:** `POST /bookings` (re-check the slot server-side, catch the constraint's `IntegrityError` and return 409), the 15-minute expiry job that cancels lapsed pending bookings, the refund-policy and webhook-idempotency tests, then Paystack. Later: fix `DELETE /services/{id}` returning 500 once bookings exist, a per-salon timezone column, rate limiting.

## Day 7 (continued): bookings and the hard part

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
  - **`DELETE /services/{id}` fix:** done on Day 9 (now a 409, see below).

## Day 8: cancel and the refund policy

- **Did:** `POST /bookings/{id}/cancel`. The refund rule is a pure function, `services/refund_policy.is_refundable(starts_at, now, cancellation_hours)`: refund only when cancelled *more than* `cancellation_hours` before start, so exactly at the limit gets no refund (matches the spec wording). `services/bookings.cancel_booking` locks the row (`FOR UPDATE`), then checks the caller (the booking's client or the salon's owner, else 403), then the status: pending/confirmed are cancelled, an already-cancelled booking returns 200 unchanged, completed/no_show are 409, and so is a booking whose `starts_at` has passed. When the salon owner cancels a confirmed booking `refund_due` is always true; a client's cancel follows the salon's window. It records `refund_due` and `cancelled_at` on the booking (pending bookings get `refund_due=false`, nothing was paid); the real Paystack refund is left for the payments step. Migration `0007` adds the two nullable columns. Tests: boundary, double cancel (timestamps change once), parallel double cancel, wrong user, owner cancel inside the window, started booking 409, slot free again; 266 total passing. DB, security and code reviews run and the fixes that were not policy calls applied.
- **Learned:** `rollback()` expires ORM instances even with `expire_on_commit=False`, so the idempotent no-op path ends with `commit()` instead, or serialising the returned booking would fail. A row lock on an object already in the session needs `populate_existing=True` to avoid acting on a stale status.
- **Stuck on:** nothing now. The three policy questions from the reviews are decided: owner cancel always refunds, started bookings are 409, someone else's booking stays 403.
- **Next:** apply `0007` once reviewed. Payments step: the webhook must lock the booking row and, if it is already cancelled when `charge.success` arrives, flag a refund instead of confirming. Later: 404 instead of 403 for someone else's booking (a 403 lets a caller confirm a booking id exists; UUIDv4 makes it low risk, so it was kept for now).

## Day 8: payments, Stage A

- **Did:** `payments` table (migration `0008`; `0009` narrows the live-payment unique index to pending/paid), `PaystackClient.initialize_transaction`, `POST /bookings/{id}/pay` (payment row committed before Paystack is called; a failed init marks the row `failed`), `POST /payments/webhook` (HMAC-SHA512 on the raw body first, locks booking then payment, replays are no-ops, late/lapsed/cancelled payments go to `refund_pending` and are never confirmed, mismatched amount/currency never confirms). `expiry_cutoff` made public so the webhook shares the 15-minute rule. Reject a 0 deposit on salon create/update (422). `PAYSTACK_SECRET_KEY` is now required at boot in production. Hard parts (a) replay and (c) bad signature tested. Refunds (Stage B) not built yet: `refund_pending` rows are not refunded.
- **Learned:** moving a stale failed row to `refund_pending` while a newer payment is open broke the old unique index (the webhook would have 500ed and Paystack retried forever), found by a test; "live" must mean pending or paid only. Booking and payment are locked in that order everywhere (booking `FOR NO KEY UPDATE`, then payment).
- **Later (payments review):**
  - **Webhook body size and rate limit:** the body is read in full before the signature is checked. Add a Content-Length cap, and Redis rate limiting with a TTL.
  - **Response models:** `POST /payments/webhook` returns a bare `dict[str, str]`. Give it a small named response schema.
  - **Misleading "no deposit" 409:** `_deposit_for` returns 0 when a stylist has no salon, so broken data is reported as "this salon does not take a deposit". Use a distinct error and log it.
  - **Currency and callback:** `NGN` is hard-coded and Paystack initialize sends no `callback_url`. Make the currency a per-salon or config value, and set the callback to the web app's return page.
  - **Stuck pending payments:** a `pending` payment with no `authorization_url` (process died mid-init) blocks `/pay` until the booking lapses. The Stage B sweeper can mark such rows `failed` after a short while.
  - **Webhook vs cancel race test:** no test yet for a webhook racing `cancel_booking` or the expiry job. The lock order handles it; a test would pin it.

## Day 8: payments, Stage B (refunds)

- **Did:** `PaystackClient.create_refund` / `find_refund` (read Paystack's refund docs first: `POST /refund` with `transaction` + `amount` in kobo, a 200 only means "queued", statuses pending/processing/processed/failed, documented errors "Transaction has been fully reversed" and "Insufficient balance"). `process_refund`: claim a `refund_pending` row with a stamp and commit, call Paystack with no lock held, finalize under a fresh lock; every attempt first looks for an existing refund so a lost reply never double-refunds. The webhook and `POST /bookings/{id}/cancel` send refunds in the background after commit; `refund.processed` / `refund.failed` are handled; a retry job (`app/jobs/retry_refunds.py`, in the lifespan) picks up the rest. `cancel_booking` now locks `FOR NO KEY UPDATE` and moves a paid deposit to `refund_pending` in the same commit. Hard part (b) tested, including a late payment arriving after someone else rebooked the slot. After the code and security reviews: retries are quick at first then hourly (about 2 days in all) with error logs, a `refund.failed` that arrives mid-attempt is no longer overwritten by `refunded`, one crashing refund can't stop the batch or fail a cancel, the webhook body is capped at 64 KB, `/pay` really takes the booking lock, and the concurrency tests wait on events instead of sleeping.
- **Learned:** a Paystack 200 on refund only means "queued", so `refunded` here means Paystack accepted it; `refund.failed` reopens it. Mutation checks (put back a plain `FOR UPDATE`, drop the lapse check, drop the replay guard) each failed the intended tests. The retry budget must cover a day or more of Paystack trouble, or an outage silently abandons customers' money.
- **Stuck on:** nothing in code. Not yet verified against real Paystack test mode: the list-refunds response shape, the "fully reversed" status code, and `find_refund` matching real data.
- **Later (reviews):**
  - **Exhausted refunds need a human:** after 50 attempts a refund stays `refund_pending`. There is no alert or admin view yet; reset with `UPDATE payments SET refund_attempts = 0 WHERE status = 'refund_pending'` once the cause is fixed. Add alerting on the error logs.
  - **Secrets and test-mode guard:** hold `PAYSTACK_SECRET_KEY` (and `SECRET_KEY`, `RESEND_API_KEY`) as `SecretStr` so a logged settings object can't leak them; consider requiring an `sk_test_` key, since the project is test-mode only.
  - **Refund fee abuse:** Paystack fees on refunded deposits are generally not returned, so repeated book-pay-cancel could cost the salon. Decide who bears fees; consider a per-client cap or monitoring.
  - **Production hardening:** turn off `/docs`, `/redoc` and `/openapi.json` in production; rate limit `POST /bookings/{id}/pay` and the webhook (Redis, with a TTL).
  - **`refund_due` meaning:** a late payment on a booking cancelled with "no refund" overwrites `refund_due` to true. A separate marker on the payment would keep the original policy decision visible.
  - **Multi-worker note:** both housekeeping loops start in every worker. That is safe (claims and row locks), just redundant work.

## Day 9: booking reads and the live smoke script

- **Did:** the API could create, pay and cancel a booking but not read one back, so I added `GET /bookings/{id}` (the booking's client or its salon's owner; 404 unknown, 403 anyone else, same as cancel) returning status, `refund_due`, `starts_at` and the latest payment's status and amount (null when there is none), and `GET /bookings` (the current client's own, paginated, newest start first). Logic is in `services/bookings.py` (`get_booking_detail`, `list_client_bookings`), the router stays thin, and the new `BookingDetailResponse` has an explicit field list, so no Paystack reference, `authorization_url` or refund internals. 11 tests in `test_booking_read.py` (success, no payment, owner, wrong client, other owner, unknown id, no login, list ownership and order, pagination, bad limit, owner on the list); the targeted booking suites pass (76), the full suite was not re-run. Wrote `scripts/smoke_live.py` (httpx; base URL from `--base-url` or `SALONBOOK_BASE_URL`, nothing hardcoded): register owner and client, build salon/service/stylist/availability, book, pay with the Paystack test card, check confirmed and paid, cancel, check refunded. Updated the Bookings line in `CLAUDE.md`. Security review of the new endpoints: no Critical/High/Medium. Then added the review's follow-ups: the payment lookup now orders by `created_at` then `id` (stable "latest payment"), and 13 more tests (response has exactly the expected keys and no Paystack reference or checkout URL, the booking's own stylist gets 403 on both routes, an owner of a different salon gets 403, newest payment shown over an older failed one, same-timestamp payments resolved by id, malformed UUID 422, out-of-range `limit`/`offset` 422, cancelled bookings appear in the list); `test_booking_read.py` has 24 tests. Docs for the deploy change (`alembic upgrade head` on container start) were already in `CLAUDE.md`. Everything is on `fix/booking-read` and not yet committed.
- **Learned:** the spec listed `GET /bookings` but the code never had it, so my first smoke script could only infer "confirmed" from `/pay` returning 409; checking what endpoints actually exist before writing a test script saved a wrong assumption. Read-only endpoints take no row lock: they can't change state, so they must not block a cancel or a webhook.
- **Stuck on:** nothing. The smoke script has not been run against the live service yet, and refund timing there (30s wait, then up to 45s of polling) may need a longer wait.
- **Next:** run the smoke script live and fix whatever it shows; review and push the branch.
- **Later (security review):**
  - **404 vs 403:** a 403 lets a signed-in user tell a real booking id from a random one. Low risk with UUIDv4. If changed, change `cancel` too so both routes agree.
  - **Owner access depends on the stylist's current `salon_id`:** checked, nothing can change it today. It is set once when an owner creates the stylist (`services/stylists.py`), and no endpoint updates it. Revisit if a "move stylist to another salon" feature is added: `Booking` has no salon column, so the new owner would see the old bookings. Store the salon on the booking, or block the move, first.
  - **Which payment to show:** the newest by time is shown, even if it is a `failed` row sitting on top of an older `refunded` one. Decide if the UI needs a smarter pick.

## Day 9: live deploy, smoke test and delete fix

- **Did:** the API is live on Render (Docker runtime, Frankfurt, free tier). The first deploy went live around 3:28 PM on 7 Oct 2026, and the booking read endpoints followed on 8 Oct after the `fix/booking-read` PR was merged to `main`. The deploy took the Docker route, so the container's start command is the Dockerfile `CMD` (`alembic upgrade head && exec uvicorn ...`): migrations run every time the container starts, because the free tier has no Pre-Deploy Command, and a failed migration fails the deploy. Ran `scripts/smoke_live.py` against the live service and it passed end to end: register and log in an owner and a client, salon, service, stylist, availability, 29 slots, a pending booking, a 500000 kobo payment and checkout URL. I paid with Paystack's test "Success" option, and the booking read showed `confirmed` and the payment `paid`, so the real signed webhook works. Then the client cancelled: the booking became `cancelled` with `refund_due` true and the payment `refund_pending`, and about 30 seconds later the payment was `refunded`, so a real Paystack test refund works too. Also fixed `DELETE /services/{id}`: it returned 500 once a booking referenced the service, and now returns 409 (the database's `RESTRICT` foreign key decides, so a booking made at the same moment can't slip past a check). Two tests, on branch `fix/delete-service-conflict`.
- **Learned:** Swagger pre-fills every request body with example values, and sending them as they are fails. I left the placeholder UUID `3fa85f64-5717-4562-b3fc-2c963f66afa6` in the stylist-service call (422), and left `off_date` plus identical start and end times in the availability body (422). I also used an expired token (401) and a client token on an owner endpoint (403). So: check every id and field before pressing Execute. A 401 means the token is missing, bad or expired; a 403 means you are logged in but your role is wrong. The smoke test was worth it: it exercised the real webhook signature and a real refund, which the unit tests can only fake.
- **Stuck on:** nothing.
- **Next:** deploy `salonbook-web` to Vercel, then set `CORS_ORIGINS` on Render to the Vercel URL (no trailing slash) and redeploy. Optional smoke paths: let a booking go unpaid for 15 minutes, or pay late, to see the late-payment refund against real Paystack. Security tier 2 if time allows: login rate limiting and security headers.
- **Later (security tier 2, audit, refactors):**
  - **Security headers:** add `X-Content-Type-Options: nosniff`, `X-Frame-Options`, `Referrer-Policy`, and `Strict-Transport-Security` in production only (HSTS on local http would be wrong). No CSP on the API, because it breaks the `/docs` page. A small middleware in `main.py` is enough, so no new dependency.
  - **Login rate limiting:** Redis-based, with a TTL on every key, on its own branch. Needs tests for the login path (blocked after too many failures, allowed again after the window, other users and IPs unaffected). Keep the generic "invalid credentials" error.
  - **`pip-audit`:** clean on 8 Oct 2026, so Tier 1 security is complete. It only covers `requirements.txt`, not the Docker base image or system packages; scan the image separately if that matters. Re-run before each deploy.
  - **Refactors:** the ownership check (client or salon owner) is written twice, in `cancel_booking` and in `get_booking_detail`, so pull it into one helper; the test helpers import from each other and caused a circular import (move shared builders into a helpers module); `services/payments.py` is large and could be split into payment start, webhook handling and refunds.

## Day 10: image_url and the owner bookings list

- **Did:** branch `feat/image-url`, two changes. (1) Added a nullable `image_url` (https:// only, max 500 chars, optional) to `salons` and to `users`, since stylists are users with role `stylist` and have no table of their own. Migration `0010` is a hand-written add-column (no default, no backfill) and I ran upgrade, `downgrade -1`, upgrade and `alembic check` on the local database: all clean. Salon create/update and stylist create accept it and all responses return it. `PATCH /salons/{id}` with `"image_url": null` clears it (update now uses `exclude_unset`, and a null for a required field is still ignored). The public stylist list returns only id, name, service_ids and image_url, never email. (2) `GET /salons/{salon_id}/bookings`, no migration: owner of that salon only (403 for another owner, a client or a stylist; 401 without a token; 404 for an unknown salon, same as the other salon routes via `get_owned_salon`). Paginated (default 20, max 100), ordered by `starts_at`, filters `status`, `stylist_id` and `from`/`to` (salon-local dates, inclusive; `from` after `to` is 422). Includes cancelled and expired bookings. Each item has the booking id, status, times, `refund_due`, latest payment status, stylist (id, name), service (id, name, duration) and the client's name only. The query lives in `services/bookings.py` (`list_salon_bookings`) and the response model has an explicit field list, so no email, password hash or Paystack reference, access code or checkout URL. Tests: `test_image_url.py` and `test_salon_bookings.py`.
- **Learned:** `exclude_none` made it impossible to clear an optional field with PATCH, so "clear the image" needed `exclude_unset`. Scoping the list by the stylists who work at the salon (not just by a `stylist_id` filter) is what makes a stylist id from another salon return nothing.
- **Stuck on:** nothing.
- **Next:** review and push `feat/image-url`; Render runs migration 0010 on deploy.
- **Later:**
  - **No stylist update endpoint:** a stylist's `image_url` can only be set when the owner creates them. Add `PATCH /stylists/{id}` (owner only) so it can be changed or cleared.
  - **Image hosting:** the API stores only a URL; nothing checks that it points at a real image or a trusted host. Uploading to object storage is still to do.
  - **Owner list size:** it does one query with joins and a payment subquery; the `ix_bookings_stylist_id_starts_at` index already covers it, so revisit only if a salon gets very large.

## Day 11: booking emails (phase 1)

- **Did:** branch `feat/booking-emails`. When the Paystack webhook confirms a booking, the client and the salon owner each get an email; when a confirmed (paid) booking is cancelled, both get a cancellation email. Cancelling an unpaid pending booking sends nothing, whoever cancels. Provider is Brevo over the existing httpx client (no new dependency) behind a small `EmailSender` interface, so it can be swapped; tests use a fake sender and never call Brevo. Plain text plus minimal HTML templates, times shown in Africa/Lagos, amounts from the payment row. Refund wording comes only from what the cancel flow recorded: `refund_due`, who cancelled, and the real payment row state ("is being refunded", "has been refunded", "kept under the cancellation window", or nothing about money when the code did not queue a refund). Duplicates are prevented by four `*_email_sent_at` markers on `bookings` (migration `0011`, add-column only): the send is claimed with one atomic `UPDATE ... WHERE marker IS NULL`, and given back if the send fails. Emails run as background tasks after the response and never raise, so the webhook still answers 200 and a booking or cancel is never rolled back. Missing `BREVO_API_KEY` or `EMAIL_FROM_ADDRESS` skips sending with one log line. `BREVO_API_KEY` is a `SecretStr`. Logs carry only the booking id and the kind of email, never an address, a body or the key. `handle_paystack_event` now returns a `WebhookOutcome` and the cancel service returns a `CancelOutcome`, so only the call that really confirmed or cancelled sends. Migration checked locally: upgrade, downgrade, upgrade and `alembic check` (no drift). 579 tests pass (463 before, 116 new across the email, template, sender and log tests), lint clean. `db-reviewer`, `code-reviewer` and `security-reviewer` run; their real findings are fixed.
- **Learned:** the pending-cancel `refund_due` is `false`, not `null` (only the expiry job leaves `null`), so wording cannot be read from `refund_due` alone; the cancel now reports whether the booking was confirmed. A claim-first marker (instead of read, send, then write) is what makes concurrent webhook replays send once. Running the whole suite in one process got killed for low memory; two halves worked.
- **Stuck on:** nothing.
- **Next:** set `BREVO_API_KEY`, `EMAIL_FROM_ADDRESS` and `EMAIL_FROM_NAME` on Render and verify the sender in Brevo; run the post-deploy checks below; then phase 2 (24h and 2h reminders, owner 7:00 summary), which reuses the same markers and sender.
- **Go-live item:** email verification and rate limiting before go-live. Anyone can register with another person's address, book, cancel a paid booking and trigger emails from our sender; with no verification and no limits on register, book and cancel, that is spam from our domain and a way to use up the Brevo quota.
- **Later:**
  - **Failed emails are not retried:** a failed send gives the claim back, but nothing sends it again. A sweeper over bookings with the status but a NULL marker would fix it; a process death after the commit also loses the email.
  - **Two owner relations:** cancel finds the owner through the stylist's salon and the email through the service's salon. They agree today; use one relation if stylists can ever move.
  - **Other keys as `SecretStr`:** only `BREVO_API_KEY` was changed; `PAYSTACK_SECRET_KEY`, `SECRET_KEY` and `RESEND_API_KEY` are still plain strings.

## Day 12: booking reminders and owner morning summary (phase 2 of emails)

- **Did:** branch `feat/booking-reminders`, three commits. Clients get a reminder 24 hours and again 2 hours before a **confirmed** booking (two separate emails). Each salon owner gets one email around 7:00 Africa/Lagos listing that day's confirmed bookings still to come (time, service, stylist, client name only, never a client email); no bookings, no email. Cancelled, pending and past bookings are skipped, and a booking confirmed after a reminder's moment never gets that reminder (booked 1 hour ahead: no 24h one). Because the Render free tier sleeps, nothing runs inside the app: an external scheduler (cron-job.org, every 5 minutes) calls `POST /api/v1/internal/reminders/run`, protected by an `X-Cron-Secret` header (`CRON_SECRET`; 401 wrong or missing, 503 if unset; constant-time compare; hidden from the docs). It answers 202 at once and does the work in a background task that cannot raise; a second call while a run is still going is skipped. Due-ness is a time window with a tolerance (24h: 3h, 2h: 45 min), so a missed run catches up and a stale reminder is dropped; `payments.paid_at` is the confirmation time. Every send is claimed first with an atomic UPDATE on a sent-at marker (migration `0012`: two nullable columns on `bookings`, `users.daily_summary_sent_on`, and a partial index on confirmed bookings by start time), so repeated or concurrent calls send once. A temporary failure (timeout, 5xx, network, and 401/402/403/408/429 from Brevo, which are about our account, not the message) releases the claim and the next run retries inside the window; a permanent rejection (any other 4xx, such as an invalid address) keeps it and is logged by id only, so a bad address is not retried every 5 minutes. After 3 failures in a row a batch stops early. Batches are capped at 25 per kind per run. Migration `0012` checked locally: upgrade, downgrade, upgrade, `alembic check` (no drift). 658 tests pass (651 before this branch's follow-up fixes, plus about 70 new), lint clean. `code-reviewer` and `security-reviewer` run; fixes below.
- **Learned:** the retry rule has to separate "this message is bad" from "our account is bad": a revoked key or exhausted quota is a 4xx too, and treating it as permanent would silently burn every due reminder in one run. A Lagos "day" in the tests is easy to get wrong by an hour (UTC+1), so the tests state both times.
- **Fixed after review:** Brevo 401/402/403 are temporary; `hide_input_in_errors` on settings so a failed validation never echoes keys; the secret is compared stripped, the same way it is length-checked; one run at a time; reminder and summary queries select explicit columns (no `password_hash` loaded); the owner summary says "still to come today"; response schema for the endpoint. The existing log test now expects "rejected for good" for a 400.
- **Stuck on:** nothing.
- **Next:** set `CRON_SECRET` on Render, create the cron-job.org job (steps in the PR description), and watch the first real reminder.
- **Later:**
  - **Reschedule must clear markers:** `POST /bookings/{id}/reschedule` is not built. When it is, it must set both `reminder_*_email_sent_at` back to NULL, or a moved booking is never reminded. The model has a comment; add a test then.
  - **At-most-once on a hard kill:** the claim is stamped before the send, so a SIGKILL between them loses that email. A timeout after Brevo accepted a message releases the claim and can send a duplicate. Both are the better failure for a reminder.
  - **Wording on a catch-up:** a 24h reminder caught up late can go out about 21 hours before the start while still saying "in about 24 hours".
  - **Two sources for Lagos time:** slots use `SALON_UTC_OFFSET_MINUTES`; emails and the 7:00 window use `Africa/Lagos`. Same today (UTC+1); unify if a salon ever sits elsewhere.
  - **Unset `CRON_SECRET` is silent:** reminders just stop. Consider a startup warning in production.
  - **Names in emails:** whitespace is collapsed and HTML escaped, but zero-width and bidi characters are not stripped and the schemas may not cap name lengths.

## Day 13: owner cancels a booking (tests and docs only)

- **Did:** branch `feat/owner-cancel-booking`. The plan was a new owner-cancel endpoint, but reading the code showed `POST /bookings/{id}/cancel` already lets the owner of the booking's salon cancel: pending or confirmed only, a confirmed booking always gets `refund_due` true and the full deposit moves to `refund_pending` whatever `cancellation_hours` says, the client email says "The salon cancelled this booking" and that the deposit is being refunded, and the existing `cancellation_email_sent_at` / `owner_cancellation_email_sent_at` markers keep it to one set of emails. So no new endpoint, no migration, no dependency and no source change. Added `app/tests/test_owner_cancel.py` (15 tests) to pin it down: owner succeeds with a full refund inside the window, another salon's owner and a stranger client get 403 with nothing changed, pending vs confirmed, already-cancelled is a 200 with no change and no extra email, a double click and three parallel cancels send one set of emails and refund once, a crashing sender does not break the cancel, the owner's email has the client name and never the client email, and the owner confirmation, 24h/2h reminders and the 7:00 summary all skip a cancelled booking. `CLAUDE.md` now says owners can use the cancel route and what it does.
- **Learned:** the feature was already built as part of the refund and email work; reading `CancelOutcome` and the cancel service first saved a duplicate endpoint.
- **Stuck on:** nothing.
- **Next:** review and push `feat/owner-cancel-booking`.
- **Later:** the owner still gets a "You cancelled a booking" receipt when they cancel themselves; drop it if it turns out to be noise.

## Day 14: admin role and salon moderation

- **Did:** branch `feat/admin-salon-moderation`, from `main`. A new `admin` role (platform owner) that nothing in the API can grant: registration still only allows client and owner, and `python -m app.jobs.make_admin <email>` is run by hand against the database you mean to change (it refuses an unknown email, and an owner, stylist or anyone with bookings; no grant by matching an email at login). Salons now have a `status` (pending, approved, suspended): migration `0014` made every existing salon `approved` and new ones start `pending`; migration `0013` only adds the enum value (it needs an autocommit block, so it is its own file). Only approved salons show in the public list, detail, services and stylists (others are a plain 404); slots, `POST /bookings` and `POST /bookings/{id}/pay` on a non-approved salon are a 409 "salon is not accepting bookings". Owners see `status` in `/salons/mine`, POST and PATCH. Existing bookings, cancel and the Paystack webhook are untouched. Admin endpoints under `/admin` (401 no token, 403 other roles, shown in the docs): salon list with owner name and booking counts (paginated, filter by status), approve, suspend (optional reason), delete a salon with no bookings and no stylists, user list, delete a user with no bookings and no salon. Admins cannot suspend their own salon, delete themselves or delete another admin. Every action writes an `admin_audit_log` row (ids only; the reason lives there only) in the same transaction; repeating approve or suspend is a 200 with no change, no audit row and no email. The owner is emailed on a real approve or suspend, with no new marker: the status change itself is the claim, so only the call that flipped it sends, and a failure never touches the action. The migrations were run on throwaway Postgres 16 containers only: from empty, and from 0012 with an existing salon (it stayed approved, a new one was pending), plus `alembic check` (no drift) and a downgrade and re-upgrade of 0014. 766 tests pass in two shards (about 110 new), lint clean, `pip-audit` clean. `code-reviewer`, `security-reviewer` and `db-reviewer` run.
- **Learned:** running Alembic inside the pytest process calls `fileConfig`, which silenced the log assertions of every test after it (four refund tests failed only in the full run); the migration test now runs it in a subprocess. FastAPI nests included routers lazily here, so `app.routes` does not list them: the route-protection test reads the OpenAPI paths instead.
- **Fixed after review:** the admin salon list counts bookings only for the salons on the page; a no-op approve refreshes the salon after releasing its lock (a test caught the expired object).
- **Stuck on:** nothing.
- **Next:** run `make_admin` on production for your own account, then approve the existing salons that should stay (they were all migrated to approved); set `SALONBOOK_ADMIN_TOKEN` to use the live smoke test; the web app needs to show `status` to owners.
- **Later:**
  - **No index on `bookings.service_id`:** the admin salon list and salon delete scan `bookings` (and so does deleting a service). A migration `0015` with the index (created concurrently) fixes it.
  - **Owner edits skip re-review:** an owner can change name, address, phone and image on an approved salon and it is public at once. Decide whether that sends it back to pending.
  - **Owner can't list a hidden salon's services or stylists:** those routes are public and give 404 until approved; only writes work. Optional auth on them would fix it.
  - **A suspend can race a booking:** the status is read without a lock in slots, booking and pay, so one request already past the check can go through; it is blocked at the next step and lapses in 15 minutes.
  - **Stylist insert racing a salon delete** is reported as "email already registered" (any `IntegrityError` there is).
  - **make_admin writes no audit row** (there is no acting admin); its log line is the record.
  - **Reschedule is not built;** when it is, it needs the same non-approved check.
  - **No concurrency tests for the admin locking:** nothing tests two concurrent approve/suspend calls (only one should flip, with one audit row and one email), or a salon or user delete racing a booking or stylist insert (the `IntegrityError` branches in `services/admin.py`). Add one `asyncio.gather` test each with two sessions, like `test_hard_part_double_booking`.
  - **Admin routes are listed in the public `/docs` and `/openapi.json`:** kept on purpose (you chose to show them), but anyone can see the `/admin` paths. `include_in_schema=False` on the admin router, or turning docs off in production, would hide them.
  - **Slots and booking leak a non-approved salon's existence:** a signed-in user gets 409 "salon is not accepting bookings" for a stylist in a pending or suspended salon, but 404 for an unknown one, so a guessed stylist id can be confirmed. Low risk with UUIDs; accepted for now.
  - **`owner_id` is in the public salon response** (there before this branch). Consider dropping it from the public shape.
  - **Admin deletes are permanent:** salon and user deletes are hard deletes with no undo, and the audit row keeps ids only, so a deleted salon's name or a deleted user's email can't be recovered. Bookings and payments are safe (RESTRICT foreign keys, plus the 409 checks).
