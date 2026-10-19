"""The owner is emailed when an admin approves or suspends their salon, exactly once."""

import asyncio
import logging

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.salon import SalonStatus
from app.services.email.salon_status import notify_salon_status_changed
from app.tests.email_fakes import FakeEmailSender
from app.tests.test_admin_api import ADMIN, audit_rows, make_admin
from app.tests.test_salons_api import make_salon, make_user

REASON = "private reason about the address"


async def test_approving_emails_the_owner_once(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    _, headers = await make_admin(db_session)
    owner, _ = await make_user(db_session)
    salon = await make_salon(db_session, owner, status=SalonStatus.PENDING)

    resp = await db_client.post(f"{ADMIN}/salons/{salon.id}/approve", headers=headers)

    assert resp.status_code == 200
    (message,) = fake_email.to(owner.email)
    assert "approved" in message.subject
    assert salon.name in message.text
    assert len(fake_email.sent) == 1


async def test_suspending_emails_the_owner_but_never_the_reason(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    _, headers = await make_admin(db_session)
    owner, _ = await make_user(db_session)
    salon = await make_salon(db_session, owner)

    resp = await db_client.post(
        f"{ADMIN}/salons/{salon.id}/suspend", json={"reason": REASON}, headers=headers
    )

    assert resp.status_code == 200
    (message,) = fake_email.to(owner.email)
    assert "suspended" in message.subject
    assert REASON not in message.text and REASON not in message.html
    (row,) = await audit_rows(db_session)
    assert row.reason == REASON


async def test_repeating_the_same_action_sends_nothing_more(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    _, headers = await make_admin(db_session)
    owner, _ = await make_user(db_session)
    salon = await make_salon(db_session, owner)
    url = f"{ADMIN}/salons/{salon.id}/suspend"

    await db_client.post(url, headers=headers)
    await db_client.post(url, headers=headers)

    assert len(fake_email.sent) == 1


async def test_concurrent_approvals_send_one_email(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    _, headers = await make_admin(db_session)
    owner, _ = await make_user(db_session)
    salon = await make_salon(db_session, owner, status=SalonStatus.PENDING)
    url = f"{ADMIN}/salons/{salon.id}/approve"

    results = await asyncio.gather(
        *(db_client.post(url, headers=headers) for _ in range(3))
    )

    assert [r.status_code for r in results] == [200, 200, 200]
    assert len(fake_email.sent) == 1
    assert len(await audit_rows(db_session)) == 1


async def test_a_failing_sender_does_not_break_the_action(
    db_client: AsyncClient, db_session: AsyncSession, fake_email: FakeEmailSender
) -> None:
    _, headers = await make_admin(db_session)
    owner, _ = await make_user(db_session)
    salon = await make_salon(db_session, owner, status=SalonStatus.PENDING)
    fake_email.fail_next = 1

    resp = await db_client.post(f"{ADMIN}/salons/{salon.id}/approve", headers=headers)

    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    assert fake_email.attempts == 1 and fake_email.sent == []


async def test_a_missing_email_config_does_not_break_the_action(
    db_session: AsyncSession, session_maker: async_sessionmaker[AsyncSession]
) -> None:
    owner, _ = await make_user(db_session)
    salon = await make_salon(db_session, owner)
    # No sender configured: the notifier logs and returns, never raises.
    await notify_salon_status_changed(
        session_maker, None, salon.id, SalonStatus.APPROVED
    )


async def test_an_email_is_skipped_if_the_salon_moved_on_meanwhile(
    db_session: AsyncSession,
    session_maker: async_sessionmaker[AsyncSession],
    fake_email: FakeEmailSender,
) -> None:
    owner, _ = await make_user(db_session)
    salon = await make_salon(db_session, owner, status=SalonStatus.SUSPENDED)

    await notify_salon_status_changed(
        session_maker, fake_email, salon.id, SalonStatus.APPROVED
    )

    assert fake_email.sent == []


async def test_no_log_line_has_an_address_or_a_name(
    db_client: AsyncClient,
    db_session: AsyncSession,
    fake_email: FakeEmailSender,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _, headers = await make_admin(db_session)
    owner, _ = await make_user(db_session)
    salon = await make_salon(db_session, owner, status=SalonStatus.PENDING)
    fake_email.fail_next = 1

    with caplog.at_level(logging.DEBUG):
        await db_client.post(f"{ADMIN}/salons/{salon.id}/approve", headers=headers)

    assert owner.email not in caplog.text
    assert salon.name not in caplog.text
