"""`notify.owner`: the transactional type, and the two defects it was written to remove.

`test_email.py` owns the marketing rules. This file owns what is DIFFERENT, and the
differences are the reason the action type exists rather than a flag on the old one:

* **The marketing apparatus is refused BY NAME.** An unsubscribe link or a consent basis
  on an operational notice is not a harmless extra field — it is either an offer to switch
  off the product's own reporting, or a marketing send hiding inside an operational action
  type. Both are asserted refused.
* **The address never reaches `target`.** That is defect (i): `actuate()` logs `target`,
  `Outcome.summary()` renders it, and `agents.nodes._outcome_row` copies it into
  `runs.checkpoint`, which the Delivery tab reads. One test asserts the target contains no
  `@`; another asserts a hand-built actuation with the address there is refused.
* **A crawled provenance is refused.** That is defect (ii), at the boundary: the recipient
  must be declared as the authenticated account's address, and `dna` / `crawled_website`
  are stopped by name rather than falling through a generic branch.
* **Every refusal is exercised on the FAKE.** The checks run before the fake/real branch,
  so a refusal cannot be one that would first fire in front of a real recipient.

Hermetic: HTTP is an injected `httpx.MockTransport`, so there is no socket.
`backend/tests/conftest.py` strips `RESEND_API_KEY`, and the one configured case passes
`env={...}` explicitly.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import Any, Final
from uuid import UUID

import httpx
import pytest

from backend.app.actuators.actuate import actuate
from backend.app.actuators.contract import (
    Actuation,
    ActuationRefusedError,
    Outcome,
    OutcomeStatus,
)
from backend.app.actuators.email import (
    RESEND_API_KEY_ENV,
    ResendSender,
    parse_email_payload,
    recipient_target,
)
from backend.app.actuators.owner_notice import (
    ACTION_TYPE,
    AUTO_SUBMITTED_HEADER,
    SENDER_ENV,
    TARGET_PREFIX,
    OwnerNoticeActuator,
    OwnerNoticeIdentity,
    build_owner_notice_actuation,
    build_owner_notice_actuator,
    owner_notice_sender,
    owner_target,
    parse_owner_notice_payload,
)

FAKE_KEY: Final = "re_test-key-not-real"
BUSINESS_ID: Final[UUID] = UUID("11111111-2222-3333-4444-555555555555")
APPROVER: Final = "user:owner-1"

#: Reserved domains, so nothing here can name a real person. `ACCOUNT` is what the
#: authenticated account holds; `CRAWLED` is what a homepage might claim, and the whole
#: point of defect (ii) is that the second must never be used.
ACCOUNT: Final = "owner@account.example"
CRAWLED: Final = "info@crawled-homepage.example"
SENDER: Final = "Social Marketing Agent <notices@sma.example>"
SUBJECT: Final = "Published 1 of 2"
BODY: Final = "Published 1 of 2; nothing was published to facebook"

IDENTITY: Final = OwnerNoticeIdentity(account_email=ACCOUNT, sender=SENDER)


class _Omit:
    """Sentinel so a test can remove a key rather than blank it."""


_OMIT: Final = _Omit()


def payload(**overrides: Any) -> dict[str, Any]:
    """A complete owner-notice payload, so each test can break exactly one thing."""
    base: dict[str, Any] = {
        "to": ACCOUNT,
        "sender": SENDER,
        "subject": SUBJECT,
        "text": BODY,
        "recipient_source": "account",
    }
    base.update(overrides)
    return {key: value for key, value in base.items() if value is not _OMIT}


def actuation(**overrides: Any) -> Actuation:
    """A valid `notify.owner` actuation, with `target` derived the way callers must.

    Built through `Actuation` directly rather than through `build_owner_notice_actuation`
    so a test can produce a deliberately WRONG shape — which the helper, by design, cannot.
    """
    payload_overrides: dict[str, Any] = overrides.pop("payload_overrides", {})
    body = payload(**payload_overrides)
    fields: dict[str, Any] = {
        "business_id": BUSINESS_ID,
        "action_type": ACTION_TYPE,
        "target": owner_target(str(body.get("to", ""))),
        "payload": body,
        "approved_by": APPROVER,
    }
    fields.update(overrides)
    return Actuation(**fields)


class StoreStub:
    """The `actions` ledger, in a dict, with the contract's claim semantics."""

    def __init__(self) -> None:
        self.settled: dict[str, Outcome] = {}
        self.claims: list[str] = []

    async def claim(self, actuation: Actuation) -> Outcome | None:
        key = actuation.idempotency_key()
        self.claims.append(key)
        existing = self.settled.get(key)
        if existing is not None and existing.status is OutcomeStatus.SUCCEEDED:
            return existing
        return None

    async def settle(self, actuation: Actuation, outcome: Outcome) -> None:
        self.settled[actuation.idempotency_key()] = outcome


