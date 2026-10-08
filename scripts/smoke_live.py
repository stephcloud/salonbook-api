"""Smoke test against a LIVE SalonBook API (Paystack test mode).

Usage:
    python scripts/smoke_live.py --base-url https://<host>/api/v1
    SALONBOOK_BASE_URL=https://<host>/api/v1 python scripts/smoke_live.py

It creates throwaway users/data on the target, so only point it at a test deployment.
Nothing is read from .env and no secrets are needed: only the public API is used.

State is read with GET /bookings/{id}, which returns the booking status, refund_due
and the latest payment status and amount.
"""

import argparse
import os
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

FIRST_REQUEST_TIMEOUT = 120.0  # free Render instances cold-start slowly
TIMEOUT = 30.0
POLL_SECONDS = 45
REFUND_WAIT_SECONDS = 30
DEPOSIT_KOBO = 500_000

failures = 0


def report(ok: bool, step: str, detail: str = "") -> bool:
    global failures
    if not ok:
        failures += 1
    print(f"[{'PASS' if ok else 'FAIL'}] {step}" + (f" - {detail}" if detail else ""))
    return ok


def info(msg: str) -> None:
    print(f"[INFO] {msg}")


def abort(step: str, resp: httpx.Response) -> None:
    report(False, step, f"{resp.status_code} {resp.text[:300]}")
    print("Aborting: later steps depend on this one.")
    sys.exit(1)


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def register_and_login(
    http: httpx.Client, role: str, password: str, timeout: float | None = None
) -> tuple[str, str]:
    email = f"smoke-{role}-{secrets.token_hex(6)}@example.com"
    r = http.post(
        "/auth/register",
        json={
            "name": f"Smoke {role}",
            "email": email,
            "password": password,
            "role": role,
        },
        timeout=timeout or TIMEOUT,
    )
    if r.status_code != 201:
        abort(f"register {role}", r)
    r = http.post("/auth/login", json={"email": email, "password": password})
    if r.status_code != 200:
        abort(f"login {role}", r)
    report(True, f"register + login {role}", email)
    return email, r.json()["access_token"]


