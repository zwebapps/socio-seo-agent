"""Where EXPORT's owner notice is addressed FROM: the authenticated account, on real SQL.

This is the security half of the owner-notice fix, and it needs a database to mean
anything. The node used to take the recipient from ``state["dna"]["email"]`` — a contact
address the crawler extracted from the business's own homepage — so a page we do not
control was the authority over where our operational mail went. Change the address in an
Impressum and the next run's notice follows it.

A double cannot prove the fix, for the same reason ``test_landing_actuator.py`` gives about
a missing row: what has to be true is that the address comes out of ``users`` by way of
``businesses.owner_id``, and only the database can say whether that join returns the right
person. So these run on real SQL, and the failure directions are asserted too — an
unresolvable owner must produce NO notice rather than a fallback, because a fallback is the
crawled address again.
"""

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.actuators.owner_notice import SENDER_ENV
from backend.app.services.run_executor import _resolve_owner_notice

pytestmark = [pytest.mark.db]

ACCOUNT_EMAIL = "the.owner@account.test"
SENDER = "SMA <notices@sma.test>"
CONFIGURED = {SENDER_ENV: SENDER}


@pytest.fixture
async def account(owner_session: AsyncSession) -> AsyncIterator[tuple[UUID, UUID]]:
    """One business under one active owner whose address we know. Returns (business, user)."""
    user_id, business_id = uuid4(), uuid4()
    await owner_session.execute(
        text(
            "INSERT INTO users (id, email, password_hash, is_active) "
            "VALUES (:id, :email, 'x', true)"
        ),
        {"id": user_id, "email": f"{user_id}-{ACCOUNT_EMAIL}"},
    )
    await owner_session.execute(
        text(
            "INSERT INTO businesses (id, owner_id, name, slug, locale, dna) VALUES "
            "(:id, :o, 'Owner Notice Fixture', 'notice-' || "
            "left(replace(cast(gen_random_uuid() AS text), '-', ''), 12), 'de', :dna)"
        ),
        # The crawled contact address lives here, exactly as a real run's would, so the
        # tests below are proving a choice rather than an absence.
        {"id": business_id, "o": user_id, "dna": '{"email": "info@crawled-homepage.test"}'},
    )
    await owner_session.commit()

    yield business_id, user_id

    await owner_session.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})
    await owner_session.commit()


async def test_the_recipient_is_the_authenticated_account_holder(
    account: tuple[UUID, UUID], scoped_sessions: None
) -> None:
    """The assertion the whole change exists for.

    The address comes out of the account, and the address sitting in `dna` — which is what
    the node used to read — is not it.
    """
    business_id, user_id = account

    identity = await _resolve_owner_notice(business_id, env=CONFIGURED)

    assert identity is not None
    assert identity.account_email == f"{user_id}-{ACCOUNT_EMAIL}"
    assert identity.sender == SENDER
    assert "crawled-homepage" not in identity.account_email


async def test_a_deactivated_owner_gets_no_notice_rather_than_one_anyway(
    account: tuple[UUID, UUID], owner_session: AsyncSession, scoped_sessions: None
) -> None:
    """A deactivated account is not somebody we mail, and the SQL says so rather than the
    caller remembering to check."""
    business_id, user_id = account
    await owner_session.execute(
        text("UPDATE users SET is_active = false WHERE id = :id"), {"id": user_id}
    )
    await owner_session.commit()

    assert await _resolve_owner_notice(business_id, env=CONFIGURED) is None


async def test_an_unknown_business_resolves_to_nothing_and_not_to_a_guess(
    scoped_sessions: None,
) -> None:
    """The direction that matters: no row means NO notice.

    Any fallback here would be the crawled address again, which is the defect. EXPORT
    reports a named note instead, so the run says nobody was told and why.
    """
    assert await _resolve_owner_notice(uuid4(), env=CONFIGURED) is None