def sending_actuator() -> tuple[OwnerNoticeActuator, list[httpx.Request]]:
    """A real `ResendSender` over a transport that cannot reach a network."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "msg_owner_1"})

    sender = ResendSender(
        FAKE_KEY, client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    return OwnerNoticeActuator(sender), seen


# --------------------------------------------------------------------------- #
# It is its own action type, and that is the point
# --------------------------------------------------------------------------- #


def test_the_action_type_is_notify_owner_and_not_notify_email() -> None:
    """A typo here is a dead route, and reusing the marketing name is the bug A4 fixes."""
    assert ACTION_TYPE == "notify.owner"
    assert OwnerNoticeActuator().action_type == "notify.owner"


def test_the_marketing_parser_would_refuse_the_very_message_this_type_sends() -> None:
    """The whole justification for a second type, asserted rather than asserted-in-prose.

    A well-formed owner notice put through `parse_email_payload` is REFUSED — no consent
    basis, no unsubscribe link — which is exactly why the node's `notify.email` never once
    worked. Widening `CONSENT_BASES` would have made this test pass by deleting the rule
    the email actuator enforces hardest.
    """
    notice = build_owner_notice_actuation(
        business_id=BUSINESS_ID, identity=IDENTITY, subject=SUBJECT, approved_by=APPROVER, text=BODY
    )

    with pytest.raises(ActuationRefusedError):
        parse_email_payload(notice)

    # The first refusal is the target prefix (the two types handle their targets
    # separately, which is itself the point). Re-targeted the marketing way, the refusal
    # becomes the substantive one: there is no consent basis, because an operational notice
    # rests on none -- and the next refusal after that would be the missing unsubscribe
    # link. Neither is a field the node forgot; both are rules that should not apply.
    with pytest.raises(ActuationRefusedError, match="consent_basis"):
        parse_email_payload(replace(notice, target=recipient_target(ACCOUNT)))


def test_this_parser_refuses_a_marketing_payload_so_neither_type_is_a_back_door() -> None:
    """Symmetry, and it is the direction that protects the consent rule.

    If `notify.owner` accepted a consent basis and an unsubscribe URL it would be the route
    a marketing send takes when it does not want the marketing rules applied to it.
    """
    with pytest.raises(ActuationRefusedError, match="does not carry marketing fields"):
        parse_owner_notice_payload(
            actuation(
                payload_overrides={
                    "consent_basis": "existing_customer",
                    "unsubscribe_url": "https://links.example/u/1",
                }
            )
        )


def test_the_parser_refuses_an_actuation_of_another_action_type() -> None:
    """The rules differ per type, so applying these to another type applies the wrong ones."""
    with pytest.raises(ActuationRefusedError, match=r"notify\.email"):
        parse_owner_notice_payload(actuation(action_type="notify.email"))


# --------------------------------------------------------------------------- #
# Defect (i): the address must never reach `target`
# --------------------------------------------------------------------------- #


def test_the_target_carries_no_address() -> None:
    """The acceptance criterion, asserted the blunt way: no `@` in the target.

    `actuate()` logs `target`, `Outcome.summary()` renders it into a timeline line, and
    `agents.nodes._outcome_row` copies it into `runs.checkpoint`, which the Delivery tab
    reads. So an address in `target` reaches a log file AND a JSONB column AND a screen.
    """
    built = build_owner_notice_actuation(
        business_id=BUSINESS_ID, identity=IDENTITY, subject=SUBJECT, approved_by=APPROVER, text=BODY
    )

    assert "@" not in built.target
    assert ACCOUNT not in built.target
    assert built.target.startswith(TARGET_PREFIX)
    # The address itself still travels in the payload, which IS persisted verbatim: an
    # audit row that cannot say who was written to is not an audit.
    assert built.payload["to"] == ACCOUNT


def test_no_part_of_the_outcome_or_its_summary_leaks_the_address() -> None:
    """The property the handle exists for, asserted on what actually gets rendered."""
    outcome = Outcome(
        status=OutcomeStatus.SUCCEEDED,
        action_type=ACTION_TYPE,
        target=owner_target(ACCOUNT),
        external_ref="msg_owner_1",
    )

    assert ACCOUNT not in outcome.summary()
    assert "@" not in outcome.target


async def test_an_address_in_the_target_is_refused_and_says_why() -> None:
    """Defect (i) in the shape it actually shipped in: `target=address`.

    This is the exact actuation `nodes._notify_owner` used to build, and it must be refused
    rather than sent — a refusal that explains itself beats a leak nobody notices.
    """
    request = actuation(target=ACCOUNT)

    outcome = await actuate(request, actuator=OwnerNoticeActuator(), store=StoreStub())

    assert outcome.status is OutcomeStatus.REFUSED
    assert "not the address itself" in (outcome.error or "")
    # And the refusal itself does not quote the address it is complaining about.
    assert ACCOUNT not in (outcome.error or "")


def test_a_target_for_a_different_address_is_refused() -> None:
    """The target is half the idempotency key, so a mismatch is one key for two people."""
    with pytest.raises(ActuationRefusedError, match="does not match the recipient"):
        parse_owner_notice_payload(actuation(target=owner_target("someone.else@account.example")))


def test_the_handle_is_stable_normalised_and_prefixed() -> None:
    assert owner_target(ACCOUNT) == owner_target(f"  {ACCOUNT.upper()}  ")
    assert owner_target(ACCOUNT) != owner_target(CRAWLED)
    # A different prefix from `notify.email`'s `rcpt:`: the same person can be both a
    # marketing recipient and an account holder, and a reader of the `actions` table
    # should be able to tell which relationship a row is about.
    assert owner_target(ACCOUNT).startswith("acct:")


# --------------------------------------------------------------------------- #
# Defect (ii): the recipient comes from the account, never from crawled data
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("source", ["dna", "crawled_website", "website", "scraped"])
def test_a_crawled_provenance_is_refused_by_name(source: str) -> None:
    """Named explicitly rather than left to the generic branch: a refusal that names the
    mistake is what stops it recurring."""
    with pytest.raises(ActuationRefusedError, match="data we do not control"):
        parse_owner_notice_payload(actuation(payload_overrides={"recipient_source": source}))


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (_OMIT, "no recipient_source recorded"),
        ("   ", "no recipient_source recorded"),
        ("wherever", "not a recognised provenance"),
    ],
)
def test_the_provenance_field_cannot_be_forgotten_or_vague(source: Any, expected: str) -> None:
    with pytest.raises(ActuationRefusedError, match=expected):
        parse_owner_notice_payload(actuation(payload_overrides={"recipient_source": source}))


def test_the_builder_stamps_the_provenance_so_a_caller_cannot_forget_it() -> None:
    built = build_owner_notice_actuation(
        business_id=BUSINESS_ID, identity=IDENTITY, subject=SUBJECT, approved_by=APPROVER, text=BODY
    )

    assert built.payload["recipient_source"] == "account"


def test_the_builder_takes_one_identity_so_the_two_halves_cannot_diverge() -> None:
    """`OwnerNoticeIdentity` is passed whole on purpose: an address from one place and a
    sender from another is how a crawled address gets in beside a real sender."""
    built = build_owner_notice_actuation(
        business_id=BUSINESS_ID,
        identity=OwnerNoticeIdentity(account_email=ACCOUNT, sender=SENDER),
        subject=SUBJECT,
        approved_by=APPROVER,
        text=BODY,
    )

    assert built.payload["to"] == ACCOUNT
    assert built.payload["sender"] == SENDER


# --------------------------------------------------------------------------- #
# The named refusals the acceptance criterion asks for
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"sender": _OMIT}, "missing a usable 'sender'"),
        ({"sender": "   "}, "missing a usable 'sender'"),
        # A display name with no address is the plainest form of an unidentifiable sender.
        ({"sender": "Social Marketing Agent"}, "sender identity"),
        ({"sender": "notices@localhost"}, "sender identity"),
        ({"text": _OMIT}, "no body"),
        ({"text": "   "}, "no body"),
        ({"text": _OMIT, "html": "   "}, "no body"),
        ({"to": _OMIT}, "missing a usable 'to'"),
        ({"to": "not-an-address"}, "not a usable email address"),
        ({"subject": _OMIT}, "missing a usable 'subject'"),
        ({"subject": "  "}, "missing a usable 'subject'"),
    ],
)
def test_the_transactional_gate_refuses(overrides: dict[str, Any], expected: str) -> None:
    with pytest.raises(ActuationRefusedError, match=expected):
        parse_owner_notice_payload(actuation(payload_overrides=overrides))


def test_the_missing_sender_refusal_names_the_variable_that_fixes_it() -> None:
    """A refusal nobody can act on is a refusal somebody works around."""
    with pytest.raises(ActuationRefusedError, match=SENDER_ENV):
        parse_owner_notice_payload(actuation(payload_overrides={"sender": "Nobody"}))


@pytest.mark.parametrize("separator", [",", ";"])
def test_more_than_one_account_is_refused(separator: str) -> None:
    """A run has one account holder, so a separator here is an upstream bug, not a batch —
    and one target for two addresses is one idempotency key that can never be retried for
    the address it missed."""
    with pytest.raises(ActuationRefusedError, match="more than one recipient"):
        parse_owner_notice_payload(
            actuation(payload_overrides={"to": f"{ACCOUNT}{separator}{CRAWLED}"})
        )


async def test_every_refusal_fires_without_a_credential() -> None:
    """The rule this inherits from `email.py` undiluted: the checks run BEFORE the
    fake/real branch, so a refusal is never one first exercised on a real recipient."""
    actuator = build_owner_notice_actuator(env={})
    assert actuator.fake is True

    outcome = await actuate(
        actuation(payload_overrides={"sender": _OMIT}),
        actuator=actuator,
        store=StoreStub(),
    )

    assert outcome.status is OutcomeStatus.REFUSED
    assert "sender" in (outcome.error or "")


# --------------------------------------------------------------------------- #
# The success path, through the REAL parser
# --------------------------------------------------------------------------- #


async def test_a_well_formed_owner_notice_succeeds_on_the_fake_and_says_it_simulated() -> None:
    """The other half of the acceptance criterion: it does not merely refuse cleanly, it
    SUCCEEDS on a well-formed notice — and it says nothing left the process."""
    outcome = await actuate(
        actuation(), actuator=build_owner_notice_actuator(env={}), store=StoreStub()
    )

    assert outcome.status is OutcomeStatus.SUCCEEDED
    assert outcome.fake is True
    assert outcome.detail["checks_passed"] is True
    assert RESEND_API_KEY_ENV in str(outcome.detail["reason"])


async def test_a_configured_notice_reaches_the_provider_with_no_unsubscribe_header() -> None:
    """The message that actually goes out, asserted on the wire body.

    No `List-Unsubscribe`, and that is the product decision: a header offering to switch
    off "your run published 3 of 4" is a way to make the next run's outcome invisible.
    """
    actuator, requests = sending_actuator()

    outcome = await actuate(actuation(), actuator=actuator, store=StoreStub())

    assert outcome.status is OutcomeStatus.SUCCEEDED
    assert outcome.fake is False
    assert outcome.external_ref == "msg_owner_1"
    body = json.loads(requests[0].content)
    assert body["to"] == [ACCOUNT]
    assert body["from"] == SENDER
    assert body["text"] == BODY
    assert body["headers"] == {AUTO_SUBMITTED_HEADER: "auto-generated"}
    assert not any("unsubscribe" in key.lower() for key in body["headers"])


def test_the_notice_is_marked_auto_generated_so_autoresponders_stay_quiet() -> None:
    """RFC 3834. A request, not a guarantee — which is why the docstring says so."""
    message = parse_owner_notice_payload(actuation())

    assert message.headers[AUTO_SUBMITTED_HEADER] == "auto-generated"


async def test_the_same_notice_twice_replays_and_does_not_send_again() -> None:
    """`contract.py` rule 2, on the type most likely to be resumed: EXPORT is reachable
    only by resuming past the REVIEW interrupt, and a human who resumes resumes twice."""
    actuator, requests = sending_actuator()
    store = StoreStub()

    first = await actuate(actuation(), actuator=actuator, store=store)
    second = await actuate(actuation(), actuator=actuator, store=store)

    assert first.replayed is False
    assert second.replayed is True
    assert len(requests) == 1


async def test_nothing_sensitive_reaches_a_log(caplog: pytest.LogCaptureFixture) -> None:
    """The success log line carries a fingerprint, never the address or the body."""
    actuator, _ = sending_actuator()

    with caplog.at_level(logging.INFO):
        await actuate(actuation(), actuator=actuator, store=StoreStub())

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert ACCOUNT not in logged
    assert BODY not in logged
    assert "msg_owner_1" in logged


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def test_no_sender_configured_is_none_rather_than_an_invented_address() -> None:
    """A default would have to invent a sending domain, and an unidentifiable sender is the
    refusal this type enforces first. Blank counts as absent: `OWNER_NOTICE_FROM=` is a very
    common way to unset a variable."""
    assert owner_notice_sender(env={}) is None
    assert owner_notice_sender(env={SENDER_ENV: "   "}) is None
    assert owner_notice_sender(env={SENDER_ENV: SENDER}) == SENDER


def test_the_credential_alone_decides_fake_or_real() -> None:
    """No flag, no separate "enable notifications" switch that could disagree with whether
    a key exists."""
    assert build_owner_notice_actuator(env={}).fake is True
    assert build_owner_notice_actuator(env={RESEND_API_KEY_ENV: "   "}).fake is True

    real = build_owner_notice_actuator(env={RESEND_API_KEY_ENV: FAKE_KEY})
    assert real.fake is False
    assert real.provider == "resend"