def expect(
    resp: httpx.Response, step: str, status: int, detail_key: str | None = None
) -> dict[str, Any]:
    if resp.status_code != status:
        abort(step, resp)
    body: dict[str, Any] = resp.json() if resp.content else {}
    report(True, step, str(body.get(detail_key)) if detail_key else "")
    return body


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--base-url",
        default=os.environ.get("SALONBOOK_BASE_URL"),
        help="API base incl. /api/v1 (or env SALONBOOK_BASE_URL)",
    )
    parser.add_argument(
        "--utc-offset-minutes",
        type=int,
        default=int(os.environ.get("SALON_UTC_OFFSET_MINUTES", "60")),
        help="Salon-local UTC offset used to pick the date (server SALON_UTC_OFFSET_MINUTES)",
    )
    parser.add_argument("--days-ahead", type=int, default=3, help="must be >= 2")
    args = parser.parse_args()
    if not args.base_url:
        parser.error("--base-url or SALONBOOK_BASE_URL is required")
    if args.days_ahead < 2:
        parser.error("--days-ahead must be at least 2")

    password = secrets.token_urlsafe(16)  # throwaway, never printed
    http = httpx.Client(base_url=args.base_url.rstrip("/"), timeout=TIMEOUT)

    # 1. Users. The first request gets a long timeout in case the instance is asleep.
    info("First request may wake a sleeping Render instance (up to 120s)...")
    _, owner_token = register_and_login(
        http, "owner", password, timeout=FIRST_REQUEST_TIMEOUT
    )
    _, client_token = register_and_login(http, "client", password)

    # 2. Owner setup.
    salon = expect(
        http.post(
            "/salons",
            headers=auth(owner_token),
            json={
                "name": f"Smoke Salon {secrets.token_hex(3)}",
                "address": "1 Test Street",
                "phone": "08000000000",
                "deposit_amount": DEPOSIT_KOBO,
            },
        ),
        "owner: create salon",
        201,
        "id",
    )
    salon_id = salon["id"]
    report(
        salon["deposit_amount"] == DEPOSIT_KOBO,
        "salon deposit_amount",
        str(salon["deposit_amount"]),
    )

    service = expect(
        http.post(
            f"/salons/{salon_id}/services",
            headers=auth(owner_token),
            json={
                "name": "Smoke Braids",
                "duration_minutes": 60,
                "price_type": "fixed",
                "price": 1_500_000,
            },
        ),
        "owner: create service",
        201,
        "id",
    )
    service_id = service["id"]

    stylist = expect(
        http.post(
            f"/salons/{salon_id}/stylists",
            headers=auth(owner_token),
            json={
                "name": "Smoke Stylist",
                "email": f"smoke-stylist-{secrets.token_hex(6)}@example.com",
                "password": secrets.token_urlsafe(16),
            },
        ),
        "owner: create stylist",
        201,
        "id",
    )
    stylist_id = stylist["id"]

    expect(
        http.put(
            f"/stylists/{stylist_id}/services",
            headers=auth(owner_token),
            json={"service_ids": [service_id]},
        ),
        "owner: assign service to stylist",
        200,
    )

    expect(
        http.put(
            f"/stylists/{stylist_id}/availability",
            headers=auth(owner_token),
            json={
                "rules": [
                    {
                        "kind": "working",
                        "weekday": day,
                        "start_time": "09:00:00",
                        "end_time": "17:00:00",
                    }
                    for day in range(7)
                ]
            },
        ),
        "owner: set availability (7 days, 09:00-17:00)",
        200,
    )

    # 3. Slots, on a date N days ahead in salon-local time.
    local_tz = timezone(timedelta(minutes=args.utc_offset_minutes))
    day = (datetime.now(local_tz) + timedelta(days=args.days_ahead)).date()
    slots_body = expect(
        http.get(
            f"/stylists/{stylist_id}/slots",
            headers=auth(client_token),
            params={"service_id": service_id, "date": day.isoformat()},
        ),
        f"client: get slots for {day}",
        200,
    )
    slots = slots_body["slots"]
    if not report(bool(slots), "slots available", f"{len(slots)} slots"):
        return 1
    starts_at = slots[0]
    info(f"Picked first slot: {starts_at}")

    # 4. Book and start payment.
    booking = expect(
        http.post(
            "/bookings",
            headers=auth(client_token),
            json={
                "stylist_id": stylist_id,
                "service_id": service_id,
                "starts_at": starts_at,
            },
        ),
        "client: create booking",
        201,
        "status",
    )
    booking_id = booking["id"]
    report(booking["status"] == "pending", "booking starts pending", booking["status"])

    payment = expect(
        http.post(f"/bookings/{booking_id}/pay", headers=auth(client_token)),
        "client: start payment",
        200,
        "status",
    )
    report(
        payment["amount"] == DEPOSIT_KOBO,
        "payment amount equals salon deposit",
        f"{payment['amount']} {payment['currency']}",
    )
    url = payment.get("authorization_url")
    if not report(bool(url), "authorization_url present"):
        return 1

    print("\n" + "=" * 70)
    print("Open this URL, pay with the Paystack TEST card, then come back:\n")
    print(f"  {url}\n")
    print("=" * 70)
    input("Press Enter once the payment has completed... ")

    def read_booking() -> dict[str, Any]:
        r = http.get(f"/bookings/{booking_id}", headers=auth(client_token))
        if r.status_code != 200:
            abort("client: GET booking", r)
        return r.json()

    def poll_until(done: Any, seconds: float) -> dict[str, Any]:
        deadline = time.monotonic() + seconds
        while True:
            current = read_booking()
            if done(current) or time.monotonic() >= deadline:
                return current
            time.sleep(3)

    # 5. Confirmation (the webhook may take a few seconds to arrive).
    got = poll_until(lambda b: b["status"] == "confirmed", POLL_SECONDS)
    report(got["status"] == "confirmed", "booking status is confirmed", got["status"])
    report(
        got["payment_status"] == "paid",
        "payment status is paid",
        str(got["payment_status"]),
    )
    report(
        got["payment_amount"] == DEPOSIT_KOBO,
        "payment amount equals the deposit",
        str(got["payment_amount"]),
    )

    # 6. Cancel (slot is 2+ days out, so beyond the 24h window: refund expected).
    cancelled = expect(
        http.post(f"/bookings/{booking_id}/cancel", headers=auth(client_token)),
        "client: cancel booking",
        200,
        "status",
    )
    report(cancelled["status"] == "cancelled", "cancel response status is cancelled")
    got = read_booking()
    info(
        f"after cancel: booking={got['status']} refund_due={got['refund_due']} "
        f"payment={got['payment_status']}"
    )
    report(got["refund_due"] is True, "refund_due is true", str(got["refund_due"]))

    info(f"Waiting {REFUND_WAIT_SECONDS}s for the refund to be processed...")
    time.sleep(REFUND_WAIT_SECONDS)
    got = poll_until(lambda b: b["payment_status"] == "refunded", POLL_SECONDS)
    info(f"after wait: booking={got['status']} payment={got['payment_status']}")
    report(
        got["payment_status"] == "refunded",
        "payment status is refunded",
        str(got["payment_status"]),
    )

    print()
    print("RESULT:", "ALL PASS" if failures == 0 else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
